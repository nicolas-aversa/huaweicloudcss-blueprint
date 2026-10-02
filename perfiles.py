"""
perfiles.py
===========

Builders PUROS (sin I/O) del perfil por entidad: un Transform de Index
Management que resume los datos crudos del caso en un índice con una fila por
entidad (IP de origen en el SIEM, cliente en e-commerce, pozo en producción),
con sus medidas. Lo declara cada vertical en su clave `perfil`; cada medida se
probó con `_preview` sobre los datos de demo (Transforms no soporta
`cardinality`: solo value_count, sum, avg, min, max, percentiles y scripted).

El índice destino es `perfil-<caso>` y NO `<caso>-perfil`: este último caería
en el pattern `<caso>-*` y se mezclaría con los datos crudos (dashboards,
asistente, anomalías).
"""

from __future__ import annotations

from typing import Any


def nombre_del_transform(slug: str) -> str:
    return f"{slug}-perfil"


def indice_destino(slug: str) -> str:
    return f"perfil-{slug}"


def build_transform(slug: str, index_pattern: str, perfil: dict) -> dict[str, Any]:
    """`PUT _plugins/_transform/<slug>-perfil`. No continuo: recorre todos los
    datos una vez y queda terminado (los datos de demo no cambian)."""
    cuerpo: dict[str, Any] = {
        "enabled": True, "continuous": False,
        "description": f"Perfil por {perfil.get('etiqueta') or perfil['campo']} de {slug} (plataforma)",
        "source_index": index_pattern, "target_index": indice_destino(slug), "page_size": 1000,
        "schedule": {"interval": {"period": 1, "unit": "Hours", "start_time": 1}},
        "groups": [{"terms": {"source_field": perfil["campo"], "target_field": "entidad"}}],
        "aggregations": perfil["medidas"],
    }
    if perfil.get("filtro"):
        cuerpo["data_selection_query"] = perfil["filtro"]
    return {"transform": cuerpo}


def orden_del_perfil(perfil: dict) -> str:
    """La medida por la que se ordena la tabla (la primera declarada)."""
    return perfil.get("orden") or next(iter(perfil["medidas"]))


def tabla_del_perfil(hits: list[dict], perfil: dict) -> dict:
    """Las filas del índice destino, para la tabla de la vista: la entidad y
    sus medidas, en el orden declarado. Las fechas (epoch ms) se dejan como
    número: el front las formatea."""
    medidas = list(perfil["medidas"])
    filas = []
    for h in hits:
        d = h.get("_source") or {}
        filas.append([d.get("entidad")] + [d.get(m) for m in medidas])
    return {"columnas": [perfil.get("etiqueta") or perfil["campo"]] + [perfil.get("nombres", {}).get(m, m) for m in medidas],
            "fechas": [i + 1 for i, m in enumerate(medidas) if m in set(perfil.get("fechas", []))],
            "filas": filas}


# ── Datasets nuevos: el perfil sale de los campos del paso 2 ────────────────
import re as _re

# Lo que suele identificar a "alguien" en un log: cliente, usuario, IP, cuenta…
_PISTA_DE_ENTIDAD = _re.compile(
    r"(^|[._])(id|ids|user|usuario|cliente|customer|client|account|cuenta|ip|host|device|dispositivo|"
    r"patient|paciente|comitente|sesion|session|merchant|comercio|well|pozo)($|[._])", _re.I)
_NUMERICOS = ("integer", "long", "float", "double")


def _ruta(f: dict) -> str:
    return f.get("field_path") or f.get("ecs_path") or f.get("raw_name") or ""


def entidad_propuesta(fields: list[dict]) -> str:
    """El campo que identifica a la entidad: el que marcó el usuario en el
    paso 2 o, si no marcó ninguno, la primera dimensión de texto o IP cuyo
    nombre lo sugiere (cliente, usuario, IP, cuenta…). '' si no hay."""
    marcado = next((_ruta(f) for f in fields or [] if f.get("entity")), "")
    if marcado:
        return marcado
    for f in fields or []:
        if f.get("type") in ("string", "keyword", "ip") and f.get("dimension", True) and \
                (_PISTA_DE_ENTIDAD.search(_ruta(f)) or _PISTA_DE_ENTIDAD.search(f.get("raw_name") or "")):
            return _ruta(f)
    return ""


def perfil_desde_campos(fields: list[dict]) -> "dict | None":
    """El perfil de un dataset nuevo: agrupado por su entidad, con los eventos,
    hasta tres medidas numéricas sumadas y el último evento. None si no hay
    entidad."""
    entidad = entidad_propuesta(fields)
    if not entidad:
        return None
    etiqueta = next((f.get("business_label") or f.get("raw_name") or entidad
                     for f in fields if _ruta(f) == entidad), entidad)
    medidas: dict = {"eventos": {"value_count": {"field": "@timestamp"}}}
    nombres = {"eventos": "Eventos"}
    for f in [f for f in fields if f.get("type") in _NUMERICOS and _ruta(f) != entidad][:3]:
        clave = _re.sub(r"[^a-z0-9_]", "_", _ruta(f).lower())
        medidas[clave] = {"sum": {"field": _ruta(f)}}
        nombres[clave] = f.get("business_label") or f.get("raw_name") or _ruta(f)
    medidas["ultimo"] = {"max": {"field": "@timestamp"}}
    nombres["ultimo"] = "Último evento"
    return {"campo": entidad, "etiqueta": etiqueta, "medidas": medidas, "nombres": nombres, "fechas": ["ultimo"]}
