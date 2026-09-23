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


# ── La gramática de Logstash, portada ────────────────────────────────────────
# Chequear el `.conf` con reglas sueltas siempre deja un agujero. El que lo
# demostró: el modelo escribió
#
#     convert => { "a" => "integer", "b" => "float" }
#
# y Logstash murió con `Expected one of [ \t\r\n], "#", "{", "}" at line 51,
# column 45` — la coma. En su gramática las entradas de un hash se separan con
# ESPACIO (`hashentries = hashentry (whitespace hashentry)*`); las comas son de
# los arrays. Ninguna heurística de llaves o de plugins iba a ver eso.
#
# Así que acá está la gramática de verdad —`logstash/config/grammar.treetop` de
# la 7.x— en descenso recursivo, regla por regla y en el mismo orden (importa:
# es un PEG, la primera alternativa que matchea gana). Si el parser acepta el
# archivo, Logstash lo compila.

_BLANCOS = " \t\r\n"
_COMPARADORES = ("==", "!=", "<=", ">=", "<", ">")
_BOOLEANOS = ("and", "or", "xor", "nand")


def _es_nombre(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch in "_-"


def _es_bareword_ini(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ch == "_"


def _es_bareword(ch: str) -> bool:
    return _es_bareword_ini(ch) or ("0" <= ch <= "9")


@dataclass(frozen=True)
class Bareword:
    """Un valor sin comillas: `plain`, `true`, `multiline`."""
    texto: str


@dataclass(frozen=True)
class Regex:
    texto: str


@dataclass(frozen=True)
class Selector:
    """Una referencia a un campo del evento: `[data][fecha_hora]`."""
    texto: str


@dataclass(frozen=True)
class Hash:
    entradas: tuple

    def get(self, clave: str):
        for k, v in self.entradas:
            if k == clave:
                return v
        return None


@dataclass(frozen=True)
class NodoPlugin:
    nombre: str
    atributos: tuple
    pos: int

    def attr(self, nombre: str):
        for k, v in self.atributos:
            if k == nombre:
                return v
        return None


@dataclass(frozen=True)
class NodoBranch:
    ramas: tuple          # ((condición | None, cuerpo), …); None = `else`
    pos: int


@dataclass(frozen=True)
class NodoSeccion:
    tipo: str             # input | filter | output
    cuerpo: tuple
    pos: int


@dataclass(frozen=True)
class Arbol:
    secciones: tuple

    def plugins(self):
        """`(plugin, sección)` de todo el árbol, también dentro de los `if`."""
        for s in self.secciones:
            yield from _plugins_de(s.cuerpo, s.tipo)


def _plugins_de(cuerpo, seccion: str):
    for nodo in cuerpo:
        if isinstance(nodo, NodoPlugin):
            yield nodo, seccion
            # Un codec con opciones es un plugin anidado: `codec => multiline { … }`
            for _clave, valor in nodo.atributos:
                if isinstance(valor, NodoPlugin):
                    yield valor, seccion
        elif isinstance(nodo, NodoBranch):
            for _cond, sub in nodo.ramas:
                yield from _plugins_de(sub, seccion)


class ErrorDeSintaxis(Exception):
    """Dónde se rompe el `.conf` y, cuando se puede, por qué."""

    def __init__(self, conf: str, pos: int, mensaje: str):
        self.pos = pos
        self.linea = conf.count("\n", 0, pos) + 1
        self.columna = pos - (conf.rfind("\n", 0, pos) + 1) + 1
        self.mensaje = mensaje
        super().__init__(f"línea {self.linea}, columna {self.columna}: {mensaje}")


# Una `{` abre un hash (y no un bloque de plugin) cuando viene como VALOR:
# después de `=>`, o adentro de un array.
_ABREN_HASH = (">", "[", ",")


def _pila_en(conf: str, pos: int, mascara: str | None = None) -> list[str]:
    """Qué quedó abierto en `pos`: hashes, arrays y bloques, de afuera hacia adentro."""
    m = mascara or scan(conf)
    pila: list[str] = []
    previo = ""
    for i in range(min(pos, len(conf))):
        if m[i] != CODIGO:
            if m[i] == STRING:
                previo = '"'
            continue
        ch = conf[i]
        if ch == "{":
            pila.append("hash" if previo in _ABREN_HASH else "bloque")
        elif ch == "[":
            pila.append("array")
        elif ch in "}]" and pila:
            pila.pop()
        if not ch.isspace():
            previo = ch
    return pila


def normalizar(conf: str) -> tuple[str, list[str]]:
    """Corrige los tics mecánicos del modelo y devuelve `(conf, notas)`.

    Solo lo inequívoco, que es todo lo que se puede arreglar sin adivinar:

      * la coma entre entradas de un hash, que es el error que dejó una pipeline
        en `unavailable` (en Logstash van separadas por espacio);
      * la coma de más antes de un `]` o un `}`;
      * los saltos de línea de Windows.

    Lo ambiguo —un `=` en vez de `=>`, una comilla que falta— no se toca: eso va
    al reintento con el modelo, que es el único que sabe qué quiso escribir.
    """
    notas: list[str] = []
    if "\r\n" in conf:
        conf = conf.replace("\r\n", "\n")
        notas.append("pasé los saltos de línea a formato Unix")

    m = scan(conf)
    chars = list(conf)
    pila: list[str] = []
    previo = ""
    for i, ch in enumerate(conf):
        if m[i] != CODIGO:
            if m[i] == STRING:
                previo = '"'
            continue
        if ch == "{":
            pila.append("hash" if previo in _ABREN_HASH else "bloque")
        elif ch == "[":
            pila.append("array")
        elif ch in "}]":
            if pila:
                pila.pop()
        elif ch == ",":
            siguiente = next((conf[j] for j in range(i + 1, len(conf))
                              if m[j] != CODIGO or not conf[j].isspace()), "")
            dentro = pila[-1] if pila else ""
            if dentro == "hash":
                chars[i] = " "
                notas.append(f"línea {_linea(conf, i)}: saqué una coma entre entradas de un hash")
            elif dentro == "array" and siguiente == "]":
                chars[i] = " "
                notas.append(f"línea {_linea(conf, i)}: saqué la coma final de un array")
        if not ch.isspace():
            previo = ch
    return "".join(chars), notas


def _diagnostico(conf: str, pos: int, esperados: set) -> str:
    """El error en castellano, con la causa cuando es inequívoca."""
    ch = conf[pos] if pos < len(conf) else ""
    anterior = conf[:pos].rstrip()
    ultimo = anterior[-1:] if anterior else ""
    pila = _pila_en(conf, pos)

    if ch == ",":
        if pila and pila[-1] == "hash":
            return ("sobra una coma: las entradas de un hash van separadas por espacio o "
                    'salto de línea, no por coma (`{ "a" => "integer"  "b" => "float" }`). '
                    "La coma es solo para los arrays")
        return "sobra una coma acá"
    if ch in "]}" and ultimo == ",":
        return f"sobra la coma antes del `{ch}`"
    if ch in "]}" and not pila:
        return f"sobra un `{ch}`: acá no hay nada abierto que cerrar"
    if ch == "=" and not conf.startswith("=>", pos) and not conf.startswith("==", pos):
        return "en Logstash la asignación se escribe `=>`, no `=`"
    if ch == "#":
        return "un comentario tiene que terminar en un salto de línea"
    if any(e.startswith("la comilla que cierra") for e in esperados):
        return "falta la comilla que cierra este texto: sin ella se come el resto del archivo"
    if ch == "":
        abiertas = [x for x in pila if x != "array"]
        if pila:
            falta = "}" if (abiertas or not pila) else "]"
            return (f"el archivo termina sin cerrar {len(pila)} bloque"
                    f"{'s' if len(pila) > 1 else ''}: falta un `{falta}`")
        return f"el archivo se corta antes de tiempo (esperaba {', '.join(sorted(esperados))})"
    if "input/filter/output" in esperados:
        return ("arriba de todo solo van `input`, `filter` y `output`: un bloque de filtros "
                "suelto, sin el `filter { … }` que lo envuelve, no es un .conf válido")
    visto = conf[pos:pos + 20].split("\n")[0]
    return f"esperaba {', '.join(sorted(esperados))} y encontré `{visto}`"


class _Parser:
    """Descenso recursivo sobre la gramática. Cada regla devuelve su nodo o
    `None`, y al devolver `None` deja el cursor donde estaba (backtracking).

    Del punto más lejano al que se llegó sale el error: es el mismo criterio de
    treetop, y por eso el mensaje cae en la misma línea y columna que el de
    Logstash.
    """

    def __init__(self, texto: str):
        self.t = texto
        self.n = len(texto)
        self.i = 0
        self.max_pos = 0
        self.esperados: set[str] = set()

    # — primitivas —
    def _fallo(self, etiqueta: str):
        if self.i > self.max_pos:
            self.max_pos, self.esperados = self.i, {etiqueta}
        elif self.i == self.max_pos:
            self.esperados.add(etiqueta)
        return None

    def _lit(self, s: str, etiqueta: str | None = None):
        if self.t.startswith(s, self.i):
            self.i += len(s)
            return s
        return self._fallo(etiqueta or f"`{s}`")

    def _blancos(self):
        """`whitespace`: uno o más. Obligatorio entre atributos y entre
        entradas de un hash — de ahí sale el error de la coma."""
        j = self.i
        while j < self.n and self.t[j] in _BLANCOS:
            j += 1
        if j == self.i:
            return self._fallo("un espacio")
        self.i = j
        return True

    def _skip(self):
        """`_`: blancos y comentarios, cero o más."""
        while True:
            while self.i < self.n and self.t[self.i] in _BLANCOS:
                self.i += 1
            if self.i < self.n and self.t[self.i] == "#":
                fin = self.t.find("\n", self.i)
                if fin < 0:
                    self._fallo("un salto de línea al final del comentario")
                    return
                self.i = fin + 1
                continue
            return

    # — estructura —
    def config(self):
        self._skip()
        secciones = []
        while True:
            s = self.seccion()
            if s is None:
                break
            secciones.append(s)
            self._skip()
        if not secciones or self.i < self.n:
            return None
        return Arbol(tuple(secciones))

    def seccion(self):
        ini = self.i
        tipo = None
        for t in ("input", "filter", "output"):
            if self.t.startswith(t, self.i):
                self.i += len(t)
                tipo = t
                break
        if tipo is None:
            return self._fallo("input/filter/output")
        self._skip()
        if self._lit("{") is None:
            self.i = ini
            return None
        cuerpo = self._cuerpo()
        if self._lit("}") is None:
            self.i = ini
            return None
        return NodoSeccion(tipo, tuple(cuerpo), ini)

    def _cuerpo(self):
        """`_ (branch_or_plugin _)*`"""
        self._skip()
        fuera = []
        while True:
            nodo = self.branch()
            if nodo is None:
                nodo = self.plugin()
            if nodo is None:
                break
            fuera.append(nodo)
            self._skip()
        return fuera

    def plugin(self):
        ini = self.i
        nombre = self._nombre()
        if nombre is None:
            return None
        self._skip()
        if self._lit("{") is None:
            self.i = ini
            return None
        self._skip()
        atributos = []
        a = self.atributo()
        if a is not None:
            atributos.append(a)
            while True:
                guardado = self.i
                if self._blancos() is None:
                    self.i = guardado
                    break
                self._skip()
                otro = self.atributo()
                if otro is None:
                    self.i = guardado
                    break
                atributos.append(otro)
        self._skip()
        if self._lit("}") is None:
            self.i = ini
            return None
        return NodoPlugin(nombre, tuple(atributos), ini)

    def atributo(self):
        ini = self.i
        nombre = self._nombre()
        if nombre is None:
            return None
        self._skip()
        if self._lit("=>") is None:
            self.i = ini
            return None
        self._skip()
        valor = self.valor()
        if valor is None:
            self.i = ini
            return None
        return (nombre, valor)

    def _nombre(self):
        j = self.i
        while j < self.n and _es_nombre(self.t[j]):
            j += 1
        if j > self.i:
            s = self.t[self.i:j]
            self.i = j
            return s
        s = self._string()
        if s is not None:
            return s
        return self._fallo("un nombre")

    # — valores —
    def valor(self):
        """`plugin / bareword / string / number / array / hash`, en ese orden:
        `codec => multiline { … }` tiene que ganarle a `codec => multiline`."""
        for regla in (self.plugin, self._bareword, self._string,
                      self._numero, self._array, self._hash):
            v = regla()
            if v is not None:
                return v
        return self._fallo("un valor")

    def _bareword(self):
        """`[A-Za-z_][A-Za-z0-9_]+`: dos caracteres o más, sin guiones."""
        j = self.i
        if j < self.n and _es_bareword_ini(self.t[j]):
            j += 1
            while j < self.n and _es_bareword(self.t[j]):
                j += 1
        if j - self.i >= 2:
            s = self.t[self.i:j]
            self.i = j
            return Bareword(s)
        return None

    def _entre(self, comilla: str) -> str | None:
        """Un literal delimitado, con `\\<delim>` como única secuencia de escape
        (es lo que dice la gramática: `'\\"' / !'"' .`)."""
        if self.i >= self.n or self.t[self.i] != comilla:
            return None
        j = self.i + 1
        while j < self.n:
            if self.t[j] == "\\" and j + 1 < self.n and self.t[j + 1] == comilla:
                j += 2
                continue
            if self.t[j] == comilla:
                dentro = self.t[self.i + 1:j]
                self.i = j + 1
                return dentro
            j += 1
        return None

    def _string(self):
        for comilla in ('"', "'"):
            if self.i < self.n and self.t[self.i] == comilla:
                s = self._entre(comilla)
                if s is None:
                    return self._fallo(f"la comilla que cierra ({comilla})")
                return s
        return None

    def _numero(self):
        j = self.i
        if j < self.n and self.t[j] == "-":
            j += 1
        k = j
        while j < self.n and "0" <= self.t[j] <= "9":
            j += 1
        if j == k:
            return None
        if j < self.n and self.t[j] == ".":
            j += 1
            while j < self.n and "0" <= self.t[j] <= "9":
                j += 1
        crudo = self.t[self.i:j]
        self.i = j
        return float(crudo) if "." in crudo else int(crudo)

    def _array(self):
        ini = self.i
        if self._lit("[") is None:
            return None
        self._skip()
        elementos = []
        v = self.valor()
        if v is not None:
            elementos.append(v)
            while True:
                guardado = self.i
                self._skip()
                if self._lit(",") is None:
                    self.i = guardado
                    break
                self._skip()
                otro = self.valor()
                if otro is None:
                    self.i = guardado
                    break
                elementos.append(otro)
        self._skip()
        if self._lit("]") is None:
            self.i = ini
            return None
        return elementos

    def _hash(self):
        ini = self.i
        if self._lit("{") is None:
            return None
        self._skip()
        entradas = []
        e = self._hashentry()
        if e is not None:
            entradas.append(e)
            while True:
                guardado = self.i
                # `hashentry (whitespace hashentry)*`: separador = espacio. Una
                # coma acá es el error que dejó la pipeline en unavailable.
                if self._blancos() is None:
                    self.i = guardado
                    break
                otro = self._hashentry()
                if otro is None:
                    self.i = guardado
                    break
                entradas.append(otro)
        self._skip()
        if self._lit("}") is None:
            self.i = ini
            return None
        return Hash(tuple(entradas))

    def _hashentry(self):
        ini = self.i
        clave = self._numero()
        if clave is None:
            b = self._bareword()
            clave = b.texto if b is not None else self._string()
        if clave is None:
            return None
        self._skip()
        if self._lit("=>") is None:
            self.i = ini
            return None
        self._skip()
        # El valor de una entrada NO puede ser un plugin (a diferencia del de
        # un atributo).
        for regla in (self._bareword, self._string, self._numero, self._array, self._hash):
            v = regla()
            if v is not None:
                return (clave, v)
        self.i = ini
        return self._fallo("un valor")

    # — condicionales —
    def branch(self):
        ini = self.i
        rama = self._if("if")
        if rama is None:
            return None
        ramas = [rama]
        while True:
            guardado = self.i
            self._skip()
            otra = self._else_if()
            if otra is None:
                self.i = guardado
                break
            ramas.append(otra)
        guardado = self.i
        self._skip()
        final = self._else()
        if final is None:
            self.i = guardado
        else:
            ramas.append(final)
        return NodoBranch(tuple(ramas), ini)

    def _if(self, palabra: str):
        ini = self.i
        if self._lit(palabra, f"`{palabra}`") is None:
            return None
        self._skip()
        cond = self.condicion()
        if cond is None:
            self.i = ini
            return None
        self._skip()
        if self._lit("{") is None:
            self.i = ini
            return None
        cuerpo = self._cuerpo()
        if self._lit("}") is None:
            self.i = ini
            return None
        return (cond, tuple(cuerpo))

    def _else_if(self):
        ini = self.i
        if self._lit("else") is None:
            return None
        self._skip()
        rama = self._if("if")
        if rama is None:
            self.i = ini
            return None
        return rama

    def _else(self):
        ini = self.i
        if self._lit("else") is None:
            return None
        self._skip()
        if self._lit("{") is None:
            self.i = ini
            return None
        cuerpo = self._cuerpo()
        if self._lit("}") is None:
            self.i = ini
            return None
        return (None, tuple(cuerpo))

    def condicion(self):
        """`expression (_ boolean_operator _ expression)*`"""
        e = self.expresion()
        if e is None:
            return None
        partes = [e]
        while True:
            guardado = self.i
            self._skip()
            op = next((o for o in _BOOLEANOS if self.t.startswith(o, self.i)), None)
            if op is None:
                self.i = guardado
                break
            self.i += len(op)
            self._skip()
            otra = self.expresion()
            if otra is None:
                self.i = guardado
                break
            partes.append(otra)
        return partes

    def expresion(self):
        ini = self.i
        # ( condición )
        if self._lit("(") is not None:
            self._skip()
            c = self.condicion()
            if c is not None:
                self._skip()
                if self._lit(")") is not None:
                    return c
            self.i = ini
        # ! ( condición )  /  ! selector
        if self._lit("!") is not None:
            self._skip()
            if self._lit("(") is not None:
                self._skip()
                c = self.condicion()
                if c is not None:
                    self._skip()
                    if self._lit(")") is not None:
                        return c
                self.i = ini
            else:
                s = self._selector()
                if s is not None:
                    return s
                self.i = ini
        izq = self._rvalue()
        if izq is None:
            return None
        for operadores in (("not in",), ("in",), _COMPARADORES, ("=~", "!~")):
            guardado = self.i
            self._skip()
            op = next((o for o in operadores if self.t.startswith(o, self.i)), None)
            if op is None:
                self.i = guardado
                continue
            self.i += len(op)
            self._skip()
            der = self._rvalue()
            if der is None:
                self.i = guardado
                continue
            return (izq, op, der)
        return izq

    def _rvalue(self):
        """`string / number / selector / array / method_call / regexp`"""
        for regla in (self._string, self._numero, self._selector,
                      self._array, self._method_call, self._regex):
            v = regla()
            if v is not None:
                return v
        return self._fallo("un campo, un número o un texto")

    def _selector(self):
        ini = self.i
        partes = []
        while self.i < self.n and self.t[self.i] == "[":
            j = self.i + 1
            while j < self.n and self.t[j] not in "], ":
                j += 1
            if j == self.i + 1 or j >= self.n or self.t[j] != "]":
                break
            partes.append(self.t[self.i + 1:j])
            self.i = j + 1
        if not partes:
            self.i = ini
            return None
        return Selector("[" + "][".join(partes) + "]")

    def _method_call(self):
        ini = self.i
        nombre = self._bareword()
        if nombre is None:
            return None
        self._skip()
        if self._lit("(") is None:
            self.i = ini
            return None
        self._skip()
        v = self._rvalue()
        if v is not None:
            while True:
                guardado = self.i
                self._skip()
                if self._lit(",") is None:
                    self.i = guardado
                    break
                self._skip()
                if self._rvalue() is None:
                    self.i = guardado
                    break
        self._skip()
        if self._lit(")") is None:
            self.i = ini
            return None
        return Bareword(nombre.texto)

    def _regex(self):
        if self.i >= self.n or self.t[self.i] != "/":
            return None
        j = self.i + 1
        while j < self.n:
            if self.t[j] == "\\" and j + 1 < self.n:
                j += 2
                continue
            if self.t[j] == "/":
                dentro = self.t[self.i + 1:j]
                self.i = j + 1
                return Regex(dentro)
            j += 1
        return None


def _desbalance(conf: str) -> tuple[int, str] | None:
    """La primera llave o corchete que no cierra, y qué decir de eso.

    Va antes que el error del parser porque apunta MUCHO mejor: con un `}` de
    menos, el parser sigue leyendo y se queja recién en la línea donde ya no
    entiende nada —típicamente el `output` de abajo de todo—, mientras que la
    llave que falta cerrar está veinte líneas más arriba.
    """
    m = scan(conf)
    # Primero la comilla: un texto sin cerrar se come el resto del archivo, así
    # que TODAS las llaves de ahí para abajo parecen faltar. Reportar una de
    # ellas manda a mirar donde no es.
    i = 0
    while i < len(conf):
        if m[i] == STRING:
            j = i
            while j < len(conf) and m[j] == STRING:
                j += 1
            if j - i < 2 or conf[j - 1] != conf[i]:
                return i, ("falta la comilla que cierra este texto: sin ella se come el "
                           "resto del archivo")
            i = j
        else:
            i += 1

    pila: list[tuple[str, int]] = []
    for i, ch in enumerate(conf):
        if m[i] != CODIGO:
            continue
        if ch in "{[":
            pila.append((ch, i))
        elif ch in "}]":
            if not pila:
                return i, f"sobra un `{ch}`: acá no hay nada abierto que cerrar"
            abre, j = pila.pop()
            cierra = "}" if abre == "{" else "]"
            if ch != cierra:
                return i, (f"acá va un `{cierra}` (el que cierra el `{abre}` de la línea "
                           f"{_linea(conf, j)}), no un `{ch}`")
    if pila:
        abre, j = pila[-1]
        return j, f"este `{abre}` quedó sin cerrar: falta el `{'}' if abre == '{' else ']'}`"
    return None


def parse(conf: str) -> Arbol:
    """El `.conf` como árbol, o `ErrorDeSintaxis` si Logstash no lo compilaría."""
    p = _Parser(conf)
    arbol = p.config()
    if arbol is not None:
        return arbol
    desbalance = _desbalance(conf)
    if desbalance is not None:
        return _fallar(conf, *desbalance)
    pos = min(p.max_pos, len(conf))
    return _fallar(conf, pos, _diagnostico(conf, pos, p.esperados))


def _fallar(conf: str, pos: int, mensaje: str):
    raise ErrorDeSintaxis(conf, pos, mensaje)


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


def _textos_de(valor) -> list[str]:
    """Todos los strings que hay adentro de un valor, por anidado que esté."""
    if isinstance(valor, str):
        return [valor]
    if isinstance(valor, list):
        return [t for v in valor for t in _textos_de(v)]
    if isinstance(valor, Hash):
        fuera = []
        for clave, v in valor.entradas:
            if isinstance(clave, str):
                fuera.append(clave)
            fuera += _textos_de(v)
        return fuera
    return []


def _grok_desconocidos(grok: NodoPlugin) -> list[str]:
    """Patrones `%{NOMBRE}` que no existen ni en el core ni definidos ahí mismo."""
    if grok.attr("patterns_dir") is not None:
        return []                       # trae su propio directorio de patrones
    definiciones = grok.attr("pattern_definitions")
    definidos = {c for c, _v in definiciones.entradas
                 if isinstance(c, str)} if isinstance(definiciones, Hash) else set()
    faltan = []
    for clave in ("match", "pattern_definitions"):
        valor = grok.attr(clave)
        if valor is None:
            continue
        for texto in _textos_de(valor):
            for nombre in _GROK_REF.findall(texto):
                if nombre not in catalogo.GROK_CORE and nombre not in definidos:
                    faltan.append(nombre)
    return faltan


def lint(conf: str, *, marcador_hosts: bool = False) -> list[Problema]:
    """Revisa un `.conf` entero. `marcador_hosts`: es el que va a Terraform.

    Primero lo parsea con la gramática de Logstash: si eso falla, la pipeline no
    arranca y lo demás es ruido. Después, sobre el árbol, lo que la gramática no
    puede saber — que el plugin exista en la Logstash de CSS, que el grok use
    patrones que existan, que el input s3 tenga bucket.

    `marcador_hosts`: Terraform inyecta el cluster con un `replace` literal de
    `hosts => []` (main.tf). Si ese marcador exacto no está —porque el front
    mandó hosts, o porque el espaciado cambió—, el reemplazo no matchea y la
    pipeline queda apuntando a cualquier cosa. Por eso es una regla y no un
    detalle.
    """
    if not (conf or "").strip():
        return [Problema(ERROR, 1, "el configuration file está vacío.")]
    try:
        arbol = parse(conf)
    except ErrorDeSintaxis as exc:
        return [Problema(ERROR, exc.linea, f"{exc.mensaje} (columna {exc.columna})")]

    problemas: list[Problema] = []
    nombres = [s.tipo for s in arbol.secciones]
    for req in ("input", "output"):
        if req not in nombres:
            problemas.append(Problema(ERROR, 1, f"falta la sección `{req}`."))
    for nombre in _SECCIONES:
        if nombres.count(nombre) > 1:
            problemas.append(Problema(ERROR, 1, f"la sección `{nombre}` está {nombres.count(nombre)} veces."))

    for p, seccion in arbol.plugins():
        permitidos = catalogo.POR_SECCION.get(seccion, set()) | catalogo.CODECS
        if p.nombre in catalogo.NO_EN_CSS:
            problemas.append(Problema(
                ERROR, _linea(conf, p.pos),
                f"el plugin `{p.nombre}` no está instalado en la Logstash de CSS. "
                f"Reemplazalo (ej. translate → condicionales `mutate`)."))
        elif p.nombre not in permitidos:
            problemas.append(Problema(
                ERROR, _linea(conf, p.pos),
                f"`{p.nombre}` no es un plugin de `{seccion}` de Logstash 7.10: "
                f"la pipeline no va a arrancar."))
        if p.nombre == "grok":
            for pat in _grok_desconocidos(p):
                problemas.append(Problema(
                    AVISO, _linea(conf, p.pos),
                    f"el patrón `%{{{pat}}}` no está entre los del core: si no existe, "
                    f"la pipeline no arranca. Definilo con `pattern_definitions` "
                    f"o usá uno del core."))
        if p.nombre == "s3" and seccion == "input":
            bucket = p.attr("bucket")
            if not (bucket if isinstance(bucket, str) else "").strip():
                problemas.append(Problema(
                    ERROR, _linea(conf, p.pos),
                    "el input `s3` no dice de qué bucket leer: Logstash arranca igual "
                    "y se queda poleando la nada."))

    if marcador_hosts and _MARCADOR_HOSTS not in conf:
        salidas = [p for p, s in arbol.plugins() if p.nombre == "elasticsearch" and s == "output"]
        if salidas:
            problemas.append(Problema(
                ERROR, _linea(conf, salidas[0].pos),
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
