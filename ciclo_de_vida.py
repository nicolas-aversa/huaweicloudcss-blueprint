"""
ciclo_de_vida.py
================

Builders PUROS (sin I/O) de lo que hace que un cluster no quede "pelado" con el
paso del tiempo: la política de Index State Management (ISM) de cada dataset y
su rollup (Index Rollups).

- **Política ISM** `<slug>-ciclo-de-vida`: los índices del dataset pasan a solo
  lectura y se compactan (`force_merge` a un segmento) a los 35 días, y se
  borran pasada la retención. Los índices son mensuales (`<base>-YYYY.MM`):
  el del mes en curso recibe datos hasta 31 días después de creado, por eso
  "tibio" recién a los 35 y el borrado a los `retencion + 31` (así cada dato
  se conserva, al menos, la retención). Se aplica por `ism_template` a los
  índices nuevos y por `_plugins/_ism/add` a los que ya existen.
- **Rollup** `<slug>-rollup` → índice `rollup-<slug>` (fuera del pattern del
  caso, como `perfil-<slug>`): un resumen por hora de las medidas, por las
  dimensiones de pocos valores. Queda aunque los datos crudos se borren: es la
  retención larga barata.

`ahora_ms` se pasa de afuera (el `start_time` del schedule): así el builder es
puro y testeable.
"""

from __future__ import annotations

import re
from typing import Any

# Los índices son mensuales: el del mes en curso se sigue escribiendo hasta 31
# días después de creado. Antes de eso no puede pasar a solo lectura.
DIAS_HASTA_TIBIO = 35
_MES = 31
RETENCION_POR_DEFECTO = 90
RETENCION_MINIMA, RETENCION_MAXIMA = 7, 3650

_NUMERICOS = ("integer", "long", "float", "double", "short", "scaled_float", "half_float")
_TEXTO = ("keyword", "string")
_ROLES_DIMENSION = ("primary_dimension", "dimension", "critical_indicator", "success_indicator")
_MAX_DIMENSIONES, _MAX_MEDIDAS = 3, 3


def nombre_de_politica(slug: str) -> str:
    return f"{slug}-ciclo-de-vida"


def nombre_del_rollup(slug: str) -> str:
    return f"{slug}-rollup"


def indice_del_rollup(slug: str) -> str:
    return f"rollup-{slug}"


def retencion(valor: Any) -> int:
    """Los días de retención pedidos, dentro de lo razonable; sin dato, 90."""
    try:
        dias = int(valor or 0)
    except (TypeError, ValueError):
        dias = 0
    if dias <= 0:
        return RETENCION_POR_DEFECTO
    return max(RETENCION_MINIMA, min(RETENCION_MAXIMA, dias))


def dias_hasta_borrar(retencion_dias: int) -> int:
    return max(DIAS_HASTA_TIBIO + 1, retencion(retencion_dias) + _MES)


def politica(slug: str, index_pattern: str, retencion_dias: int) -> dict[str, Any]:
    """`PUT _plugins/_ism/policies/<slug>-ciclo-de-vida`."""
    borrar = dias_hasta_borrar(retencion_dias)
    return {"policy": {
        "description": (f"Ciclo de vida de {slug} (plataforma): solo lectura a los {DIAS_HASTA_TIBIO} días, "
                        f"borrado a los {borrar} (retención de {retencion(retencion_dias)} días)"),
        "default_state": "caliente",
        "states": [
            {"name": "caliente", "actions": [],
             "transitions": [{"state_name": "tibio", "conditions": {"min_index_age": f"{DIAS_HASTA_TIBIO}d"}}]},
            {"name": "tibio", "actions": [{"read_only": {}}, {"force_merge": {"max_num_segments": 1}}],
             "transitions": [{"state_name": "borrar", "conditions": {"min_index_age": f"{borrar}d"}}]},
            {"name": "borrar", "actions": [{"delete": {}}], "transitions": []},
        ],
        "ism_template": [{"index_patterns": [index_pattern], "priority": 100}],
    }}


def _ruta(f: dict) -> str:
    return (f.get("field_path") or "").strip()


# Números que no son una medida: códigos, tipos, puertos, secuencias,
# coordenadas, índices (visto: el rollup de billetera sumaba `message_type` y
# `sequence_number`; el de CTS, el `code` HTTP).
_NO_ES_MEDIDA = re.compile(r"code|codigo|type|tipo|seq|number|nro|port|puerto|status|estado|proto|transport|"
                           r"geo|lat$|lon|long$|index|counter|year|month|day|hour|version|level|nivel", re.I)


def candidatos_a_dimension(fields: list[dict]) -> list[dict]:
    """Los campos de texto que pueden ser una dimensión (no un id), marcados
    para que el cluster diga cuáles tienen pocos valores (`_discover_enums`
    solo mira los marcados: en un caso curado no hay marcas)."""
    return [{**f, "dimension": True} for f in fields or []
            if isinstance(f, dict) and (f.get("type") or "") in _TEXTO and _ruta(f) and not _parece_id(_ruta(f))]


def _parece_id(ruta: str) -> bool:
    nombre = ruta.lower().rsplit(".", 1)[-1]
    return nombre in ("id", "uuid") or nombre.endswith(("_id", "id_", "_uuid")) or nombre.startswith("id_")


def dimensiones_y_medidas(fields: list[dict], spec: "dict | None" = None,
                          enums: "dict[str, list] | None" = None) -> tuple[list[str], list[str]]:
    """(dimensiones, medidas) del rollup.

    Dimensiones: campos de texto con pocos valores. Con el cluster, los que
    descubrió `_discover_enums` (`enums`); sin él, los marcados con rol de
    dimensión en el paso 2. Medidas: las marcadas como medida, las que suman
    los pronósticos y, si no hay, los números que no son un id."""
    spec = spec or {}
    fields = [f for f in fields or [] if isinstance(f, dict) and _ruta(f)]
    por_ruta = {_ruta(f): f for f in fields}
    dims: list[str] = []
    con_rol = [_ruta(f) for f in fields if (f.get("role") or "") in _ROLES_DIMENSION
               and (f.get("type") or "") in _TEXTO]
    for ruta in con_rol + [r for r in (enums or {}) if r in por_ruta or not por_ruta]:
        if ruta not in dims and not _parece_id(ruta):
            dims.append(ruta)
    medidas: list[str] = [_ruta(f) for f in fields if (f.get("role") or "") == "measure"
                          and (f.get("type") or "") in _NUMERICOS]
    for fc in spec.get("forecasts") or []:
        for agg in (fc.get("aggregation_query") or {}).values():
            for tipo, cuerpo in (agg or {}).items():
                campo = (cuerpo or {}).get("field", "")
                if tipo in ("sum", "avg") and campo and campo not in medidas:
                    medidas.append(campo)
    if not medidas:
        medidas = [_ruta(f) for f in fields if (f.get("type") or "") in _NUMERICOS and not _parece_id(_ruta(f))
                   and not _NO_ES_MEDIDA.search(_ruta(f).rsplit(".", 1)[-1])]
    return dims[:_MAX_DIMENSIONES], medidas[:_MAX_MEDIDAS]


def rollup(slug: str, index_pattern: str, dimensiones: list[str], medidas: list[str],
           ahora_ms: int, intervalo: str = "1h") -> dict[str, Any]:
    """`PUT _plugins/_rollup/jobs/<slug>-rollup`. Continuo: cada hora suma lo
    que entró."""
    return {"rollup": {
        "enabled": True,
        "continuous": True,
        "description": f"Resumen por hora de {slug} (plataforma): queda aunque se borren los datos crudos",
        "source_index": index_pattern,
        "target_index": indice_del_rollup(slug),
        "page_size": 1000,
        "delay": 0,
        "schedule": {"interval": {"period": 1, "unit": "Hours", "start_time": int(ahora_ms)}},
        "dimensions": [{"date_histogram": {"source_field": "@timestamp", "fixed_interval": intervalo,
                                           "timezone": "UTC"}}]
                      + [{"terms": {"source_field": d}} for d in dimensiones],
        "metrics": [{"source_field": m, "metrics": [{"sum": {}}, {"avg": {}}, {"min": {}}, {"max": {}},
                                                    {"value_count": {}}]} for m in medidas],
    }}
