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

from dataclasses import dataclass

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
            # no se reporta como bloque.
            pila.append((nombre, j + 1, i) if nombre else ("", i, i))
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
