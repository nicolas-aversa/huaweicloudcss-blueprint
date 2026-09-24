"""Las filas reales deciden los tipos: perfil de un dataset nuevo.

Hasta acá el `.conf` de un dataset nuevo lo armaba el LLM mirando UNA línea —en
un CSV, el header—, así que cada columna se tipaba adivinando por el nombre, sin
haber visto un solo valor. De ahí salían fechas que el index template descartaba
en silencio (`2026-09-18 11:04:12` no es `strict_date_optional_time`), un
`12.000` leído como 12.0, o un `865,5` que quedaba como texto.

Acá se lee una muestra de verdad (header + hasta unos cientos de filas) y se
decide todo lo que puede romper la pipeline: el separador, si hay header, el
tipo de cada columna, el formato exacto de cada fecha y el locale de cada número.
Un tipo se asigna SOLO si todos los valores no vacíos de la muestra lo cumplen;
si no, la columna queda como texto. Correcto por construcción: lo que no se
puede afirmar, no se afirma.

Tres lectores —delimitado, JSON/JSONL y clave=valor— producen la misma tabla
(columna → valores), y hay una sola lógica de tipado para los tres.
"""
from __future__ import annotations

import csv
import ipaddress
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

# Lo que en un dataset significa "sin dato". Se tratan como vacíos al tipar y el
# filter los borra antes de convertir: un "-" en una columna numérica no puede
# volver la columna texto, ni un "N/A" hacer fallar un `convert`.
VACIOS = frozenset({"", "-", "--", "n/a", "null", "none", "nan", "nil", "s/d", "#n/a", "undefined"})

# Zona de las fechas que no traen la suya. La plataforma corre en la-south-2 y
# sus datasets son de acá: sin esto una hora local se indexaría como UTC y todos
# los gráficos quedarían corridos tres horas.
ZONA_POR_DEFECTO = "America/Argentina/Buenos_Aires"

_SEPARADORES = (",", ";", "\t", "|")
# Un nombre de columna que sugiere "el momento del evento".
_NOMBRE_TIEMPO = re.compile(
    r"(^|_)(ts|time|timestamp|fecha|date|datetime|hora|created|updated|momento|"
    r"event_time|fecha_hora|fechahora)(_|$)", re.I)
_NOMBRE_HORA = re.compile(r"(^|_)(time|hora)(_|$)", re.I)
# Nombres de identificador o código: aunque todos sus valores sean dígitos, NO
# son números. Sumar ids no significa nada, un código pierde sus ceros a la
# izquierda (`000` → 0) y el dashboard los tomaría como la medida principal.
_NOMBRE_ID = re.compile(
    r"((^|_)(id|ids|code|codigo|cod|nro|num|number|numero|zip|cp|dni|cuit|cuil|phone|"
    r"telefono|tel|cuenta|account|card|tarjeta|sku|ean|isbn|legajo|patente|serial|"
    r"hash|uuid|guid)(_|$|\d))|id$", re.I)
_HORA = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_KV = re.compile(r'([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|\S*)')


@dataclass(frozen=True)
class _Fecha:
    """Un formato de fecha candidato: su patrón de Logstash (Joda), una regex de
    forma estricta y cómo parsearlo en Python para verificar que sea real."""
    joda: str
    forma: str
    strptime: str = ""     # vacío = ISO8601 / epoch, que tienen su propio parser
    con_zona: bool = False


# El orden importa: gana el PRIMERO que parsea todos los valores. dd/MM va antes
# que MM/dd (la plataforma es de acá); si un valor lo desmiente —un 13 en el
# lugar del mes— cae solo al siguiente.
_FECHAS: tuple[_Fecha, ...] = (
    _Fecha("ISO8601", r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}:?\d{2})?", con_zona=True),
    _Fecha("yyyy-MM-dd HH:mm:ss.SSS", r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", "%Y-%m-%d %H:%M:%S.%f"),
    _Fecha("yyyy-MM-dd HH:mm:ss", r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "%Y-%m-%d %H:%M:%S"),
    _Fecha("yyyy-MM-dd HH:mm", r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", "%Y-%m-%d %H:%M"),
    _Fecha("yyyy-MM-dd", r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
    _Fecha("yyyy/MM/dd HH:mm:ss", r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", "%Y/%m/%d %H:%M:%S"),
    _Fecha("yyyy/MM/dd", r"\d{4}/\d{2}/\d{2}", "%Y/%m/%d"),
    _Fecha("dd/MM/yyyy HH:mm:ss", r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", "%d/%m/%Y %H:%M:%S"),
    _Fecha("dd/MM/yyyy HH:mm", r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}", "%d/%m/%Y %H:%M"),
    _Fecha("dd/MM/yyyy", r"\d{2}/\d{2}/\d{4}", "%d/%m/%Y"),
    _Fecha("d/M/yyyy H:mm:ss", r"\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2}", "%d/%m/%Y %H:%M:%S"),
    _Fecha("d/M/yyyy H:mm", r"\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}", "%d/%m/%Y %H:%M"),
    _Fecha("d/M/yyyy", r"\d{1,2}/\d{1,2}/\d{4}", "%d/%m/%Y"),
    _Fecha("MM/dd/yyyy HH:mm:ss", r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", "%m/%d/%Y %H:%M:%S"),
    _Fecha("MM/dd/yyyy HH:mm", r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}", "%m/%d/%Y %H:%M"),
    _Fecha("MM/dd/yyyy", r"\d{2}/\d{2}/\d{4}", "%m/%d/%Y"),
    _Fecha("dd-MM-yyyy HH:mm:ss", r"\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}", "%d-%m-%Y %H:%M:%S"),
    _Fecha("dd-MM-yyyy", r"\d{2}-\d{2}-\d{4}", "%d-%m-%Y"),
    _Fecha("yyyyMMddHHmmssSSS", r"\d{17}", "%Y%m%d%H%M%S%f"),
    _Fecha("yyyyMMddHHmmss", r"\d{14}", "%Y%m%d%H%M%S"),
)
# Epoch: solo con un nombre que diga que es tiempo. Un `TransactionID` de diez
# dígitos es un id, no el 2001.
_EPOCHS = (("UNIX_MS", r"1\d{12}"), ("UNIX", r"1\d{9}"))

_SIMBOLOS = re.compile(r"^\s*([$€£¥₹]|USD|ARS|EUR|BRL|CLP|UYU|MXN)?\s*(.*?)\s*(%|[$€£¥₹]|USD|ARS|EUR)?\s*$")


@dataclass
class Columna:
    nombre: str                 # snake_case ASCII, único: lo que va en el .conf
    etiqueta: str               # el texto original, para mostrarle a una persona
    path: tuple[str, ...]       # tramos del campo bajo el namespace (JSON anidado)
    valores: list = field(default_factory=list)   # crudos, en orden de fila
    unidad: str | None = None
    tipo: str = "string"        # string | text | integer | float | date | boolean | ip
    formato_fecha: str = ""     # patrón de Logstash si tipo == date
    con_zona: bool = False      # la fecha trae su zona (no hace falta `timezone`)
    decimal: str = "."          # separador decimal de los números
    limpiar_numero: bool = False  # hay miles, coma decimal o símbolos que sacar
    nativo: bool = False        # JSON: todos los valores ya son números/bools
    vacios: int = 0
    distintos: int = 0
    frecuentes: list[str] = field(default_factory=list)
    dimension: bool = False
    rol: str | None = None      # el que puso la semántica; None = se infiere por nombre
    # La etiqueta salió de un nombre técnico (`CantidadFacturas`, `medio_pago`,
    # una clave de JSON, `columna_3`): el LLM la puede mejorar. La que escribió
    # una persona ("Páginas Totales") es el vocabulario del cliente y no se toca.
    etiqueta_tecnica: bool = False

    @property
    def campo(self) -> str:
        return ".".join(self.path)


@dataclass
class Perfil:
    formato: str                # "delimitado" | "json" | "kv" | "" (no estructurado)
    columnas: list[Columna]
    filas: int                  # filas de datos perfiladas
    separador: str = ","
    comillas: str = '"'
    header: str = ""            # la línea exacta del header, si hay
    kv_separador: str = " "
    fecha_evento: str = ""      # campo que va a @timestamp
    fecha_compuesta: tuple[str, str] | None = None   # (columna fecha, columna hora)
    lineas_datos: list[str] = field(default_factory=list)

    @property
    def estructurado(self) -> bool:
        return bool(self.formato) and len(self.columnas) >= 2

    def columna(self, nombre: str) -> Columna | None:
        return next((c for c in self.columnas if c.nombre == nombre), None)


# ── Utilidades ──────────────────────────────────────────────────────────────
def es_vacio(valor) -> bool:
    return valor is None or (isinstance(valor, str) and valor.strip().lower() in VACIOS)


def _sanear(texto: str, i: int) -> str:
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_").lower()
    if not t:
        return f"columna_{i + 1}"
    return f"c_{t}" if t[0].isdigit() else t


def _etiqueta_y_unidad(texto: str) -> tuple[str, str | None]:
    """`Consumo Tinta (ml)` → ("Consumo Tinta", "ml")."""
    m = re.match(r"^(.*?)\s*\(([^()]{1,15})\)\s*$", (texto or "").strip())
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    return (texto or "").strip(), None


# Frontera de CamelCase: `CantidadFacturas`, y el fin de una sigla (`IDTrabajo`).
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _humanizar(texto: str) -> tuple[str, bool]:
    """`CantidadFacturas` → ("Cantidad facturas", True).

    Un nombre técnico —snake_case, CamelCase, todo en minúscula— se lee como
    etiqueta: palabras separadas, la primera en mayúscula, las siglas intactas
    ("IDTrabajo" → "ID trabajo"). Devuelve también si ERA técnico, que es lo que
    habilita al LLM a proponer una mejor ("Cantidad de facturas"). Lo que ya
    escribió una persona ("Páginas Totales", "Country") queda como está.
    """
    t = (texto or "").strip()
    if not t or " " in t:
        return t, False
    palabras = [w for parte in re.split(r"[_\-.]+", t) for w in _CAMEL.split(parte) if w]
    tecnico = len(palabras) > 1 or t.islower()
    if not tecnico:
        return t, False

    def _caso(w: str, primera: bool) -> str:
        if len(w) > 1 and w.isupper():
            return w                                    # sigla: ID, PPM, SKU
        return w.capitalize() if primera else w.lower()

    return " ".join(_caso(w, i == 0) for i, w in enumerate(palabras)), True


def _nombres_unicos(nombres: list[str]) -> list[str]:
    vistos: Counter = Counter()
    fuera = []
    for n in nombres:
        vistos[n] += 1
        fuera.append(n if vistos[n] == 1 else f"{n}_{vistos[n]}")
    return fuera


def _sin_bom(linea: str) -> str:
    return linea.lstrip("﻿")


# ── Lectores ────────────────────────────────────────────────────────────────
_JSON_INDENTADO = object()


def _leer_json(lineas: list[str]):
    """Las filas de un JSONL (un objeto por línea), `_JSON_INDENTADO`, o None.

    Un JSON indentado en varias líneas NO se perfila: el input de OBS lo leería
    línea por línea y ninguna línea suelta es un objeto, así que la pipeline no
    parsearía nada. Se marca aparte para que no lo agarre el detector de CSV
    —`  "a": 1,` partido por comas da dos columnas prolijas en cada línea—.
    """
    filas = []
    for l in lineas:
        s = l.strip()
        if not s:
            continue
        if not (s.startswith("{") and s.endswith("}")):
            filas = []
            break
        try:
            obj = json.loads(s)
        except ValueError:
            filas = []
            break
        if isinstance(obj, dict):
            filas.append(obj)
    if filas:
        return filas
    try:
        json.loads("\n".join(lineas))
    except ValueError:
        return None
    return _JSON_INDENTADO


def _aplanar(obj: dict, prefijo: tuple[str, ...] = ()) -> dict[tuple[str, ...], object]:
    fuera: dict[tuple[str, ...], object] = {}
    for k, v in obj.items():
        clave = prefijo + (str(k),)
        if isinstance(v, dict) and v:
            fuera.update(_aplanar(v, clave))
        else:
            fuera[clave] = v
    return fuera


def _es_kv(lineas: list[str]) -> str | None:
    """El separador de campos si las líneas son clave=valor desde el principio.

    Pide que la línea ARRANQUE con un par: un log con un prefijo libre (fecha,
    host, mercado…) antes de los pares tiene un envoltorio que un `kv` solo no
    entiende, y ese va al LLM.
    """
    buenas = 0
    separador = " "
    datos = [l.strip() for l in lineas if l.strip()]
    for l in datos:
        if "|" in l and l.count("=") >= 2 and _KV.match(l.split("|", 1)[0]):
            separador = "|"
        pares = _KV.findall(l)
        if len(pares) >= 3 and _KV.match(l):
            buenas += 1
    if datos and buenas >= max(1, int(len(datos) * 0.9)):
        return separador
    return None


def _leer_kv(lineas: list[str], separador: str) -> list[dict]:
    filas = []
    for l in lineas:
        s = l.strip()
        if not s:
            continue
        partes = s.split("|") if separador == "|" else [s]
        fila = {}
        for parte in partes:
            for k, v in _KV.findall(parte):
                if len(v) >= 2 and v[0] == v[-1] == '"':
                    v = v[1:-1]
                fila[k] = v
        filas.append(fila)
    return filas


# Líneas de log que un separador parte en columnas "consistentes" sin ser una
# tabla: el `,123` de los milisegundos de log4j (`2026-09-18 11:04:12,123 INFO
# [main] …`), los `|` del encabezado CEF, el `<34>` de syslog. Van al LLM (o a
# su generador curado), que las lee con grok.
_LINEA_DE_LOG = re.compile(
    r"^\s*(?:"
    r"CEF:\d+\|"
    r"|<\d{1,3}>"
    r"|\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s+\[?"
    r"(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|SEVERE|CRITICAL|NOTICE)\b"
    r")",
    re.IGNORECASE,
)


def _elegir_separador(lineas: list[str]) -> tuple[str, list[list[str]]] | None:
    """El separador que parte TODAS las líneas en la misma cantidad de columnas.

    Con `csv.reader`, así que una coma dentro de un campo entre comillas no
    cuenta. Se exige consistencia real (el 90% de las filas con la moda) y al
    menos dos columnas: un texto cualquiera partido por comas da cantidades
    distintas en cada línea.
    """
    mejor = None
    for sep in _SEPARADORES:
        try:
            filas = list(csv.reader(lineas, delimiter=sep, quotechar='"'))
        except csv.Error:
            continue
        filas = [f for f in filas if f and any(c.strip() for c in f)]
        if not filas:
            continue
        # Celdas que son pares clave=valor: es un log con envoltorio
        # (`<fecha> - <host> a=1|b=2|…`), no una tabla. Partido por `|` daría
        # columnas "a=1", "b=2"… con el nombre del campo metido en el valor.
        primera = filas[0]
        if sum(1 for c in primera if re.match(r"\s*[A-Za-z_][\w.\-]*=", c)) >= max(2, len(primera) // 2):
            continue
        cantidades = Counter(len(f) for f in filas)
        moda, veces = cantidades.most_common(1)[0]
        if moda < 2 or veces < max(1, int(len(filas) * 0.9)):
            continue
        if mejor is None or moda > mejor[2]:
            mejor = (sep, filas, moda)
    return (mejor[0], mejor[1]) if mejor else None


def _parece_header(celdas: list[str], resto: list[list[str]]) -> bool:
    """¿La primera fila son nombres de columna?

    Todos sus valores tienen que ser texto no vacío que no sea número ni fecha.
    Con filas de datos, además, alguna columna tiene que cambiar de naturaleza
    entre el header y los datos (un nombre arriba, números abajo). Con una sola
    línea no hay contra qué comparar: si parecen nombres, se toman como nombres.
    """
    if not celdas or any(es_vacio(c) for c in celdas):
        return False

    def _es_dato(v: str) -> bool:
        return _cuerpo_numerico(v) is not None or _fecha_de(v) is not None

    if any(_es_dato(c.strip()) for c in celdas):
        return False
    if not resto:
        return all(re.search(r"[A-Za-zÁÉÍÓÚáéíóúñÑ]", c) for c in celdas)
    for i, c in enumerate(celdas):
        valores = [f[i].strip() for f in resto if i < len(f) and not es_vacio(f[i])]
        if valores and all(_es_dato(v) for v in valores):
            return True
    # Sin columnas numéricas: header si ningún valor de datos repite su texto.
    return all(all(i >= len(f) or f[i].strip() != c.strip() for f in resto)
               for i, c in enumerate(celdas))


# ── Tipado ──────────────────────────────────────────────────────────────────
def _cuerpo_numerico(valor: str) -> tuple[str, str | None] | None:
    """El número sin símbolos (`$ 1.500` → `1.500`, `15%` → `15`) y el símbolo."""
    m = _SIMBOLOS.match(valor)
    if not m:
        return None
    s = m.group(2).replace(" ", "")
    if not s or not re.fullmatch(r"[+-]?[\d.,]+", s) or not re.search(r"\d", s):
        return None
    return s, (m.group(1) or m.group(3))


def _numero(valor: str, decimal: str = ".") -> float | None:
    """El valor, leído con el separador decimal de su columna, o None.

    Estricto con los miles: el otro separador solo vale agrupando de a tres
    (`1.234.567`); `1.23.4` no es un número en ningún locale.
    """
    cuerpo = _cuerpo_numerico(valor)
    if cuerpo is None:
        return None
    s = cuerpo[0]
    neg = s.startswith("-")
    s = s.lstrip("+-")
    miles = "," if decimal == "." else "."
    if s.count(decimal) > 1:
        return None
    entero, _, fraccion = s.partition(decimal)
    if miles in fraccion:
        return None
    if miles in entero:
        grupos = entero.split(miles)
        if not (1 <= len(grupos[0]) <= 3 and all(len(g) == 3 for g in grupos[1:])):
            return None
        entero = "".join(grupos)
    if not entero.isdigit() and not (entero == "" and fraccion.isdigit()):
        return None
    if fraccion and not fraccion.isdigit():
        return None
    try:
        numero = float(f"{entero or '0'}.{fraccion or '0'}")
    except ValueError:
        return None
    return -numero if neg else numero


def _decimal_de_columna(textos: list[str]) -> str | None:
    """El separador decimal que explica TODA la columna, o None si no hay uno.

    Se decide por columna y no por valor: `12.000` solo es ambiguo mirado solo.
    Una coma o un punto seguidos de 1-2 o de 4+ dígitos, o precedidos por un
    cero (`0,5`), son decimales sin discusión. Si la columna no trae ninguna
    pista, un punto que agrupa de a tres son miles: la plataforma es de acá,
    donde `12.000` son doce mil.
    """
    cuerpos = [_cuerpo_numerico(t) for t in textos]
    if any(c is None for c in cuerpos):
        return None
    s = [c[0].lstrip("+-") for c in cuerpos]
    pista_punto = any(re.search(r"\.\d{1,2}$|\.\d{4,}$|^0\.|,\d{3}\.\d", x) for x in s)
    pista_coma = any(re.search(r",\d{1,2}$|,\d{4,}$|^0,|\.\d{3},\d", x) for x in s)
    if pista_punto and pista_coma:
        return None
    if pista_punto:
        candidato = "."
    elif pista_coma:
        candidato = ","
    elif any("." in x for x in s):
        candidato = ","          # los puntos agrupan miles
    else:
        candidato = "."
    return candidato if all(_numero(t, candidato) is not None for t in textos) else None


def _fecha_de(valor: str, candidatos=_FECHAS) -> _Fecha | None:
    for f in candidatos:
        if _parsea(valor, f):
            return f
    return None


def _parsea(valor: str, f: _Fecha) -> bool:
    if not re.fullmatch(f.forma, valor):
        return False
    try:
        if f.joda == "ISO8601":
            d = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        elif f.strptime.endswith("%f") and "." not in f.strptime[:-2] and len(valor) == 17:
            d = datetime.strptime(valor[:14] + valor[14:].ljust(6, "0"), f.strptime)
        else:
            d = datetime.strptime(valor, f.strptime)
    except ValueError:
        return False
    return 1970 <= d.year <= 2100


def _tipar(col: Columna, n_filas: int) -> None:
    """Decide el tipo mirando TODOS los valores no vacíos de la muestra."""
    crudos = col.valores
    col.vacios = sum(1 for v in crudos if es_vacio(v) if not isinstance(v, (bool, int, float)))
    llenos = [v for v in crudos if not (isinstance(v, str) and es_vacio(v)) and v is not None]
    col.distintos = len({str(v) for v in llenos})
    col.frecuentes = [str(v) for v, _ in Counter(str(v) for v in llenos).most_common(8)]
    if not llenos:
        col.tipo = "string"
        return

    # JSON: los tipos nativos mandan.
    if all(isinstance(v, bool) for v in llenos):
        col.tipo, col.nativo = "boolean", True
        return
    # Un identificador es texto aunque sean todos dígitos, venga de un CSV o sea un
    # número nativo de JSON (`"order_id": 100136`): sumar ids no significa nada, y
    # el dashboard lo tomaría como la medida principal.
    if _NOMBRE_ID.search(col.nombre) and not any(isinstance(v, (list, dict)) for v in llenos):
        col.tipo = "string"
        return
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in llenos):
        col.tipo = "integer" if all(float(v).is_integer() and isinstance(v, int) for v in llenos) else "float"
        col.nativo = True
        return
    if any(isinstance(v, (list, dict)) for v in llenos):
        col.tipo = "string"
        return

    textos = [str(v).strip() for v in llenos]

    # Booleanos: solo lo que el `convert => boolean` de Logstash entiende. Un
    # "Sí/No" queda como texto: un boolean no admite ignore_malformed y un valor
    # raro tiraría el documento entero, no el campo.
    if all(t.lower() in ("true", "false", "yes", "no", "y", "n", "t", "f") for t in textos) \
            and any(t.lower() in ("true", "false", "yes", "no") for t in textos):
        col.tipo = "boolean"
        return

    # Fechas: el primer formato que parsea TODOS los valores.
    for f in _FECHAS:
        if all(_parsea(t, f) for t in textos):
            col.tipo, col.formato_fecha, col.con_zona = "date", f.joda, f.con_zona
            return
    if _NOMBRE_TIEMPO.search(col.nombre):
        for joda, forma in _EPOCHS:
            if all(re.fullmatch(forma, t) for t in textos):
                col.tipo, col.formato_fecha, col.con_zona = "date", joda, True
                return

    # Un código con ceros a la izquierda es texto aunque sean todos dígitos: un
    # `000` convertido a número es un 0. (Los ids por nombre ya salieron arriba.)
    if any(re.fullmatch(r"0\d+", t) for t in textos):
        col.tipo = "string"
        return

    # Números, con el locale que diga la columna entera.
    decimal = _decimal_de_columna(textos)
    if decimal is not None:
        valores = [_numero(t, decimal) for t in textos]
        con_fraccion = any(decimal in _cuerpo_numerico(t)[0] for t in textos)
        col.tipo = "float" if (con_fraccion or not all(float(v).is_integer() for v in valores)) else "integer"
        col.decimal = decimal
        # Hay algo que sacar antes de convertir: miles, coma decimal o símbolos.
        # "Limpio" depende del decimal de la columna: con coma decimal, el punto
        # de `12.000` son miles, y sin sacarlo el `convert` leería un 12.
        limpio = r"[+-]?\d+(\.\d+)?" if decimal == "." else r"[+-]?\d+"
        col.limpiar_numero = any(not re.fullmatch(limpio, t) for t in textos)
        simbolo = _cuerpo_numerico(textos[0])[1]
        if simbolo and not col.unidad:
            col.unidad = simbolo
        return

    if all(_es_ip(t) for t in textos):
        col.tipo = "ip"
        return

    largo = sum(len(t) for t in textos) / len(textos)
    col.tipo = "text" if (largo > 60 and sum(" " in t for t in textos) > len(textos) // 2) else "string"


def _es_ip(texto: str) -> bool:
    try:
        ipaddress.ip_address(texto)
    except ValueError:
        return False
    return True


def _dimension(col: Columna, n_filas: int) -> bool:
    """¿Sirve para agrupar?

    Una columna con (casi) un valor distinto por fila es un id o un nombre
    propio: agrupar por ella da una barra por documento. Todo lo demás sirve
    para un Top-N, aunque tenga cien valores distintos.
    """
    if col.tipo not in ("string", "boolean"):
        return False
    if col.distintos == 0 or (col.tipo == "string" and _NOMBRE_ID.search(col.nombre)):
        return False
    llenos = max(1, n_filas - col.vacios)
    if n_filas >= 10 and col.distintos >= 0.9 * llenos:
        return False
    return col.distintos <= 60 or col.distintos < 0.5 * llenos


# ── Perfil ──────────────────────────────────────────────────────────────────
def perfilar(lineas: list[str]) -> Perfil:
    """El perfil de la muestra, o uno con `formato=""` si no tiene estructura."""
    lineas = [_sin_bom(l.rstrip("\r")) for l in (lineas or [])]
    lineas = [l for l in lineas if l.strip() and not l.lstrip().startswith("#")]
    if not lineas:
        return Perfil("", [], 0)

    filas_json = _leer_json(lineas)
    if filas_json is _JSON_INDENTADO:
        return Perfil("", [], 0)
    if filas_json is not None:
        return _perfil_de_dicts("json", filas_json, lineas)

    sep_kv = _es_kv(lineas)
    if sep_kv:
        perfil = _perfil_de_dicts("kv", _leer_kv(lineas, sep_kv), lineas)
        perfil.kv_separador = sep_kv
        return perfil

    if sum(1 for l in lineas if _LINEA_DE_LOG.match(l)) * 2 >= len(lineas):
        return Perfil("", [], 0)

    elegido = _elegir_separador(lineas)
    if elegido is None:
        return Perfil("", [], 0)
    sep, filas = elegido
    primera = filas[0]
    hay_header = _parece_header(primera, filas[1:])
    if hay_header:
        nombres = [_sanear(c, i) for i, c in enumerate(primera)]
        # `medio_pago` o `CantidadFacturas` son nombres técnicos, no etiquetas:
        # se muestran "Medio pago" y "Cantidad facturas" (y el LLM los puede
        # mejorar). "Páginas Totales" queda tal cual. La unidad se saca primero:
        # `importe_ars (ARS)` → "Importe ars" en ARS.
        etiquetas = []
        for c in primera:
            texto, unidad = _etiqueta_y_unidad(c)
            etiquetas.append((*_humanizar(texto), unidad))
        # Si la mayoría de los headers son técnicos, el archivo es un export y
        # también lo es "Country" al lado de `StockCode` y `CantidadFacturas`:
        # el LLM la puede traducir ("País"). En un archivo escrito por una
        # persona, las etiquetas siguen siendo suyas.
        if sum(1 for _, tecnica, _ in etiquetas if tecnica) * 2 > len(etiquetas):
            etiquetas = [(e, True, u) for e, _, u in etiquetas]
        datos = filas[1:]
        lineas_datos = lineas[1:]
    else:
        nombres = [f"columna_{i + 1}" for i in range(len(primera))]
        etiquetas = [(f"Columna {i + 1}", True, None) for i in range(len(primera))]
        datos = filas
        lineas_datos = lineas
    nombres = _nombres_unicos(nombres)
    columnas = []
    for i, nombre in enumerate(nombres):
        etiqueta, tecnica, unidad = etiquetas[i]
        col = Columna(nombre=nombre, etiqueta=etiqueta or nombre, path=(nombre,), unidad=unidad,
                      valores=[f[i] if i < len(f) else "" for f in datos],
                      etiqueta_tecnica=tecnica or not etiqueta)
        columnas.append(col)
    perfil = Perfil("delimitado", columnas, len(datos), separador=sep,
                    header=lineas[0].strip() if hay_header else "", lineas_datos=lineas_datos)
    _cerrar(perfil)
    return perfil


def _perfil_de_dicts(formato: str, filas: list[dict], lineas: list[str]) -> Perfil:
    planas = [_aplanar(f) for f in filas]
    orden: list[tuple[str, ...]] = []
    for p in planas:
        for k in p:
            if k not in orden:
                orden.append(k)
    columnas = []
    for k in orden:
        nombre = "_".join(k)
        texto, unidad = _etiqueta_y_unidad(k[-1])
        etiqueta, _ = _humanizar(texto)
        # Una clave de JSON o de clave=valor siempre es un nombre técnico.
        columnas.append(Columna(nombre=nombre, etiqueta=etiqueta or texto, path=k, unidad=unidad,
                                valores=[p.get(k) for p in planas], etiqueta_tecnica=True))
    perfil = Perfil(formato, columnas, len(planas), lineas_datos=lineas)
    _cerrar(perfil)
    return perfil


def _cerrar(perfil: Perfil) -> None:
    """Tipa cada columna y elige la fecha del evento."""
    for col in perfil.columnas:
        _tipar(col, perfil.filas)
        col.dimension = _dimension(col, perfil.filas)

    fechas = [c for c in perfil.columnas if c.tipo == "date"]
    # Fecha y hora en columnas separadas (date=2026-04-13 time=21:08:17): se
    # juntan, o @timestamp tendría precisión de día.
    solo_dia = [c for c in fechas if c.formato_fecha in ("yyyy-MM-dd", "dd/MM/yyyy", "d/M/yyyy",
                                                          "yyyy/MM/dd", "MM/dd/yyyy", "dd-MM-yyyy")]
    for dia in solo_dia:
        hora = next((c for c in perfil.columnas if c.tipo == "string" and _NOMBRE_HORA.search(c.nombre)
                     and c.valores and all(es_vacio(v) or _HORA.fullmatch(str(v).strip()) for v in c.valores)
                     and any(not es_vacio(v) for v in c.valores)), None)
        if hora is not None:
            perfil.fecha_compuesta = (dia.nombre, hora.nombre)
            perfil.fecha_evento = dia.nombre
            return
    if fechas:
        perfil.fecha_evento = next((c.nombre for c in fechas if _NOMBRE_TIEMPO.search(c.nombre)),
                                   fechas[0].nombre)


def formato_compuesto(perfil: Perfil) -> str:
    """El patrón de la fecha+hora juntas (`yyyy-MM-dd HH:mm:ss`)."""
    if not perfil.fecha_compuesta:
        return ""
    dia = perfil.columna(perfil.fecha_compuesta[0])
    hora = perfil.columna(perfil.fecha_compuesta[1])
    con_seg = all(es_vacio(v) or str(v).strip().count(":") == 2 for v in hora.valores)
    formato_hora = "H:mm:ss" if con_seg else "H:mm"
    return f"{dia.formato_fecha} {formato_hora}"


# ── El .conf ────────────────────────────────────────────────────────────────
# Lo que sigue arma el filter a partir del perfil. Cada decisión ya la tomó el
# perfilador mirando las filas; acá solo se escribe, en el orden en que Logstash
# la necesita. El orden importa en dos lugares:
#
#   * dentro de un mismo `mutate`, Logstash corre `convert` ANTES que `gsub`
#     (el orden es fijo, no el del archivo), así que limpiar y convertir en el
#     mismo bloque no limpia nada: van en bloques separados;
#   * los placeholders ("-", "N/A") se borran antes de convertir, o un `convert`
#     de "N/A" a número deja un 0 donde no había dato.

_BOOLEANOS_LS = ("true", "false", "yes", "no", "y", "n", "t", "f")


def _ref(ns: str, path: tuple[str, ...]) -> str:
    """`data` + (card, number) → `[data][card][number]`."""
    tramos = ([ns] if ns else []) + list(path)
    return "".join(f"[{t}]" for t in tramos)


def _cadena(texto: str) -> str | None:
    """`texto` como string de Logstash, o None si no se puede escribir literal.

    Logstash corre con `config.support_escapes` apagado: un `\\"` adentro de un
    string queda como barra + comilla y la comparación nunca matchea. Si el
    texto tiene comillas dobles se usan simples, y si tiene de las dos no hay
    forma literal.
    """
    if '"' not in texto and "\\" not in texto:
        return f'"{texto}"'
    if "'" not in texto and "\\" not in texto:
        return f"'{texto}'"
    return None


def _condicion_linea(texto: str) -> str:
    """`[message] == "…"`, o un regex anclado si no hay string literal posible."""
    literal = _cadena(texto)
    if literal is not None:
        return f"[message] == {literal}"
    patron = re.escape(texto).replace("/", "\\/")
    return f"[message] =~ /^{patron}$/"


def _ruby_limpieza(ns: str) -> list[str]:
    vacios = ", ".join(f'"{v}"' for v in sorted(VACIOS))
    return [
        "  ruby {",
        "    code => '",
        f"      vacios = [{vacios}]",
        "      limpiar = lambda do |h|",
        "        h.delete_if do |k, v|",
        "          if v.is_a?(Hash)",
        "            limpiar.call(v)",
        "            v.empty?",
        "          else",
        "            v.nil? || (v.is_a?(String) && vacios.include?(v.strip.downcase))",
        "          end",
        "        end",
        "      end",
        f'      d = event.get("{ns}")',
        "      if d.is_a?(Hash)",
        "        limpiar.call(d)",
        f'        event.set("{ns}", d)',
        "      end",
        "    '",
        "  }",
    ]


def armar_filter(perfil: Perfil, ns: str = "data") -> str:
    """El `filter { … }` que Logstash corre sobre ESTE dataset."""
    if not perfil.estructurado:
        raise ValueError("el perfil no tiene estructura: esto lo arma el LLM")
    ns = (ns or "data").strip() or "data"
    L: list[str] = ["filter {"]

    # Higiene de la línea: el BOM de un archivo de Excel y el \r de un archivo de
    # Windows llegan tal cual (el archivo se sube byte a byte). Sin esto el header
    # no se reconoce —y entra como un documento más, con "Páginas Totales"
    # convertido a 0— y la última columna de cada fila arrastra un \r.
    L += ['  mutate { gsub => ["message", "^\\uFEFF", ""] }',
          '  mutate { strip => ["message"] }',
          '  if [message] == "" { drop {} }']
    if perfil.header:
        L.append(f"  if {_condicion_linea(perfil.header)} {{ drop {{}} }}")
    L.append("")

    if perfil.formato == "delimitado":
        columnas = ", ".join(f'"{c.nombre}"' for c in perfil.columnas)
        # El tab va literal: con `support_escapes` apagado, "\t" serían dos caracteres.
        L += ["  csv {",
              '    source => "message"',
              f'    separator => "{perfil.separador}"',
              f"    columns => [{columnas}]",
              f'    target => "{ns}"',
              "    skip_empty_columns => true",
              "    autogenerate_column_names => false",
              "  }"]
    elif perfil.formato == "kv":
        L += ["  kv {",
              '    source => "message"',
              f'    field_split => "{perfil.kv_separador}"',
              '    value_split => "="',
              f'    target => "{ns}"',
              "  }"]
    else:
        L += ["  json {",
              '    source => "message"',
              f'    target => "{ns}"',
              "  }"]
    L.append("")
    L += _ruby_limpieza(ns)

    numericas = [c for c in perfil.columnas if c.tipo in ("integer", "float") and not c.nativo]
    a_limpiar = [c for c in numericas if c.limpiar_numero]
    if a_limpiar:
        L += ["", "  mutate {", "    gsub => ["]
        entradas = []
        for c in a_limpiar:
            ref = _ref(ns, c.path)
            permitido = "0-9,-" if c.decimal == "," else "0-9.-"
            entradas.append(f'      "{ref}", "[^{permitido}]", ""')
            if c.decimal == ",":
                entradas.append(f'      "{ref}", ",", "."')
        L.append(",\n".join(entradas))
        L += ["    ]", "  }"]

    a_convertir = [(c, c.tipo) for c in numericas] + \
                  [(c, "boolean") for c in perfil.columnas if c.tipo == "boolean" and not c.nativo]
    if a_convertir:
        L += ["", "  mutate {", "    convert => {"]
        L += [f'      "{_ref(ns, c.path)}" => "{t}"' for c, t in a_convertir]
        L += ["    }", "  }"]

    L += _bloque_fecha(perfil, ns)
    L += ["", '  mutate { remove_field => ["message"] }', "}"]
    return "\n".join(L) + "\n"


def _bloque_fecha(perfil: Perfil, ns: str) -> list[str]:
    if not perfil.fecha_evento:
        return []
    col = perfil.columna(perfil.fecha_evento)
    zona = [] if col.con_zona else [f'    timezone => "{ZONA_POR_DEFECTO}"']
    if perfil.fecha_compuesta:
        dia, hora = (perfil.columna(n) for n in perfil.fecha_compuesta)
        r_dia, r_hora = _ref(ns, dia.path), _ref(ns, hora.path)
        return ["",
                f"  if {r_dia} and {r_hora} {{",
                f'    mutate {{ add_field => {{ "[@metadata][_momento]" => "%{{{r_dia}}} %{{{r_hora}}}" }} }}',
                "    date {",
                f'      match => ["[@metadata][_momento]", "{formato_compuesto(perfil)}"]',
                '      target => "@timestamp"',
                *[("  " + z) for z in zona],
                '      tag_on_failure => ["_fecha_no_parseada"]',
                "    }",
                "  }"]
    return ["",
            "  date {",
            f'    match => ["{_ref(ns, col.path)}", "{col.formato_fecha}"]',
            '    target => "@timestamp"',
            *zona,
            '    tag_on_failure => ["_fecha_no_parseada"]',
            "  }"]


def campos(perfil: Perfil, ns: str = "data") -> list[dict]:
    """Los `fields` del paso 2, con el tipo y el formato que decidieron las filas."""
    from maas_integrator import infer_role

    ns = (ns or "data").strip() or "data"
    fuera = []
    for c in perfil.columnas:
        path = ".".join([ns, *c.path])
        if c.nombre == perfil.fecha_evento:
            rol = "timestamp"
        else:
            rol = c.rol or infer_role(path, c.tipo)
        # Un "hora" que es texto no es la fecha del evento: con ese rol el
        # dashboard creía tener serie temporal y graficaba la hora de ingesta.
        if rol == "timestamp" and c.tipo != "date":
            rol = None
        muestra = next((str(v) for v in c.valores
                        if v is not None and not (isinstance(v, str) and es_vacio(v))), "")
        fuera.append({
            "raw_name": c.nombre,
            "field_path": path,
            "ecs_path": path,
            "type": c.tipo,
            "date_format": c.formato_fecha or None,
            "business_label": c.etiqueta,
            "unit": c.unidad,
            "dimension": c.dimension,
            "role": rol,
            "is_ecs": False,
            "ecs_type_official": None,
            "normalized_path": path,
            "ecs_overlay_path": None,
            "sample": muestra[:200],
            "frecuentes": c.frecuentes[:6] if c.dimension else [],
        })
    return fuera


# ── Verificación en seco ────────────────────────────────────────────────────
@dataclass
class Verificacion:
    total: int
    ok: int
    problemas: list[str] = field(default_factory=list)

    @property
    def completa(self) -> bool:
        return self.total > 0 and self.ok == self.total


def verificar(perfil: Perfil) -> Verificacion:
    """Repite en Python lo que Logstash va a hacer con cada fila de la muestra.

    Parte la línea igual (mismo separador, mismas comillas), descarta los
    placeholders, lee cada número con el decimal de su columna y parsea cada
    fecha con el patrón exacto del filter. Una fila está bien si todos sus
    campos tipados se leen. Por cómo se tipa, sobre la misma muestra da 100%:
    lo que protege es que el filter y el perfil digan lo mismo.
    """
    total = ok = 0
    problemas: list[str] = []
    header = perfil.header
    for n, cruda in enumerate(perfil.lineas_datos, start=1):
        linea = _sin_bom(cruda).strip()
        if not linea or (header and linea == header):
            continue
        total += 1
        valores = _valores_de_linea(perfil, linea)
        if valores is None:
            problemas.append(f"fila {n}: no se pudo partir con el formato detectado")
            continue
        fallas = []
        for c in perfil.columnas:
            v = valores.get(c.path)
            if v is None or (isinstance(v, str) and es_vacio(v)):
                continue
            if not _valor_ok(c, v):
                fallas.append(f"{c.nombre}={str(v)[:40]!r}")
        if perfil.fecha_compuesta:
            dia, hora = (perfil.columna(x) for x in perfil.fecha_compuesta)
            vd, vh = valores.get(dia.path), valores.get(hora.path)
            if vd and vh and not _momento_ok(dia, str(vd).strip(), str(vh).strip()):
                fallas.append(f"{dia.nombre}+{hora.nombre}={vd} {vh}")
        if fallas:
            problemas.append(f"fila {n}: " + ", ".join(fallas[:3]))
        else:
            ok += 1
    return Verificacion(total, ok, problemas[:5])


def _valores_de_linea(perfil: Perfil, linea: str) -> dict | None:
    if perfil.formato == "delimitado":
        try:
            celdas = next(csv.reader([linea], delimiter=perfil.separador, quotechar='"'))
        except (csv.Error, StopIteration):
            return None
        return {c.path: (celdas[i] if i < len(celdas) else None) for i, c in enumerate(perfil.columnas)}
    if perfil.formato == "kv":
        filas = _leer_kv([linea], perfil.kv_separador)
        return {(k,): v for k, v in (filas[0] if filas else {}).items()}
    try:
        obj = json.loads(linea)
    except ValueError:
        return None
    return _aplanar(obj) if isinstance(obj, dict) else None


def _como_logstash(col: Columna, texto: str) -> float | None:
    """El número que Logstash va a indexar para `texto`, simulando el filter.

    Aplica el mismo `gsub` que se escribió en el .conf (si la columna lo lleva)
    y después lo que hace `convert`: sacar comas de miles y leer el prefijo
    numérico, como `to_i`/`to_f` de Ruby. Simular el filter —y no releer el
    perfil— es lo que hace que la verificación sirva: comparar el perfil
    consigo mismo aprobaba un `12.000` que Logstash iba a indexar como 12.
    """
    s = texto
    if col.limpiar_numero:
        permitido = "0-9,-" if col.decimal == "," else "0-9.-"
        s = re.sub(f"[^{permitido}]", "", s)
        if col.decimal == ",":
            s = s.replace(",", ".")
    s = s.replace(",", "").strip()
    m = re.match(r"[+-]?\d+" if col.tipo == "integer" else r"[+-]?\d+(\.\d+)?", s)
    return float(m.group()) if m else None


def _valor_ok(col: Columna, v) -> bool:
    if col.tipo in ("integer", "float"):
        if col.nativo:
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        texto = str(v).strip()
        esperado = _numero(texto, col.decimal)
        if esperado is None:
            return False
        indexado = _como_logstash(col, texto)
        if indexado is None:
            return False
        if col.tipo == "integer":
            return int(indexado) == int(esperado)
        return abs(indexado - esperado) <= 1e-9 * max(1.0, abs(esperado))
    if col.tipo == "date":
        t = str(v).strip()
        if col.formato_fecha in ("UNIX", "UNIX_MS"):
            return t.isdigit()
        f = next((x for x in _FECHAS if x.joda == col.formato_fecha), None)
        return f is not None and _parsea(t, f)
    if col.tipo == "boolean":
        return isinstance(v, bool) or str(v).strip().lower() in _BOOLEANOS_LS
    if col.tipo == "ip":
        return _es_ip(str(v).strip())
    return True


def _momento_ok(dia: Columna, vd: str, vh: str) -> bool:
    f = next((x for x in _FECHAS if x.joda == dia.formato_fecha), None)
    if f is None or not _parsea(vd, f) or not _HORA.fullmatch(vh):
        return False
    try:
        datetime.strptime(vh, "%H:%M:%S" if vh.count(":") == 2 else "%H:%M")
    except ValueError:
        return False
    return True
