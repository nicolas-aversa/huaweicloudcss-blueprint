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
def _valores(f: dict, cuantos: int = 2) -> str:
    vals = [str(v)[:30] for v in (f.get("frecuentes") or [])[:cuantos] if str(v).strip()]
    return f" ({', '.join(vals)})" if vals else ""


def _con_rol(fields: list[dict], rol: str) -> dict | None:
    return next((f for f in fields if f.get("role") == rol), None)


def plantillas(fields: list[dict]) -> list[str]:
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

    qs: list[str] = []
    if exito:
        qs.append(f"¿Cuántos registros hay por {_etiqueta(exito)}{_valores(exito)}?")
    if hay_fecha:
        qs.append("¿Cuántos registros hay por día?")
    if medida and primaria:
        qs.append(f"¿Cuál es el {_agg(medida)} de {_etiqueta(medida)} por {_etiqueta(primaria)}?")
    if entidad:
        qs.append(f"¿Cuáles son los 10 valores de {_etiqueta(entidad)} con más registros?")
    if critico:
        qs.append(f"¿Cuántos registros tienen {_etiqueta(critico)}?")
    if exito and primaria and primaria is not exito:
        falla = next((v for v in exito.get("frecuentes") or [] if _PARECE_FALLA.search(str(v))), None)
        if falla:
            qs.append(f"¿Qué {_etiqueta(primaria)} tiene más registros con "
                      f"{_etiqueta(exito)} {str(falla)[:30]}?")
    if medida:
        qs.append(f"¿Cuál es el máximo de {_etiqueta(medida)}?")
    if medida and hay_fecha:
        qs.append(f"¿Cómo evolucionó el {_agg(medida)} de {_etiqueta(medida)} por día?")
    if critico:
        qs.append(f"¿Cuáles son los valores de {_etiqueta(critico)} más frecuentes?")
    for d in dims:
        if d is not exito:
            qs.append(f"¿Cuántos registros hay por {_etiqueta(d)}{_valores(d)}?")
    if entidad:
        qs.append(f"¿Cuántos valores distintos de {_etiqueta(entidad)} hay?")
    if medida and primaria:
        qs.append(f"¿Qué {_etiqueta(primaria)} tiene el mayor {_agg(medida)} de {_etiqueta(medida)}?")
    for m in medidas[1:]:
        qs.append(f"¿Cuál es el {_agg(m)} de {_etiqueta(m)}?")
    # Más variantes, para que un dataset chico (cuatro columnas) también llegue
    # a diez sin repetir: todas siguen siendo una sola agregación.
    for d in dims:
        # "¿Qué X…?" y no "¿Cuál es el X…?": el artículo depende del género
        # de la etiqueta ("el Cliente", "la Sucursal") y no lo sabemos.
        qs.append(f"¿Qué {_etiqueta(d)} tiene más registros?")
        top = next((str(v)[:30] for v in d.get("frecuentes") or [] if str(v).strip()), None)
        if top:
            qs.append(f"¿Cuántos registros tienen {_etiqueta(d)} {top}?")
        if medida and d is not primaria:
            qs.append(f"¿Cuál es el {_agg(medida)} de {_etiqueta(medida)} por {_etiqueta(d)}?")
    if medida:
        otra = "total" if _agg(medida) == "promedio" else "promedio"
        qs.append(f"¿Cuál es el {otra} de {_etiqueta(medida)}?")
        qs.append(f"¿Cuál es el mínimo de {_etiqueta(medida)}?")
    if hay_fecha:
        qs.append("¿Qué día hubo más registros?")
    qs.append("¿Cuántos registros hay en total?")
    return qs


def armar(fields: list[dict], candidatas: list | None = None, cantidad: int = CANTIDAD) -> list[str]:
    """Las del LLM que pasen la validación y, detrás, las plantillas."""
    vocab = _vocabulario(fields or [])
    fuera: list[str] = []
    vistas: set[str] = set()

    def _sumar(q: str | None) -> None:
        if q and len(fuera) < cantidad and _normal(q) not in vistas:
            vistas.add(_normal(q))
            fuera.append(q)

    for c in candidatas or []:
        _sumar(validar(c, vocab))
    for q in plantillas(fields or []):
        _sumar(q)
    return fuera
