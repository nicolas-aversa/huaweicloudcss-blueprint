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
#
# El template NOMBRA todos los campos que recibe, strings incluidos. Antes solo
# declaraba números, IPs, fechas y booleanos, y el resto caía al dynamic
# template: el resultado era el mismo keyword, pero el template no decía qué
# campos tenía el índice, y un campo que llegaba como texto con cara de número
# ("12") quedaba keyword sin que nada lo avisara. El dynamic template sigue,
# como red para un campo que aparezca y nadie haya declarado.
_KEYWORD: dict[str, Any] = {"type": "keyword", "ignore_above": 1024}
_FIELD_TYPE_TO_OS: dict[str, dict[str, Any]] = {
    "string": _KEYWORD,
    "keyword": _KEYWORD,
    "integer": {"type": "long"},
    "long": {"type": "long"},
    "float": {"type": "double"},
    "double": {"type": "double"},
    "ip": {"type": "ip"},
    "boolean": {"type": "boolean"},
    # `ignore_malformed` va acá adentro a propósito: el del índice (más abajo,
    # en `settings`) NO cubre geo_point, y sin esto una sola coordenada basura
    # —un "N/D", un (0,0) mal armado— rechaza el documento ENTERO en vez del
    # campo. Es la diferencia entre perder una columna y perder la fila.
    "geo_point": {"type": "geo_point", "ignore_malformed": True},
    # Format lenient, COMPACTO PRIMERO. El orden importa: OpenSearch prueba los
    # formatos en orden y usa el primero que matchea. Un timestamp compacto de
    # 17 dígitos (`20251004235759139`) es un long válido, así que si `epoch_millis`
    # va antes lo parsea como epoch-millis → año 643698 (fuera de rango, rompe
    # Discover/visualizaciones). Espeja el orden de `_TS_DATE_PATTERNS`
    # (maas_integrator): compactos antes que epoch.
    #
    # Y con las variantes con ESPACIO y `dd/MM/yyyy`: `strict_date_optional_time`
    # exige la `T`, así que `2026-09-18 11:04:12` —lo más común en un CSV— no
    # entraba en ninguno de los formatos y el campo se descartaba en silencio
    # (queda en `_source`, pero no se puede filtrar, agregar ni graficar). Esto es
    # la red para un campo `date` sin formato conocido; cuando el perfilador lo
    # detectó, `date_format` va primero (ver `_mapping_de`).
    "date": {
        "type": "date",
        "format": ("yyyyMMddHHmmssSSS||yyyyMMddHHmmss||strict_date_optional_time"
                   "||yyyy-MM-dd HH:mm:ss.SSS||yyyy-MM-dd HH:mm:ss||yyyy-MM-dd HH:mm"
                   "||dd/MM/yyyy HH:mm:ss||dd/MM/yyyy HH:mm||dd/MM/yyyy||epoch_millis"),
    },
    # Sub-campo keyword: full-text searchable (text) Y aggregatable (.keyword).
    "text": {
        "type": "text",
        "fields": {"keyword": {"type": "keyword", "ignore_above": 1024}},
    },
}


# Palabras clave del `date` filter de Logstash que OpenSearch no entiende. Los
# patrones literales (`yyyy-MM-dd HH:mm:ss`, `dd/MM/yyyy`) valen igual en los dos.
_JODA_A_OPENSEARCH = {
    "ISO8601": "strict_date_optional_time",
    "UNIX": "epoch_second",
    "UNIX_MS": "epoch_millis",
}


def _mapping_de(f: dict[str, Any]) -> dict[str, Any] | None:
    """El mapping de un campo, con el formato real de su fecha si se conoce.

    El `date` filter del .conf y el `format` del template tienen que decir lo
    mismo: si no, el filter lleva bien la fecha a `@timestamp` pero el campo
    original se indexa con un formato que no lo acepta y se descarta.
    """
    tipo = (f.get("type") or "string").strip().lower()
    # Un tipo que no conocemos va como keyword —lo mismo que haría el dynamic
    # template—, pero declarado. Un objeto no: sus hijos se declaran solos.
    if tipo in ("object", "nested"):
        return None
    base = _FIELD_TYPE_TO_OS.get(tipo, _KEYWORD)
    mapping = copy.deepcopy(base)
    formato = (f.get("date_format") or "").strip()
    if mapping.get("type") == "date" and formato:
        propio = _JODA_A_OPENSEARCH.get(formato, formato)
        resto = [x for x in mapping["format"].split("||") if x != propio]
        mapping["format"] = "||".join([propio, *resto])
    return mapping


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
    # Si ya hay un objeto con ese nombre (`user` con `user.name` adentro), gana
    # el objeto: un keyword `user` encima lo borraría, y el documento —que trae
    # `user` como objeto— rebotaría entero. Da lo mismo el orden de los campos.
    actual = node.get(parts[-1])
    if isinstance(actual, dict) and "properties" in actual:
        return
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
        os_mapping = _mapping_de(f)
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
        _set_nested(properties, path, os_mapping)

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
