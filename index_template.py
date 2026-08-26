"""
index_template.py
=================

Construye el **index template** de OpenSearch a partir de los campos detectados
en el step 2 del wizard.

Filosofía (decidida con el operador): el mapeo de TIPOS pertenece a un index
template en OpenSearch, NO a `mutate convert` en Logstash. Logstash solo
parsea/estructura; el template tipa. Esto resuelve de raíz problemas que el
convert no podía:

  - `trxl_resp="000"` → keyword → se preserva (convert lo volvía 0).
  - `trxl_sw_version` numérico en un doc y `"1.13.5_fix_DHWS"` en otro → keyword
    → cero conflicto de mapping (con convert, OpenSearch rechazaba el 2do doc).

Estrategia del template (espeja `docs/reference_index_template.json`):
  - `dynamic_templates: strings_as_keyword` → TODO string se mapea keyword por
    default (aggregatable, sin análisis de texto, sin pérdida de ceros).
  - `properties` explícitas SOLO para lo que no es keyword: medidas numéricas
    (`integer→long`, `float→double`), `ip`, `boolean`, `date` (con format
    lenient), `geo_point`, `text` (con sub-campo keyword) y `@timestamp`. El
    resto (códigos, IDs, strings) cae al dynamic template.

Los campos COMPUTADOS de capa 2 del pipeline de referencia —`nested` para
`trxl_tech_detail.data.steps`, el objeto `funnel`, `trxl_geo` (geo_point)— NO
salen del detector plano del step 2: son reglas de negocio que el copiloto
agrega al template cuando se construye la capa 2. Este builder solo tipa los
campos planos detectados.
"""

from __future__ import annotations

import copy
import json
from typing import Any


def index_pattern_from_name(index_name: str) -> str:
    """Deriva el ``index_patterns`` del nombre de índice del output Logstash.

    - ``"logs-hoje-%{+YYYY.MM}"`` → ``"logs-hoje-*"`` (corta en el date math).
    - ``"transacciones"`` (estático) → ``"transacciones"`` (match exacto).
    """
    name = (index_name or "").strip()
    if not name:
        return "logs-*"
    if "%{" in name:
        prefix = name.split("%{", 1)[0].rstrip("-._")
        return f"{prefix}-*" if prefix else "logs-*"
    return name


# Tipo del campo (lo que el step 2 muestra) → mapping de OpenSearch.
# Solo mapeamos explícito lo que NO debe caer al dynamic keyword; los strings,
# códigos e IDs caen al dynamic template (keyword). Cubre TODO el vocabulario
# que emite el detector (`field_mappings.json` → `_infer_field_type`):
# integer/float/ip/boolean/date/text + geo_point (passthrough para edición
# manual o el copiloto). Espeja los tipos de `docs/reference_index_template.json`.
_FIELD_TYPE_TO_OS: dict[str, dict[str, Any]] = {
    "integer": {"type": "long"},
    "float": {"type": "double"},
    "ip": {"type": "ip"},
    "boolean": {"type": "boolean"},
    "geo_point": {"type": "geo_point"},
    # Format lenient, COMPACTO PRIMERO. El orden importa: OpenSearch prueba los
    # formatos en orden y usa el primero que matchea. Un timestamp compacto de
    # 17 dígitos (`20251004235759139`) es un long válido, así que si `epoch_millis`
    # va antes lo parsea como epoch-millis → año 643698 (fuera de rango, rompe
    # Discover/visualizaciones). Espeja el orden de `_TS_DATE_PATTERNS`
    # (maas_integrator): compactos antes que epoch.
    "date": {
        "type": "date",
        "format": "yyyyMMddHHmmssSSS||yyyyMMddHHmmss||strict_date_optional_time||epoch_millis",
    },
    # Sub-campo keyword: full-text searchable (text) Y aggregatable (.keyword).
    "text": {
        "type": "text",
        "fields": {"keyword": {"type": "keyword", "ignore_above": 1024}},
    },
}


def _set_nested(props: dict[str, Any], dotted_path: str, mapping: dict[str, Any]) -> None:
    """Inserta ``mapping`` en ``props`` siguiendo ``dotted_path`` (ej.
    ``source.geo.country_name``), creando/mergeando objetos
    ``{parent: {"properties": {...}}}`` por nivel. Varias hojas con el mismo
    parent (``source.ip``, ``source.port``) se mergean bajo un único ``source``.
    """
    parts = [p for p in dotted_path.split(".") if p]
    if not parts:
        return
    node = props
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict) or "properties" not in child:
            child = {"properties": {}}
            node[part] = child
        node = child["properties"]
    node[parts[-1]] = mapping


def build_index_template(
    fields: list[dict[str, Any]],
    namespace: str,
    index_name: str,
) -> dict[str, Any]:
    """Arma el composable index template a partir de los campos detectados.

    Parameters
    ----------
    fields
        Lista de campos del step 2 (cada uno con ``raw_name`` y ``type``).
    namespace
        Parent bajo el cual viven los campos (``data`` por default).
    index_name
        Nombre del índice del output Logstash (para derivar el pattern).
    """
    # Namespace vacío ("") => campos top-level. None/ausente => "data" (back-compat).
    ns = namespace.strip() if isinstance(namespace, str) else "data"

    # Propiedades explícitas SOLO para medidas numéricas/ip/date/etc.; el resto
    # (códigos, IDs, strings) cae al dynamic template (keyword). Se tipa por el
    # PATH REAL del campo en el evento (lo que se indexa), NO por raw_name: el
    # filtro puede renombrar `srcip` → `[source][ip]`, así que el campo indexado
    # es `source.ip` y el mapping tiene que ir ahí (anidado), no en `srcip`.
    properties: dict[str, Any] = {"@timestamp": {"type": "date"}}
    for f in fields or []:
        os_mapping = _FIELD_TYPE_TO_OS.get(f.get("type"))
        if not os_mapping:
            continue
        # field_path/ecs_path ya incluyen el namespace cuando aplica (ej.
        # `data.xxx` en custom, `source.ip` en los predefinidos ECS). Fallback:
        # raw_name bajo el namespace (back-compat con callers que solo mandan
        # raw_name, ej. el detector del step 2 sin path explícito).
        path = (f.get("field_path") or f.get("ecs_path") or "").strip()
        if not path:
            raw = (f.get("raw_name") or "").strip()
            if not raw:
                continue
            path = f"{ns}.{raw}" if ns else raw
        _set_nested(properties, path, copy.deepcopy(os_mapping))

    return {
        "index_patterns": [index_pattern_from_name(index_name)],
        "template": {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 1,
                # Tolerar valores que no coercen al tipo del campo (ej. el string
                # "null" en un campo `double`, o basura en un date/ip): se saltea
                # SOLO ese campo (queda en _source, no agregable) en vez de
                # rechazar el documento entero con mapper_parsing_exception. Los
                # logs reales siempre traen nulls/vacíos en campos numéricos.
                "index.mapping.ignore_malformed": True,
                # Subir el límite de campos (default 1000): los logs financieros/
                # propietarios traen 200+ campos por evento (+ subobjetos del JSON
                # anidado), y pasarse del límite también rechaza el documento.
                "index.mapping.total_fields.limit": 2000,
            },
            "mappings": {
                "dynamic_templates": [
                    {
                        "strings_as_keyword": {
                            "match_mapping_type": "string",
                            "mapping": {"type": "keyword", "ignore_above": 1024},
                        }
                    }
                ],
                "properties": properties,
            },
        },
    }


def put_snippet(template_name: str, template: dict[str, Any]) -> str:
    """Devuelve el snippet ``PUT _index_template/<name>`` listo para pegar en
    Kibana Dev Tools del console (que alcanza OpenSearch in-VPC)."""
    name = (template_name or "log-analytics").strip() or "log-analytics"
    body = json.dumps(template, indent=2, ensure_ascii=False)
    return f"PUT _index_template/{name}\n{body}"
