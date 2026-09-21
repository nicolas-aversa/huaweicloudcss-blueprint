"""Leer y revisar el configuration file de Logstash sin adivinar con regex.

Todo lo que sabíamos del `.conf` lo sacábamos con expresiones regulares sueltas,
y cada una se equivocaba a su manera: `_S3_BLOCK_RE` cortaba el bloque en el
primer `\\n  }`, así que un input con un sub-bloque multilínea quedaba truncado y
su `bucket` no se veía; el chequeo de plugins buscaba `\\btranslate\\s*{` y habría
matcheado esa palabra dentro de un comentario o de un string.

Acá hay un scanner de una sola pasada que clasifica cada carácter —código,
string, comentario o literal de regex— y, sobre esa máscara, funciones que
encuentran bloques y settings de verdad. Lo usan el guard del bucket, la
corrección del origen de un caso y el lint del `.conf` generado por el LLM.

El detalle que obliga a scanear en vez de contar llaves: en Logstash un regex
va sin comillas (`if [message] =~ /^\\d{3}/`) y puede traer llaves adentro. Con
un contador ingenuo, ese `{` descuadra el archivo entero.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import logstash_catalogo as catalogo

CODIGO = "c"
STRING = "s"
COMENTARIO = "#"
REGEX = "r"

# Un `/` abre un literal de regex solo en posición de valor. Si el último
# carácter de código significativo es uno de estos, lo que sigue es un valor
# (típicamente después de `=~`, `!~`, `=>`, una coma o un paréntesis).
_ANTES_DE_REGEX = set("~(,[=>|&!") | {""}


def scan(conf: str) -> str:
    """Máscara del mismo largo que `conf`: qué es cada carácter.

    Un solo recorrido, sin retroceso. Los strings respetan `\\` como escape;
    los comentarios llegan hasta el fin de línea; los regex hasta su `/` de
    cierre (con escapes).
    """
    out: list[str] = []
    i, n = 0, len(conf)
    previo = ""          # último carácter de código que no es espacio
    while i < n:
        ch = conf[i]
        if ch == "#":
            fin = conf.find("\n", i)
            fin = n if fin < 0 else fin
            out.append(COMENTARIO * (fin - i))
            i = fin
            continue
        if ch in "\"'":
            j = i + 1
            while j < n:
                if conf[j] == "\\":
                    j += 2
                    continue
                if conf[j] == ch:
                    j += 1
                    break
                j += 1
            else:
                j = n                      # string sin cerrar: lo marca el lint
            out.append(STRING * (j - i))
            i = j
            previo = "\""
            continue
        if ch == "/" and previo in _ANTES_DE_REGEX:
            j = i + 1
            while j < n:
                if conf[j] == "\\":
                    j += 2
                    continue
                if conf[j] == "\n":
                    break                  # un regex no cruza líneas: no lo era
                if conf[j] == "/":
                    j += 1
                    break
                j += 1
            if j <= n and j > i + 1 and conf[j - 1] == "/":
                out.append(REGEX * (j - i))
                i = j
                previo = "/"
                continue
        out.append(CODIGO)
        if not ch.isspace():
            previo = ch
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class Bloque:
    """`nombre { … }`: dónde empieza el nombre, la llave que abre y la que cierra."""
    nombre: str
    inicio: int          # primer carácter del nombre
    abre: int            # índice de `{`
    cierra: int          # índice de `}`
    nivel: int           # 0 = sección top-level (input/filter/output)

    @property
    def cuerpo(self) -> tuple[int, int]:
        return self.abre + 1, self.cierra


def _es_ident(ch: str) -> bool:
    return ch.isalnum() or ch in "_-"


def bloques(conf: str, mascara: str | None = None) -> list[Bloque]:
    """Todos los `nombre { … }` del archivo, con su nivel de anidamiento.

    Un hash (`match => { … }`, `add_field => { … }`) NO es un bloque: se
    reconoce porque antes de la llave hay un `=>`, no un identificador.
    Los bloques sin `}` de cierre se descartan — de eso se queja el lint.
    """
    m = mascara or scan(conf)
    fuera: list[Bloque] = []
    pila: list[tuple[str, int, int]] = []
    i, n = 0, len(conf)
    while i < n:
        if m[i] != CODIGO:
            i += 1
            continue
        ch = conf[i]
        if ch == "{":
            # Hacia atrás: el identificador pegado a la llave, si lo hay.
            j = i - 1
            while j >= 0 and (m[j] != CODIGO or conf[j].isspace()):
                j -= 1
            fin_ident = j + 1
            while j >= 0 and m[j] == CODIGO and _es_ident(conf[j]):
                j -= 1
            nombre = conf[j + 1:fin_ident]
            # Sin nombre pegado a la llave es un hash (`match => { … }`) o un
            # bloque anónimo: entra en la pila para que las llaves cuadren, pero
            # no se reporta como bloque. Un "nombre" que empieza con un dígito
            # tampoco es un bloque: es el final de una condición, como en
            # `if [code] >= 400 {` — sin esto el lint lo reportaba como un
            # plugin llamado `400` y frenaba el deploy del caso SIEM.
            if nombre and not nombre[0].isdigit():
                pila.append((nombre, j + 1, i))
            else:
                pila.append(("", i, i))
        elif ch == "}":
            if pila:
                nombre, inicio, abre = pila.pop()
                if nombre:
                    fuera.append(Bloque(nombre, inicio, abre, i, len(pila)))
        i += 1
    fuera.sort(key=lambda b: b.abre)
    return fuera


def secciones(conf: str, mascara: str | None = None) -> list[Bloque]:
    """Las secciones de primer nivel: `input`, `filter`, `output`."""
    return [b for b in bloques(conf, mascara) if b.nivel == 0]


# `if`/`else` abren llaves pero no son plugins; el lint los saltea para no
# reportarlos como plugin desconocido.
CONTROL = {"if", "else", "elsif"}


def plugins(conf: str, dentro: Bloque | None = None,
            mascara: str | None = None) -> list[Bloque]:
    """Bloques de plugin (a cualquier profundidad, también dentro de un `if`)."""
    todos = bloques(conf, mascara)
    if dentro is not None:
        todos = [b for b in todos if dentro.abre < b.abre < dentro.cierra]
    return [b for b in todos if b.nivel > 0 and b.nombre not in CONTROL]


def buscar_plugin(conf: str, nombre: str, seccion: str = "",
                  mascara: str | None = None) -> Bloque | None:
    """El primer plugin `nombre`, opcionalmente dentro de una sección dada."""
    m = mascara or scan(conf)
    ambito = None
    if seccion:
        ambito = next((s for s in secciones(conf, m) if s.nombre == seccion), None)
        if ambito is None:
            return None
    return next((b for b in plugins(conf, ambito, m) if b.nombre == nombre), None)


def _span_valor(conf: str, bloque: Bloque, clave: str,
                mascara: str | None = None) -> tuple[int, int] | None:
    """Dónde está el VALOR de `clave => …` dentro del bloque (sin las comillas)."""
    m = mascara or scan(conf)
    ini, fin = bloque.cuerpo
    i = ini
    while i < fin:
        if m[i] != CODIGO or not _es_ident(conf[i]):
            i += 1
            continue
        j = i
        while j < fin and m[j] == CODIGO and _es_ident(conf[j]):
            j += 1
        palabra = conf[i:j]
        k = j
        while k < fin and conf[k].isspace():
            k += 1
        if palabra == clave and conf[k:k + 2] == "=>":
            k += 2
            while k < fin and conf[k].isspace():
                k += 1
            if k < fin and conf[k] in "\"'":
                comilla = conf[k]
                cierre = k + 1
                while cierre < fin:
                    if conf[cierre] == "\\":
                        cierre += 2
                        continue
                    if conf[cierre] == comilla:
                        break
                    cierre += 1
                return k + 1, cierre
            return None                     # valor no literal (array, hash, número)
        i = j if j > i else i + 1
    return None


def leer_setting(conf: str, bloque: Bloque, clave: str,
                 mascara: str | None = None) -> str | None:
    """El valor string de `clave` en el bloque, o None si no está."""
    span = _span_valor(conf, bloque, clave, mascara)
    return None if span is None else conf[span[0]:span[1]]


def escribir_setting(conf: str, bloque: Bloque, clave: str, valor: str,
                     mascara: str | None = None) -> str:
    """Devuelve el `.conf` con `clave => "valor"` puesto dentro del bloque.

    Si la clave ya está, se reemplaza SOLO su valor (se respeta el resto del
    archivo, incluidas las ediciones que haya hecho el operador en el paso 3).
    Si no está, se agrega como primera línea del bloque, con la indentación de
    la línea que sigue.
    """
    escapado = valor.replace("\\", "\\\\").replace('"', '\\"')
    span = _span_valor(conf, bloque, clave, mascara)
    if span is not None:
        return conf[:span[0]] + escapado + conf[span[1]:]
    ini = bloque.abre + 1
    resto = conf[ini:]
    sangria = "    "
    for linea in resto.split("\n")[1:]:
        if linea.strip():
            sangria = linea[:len(linea) - len(linea.lstrip())] or sangria
            break
    return f'{conf[:ini]}\n{sangria}{clave} => "{escapado}"{conf[ini:]}'


# ── Lint ─────────────────────────────────────────────────────────────────────
# Un `.conf` que Logstash no puede compilar deja la pipeline caída, y eso no se
# ve desde acá: Terraform crea la configuración igual, el cluster queda vivo y
# vacío, y el único síntoma es que no entra un solo documento. Como el LLM que
# arma el filter no garantiza nada de esto —lo único que se chequeaba de su
# respuesta era que fuera un string no vacío— las reglas van antes del deploy.

ERROR = "error"
AVISO = "aviso"

_MARCADOR_HOSTS = "hosts => []"
_SECCIONES = ("input", "filter", "output")
_GROK_REF = re.compile(r"%\{(\w+)(?::[^}]*)?\}")
_SOLO_MAYUS = re.compile(r"^[A-Z0-9_]+$")


@dataclass(frozen=True)
class Problema:
    nivel: str           # ERROR corta el deploy; AVISO se muestra y sigue
    linea: int
    mensaje: str

    def __str__(self) -> str:
        return f"línea {self.linea}: {self.mensaje}"


def _linea(conf: str, i: int) -> int:
    return conf.count("\n", 0, i) + 1


def _balance(conf: str, m: str) -> list[Problema]:
    """Llaves, corchetes y comillas. Lo primero que rompe una pipeline."""
    problemas: list[Problema] = []
    pares = {"}": "{", "]": "[", ")": "("}
    pila: list[tuple[str, int]] = []
    for i, ch in enumerate(conf):
        if m[i] != CODIGO:
            continue
        if ch in "{[(":
            pila.append((ch, i))
        elif ch in pares:
            if not pila:
                problemas.append(Problema(ERROR, _linea(conf, i), f"`{ch}` de más: no hay nada abierto."))
            elif pila[-1][0] != pares[ch]:
                abre, j = pila.pop()
                problemas.append(Problema(
                    ERROR, _linea(conf, i),
                    f"`{ch}` cierra un `{abre}` abierto en la línea {_linea(conf, j)}."))
            else:
                pila.pop()
    for abre, i in pila:
        problemas.append(Problema(ERROR, _linea(conf, i), f"`{abre}` sin cerrar."))
    # Un string sin cerrar se come el resto del archivo y el error que da
    # Logstash apunta a cualquier lado menos acá.
    i = 0
    while i < len(conf):
        if m[i] == STRING:
            j = i
            while j < len(conf) and m[j] == STRING:
                j += 1
            if j - i < 2 or conf[j - 1] != conf[i]:
                problemas.append(Problema(ERROR, _linea(conf, i), "comilla sin cerrar."))
            i = j
        else:
            i += 1
    return problemas


def _seccion_de(bloque: Bloque, secs: list[Bloque]) -> str:
    for s in secs:
        if s.abre < bloque.abre < s.cierra:
            return s.nombre
    return ""


def _grok_desconocidos(conf: str, m: str, bloque: Bloque) -> list[str]:
    """Patrones `%{NOMBRE}` que no existen ni en el core ni definidos ahí mismo."""
    ini, fin = bloque.cuerpo
    if leer_setting(conf, bloque, "patterns_dir", m) is not None:
        return []                       # trae su propio directorio de patrones
    definidos = set()
    for texto in _strings_en(conf, m, ini, fin):
        if _SOLO_MAYUS.match(texto):
            definidos.add(texto)        # clave de `pattern_definitions`
    faltan = []
    for texto in _strings_en(conf, m, ini, fin):
        for nombre in _GROK_REF.findall(texto):
            if nombre in catalogo.GROK_CORE or nombre in definidos:
                continue
            faltan.append(nombre)
    return faltan


def _strings_en(conf: str, m: str, ini: int, fin: int) -> list[str]:
    fuera, i = [], ini
    while i < fin:
        if m[i] == STRING:
            j = i
            while j < fin and m[j] == STRING:
                j += 1
            fuera.append(conf[i + 1:j - 1])
            i = j
        else:
            i += 1
    return fuera


def lint(conf: str, *, marcador_hosts: bool = False) -> list[Problema]:
    """Revisa un `.conf` entero. `marcador_hosts`: es el que va a Terraform.

    Terraform inyecta el cluster con un `replace` literal de `hosts => []`
    (main.tf). Si ese marcador exacto no está —porque el front mandó hosts, o
    porque el espaciado cambió—, el reemplazo no matchea y la pipeline queda
    apuntando a cualquier cosa. Por eso es una regla y no un detalle.
    """
    problemas: list[Problema] = []
    if not (conf or "").strip():
        return [Problema(ERROR, 1, "el configuration file está vacío.")]
    m = scan(conf)
    problemas += _balance(conf, m)
    if any(p.nivel == ERROR for p in problemas):
        return problemas                # con las llaves rotas, lo demás es ruido

    secs = secciones(conf, m)
    nombres = [s.nombre for s in secs]
    for s in secs:
        if s.nombre not in _SECCIONES:
            problemas.append(Problema(
                ERROR, _linea(conf, s.inicio),
                f"`{s.nombre}` no es una sección: arriba de todo solo van "
                f"`input`, `filter` y `output`. Un bloque de filtros suelto "
                f"(sin el `filter {{ … }}` que lo envuelve) es un .conf roto."))
    for req in ("input", "output"):
        if req not in nombres:
            problemas.append(Problema(ERROR, 1, f"falta la sección `{req}`."))
    for nombre in _SECCIONES:
        if nombres.count(nombre) > 1:
            problemas.append(Problema(ERROR, 1, f"la sección `{nombre}` está {nombres.count(nombre)} veces."))

    for b in plugins(conf, None, m):
        seccion = _seccion_de(b, secs)
        permitidos = catalogo.POR_SECCION.get(seccion, set()) | catalogo.CODECS
        if b.nombre in catalogo.NO_EN_CSS:
            problemas.append(Problema(
                ERROR, _linea(conf, b.inicio),
                f"el plugin `{b.nombre}` no está instalado en la Logstash de CSS. "
                f"Reemplazalo (ej. translate → condicionales `mutate`)."))
        elif seccion and b.nombre not in permitidos:
            problemas.append(Problema(
                ERROR, _linea(conf, b.inicio),
                f"`{b.nombre}` no es un plugin de `{seccion}` de Logstash 7.10: "
                f"la pipeline no va a arrancar."))
        if b.nombre == "grok":
            for pat in _grok_desconocidos(conf, m, b):
                problemas.append(Problema(
                    AVISO, _linea(conf, b.inicio),
                    f"el patrón `%{{{pat}}}` no está entre los del core: si no existe, "
                    f"la pipeline no arranca. Definilo con `pattern_definitions` "
                    f"o usá uno del core."))
        if b.nombre == "s3" and seccion == "input":
            if not (leer_setting(conf, b, "bucket", m) or "").strip():
                problemas.append(Problema(
                    ERROR, _linea(conf, b.inicio),
                    "el input `s3` no dice de qué bucket leer: Logstash arranca igual "
                    "y se queda poleando la nada."))

    if marcador_hosts:
        es = [b for b in plugins(conf, None, m)
              if b.nombre == "elasticsearch" and _seccion_de(b, secs) == "output"]
        if es and _MARCADOR_HOSTS not in conf:
            problemas.append(Problema(
                ERROR, _linea(conf, es[0].inicio),
                f"el output `elasticsearch` no trae el marcador `{_MARCADOR_HOSTS}`: "
                f"es el texto exacto que Terraform reemplaza por el cluster real, "
                f"así que la pipeline escribiría en otro lado."))
    return problemas


def lint_filtro(filter_code: str) -> list[Problema]:
    """Revisa SOLO el `filter { … }` que devuelve el LLM.

    Se valida antes de armar el `.conf`, así el reintento con feedback tiene algo
    concreto que corregir. El caso más común y más silencioso: el modelo devuelve
    el cuerpo (`grok { … } date { … }`) sin el `filter { }` que lo envuelve.
    """
    if not (filter_code or "").strip():
        return [Problema(ERROR, 1, "el filter vino vacío.")]
    armado = f"input {{ stdin {{ }} }}\n\n{filter_code}\n\noutput {{ stdout {{ }} }}\n"
    desplazamiento = armado.index(filter_code)
    fuera = []
    for p in lint(armado):
        if "falta la sección" in p.mensaje:
            continue
        linea = max(1, p.linea - armado.count("\n", 0, desplazamiento))
        fuera.append(Problema(p.nivel, linea, p.mensaje))
    return fuera
