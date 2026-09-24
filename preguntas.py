"""Las preguntas de ejemplo del chat de un dataset nuevo: siempre diez, y todas
se pueden contestar.

El chat contesta armando UNA consulta PPL sobre el índice, así que una buena
pregunta de ejemplo es una que se resuelve con una agregación (contar, sumar,
promediar, máximo, top N, por día) y que nombra un campo que existe. Las del LLM
entran si cumplen eso; las plantillas completan hasta diez con los roles y los
valores reales del dataset ("¿Cuántos registros hay por Estado (COMPLETADO,
FALLIDO)?"), así que un dataset sin LLM tiene igual sus diez preguntas y dos
datasets distintos no muestran las mismas.
"""
from __future__ import annotations

import re
import unicodedata

CANTIDAD = 10
_MIN_LARGO, _MAX_LARGO = 12, 180

# Lo que una agregación no contesta: el chat inventaría o fallaría.
_FUERA_DE_ALCANCE = re.compile(
    r"\bpor ?que\b|correlac|predec|predic|pronostic|\bcausa|recomend|deberia|"
    r"\bque pasaria|probabilidad|\bexplica|\bwhy\b|predict|forecast",
)
# Una pregunta en castellano tiene alguno de estos.
_CASTELLANO = re.compile(r"\b(cuant\w*|cual(es)?|que|como|donde|cuando|quien(es)?|hay|tiene\w*)\b")
# Palabras de un nombre de campo que no alcanzan para decir que lo cita.
_GENERICAS = {"total", "totales", "tipo", "nivel", "codigo", "numero", "valor",
              "valores", "fecha", "hora", "dato", "datos", "campo", "registro",
              "registros", "columna", "descripcion"}
_PARECE_FALLA = re.compile(r"fall|error|rechaz|fail|deneg|cancel|bloq|critic|down|timeout", re.IGNORECASE)
_NUMERICOS = ("integer", "float", "long", "double")


def _normal(texto: str) -> str:
    sin = "".join(ch for ch in unicodedata.normalize("NFKD", texto or "")
                  if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sin.lower()).split())


def _etiqueta(f: dict) -> str:
    return (f.get("business_label") or f.get("raw_name")
            or (f.get("field_path") or "").rsplit(".", 1)[-1]).strip()


def _vocabulario(fields: list[dict]) -> set[str]:
    """Las palabras que prueban que una pregunta habla de ESTE dataset."""
    vocab: set[str] = set()
    for f in fields:
        for fuente in (_etiqueta(f), f.get("raw_name") or "",
                       (f.get("field_path") or "").rsplit(".", 1)[-1]):
            for palabra in _normal(fuente.replace("_", " ")).split():
                if len(palabra) >= 4 and palabra not in _GENERICAS:
                    vocab.add(palabra)
        for v in f.get("frecuentes") or []:
            v = _normal(str(v))
            if len(v) >= 3:
                vocab.add(v)
    return vocab


def validar(pregunta, vocab: set[str]) -> str | None:
    """La pregunta prolija, o None si no sirve como ejemplo."""
    if not isinstance(pregunta, str):
        return None
    q = " ".join(pregunta.split()).strip().strip('"').strip()
    q = re.sub(r"^\d+[.)]\s*", "", q)   # "1. ¿Cuántos…?"
    if not (_MIN_LARGO <= len(q) <= _MAX_LARGO):
        return None
    if not q.startswith("¿"):
        q = "¿" + q
    if not q.endswith("?"):
        q += "?"
    if q.count("?") > 1:   # dos preguntas en una: el chat contesta una
        return None
    n = _normal(q)
    if _FUERA_DE_ALCANCE.search(n) or not _CASTELLANO.search(n):
        return None
    palabras = set(n.split())
    if not any((v in palabras) if " " not in v else (v in n) for v in vocab):
        return None
    return q


# ── Plantillas ──────────────────────────────────────────────────────────────
def _valor(f: dict) -> str | None:
    return next((str(v)[:30] for v in (f.get("frecuentes") or []) if str(v).strip()), None)


def _en_frase(etiqueta: str) -> str:
    """"Páginas Totales" → "páginas totales"; "ID Trabajo" → "ID trabajo".

    En medio de una pregunta una etiqueta va en minúscula, como se escribe en
    castellano: "¿Cuál es el total de páginas por prensa?" y no "…de Páginas
    Totales por Prensa?", que se lee como un reporte. Las siglas (ID, PPM)
    y lo que ya viene mezclado (StockCode) se dejan como están.
    """
    # Solo baja lo que está escrito como palabra ("Totales"): una sigla ("ID")
    # o un nombre mezclado ("StockCode") no tienen el resto en minúscula.
    def _palabra(w: str) -> str:
        if w[:1].isupper() and w[1:] == w[1:].lower():
            return w.lower()
        return w
    return " ".join(_palabra(w) for w in etiqueta.split(" "))


def _con_rol(fields: list[dict], rol: str) -> dict | None:
    return next((f for f in fields if f.get("role") == rol), None)


def _sujeto(filas: str) -> tuple[str, str]:
    """`"las facturas"` → `("facturas", "Cuántas")`.

    El artículo lo pide el prompt justamente para esto: sin él, la mitad de las
    preguntas concuerdan mal ("¿Cuántos facturas hay?"). Sin nada usable, las
    filas son "registros", que concuerda con todo en masculino.
    """
    partes = (filas or "").strip().lower().split()
    if len(partes) >= 2 and partes[0] in ("los", "las"):
        return " ".join(partes[1:]), ("Cuántas" if partes[0] == "las" else "Cuántos")
    return "registros", "Cuántos"


def plantillas(fields: list[dict], filas: str = "") -> list[str]:
    fields = [f for f in fields or [] if isinstance(f, dict) and _etiqueta(f)]
    exito = _con_rol(fields, "success_indicator")
    critico = _con_rol(fields, "critical_indicator")
    entidad = _con_rol(fields, "entity_id")
    hay_fecha = any(f.get("type") == "date" or f.get("role") == "timestamp" for f in fields)
    # Igual que el dashboard: la entidad elegida tiene su propia pregunta (top
    # 10); un segundo campo con rol de entidad que agrupa bien ("Cliente", con
    # cinco valores) sigue siendo una dimensión.
    dims = [f for f in fields
            if f.get("dimension") is True and f.get("type") not in ("date", "geo_point")
            and f is not entidad and f.get("role") != "critical_indicator"]
    primaria = _con_rol(fields, "primary_dimension") or next(
        (d for d in dims if d is not exito), None)
    medidas = [f for f in fields if f.get("type") in _NUMERICOS and f.get("role") != "entity_id"]
    medidas.sort(key=lambda f: f.get("role") != "measure")   # las de rol primero
    medida = medidas[0] if medidas else None

    # El mismo criterio que el panel del dashboard, para que la pregunta y el
    # gráfico digan lo mismo.
    from dashboards import _UNIDADES_DE_PROMEDIO

    def _agg(m: dict) -> str:
        return "promedio" if (m.get("unit") or "").strip().lower() in _UNIDADES_DE_PROMEDIO else "total"

    # Las preguntas se leen en voz alta, así que están escritas como se las haría
    # una persona: "¿Cuántas facturas hubo por país?" y no "Distribución de
    # registros por dimensión País". Cada una sigue siendo UNA agregación — lo
    # que cambia es cómo suena, no lo que el chat tiene que resolver.
    cosas, cuantas = _sujeto(filas)

    def _et(f: dict) -> str:
        return _en_frase(_etiqueta(f))

    def _sumar_o_promediar(m: dict, donde: str) -> str:
        # "el total de X" concuerda con cualquier etiqueta ("¿Cuánto suma
        # unidades vendidas?" no). Y sin repetir: "el total de facturación",
        # no "el total de facturación total".
        if _agg(m) == "promedio":
            return f"¿Cuál es el promedio de {_et(m)}{donde}?"
        palabras = _et(m).split(" ")
        sin_total = [w for w in palabras if w.lower() not in ("total", "totales")]
        return f"¿Cuál es el total de {' '.join(sin_total or palabras)}{donde}?"

    qs: list[str] = []
    if exito:
        qs.append(f"¿{cuantas} {cosas} hay de cada {_et(exito)}?")
    if hay_fecha:
        qs.append(f"¿{cuantas} {cosas} hubo por día?")
    if medida and primaria:
        qs.append(_sumar_o_promediar(medida, f" por {_et(primaria)}"))
    if entidad:
        # Sin poner la etiqueta en plural: "los 10 código de producto" no
        # concuerda, y pluralizar a ciegas rompe las que están en inglés
        # ("countrys"). "Valores" concuerda siempre.
        qs.append(f"¿Cuáles son los 10 valores de {_et(entidad)} que más se repiten?")
    falla = next((str(v)[:30] for v in (exito or {}).get("frecuentes") or []
                  if _PARECE_FALLA.search(str(v))), None) if exito else None
    if falla:
        qs.append(f"¿{cuantas} {cosas} terminaron en {falla}?")
    if critico:
        qs.append(f"¿{cuantas} {cosas} tienen {_et(critico)}?")
    if falla and primaria and primaria is not exito:
        qs.append(f"¿Qué {_et(primaria)} tuvo más {cosas} en {falla}?")
    # Una por dimensión, alternando con la medida en las dos primeras: seis
    # "¿… hay por X?" seguidas se leen como una lista, no como preguntas.
    otras = [d for d in dims if d is not exito]
    for i, d in enumerate(otras):
        qs.append(f"¿{cuantas} {cosas} hay por {_et(d)}?")
        if medida and i < 2 and d is not primaria:
            qs.append(_sumar_o_promediar(medida, f" por {_et(d)}"))
    if medida:
        qs.append(f"¿Cuál es el valor más alto de {_et(medida)}?")
    if medida and hay_fecha:
        qs.append(f"¿Cómo evolucionó {_et(medida)} día a día?")
    if critico:
        qs.append(f"¿Qué {_et(critico)} aparece más veces?")
    if entidad:
        qs.append(f"¿Cuántos valores distintos tiene {_et(entidad)}?")
    if medida and primaria:
        verbo = "tiene el promedio más alto de" if _agg(medida) == "promedio" else "acumula más"
        qs.append(f"¿Qué {_et(primaria)} {verbo} {_et(medida)}?")
    for m in medidas[1:]:
        qs.append(_sumar_o_promediar(m, ""))
    # Más variantes, para que un dataset chico (cuatro columnas) también llegue
    # a diez sin repetir: todas siguen siendo una sola agregación.
    for d in otras:
        # "¿Qué X…?" y no "¿Cuál es el X…?": el artículo depende del género de
        # la etiqueta ("el Cliente", "la Sucursal") y no lo sabemos.
        qs.append(f"¿Qué {_et(d)} tiene más {cosas}?")
        top = _valor(d)
        if top:
            # "corresponden a" y no "hay en": sirve para un lugar (Palermo) y
            # para lo que no lo es (Débito, PRENSA-03).
            qs.append(f"¿{cuantas} {cosas} corresponden a {top}?")
        if medida and d is not primaria:
            qs.append(_sumar_o_promediar(medida, f" por {_et(d)}"))
    if medida:
        qs.append(f"¿Cuál es el valor más bajo de {_et(medida)}?")
        qs.append(_sumar_o_promediar(medida, ""))
    if hay_fecha:
        qs.append(f"¿Qué día hubo más {cosas}?")
    qs.append(f"¿{cuantas} {cosas} hay en total?")
    return qs


def armar(fields: list[dict], candidatas: list | None = None, filas: str = "",
          cantidad: int = CANTIDAD) -> list[str]:
    """Las del LLM que pasen la validación y, detrás, las plantillas."""
    vocab = _vocabulario(fields or [])
    # Nombrar las filas ("¿Cuántas facturas hubo por país?") es hablar de estos
    # datos aunque no se cite una columna: contar filas siempre se contesta.
    vocab.update(w for w in _normal(filas).split() if len(w) >= 4 and w not in ("los", "las"))
    fuera: list[str] = []
    vistas: set[str] = set()

    def _sumar(q: str | None) -> None:
        if q and len(fuera) < cantidad and _normal(q) not in vistas:
            vistas.add(_normal(q))
            fuera.append(q)

    for c in candidatas or []:
        _sumar(validar(c, vocab))
    for q in plantillas(fields or [], filas):
        _sumar(q)
    return fuera
