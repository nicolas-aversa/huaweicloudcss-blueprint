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
