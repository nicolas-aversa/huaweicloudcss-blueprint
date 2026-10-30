"""
dashboards.py
=============

Generador de dashboards baseline por caso de uso para OpenSearch Dashboards.

Un solo formato de spec: `"panels"`, dashboards modelados en los *sample data* de
OpenSearch (eCommerce / Flights / [Logs] Web Traffic) y mapeados a los campos
REALES de cada tipo. Hubo un segundo formato (`"visualizations"`) con su propio
builder, pero ningún spec lo usaba desde hacía tiempo: la rama era inalcanzable y
se fue con su cadena de funciones.

Los specs de cada vertical salen del registro declarativo `verticals/`
(`_verticals.dashboard_specs()`); acá solo queda `_SPECS_SIN_VERTICAL` con los que
NO tienen un vertical asociado (hoy `firewall`, naming ECS previo al SIEM).

`build_ndjson(slug)` arma los saved objects (visState/searchSourceJSON/panelsJSON
+ references) y emite NDJSON listo para importar vía
POST /api/saved_objects/_import?overwrite=true.

Decisiones de identidad (clave para que re-importar sea idempotente):
  - index-pattern `id == title == "<slug>-*"` (determinista, no uuid). Encadena con
    el output Logstash `<slug>-%{+YYYY.MM}` y el index template `<slug>-*`.
  - viz/dashboard NO hardcodean el id del index-pattern inline: lo apuntan por
    `references` + placeholder `indexRefName` en el searchSourceJSON (patrón de los
    exports sample de OpenSearch).
  - ids de viz/dashboard = uuid5(slug+título) → `overwrite=true` reemplaza el mismo
    objeto en vez de duplicar.

Los strings son keyword por el dynamic template del index template → terms directo,
sin `.keyword`.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import verticals as _verticals


# ── Identidad determinista ───────────────────────────────────────────────────

# Namespace fijo para derivar ids estables de viz/dashboard (uuid5). No cambiar:
# cambiarlo re-asigna todos los ids y duplicaría objetos en un re-import.
_ID_NAMESPACE = uuid.UUID("6f9b1d2e-3c4a-5b6c-7d8e-9f0a1b2c3d4e")


def _stable_id(slug: str, kind: str, key: str) -> str:
    """Id determinista para un saved object (viz/dashboard) de un caso."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"{slug}:{kind}:{key}"))


# ── Aggs reutilizables ───────────────────────────────────────────────────────

def _agg_label(agg: str, field: str | None) -> str:
    if agg == "count":
        return "Count"
    if agg == "cardinality":
        return f"Unique {field}"
    return f"{agg.capitalize()} of {field}"


def _metric_agg(agg: str, field: str | None, label: str | None, _id: str = "1") -> dict[str, Any]:
    params: dict[str, Any] = {}
    if agg != "count" and field:
        params["field"] = field
    if label:
        params["customLabel"] = label
    return {"id": _id, "enabled": True, "type": agg, "schema": "metric", "params": params}


def _terms_agg(_id: str, field: str, schema: str, size: int) -> dict[str, Any]:
    return {
        "id": _id, "enabled": True, "type": "terms", "schema": schema,
        "params": {
            "field": field, "orderBy": "1", "order": "desc", "size": size,
            "otherBucket": False, "otherBucketLabel": "Other",
            "missingBucket": False, "missingBucketLabel": "Missing",
        },
    }


def _datehist_agg(_id: str = "2") -> dict[str, Any]:
    return {
        "id": _id, "enabled": True, "type": "date_histogram", "schema": "segment",
        "params": {
            "field": "@timestamp", "interval": "auto",
            "drop_partials": False, "min_doc_count": 1, "extended_bounds": {},
        },
    }


def _category_axis(position: str = "bottom") -> list[dict[str, Any]]:
    return [{
        "id": "CategoryAxis-1", "type": "category", "position": position, "show": True,
        "style": {}, "scale": {"type": "linear"},
        "labels": {"show": True, "rotate": 0, "filter": position == "bottom", "truncate": 100},
        "title": {},
    }]


def _value_axis(title: str = "Count", position: str = "left") -> list[dict[str, Any]]:
    return [{
        "id": "ValueAxis-1", "name": "LeftAxis-1", "type": "value", "position": position, "show": True,
        "style": {}, "scale": {"type": "linear", "mode": "normal"},
        "labels": {"show": True, "rotate": 0, "filter": False, "truncate": 100},
        "title": {"text": title},
    }]


# ── visState por tipo (modelado en los exports sample de OpenSearch) ──────────

def _vs_markdown(title: str, md: str) -> dict[str, Any]:
    return {"title": title, "type": "markdown", "aggs": [],
            "params": {"fontSize": 14, "openLinksInNewTab": False, "markdown": md}}


def _vs_metric(title: str, agg: str, field: str | None, label: str | None) -> dict[str, Any]:
    return {
        "title": title, "type": "metric",
        "aggs": [_metric_agg(agg, field, label or _agg_label(agg, field))],
        "params": {
            "addTooltip": True, "addLegend": False, "type": "metric",
            "metric": {
                "percentageMode": False, "useRanges": False, "colorSchema": "Green to Red",
                "metricColorMode": "None", "colorsRange": [{"from": 0, "to": 10000}],
                "labels": {"show": True}, "invertColors": False,
                "style": {"bgFill": "#000", "bgColor": False, "labelColor": False,
                          "subText": "", "fontSize": 36},
            },
        },
    }


def _vs_pie(title: str, field: str, size: int = 10) -> dict[str, Any]:
    return {
        "title": title, "type": "pie",
        "aggs": [_metric_agg("count", None, None), _terms_agg("2", field, "segment", size)],
        "params": {
            "type": "pie", "addTooltip": True, "addLegend": True, "legendPosition": "right",
            "isDonut": True,
            "labels": {"show": False, "values": True, "last_level": True, "truncate": 100},
        },
    }


def _vs_bar(title: str, field: str, horizontal: bool = False, size: int = 10,
            metric_agg: str = "count", metric_field: str | None = None) -> dict[str, Any]:
    cat_pos = "left" if horizontal else "bottom"
    val_pos = "bottom" if horizontal else "left"
    vtype = "horizontal_bar" if horizontal else "histogram"
    label = _agg_label(metric_agg, metric_field)
    return {
        "title": title, "type": vtype,
        "aggs": [_metric_agg(metric_agg, metric_field, label), _terms_agg("2", field, "segment", size)],
        "params": {
            "type": "histogram", "grid": {"categoryLines": False},
            "categoryAxes": _category_axis(cat_pos),
            "valueAxes": _value_axis(label, val_pos),
            "seriesParams": [{
                "show": True, "type": "histogram", "mode": "normal",
                "data": {"label": label, "id": "1"}, "valueAxis": "ValueAxis-1",
                "drawLinesBetweenPoints": True, "showCircles": True,
            }],
            "addTooltip": True, "addLegend": True, "legendPosition": "right",
            "times": [], "addTimeMarker": False, "labels": {},
        },
    }


def _vs_timeseries(
    title: str, kind: str, metric_agg: str, metric_field: str | None,
    split_field: str | None = None, size: int = 5,
) -> dict[str, Any]:
    # La partida va antes que la fecha: así los `size` valores son los de todo
    # el rango. Anidada en la fecha, cada intervalo traía sus propios N y la
    # leyenda juntaba cientos.
    aggs = [_metric_agg(metric_agg, metric_field, None)]
    if split_field:
        aggs.append(_terms_agg("3", split_field, "group", size))
    aggs.append(_datehist_agg("2"))
    label = _agg_label(metric_agg, metric_field)
    series_type = "area" if kind == "area" else "line"
    mode = "stacked" if kind == "area" else "normal"
    return {
        "title": title, "type": kind,
        "aggs": aggs,
        "params": {
            "type": kind, "grid": {"categoryLines": False},
            "categoryAxes": _category_axis("bottom"),
            "valueAxes": _value_axis(label, "left"),
            "seriesParams": [{
                "show": True, "type": series_type, "mode": mode,
                "data": {"label": label, "id": "1"}, "valueAxis": "ValueAxis-1",
                "drawLinesBetweenPoints": True, "lineWidth": 2,
                "showCircles": True, "interpolate": "linear",
            }],
            "addTooltip": True, "addLegend": True, "legendPosition": "right",
            "times": [], "addTimeMarker": False,
        },
    }


def _vs_table(title: str, field: str, size: int = 10,
              metric_agg: str = "count", metric_field: str | None = None) -> dict[str, Any]:
    label = _agg_label(metric_agg, metric_field)
    return {
        "title": title, "type": "table",
        "aggs": [_metric_agg(metric_agg, metric_field, label), _terms_agg("2", field, "bucket", size)],
        "params": {
            "perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False,
            "showTotal": False, "totalFunc": "sum", "percentageCol": "",
        },
    }


def _vs_tagcloud(title: str, field: str, size: int = 15) -> dict[str, Any]:
    return {
        "title": title, "type": "tagcloud",
        "aggs": [_metric_agg("count", None, None), _terms_agg("2", field, "segment", size)],
        "params": {"scale": "linear", "orientation": "single",
                   "minFontSize": 18, "maxFontSize": 72, "showLabel": True},
    }


def _vs_coordinate_map(title: str, field: str, precision: int = 3) -> dict[str, Any]:
    """Mapa de coordenadas (Coordinate Map / `tile_map`): un geohash_grid sobre un
    campo geo_point con un metric count. Center/zoom apuntados a Argentina."""
    return {
        "title": title, "type": "tile_map",
        "aggs": [
            _metric_agg("count", None, None),
            {
                "id": "2", "enabled": True, "type": "geohash_grid", "schema": "segment",
                "params": {
                    "field": field, "autoPrecision": True, "precision": precision,
                    "useGeocentroid": True, "isFilteredByCollar": True,
                },
            },
        ],
        "params": {
            "mapType": "Scaled Circle Markers", "isDesaturated": True, "addTooltip": True,
            "heatClusterSize": 1.5, "legendPosition": "bottomright",
            "mapZoom": 4, "mapCenter": [-38.0, -63.0],
            "wms": {"enabled": False, "options": {"format": "image/png", "transparent": True}},
        },
    }


def _normalize_controls(controls: list) -> list[dict[str, str]]:
    """Acepta `["side", ...]` o `[{"field","label"}, ...]` → lista de {field,label}."""
    out = []
    for c in controls:
        if isinstance(c, str):
            out.append({"field": c, "label": c})
        else:
            out.append({"field": c["field"], "label": c.get("label") or c["field"]})
    return out


def _vs_input_controls(title: str, controls: list[dict[str, str]]) -> dict[str, Any]:
    """visState del `input_control_vis` (la visualización "Controls" de OpenSearch
    Dashboards): una barra de dropdowns Options-list que filtran el dashboard por
    una terms agg. Cada control referencia el index-pattern por `control_<i>_index_pattern`
    (las refs las pone `_controls_viz_obj`)."""
    ctrl_params = []
    for i, c in enumerate(controls):
        ctrl_params.append({
            "id": str(i + 1),
            "indexPatternRefName": f"control_{i}_index_pattern",
            "fieldName": c["field"],
            "parent": "",
            "label": c["label"],
            "type": "list",
            "options": {"type": "terms", "multiselect": True, "dynamicOptions": True,
                        "size": 10, "order": "desc"},
        })
    return {
        "title": title, "type": "input_control_vis", "aggs": [],
        "params": {
            "controls": ctrl_params,
            "updateFiltersOnChange": False,
            "useTimeFilter": True,
            "pinFilters": False,
        },
    }


# La capa de países del servicio de mapas de OpenSearch (maps.opensearch.org,
# la carga el navegador): tal como la arma OpenSearch Dashboards para una Region
# Map. Se fija la capa y el campo de unión: si no, Dashboards toma el primero,
# `iso2`, y los datos traen el NOMBRE del país ("Canada", "Netherlands").
_CAPA_DE_PAISES: dict[str, Any] = {
    "name": "World Countries", "origin": "elastic_maps_service", "id": "world_countries",
    "attribution": "Made with NaturalEarth",
    "fields": [
        {"type": "id", "name": "iso2", "description": "ISO 3166-1 alpha-2 Code"},
        {"type": "id", "name": "iso3", "description": "ISO 3166-1 alpha-3 Code"},
        {"type": "name", "name": "name", "description": "Name"},
    ],
    "format": {"type": "geojson"}, "layerId": "elastic_maps_service.World Countries", "isEMS": True,
}


_PISTA_DE_PAIS = re.compile(r"(country|pa[ií]s|naci[oó]n)", re.I)
_CODIGO_DE_PAIS = re.compile(r"(code|c[oó]digo|iso)", re.I)


def _vs_region_map(title: str, field: str, size: int = 100) -> dict[str, Any]:
    """Mapa por país (Region Map / `region_map`): un terms sobre el campo con el
    nombre del país, unido por nombre con la capa World Countries."""
    return {
        "title": title, "type": "region_map",
        "aggs": [
            _metric_agg("count", None, None),
            {"id": "2", "enabled": True, "type": "terms", "schema": "segment",
             "params": {"field": field, "size": size, "order": "desc", "orderBy": "1",
                        "otherBucket": False, "missingBucket": False}},
        ],
        "params": {
            "layerChosenByUser": "default", "legendPosition": "bottomright", "addTooltip": True,
            "colorSchema": "Reds", "emsHotLink": "", "isDisplayWarning": False,
            "wms": {"enabled": False, "options": {"format": "image/png", "transparent": True}},
            "mapZoom": 2, "mapCenter": [20, 0], "outlineWeight": 1, "showAllShapes": True,
            "selectedLayer": _CAPA_DE_PAISES,
            "selectedJoinField": {"type": "name", "name": "name", "description": "Name"},
        },
    }


# ── Paneles del dashboard automático: series con varias métricas o partidas por
#    consultas, percentiles, tablas de varias columnas y mapa de calor ─────────
def _agg_metrica(_id: str, agg: str, field: "str | None", label: "str | None",
                 percents: "list[int] | None" = None) -> dict[str, Any]:
    a = _metric_agg(agg, field, label, _id)
    if agg == "percentiles":
        a["params"]["percents"] = list(percents or [50, 95, 99])
    return a


def _agg_filtros(_id: str, schema: str, filtros: list) -> dict[str, Any]:
    """Una serie (o una barra) por consulta KQL: `[(consulta, nombre)]`."""
    return {"id": _id, "enabled": True, "type": "filters", "schema": schema,
            "params": {"filters": [{"input": {"query": q, "language": "kuery"}, "label": l} for q, l in filtros]}}


def _metricas_del_panel(panel: dict[str, Any]) -> list[tuple]:
    """`[(agg, campo, etiqueta, percents)]`: las de `metricas` o la única de siempre."""
    if panel.get("metricas"):
        return [tuple(m) + (None,) * (4 - len(m)) for m in panel["metricas"]]
    return [(panel.get("metric") or panel.get("agg") or "count", panel.get("field") if panel.get("metric") not in
             (None, "count") else None, panel.get("label"), panel.get("percents"))]


def _vs_serie(panel: dict[str, Any]) -> dict[str, Any]:
    """area / line / histogram con varias métricas (un promedio y un p99),
    percentiles, o partida por consultas (`filtros`) o por un campo (`split`)."""
    kind = panel["type"]
    metricas = _metricas_del_panel(panel)
    aggs = [_agg_metrica("1" if i == 0 else str(10 + i), agg, campo, etiqueta, percents)
            for i, (agg, campo, etiqueta, percents) in enumerate(metricas)]
    fecha = _datehist_agg("2")
    partida = None
    if panel.get("filtros"):
        partida = _agg_filtros("3", "group", panel["filtros"])
    elif panel.get("split"):
        partida = _terms_agg("3", panel["split"], "group", panel.get("size", 6))
    etiqueta = metricas[0][2] or _agg_label(metricas[0][0], metricas[0][1])
    tipo_serie = "line" if kind == "line" else kind
    modo = "stacked" if kind in ("area", "histogram") and partida else "normal"
    return {
        "title": panel["title"], "type": kind, "aggs": aggs + ([partida] if partida else []) + [fecha],  # la partida, antes (ver `_vs_timeseries`)
        "params": {
            "type": kind, "grid": {"categoryLines": False},
            "categoryAxes": _category_axis("bottom"), "valueAxes": _value_axis(etiqueta, "left"),
            "seriesParams": [{
                "show": True, "type": tipo_serie, "mode": modo,
                "data": {"label": a["params"].get("customLabel") or etiqueta, "id": a["id"]}, "valueAxis": "ValueAxis-1",
                "drawLinesBetweenPoints": True, "lineWidth": 2, "showCircles": True, "interpolate": "linear",
            } for a in aggs],
            "addTooltip": True, "addLegend": True, "legendPosition": "right", "times": [], "addTimeMarker": False,
        },
    }


def _vs_tabla(panel: dict[str, Any]) -> dict[str, Any]:
    """Una tabla con varias columnas (eventos, monto, tiempo promedio…) por un
    campo y, opcional, partida por otro (`sub_field`)."""
    metricas = _metricas_del_panel(panel)
    aggs = [_agg_metrica("1" if i == 0 else str(10 + i), agg, campo, etiqueta, percents)
            for i, (agg, campo, etiqueta, percents) in enumerate(metricas)]
    filas = _terms_agg("2", panel["field"], "bucket", panel.get("size", 15))
    filas["params"]["orderBy"] = panel.get("orden", "1")
    if panel.get("label_filas"):
        filas["params"]["customLabel"] = panel["label_filas"]
    sub = []
    if panel.get("sub_field"):
        s = _terms_agg("3", panel["sub_field"], "bucket", panel.get("sub_size", 3))
        if panel.get("label_sub"):
            s["params"]["customLabel"] = panel["label_sub"]
        sub = [s]
    return {"title": panel["title"], "type": "table", "aggs": aggs + [filas] + sub,
            "params": {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False,
                       "showTotal": False, "totalFunc": "sum", "percentageCol": ""}}


def _vs_mapa_de_calor(title: str, field: str, size: int = 12) -> dict[str, Any]:
    """Filas = los valores de `field` (una entidad), columnas = el tiempo.
    Las filas van antes que la fecha: anidadas en ella, cada intervalo traía
    sus 12 y una entidad con miles de valores juntaba cientos de filas
    ("There are too many series defined (526). The configured maximum is 50")."""
    return {"title": title, "type": "heatmap",
            "aggs": [_metric_agg("count", None, "Eventos"), _terms_agg("3", field, "group", size),
                     _datehist_agg("2")],
            "params": {"type": "heatmap", "addTooltip": True, "addLegend": True, "enableHover": False,
                       "legendPosition": "right", "times": [], "colorsNumber": 5, "colorSchema": "Reds",
                       "setColorRange": False, "colorsRange": [], "invertColors": False, "percentageMode": False,
                       "valueAxes": [{"show": False, "id": "ValueAxis-1", "type": "value",
                                      "scale": {"type": "linear", "defaultYExtents": False},
                                      "labels": {"show": False, "rotate": 0, "overwriteColor": False,
                                                 "color": "black"}}]}}


def _panel_to_vis_state(panel: dict[str, Any]) -> dict[str, Any]:
    """Despacha un panel (spec rich) a su visState."""
    t = panel["type"]
    title = panel["title"]
    if t == "markdown":
        return _vs_markdown(title, panel["md"])
    if t == "controls":
        return _vs_input_controls(title, _normalize_controls(panel["controls"]))
    if t == "map":
        return _vs_coordinate_map(title, panel["field"], panel.get("precision", 3))
    if t == "region_map":
        return _vs_region_map(title, panel["field"])
    if t == "metric":
        vs = _vs_metric(title, panel["agg"], panel.get("field"), panel.get("label"))
        if panel["agg"] == "percentiles":
            vs["aggs"][0]["params"]["percents"] = list(panel.get("percents") or [50, 95, 99])
        return vs
    if t == "pie":
        return _vs_pie(title, panel["field"], panel.get("size", 10))
    if t == "bar":
        return _vs_bar(title, panel["field"], panel.get("horizontal", False), panel.get("size", 10),
                       panel.get("metric", "count"), panel.get("agg_field"))
    if t == "histogram" or (t in ("area", "line") and (panel.get("metricas") or panel.get("filtros")
                                                        or panel.get("percents"))):
        return _vs_serie(panel)
    if t in ("area", "line"):
        return _vs_timeseries(title, t, panel.get("metric", "count"),
                              panel.get("field"), panel.get("split"), panel.get("size", 5))
    if t == "table":
        if panel.get("metricas") or panel.get("sub_field"):
            return _vs_tabla(panel)
        return _vs_table(title, panel["field"], panel.get("size", 10),
                         panel.get("metric", "count"), panel.get("agg_field"))
    if t == "heatmap":
        return _vs_mapa_de_calor(title, panel["field"], panel.get("size", 12))
    if t == "tagcloud":
        return _vs_tagcloud(title, panel["field"], panel.get("size", 15))
    raise ValueError(f"tipo de panel desconocido: {t!r}")


# ── Saved objects (rich) ─────────────────────────────────────────────────────

# Tipo del índice (es) → (tipo OSD, esTypes) para el `fields` del index-pattern.
_OSD_FIELD_TYPE: dict[str, tuple[str, list[str]]] = {
    "keyword": ("string", ["keyword"]),
    "text": ("string", ["text"]),
    "ip": ("ip", ["ip"]),
    "long": ("number", ["long"]),
    "integer": ("number", ["integer"]),
    "double": ("number", ["double"]),
    "float": ("number", ["float"]),
    "date": ("date", ["date"]),
    "boolean": ("boolean", ["boolean"]),
    "geo_point": ("geo_point", ["geo_point"]),
}


def _index_pattern_fields_json(index_fields: list[tuple[str, str]]) -> str:
    """Construye el `fields` (cache de field-caps) del index-pattern.

    Sin esto, las visualizaciones no localizan los campos ("Could not locate
    index-pattern-field") cuando el saved object se escribe directo en `.kibana`
    (back-door), porque OpenSearch Dashboards no corre el field-caps automático.
    """
    out = []
    for name, es in index_fields:
        osd_type, es_types = _OSD_FIELD_TYPE.get(es, ("string", [es]))
        aggregatable = es != "text"
        out.append({
            "name": name, "type": osd_type, "esTypes": es_types,
            "count": 0, "scripted": False, "searchable": True,
            "aggregatable": aggregatable, "readFromDocValues": aggregatable,
        })
    return json.dumps(out)


def _index_pattern_obj(slug: str, index_fields: "list[tuple[str, str]] | None" = None,
                       ip_id: "str | None" = None) -> dict[str, Any]:
    """index-pattern con id == title == el pattern (default `<slug>-*`) y `fields`
    poblado con los campos del tipo (para que las viz los localicen tras la
    back-door). `ip_id` permite un pattern arbitrario (custom con cualquier índice)."""
    pat = ip_id or f"{slug}-*"
    return {
        "attributes": {
            "title": pat, "timeFieldName": "@timestamp",
            "fields": _index_pattern_fields_json(index_fields or []),
        },
        "id": pat, "type": "index-pattern", "references": [],
    }


def _viz_obj(slug: str, vis_id: str, title: str, vis_state: dict[str, Any],
             ip_id: str, has_index: bool, query: str = "") -> dict[str, Any]:
    if has_index:
        ssj = json.dumps({"query": {"query": query, "language": "kuery"}, "filter": [],
                          "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index"})
        refs = [{"name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                 "type": "index-pattern", "id": ip_id}]
    else:
        ssj = json.dumps({"query": {"query": query, "language": "kuery"}, "filter": []})
        refs = []
    return {
        "attributes": {
            "title": f"[{slug}] {title}", "visState": json.dumps(vis_state),
            "uiStateJSON": "{}", "description": "", "version": 1,
            "kibanaSavedObjectMeta": {"searchSourceJSON": ssj},
        },
        "id": vis_id, "type": "visualization", "references": refs,
    }


def _controls_viz_obj(slug: str, vis_id: str, title: str, vis_state: dict[str, Any],
                      ip_id: str, n_controls: int) -> dict[str, Any]:
    """Saved object del `input_control_vis`: como `_viz_obj` pero las refs al
    index-pattern van por `control_<i>_index_pattern` (no por el searchSource),
    una por control — es como OpenSearch Dashboards serializa los controles."""
    refs = [{"name": f"control_{i}_index_pattern", "type": "index-pattern", "id": ip_id}
            for i in range(n_controls)]
    return {
        "attributes": {
            "title": f"[{slug}] {title}", "visState": json.dumps(vis_state),
            "uiStateJSON": "{}", "description": "", "version": 1,
            "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(
                {"query": {"query": "", "language": "kuery"}, "filter": []})},
        },
        "id": vis_id, "type": "visualization", "references": refs,
    }


# Tamaño (w,h) por defecto de cada tipo de panel para el auto-layout (grilla 48
# columnas). El header y los controles ocupan la fila entera; las métricas van de
# a 4 por fila; los breakdowns a media/cuarto de ancho.
_DEFAULT_WH: dict[str, tuple[int, int]] = {
    "markdown": (48, 4), "controls": (48, 6), "metric": (12, 8),
    "pie": (16, 15), "bar": (24, 15), "table": (24, 15),
    "area": (24, 14), "line": (24, 14), "tagcloud": (16, 15), "map": (24, 18), "region_map": (24, 18),
}


def _flow_grid(panels: list[dict[str, Any]]) -> list[list[int]]:
    """Auto-layout: empaqueta los paneles por filas en la grilla de 48 columnas,
    conservando el `w`/`h` de cada uno (del `grid` explícito si lo trae, si no el
    default por tipo). Así curar = editar la lista; no quedan huecos."""
    grids: list[list[int]] = []
    x = y = row_h = 0
    for p in panels:
        dw, dh = _DEFAULT_WH.get(p["type"], (24, 15))
        grid = p.get("grid")
        if grid and len(grid) == 4:
            w, h = grid[2], grid[3]
        else:
            w, h = p.get("w", dw), p.get("h", dh)
        if x + w > 48:
            x, y, row_h = 0, y + row_h, 0
        grids.append([x, y, w, h])
        x += w
        row_h = max(row_h, h)
    return grids


def _dashboard_obj(slug: str, title: str, dash_id: str,
                   panels_meta: list[tuple], restaurar_tiempo: bool = True) -> dict[str, Any]:
    """`panels_meta`: `(vis_id, grid)` o `(tipo, id, grid)` (una búsqueda
    guardada va como `("search", id, grid)`). Sin `restaurar_tiempo` el
    dashboard abre con el rango del momento (el de los datos, que la
    plataforma deja de default): uno de los de al lado, al que se llega desde el
    principal, no tiene su propio rango que quede viejo."""
    panels = []
    refs = []
    for i, meta in enumerate(panels_meta):
        tipo, obj_id, grid = meta if len(meta) == 3 else ("visualization", *meta)
        x, y, w, h = grid
        pname = f"panel_{i}"
        panels.append({
            "version": "7.10.0", "gridData": {"x": x, "y": y, "w": w, "h": h, "i": str(i)},
            "panelIndex": str(i), "embeddableConfig": {}, "panelRefName": pname,
        })
        refs.append({"name": pname, "type": tipo, "id": obj_id})
    tiempo: dict[str, Any] = ({"timeRestore": True, "timeFrom": "2025-07-01T00:00:00.000Z",
                               "timeTo": "2026-07-02T00:00:00.000Z", "refreshInterval": {"pause": True, "value": 0}}
                              if restaurar_tiempo else {"timeRestore": False})
    return {
        "attributes": {
            "title": f"[{slug}] {title}", "hits": 0, "description": "",
            "panelsJSON": json.dumps(panels),
            "optionsJSON": json.dumps({"useMargins": True, "hidePanelTitles": False}),
            "version": 1, **tiempo,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {"query": {"query": "", "language": "kuery"}, "filter": []}
                )
            },
        },
        "id": dash_id, "type": "dashboard", "references": refs,
    }


def _build_rich_ndjson(slug: str, spec: dict[str, Any]) -> str:
    lines: list[str] = []
    ip_id = spec.get("ip_id") or f"{slug}-*"
    lines.append(json.dumps(_index_pattern_obj(slug, spec.get("index_fields"), ip_id)))

    grids = _flow_grid(spec["panels"])
    panels_meta: list[tuple[str, list[int]]] = []
    for panel, grid in zip(spec["panels"], grids):
        vis_id = _stable_id(slug, "vis", panel["title"])
        vis_state = _panel_to_vis_state(panel)
        if panel["type"] == "controls":
            n = len(_normalize_controls(panel["controls"]))
            lines.append(json.dumps(_controls_viz_obj(slug, vis_id, panel["title"],
                                                      vis_state, ip_id, n)))
        else:
            has_index = panel["type"] != "markdown"
            lines.append(json.dumps(_viz_obj(slug, vis_id, panel["title"], vis_state, ip_id,
                                             has_index, panel.get("query", ""))))
        panels_meta.append((vis_id, grid))

    dash_id = _stable_id(slug, "dashboard", spec["title"])
    lines.append(json.dumps(_dashboard_obj(slug, spec["title"], dash_id, panels_meta)))
    return "\n".join(lines)


# ── Specs ────────────────────────────────────────────────────────────────────
# Layout: grilla de 48 columnas. Fila de métricas arriba (h=8), serie temporal
# ancha, breakdowns abajo. Modelado en los sample dashboards de OpenSearch.


# Specs de dashboard que NO tienen un vertical en `verticals/`. Hoy solo
# `firewall`: el slug no esta en el registro, asi que ninguna card del grid lo
# puede seleccionar y el producto nunca importa este dashboard. Se conserva
# porque es un spec rich completo y varios tests lo usan como fixture canonica;
# vivia en un dict llamado `_LEGACY_DASHBOARD_SPECS`, nombre que enganaba: el
# formato legacy ("visualizations") era otra cosa, y ese si estaba muerto.
_SPECS_SIN_VERTICAL: dict[str, dict[str, Any]] = {
    "firewall": {
        "title": "Eventos de Firewall",
        "index_fields": [
            ("@timestamp", "date"),
            ("type", "keyword"), ("subtype", "keyword"), ("level", "keyword"),
            ("source.ip", "ip"), ("destination.ip", "ip"),
            ("source.port", "long"), ("destination.port", "long"),
            ("event.action", "keyword"),
            ("network.protocol", "keyword"), ("network.application", "keyword"),
            ("network.iana_number", "long"),
            ("source.geo.country_name", "keyword"), ("destination.geo.country_name", "keyword"),
            ("source.bytes", "long"), ("destination.bytes", "long"),
        ],
        "panels": [
            {"type": "markdown", "title": "Header", "grid": [0, 0, 48, 4],
             "md": "## Firewall Events (FortiGate) — seguridad y tráfico\nEventos permitidos/denegados, severidad, IPs y puertos bajo ataque, geo-IP y ancho de banda."},
            # Métricas (h=8)
            {"type": "metric", "title": "Total de Eventos", "agg": "count", "grid": [0, 4, 12, 8]},
            {"type": "metric", "title": "Eventos Bloqueados", "agg": "count", "label": "Eventos Bloqueados",
             "query": "event.action:(blocked or dropped)", "grid": [12, 4, 12, 8]},
            {"type": "metric", "title": "IPs de Origen Únicas", "agg": "cardinality", "field": "source.ip",
             "label": "IPs de Origen Únicas", "grid": [24, 4, 12, 8]},
            {"type": "metric", "title": "Bytes Enviados", "agg": "sum", "field": "source.bytes",
             "label": "Bytes Enviados", "grid": [36, 4, 12, 8]},
            # Serie temporal
            {"type": "area", "title": "Eventos en el tiempo por acción", "metric": "count",
             "split": "event.action", "grid": [0, 12, 48, 12]},
            # Amenazas (y=24, h=15)
            {"type": "pie", "title": "Permitir vs Denegar", "field": "event.action", "grid": [0, 24, 12, 15]},
            {"type": "pie", "title": "Severidad", "field": "level", "grid": [12, 24, 12, 15]},
            {"type": "table", "title": "Top IPs de Origen Bloqueadas", "field": "source.ip",
             "query": "event.action:(blocked or dropped)", "grid": [24, 24, 12, 15]},
            {"type": "bar", "title": "Top Puertos de Destino Bloqueados", "field": "destination.port",
             "horizontal": True, "query": "event.action:(blocked or dropped)", "grid": [36, 24, 12, 15]},
            # Tráfico (y=39, h=15)
            {"type": "bar", "title": "Top Países de Origen", "field": "source.geo.country_name",
             "horizontal": True, "grid": [0, 39, 16, 15]},
            {"type": "bar", "title": "Top Aplicaciones", "field": "network.application",
             "horizontal": True, "grid": [16, 39, 16, 15]},
            {"type": "table", "title": "Top IPs de Origen", "field": "source.ip", "grid": [32, 39, 16, 15]},
            # Ancho de banda en el tiempo
            {"type": "line", "title": "Bytes enviados en el tiempo", "metric": "sum",
             "field": "source.bytes", "grid": [0, 54, 48, 12]},
        ],
    },
}


_DASHBOARD_SPECS: dict[str, dict[str, Any]] = {
    **_SPECS_SIN_VERTICAL,
    **_verticals.dashboard_specs(),
}


# ── API pública ──────────────────────────────────────────────────────────────

def build_ndjson(slug: str) -> str:
    """Genera el NDJSON baseline para el caso dado (1 index-pattern + N viz + 1
    dashboard, una línea por saved object).

    Hubo un segundo formato de spec (`"visualizations"`) con su propio builder.
    Ningún spec lo usaba desde hace tiempo —los 15 declaran `"panels"`— así que
    la rama era inalcanzable y se fue junto con su cadena de tres funciones."""
    spec = _DASHBOARD_SPECS.get(slug)
    if not spec:
        raise ValueError(f"No hay spec para el slug '{slug}'. Slugs válidos: {list(_DASHBOARD_SPECS)}")
    return _build_rich_ndjson(slug, spec)


def get_available_slugs() -> list[str]:
    """Retorna la lista de slugs con spec definida."""
    return list(_DASHBOARD_SPECS)


def get_dashboard_spec(slug: str) -> dict[str, Any] | None:
    """Retorna la spec para el slug dado, o None si no existe."""
    return _DASHBOARD_SPECS.get(slug)


# ── Generador genérico desde campos (custom / "Tu log específico") ───────────
# Para logs sin spec hecho a mano (custom), armamos un dashboard automático a
# partir de los campos detectados — misma visibilidad que los predefinidos.

# Tipo del wizard → tipo es (lo que produce el index template: integer→long, etc.).
_WIZ_TO_ES: dict[str, str] = {
    "integer": "long", "long": "long", "float": "double", "double": "double",
    "ip": "ip", "date": "date", "boolean": "boolean", "geo_point": "geo_point",
    "text": "text", "keyword": "keyword",
}


def _field_path(f: dict[str, Any]) -> str:
    return (f.get("field_path") or f.get("ecs_path") or f.get("raw_name") or "").strip()


# Unidades que se PROMEDIAN, no se suman. "Las latencias suman 40 segundos" no
# es una frase que signifique algo; "el promedio es 87 ms" sí.
_UNIDADES_DE_PROMEDIO = frozenset({
    "ms", "milisegundos", "s", "sec", "seg", "segundos", "min", "minutos",
    "h", "hs", "horas", "%", "pct", "porcentaje", "rate", "ratio",
    # Velocidades y temperaturas: 142 PPM y 118 PPM no "suman 260".
    "ppm", "rpm", "fps", "mbps", "km/h", "kmh", "°c", "°f",
})


# ── El dashboard automático: un Resumen y, si los campos lo justifican,
#    Problemas y Tiempos ─────────────────────────────────────────────────────
# Era un tablero de rankings ("Top X" de cada columna). Ahora sigue el modelo de
# los que se armaron a mano para un OMS: qué pasa en el tiempo, qué falla y a
# quién, cuánto tarda, con filtros arriba y los tableros enlazados entre sí.
# Todo sale de los roles de los campos (dimensión principal, entidad, crítico,
# éxito, medidas) y de lo que delatan sus nombres (el nivel del log, las
# duraciones): sirve para cualquier dataset, no para uno.

# El nivel de un log (log.level, severity) y el componente que lo escribió.
_NIVEL_DE_LOG = re.compile(r"(^|[._])(level|loglevel|log_level|severity|severidad|nivel|priority|prioridad)$", re.I)
_COMPONENTE = re.compile(r"(^|[._])(logger|component|componente|service|servicio|module|modulo|app|application|"
                         r"aplicacion)$", re.I)
# Lo informativo de un nivel: lo demás (WARN, ERROR o una severidad propia,
# como "Alta") es un problema, aunque no se conozcan los valores del índice.
_NIVELES_INFORMATIVOS = ("INFO", "Info", "info", "DEBUG", "Debug", "debug", "TRACE", "Trace", "trace",
                         "NOTICE", "Notice", "notice")
_UNIDADES_DE_TIEMPO = frozenset({"ms", "milisegundos", "s", "sec", "seg", "segundos", "min", "minutos",
                                 "h", "hs", "horas", "dias", "días", "days"})


def _clasificar(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Los campos del dataset por lo que cuentan. Cada uno como
    `{path, es, agg, label, dim, role, unit, raw, entity}` (`agg`: por dónde
    se agrupa; un texto, por su `.keyword`)."""
    import perfiles

    index_fields: list[tuple[str, str]] = [("@timestamp", "date")]
    campos: list[dict[str, Any]] = []
    hay_fecha = False
    for f in fields or []:
        path = _field_path(f)
        if not path:
            continue
        wiz_type = (f.get("type") or "").strip()
        es = _WIZ_TO_ES.get(wiz_type, "keyword")
        role = (f.get("role") or "").strip() or None
        # Que el log traiga SU fecha es lo que decide si una serie temporal
        # significa algo (si no, `@timestamp` es la hora de ingesta y el gráfico
        # es un pico en el momento del deploy). Vale también `role: timestamp`:
        # el prompt le pide al modelo que mande la fecha a `@timestamp` y borre
        # el campo original, y cuando lo hacía el dashboard perdía las series
        # aunque el índice tuviera una serie temporal perfecta. Y un campo que ya
        # se llama `@timestamp` es esa fecha: se salteaba antes de mirarlo, y un
        # dataset sin otra fecha se quedaba sin series.
        if es == "date" or role == "timestamp":
            hay_fecha = True
        if path == "@timestamp":
            continue
        index_fields.append((path, es))
        agg = path
        if es == "text":
            agg = f"{path}.keyword"
            index_fields.append((agg, "keyword"))
        dim = f.get("dimension")
        if not isinstance(dim, bool):
            try:
                from maas_integrator import is_dimension
                dim = is_dimension(path, wiz_type)
            except Exception:  # noqa: BLE001
                dim = es in ("keyword", "text", "boolean")
        unit = f.get("unit") if isinstance(f.get("unit"), str) else None
        campos.append({"path": path, "es": es, "agg": agg, "label": (f.get("business_label") or path),
                       "dim": bool(dim), "role": role, "unit": unit, "raw": f.get("raw_name") or "",
                       "entity": bool(f.get("entity")),
                       # Sin valores frecuentes puede ser un id que no se repite (cada
                       # fila trae el suyo): el perfilador los guarda solo de las
                       # dimensiones, y un campo de muchos valores no los tiene.
                       "agrupa": not ("frecuentes" in f and not f.get("frecuentes"))})

    def con_rol(rol: str):
        return next((c for c in campos if c["role"] == rol), None)

    texto = ("keyword", "text")
    # La entidad: la que marcó el usuario en el paso 2, que es la del perfil por
    # entidad (un afiliado o una cuenta se repiten aunque, por tener miles de
    # valores, no traigan frecuentes: se tomaba el sexo del paciente). Sin
    # marca, una con rol de entidad que agrupe ("Cliente" y no el id de cada
    # trabajo: una tabla de ids de una fila cada uno no dice nada).
    candidatas = [c for c in campos if c["es"] in texto + ("ip",) and (c["entity"] or c["role"] == "entity_id")]
    candidatas.sort(key=lambda c: not c["entity"])
    entidad = next((c for c in candidatas if c["entity"] or c["agrupa"]), None) or (
        candidatas[0] if candidatas else None)
    ips = [c for c in campos if c["es"] == "ip"]
    if entidad is None and ips:
        entidad = ips[0]          # sin entidad, la IP es "quién"
    critico = con_rol("critical_indicator")
    exito = con_rol("success_indicator")
    nivel = next((c for c in campos if c["es"] in texto and _NIVEL_DE_LOG.search(c["path"])), None)
    usados = {id(x) for x in (entidad, critico, exito, nivel) if x}
    # La dimensión principal del análisis va antes que el componente: un
    # "tipo_servicio" termina como un servicio de log y la dejaba afuera.
    principal = con_rol("primary_dimension")
    if principal is not None and id(principal) in usados:
        principal = None
    if principal:
        usados.add(id(principal))
    componente = next((c for c in campos if c["es"] in texto and id(c) not in usados
                       and _COMPONENTE.search(c["path"])), None)
    if componente:
        usados.add(id(componente))
    categorias = [c for c in campos if c["es"] in texto and c["dim"] and id(c) not in usados]
    if principal is None:
        principal = categorias[0] if categorias else None
    otras = [c for c in categorias if c is not principal]

    # Los números: los que marcó el análisis o, sin marca, los que no son
    # identificadores, coordenadas ni códigos. Cada uno con su agregación: un
    # monto se suma; una duración, un precio o una tasa no ("las latencias
    # suman 40 segundos" no significa nada).
    numeros = [c for c in campos if c["es"] in ("long", "double") and c is not entidad and c is not critico]
    medidas = [c for c in numeros if c["role"] == "measure"] or \
              [c for c in numeros if not (perfiles._NO_ES_MEDIDA.search(c["path"])
                                          or perfiles._NO_ES_MEDIDA.search(c["raw"]))]

    def es_duracion(c):
        return (c["unit"] or "").strip().lower() in _UNIDADES_DE_TIEMPO or any(
            perfiles._ES_DURACION.search(x) for x in (c["path"], c["raw"]) if x)

    def es_promedio(c):
        return (c["unit"] or "").strip().lower() in _UNIDADES_DE_PROMEDIO or any(
            perfiles._ES_PRECIO_O_TASA.search(x) for x in (c["path"], c["raw"]) if x)

    duraciones = [c for c in medidas if es_duracion(c)]
    promedios = [c for c in medidas if c not in duraciones and es_promedio(c)]
    montos = [c for c in medidas if c not in duraciones and c not in promedios]
    paises = [c for c in campos if c["es"] == "keyword" and _PISTA_DE_PAIS.search(f"{c['path']} {c['label']}")
              and not _CODIGO_DE_PAIS.search(f"{c['path']} {c['label']}")]
    return {
        "index_fields": index_fields, "hay_fecha": hay_fecha, "entidad": entidad, "critico": critico,
        "exito": exito, "nivel": nivel, "componente": componente, "principal": principal, "otras": otras,
        "booleanos": [c for c in campos if c["es"] == "boolean" and c["dim"]],
        "montos": montos, "duraciones": duraciones, "promedios": promedios,
        "geo": [c for c in campos if c["es"] == "geo_point"], "paises": paises,
        "textos_libres": [c for c in campos if c["es"] == "text" and not c["dim"]],
    }


def _consulta_critica(c: dict[str, Any]) -> str:
    """Los eventos críticos: un indicador 0/1 vale 1; un código o un motivo
    solo aparece cuando algo salió mal."""
    return f"{c['path']} > 0" if c["es"] in ("long", "double") else f"{c['path']}:*"


def _consulta_de_problemas_del_nivel(nivel: dict[str, Any]) -> str:
    informativos = " or ".join(f'"{v}"' for v in _NIVELES_INFORMATIVOS)
    return f"{nivel['path']}:* and not {nivel['path']}:({informativos})"


def _en_fila(paneles: list[dict[str, Any]], y: int, alto: int) -> int:
    """Reparte los paneles en una fila de 48 columnas (cada uno con su `w`, o
    partes iguales de lo que queda). Devuelve la `y` de la fila siguiente."""
    if not paneles:
        return y
    libres = [p for p in paneles if "w" not in p]
    resto = 48 - sum(p["w"] for p in paneles if "w" in p)
    x = 0
    for p in paneles:
        w = p.pop("w", None) or max(1, resto // max(1, len(libres)))
        p["grid"] = [x, y, w, alto]
        x += w
    return y + alto


def _tableros_desde_campos(slug: str, index_name: str, fields: list[dict[str, Any]],
                           nombre: str = "") -> list[dict[str, Any]]:
    """Los dashboards del dataset: el Resumen siempre y, si los campos lo
    justifican, Problemas (un crítico o el nivel del log) y Tiempos (alguna
    duración). El Resumen va primero (es el que abre la plataforma) y conserva
    el id del dashboard de antes: un dashboard guardado que lo reemplaza sigue
    reemplazándolo. `nombre`: el del caso ("FIX OMS"); sin él, sale del slug."""
    from index_template import index_pattern_from_name

    k = _clasificar(fields)
    entidad, critico, exito, nivel = k["entidad"], k["critico"], k["exito"], k["nivel"]
    componente, principal, hay_fecha = k["componente"], k["principal"], k["hay_fecha"]
    monto = k["montos"][0] if k["montos"] else None
    promedio = k["promedios"][0] if k["promedios"] else None
    duracion = k["duraciones"][0] if k["duraciones"] else None
    q_critico = _consulta_critica(critico) if critico else ""
    q_nivel = _consulta_de_problemas_del_nivel(nivel) if nivel else ""
    critico_texto = bool(critico and critico["es"] in ("keyword", "text"))

    # Los ids salen del slug, como siempre: el título con el nombre del caso
    # ("FIX OMS", no "Fix Oms") no cambia qué dashboard reemplaza uno guardado.
    del_slug = slug.replace("-", " ").replace("_", " ").title()
    titulo = (nombre or "").strip() or del_slug
    claves = [("resumen", "Resumen")]
    if critico or nivel:
        claves.append(("problemas", "Problemas"))
    if k["duraciones"]:
        claves.append(("tiempos", "Tiempos"))
    ids = {"resumen": _stable_id(slug, "dashboard", del_slug)}
    ids.update({c: _stable_id(slug, "dashboard", f"{del_slug}:{c}") for c, _ in claves[1:]})

    def encabezado(clave: str, bajada: str) -> dict[str, Any]:
        nav = " · ".join(f"**{n}**" if c == clave else f"[{n}](#/view/{ids[c]})" for c, n in claves)
        return {"type": "markdown", "title": "Encabezado",
                "md": f"### {titulo} · {dict(claves)[clave]}\n" + (f"{nav}\n\n" if len(claves) > 1 else "\n") + bajada}

    def filtros(*cs) -> list[dict[str, Any]]:
        controles = [{"field": c["agg"], "label": c["label"]} for c in cs if c and c["es"] != "ip"][:3]
        return [{"type": "controls", "title": "Filtros", "controls": controles}] if controles else []

    def metrica(nombre: str, agg: str, campo: "dict | None" = None, query: str = "", **kw) -> dict[str, Any]:
        p = {"type": "metric", "title": nombre, "agg": agg, "label": nombre, "query": query, **kw}
        if campo:
            p["field"] = campo["agg"] if agg == "cardinality" else campo["path"]
        return p

    def fila_de_metricas(metricas: list[dict[str, Any]], y: int) -> int:
        metricas = metricas[:6]
        for m in metricas:
            m["w"] = 48 // len(metricas)
        return _en_fila(metricas, y, 6)

    def columnas_de(*cs) -> list[tuple]:
        """Las columnas de una tabla: eventos y, si hay, el monto (sumado) y un
        tiempo o un precio (promediado)."""
        cols: list[tuple] = [("count", None, "Eventos")]
        for c in cs:
            if c and c is monto:
                cols.append(("sum", c["path"], c["label"]))
            elif c:
                cols.append(("avg", c["path"], f"{c['label']} (promedio)"))
        return cols

    tableros: list[dict[str, Any]] = []

    # ═══ Resumen ═══
    partes = ["cuánto entra"] + (["qué falla"] if (critico or nivel) else []) + \
             (["cuánto tarda"] if duracion else []) + ["quién o qué pesa más"]
    cab = encabezado("resumen", "Panorama del dataset: " + ", ".join(partes[:-1]) + " y " + partes[-1] + ".")
    y = _en_fila([cab], 0, 6)
    fil = filtros(entidad, principal, (k["otras"] or [componente])[0])
    y = _en_fila(fil, y, 5)
    kpis = [metrica("Eventos", "count")]
    if critico:
        kpis.append(metrica(f"Con {critico['label']}", "count", query=q_critico))
    if nivel:
        kpis.append(metrica("Errores y advertencias", "count", query=q_nivel))
    if entidad:
        kpis.append(metrica(f"{entidad['label']} (distintos)", "cardinality", entidad))
    if monto:
        kpis.append(metrica(f"{monto['label']} total", "sum", monto))
    elif promedio:
        kpis.append(metrica(f"{promedio['label']} promedio", "avg", promedio))
    if duracion:
        kpis.append(metrica(f"{duracion['label']} promedio", "avg", duracion))
    y = fila_de_metricas(kpis, y)
    resumen = [cab] + fil + kpis[:6]
    # Lo que pasa en el tiempo: partido por el éxito (éxitos contra fallos, lo
    # primero que mira cualquiera que opera esto) y por la dimensión principal.
    if hay_fecha:
        series = []
        if exito:
            series.append({"type": "area", "title": f"Eventos en el tiempo por {exito['label']}", "metric": "count",
                           "split": exito["agg"]})
        if principal and principal is not exito:
            series.append({"type": "area", "title": f"Eventos en el tiempo por {principal['label']}",
                           "metric": "count", "split": principal["agg"]})
        if not series:
            series.append({"type": "area", "title": "Eventos en el tiempo", "metric": "count"})
        y = _en_fila(series, y, 13)
        resumen += series
    # La medida: dónde se concentra y cómo se mueve.
    medida = []
    if monto and principal:
        medida.append({"type": "bar", "title": f"{monto['label']} por {principal['label']}",
                       "field": principal["agg"], "horizontal": True, "size": 12,
                       "metric": "sum", "agg_field": monto["path"]})
    en_el_tiempo = monto or promedio or duracion
    if en_el_tiempo and hay_fecha:
        medida.append({"type": "line", "title": f"{en_el_tiempo['label']} en el tiempo",
                       "metric": "sum" if en_el_tiempo is monto else "avg", "field": en_el_tiempo["path"]})
    y = _en_fila(medida, y, 14)
    resumen += medida
    # Las tablas: por la dimensión principal y por la entidad, con los eventos,
    # el monto y el tiempo de cada una.
    tablas = []
    if principal:
        tablas.append({"type": "table", "title": f"Por {principal['label']}", "field": principal["agg"], "size": 15,
                       "metricas": columnas_de(monto, duracion or promedio), "label_filas": principal["label"]})
    if entidad:
        tablas.append({"type": "table", "title": f"Por {entidad['label']}", "field": entidad["agg"], "size": 20,
                       "metricas": columnas_de(monto, duracion or promedio), "label_filas": entidad["label"]})
    y = _en_fila(tablas, y, 16)
    resumen += tablas
    # Hasta dos categorías más y los booleanos (dos valores son una torta, no
    # un ranking). El resto queda en los filtros y en el asistente.
    resto = [{"type": "bar", "title": f"Eventos por {c['label']}", "field": c["agg"], "horizontal": True, "size": 10}
             for c in k["otras"][:2]]
    resto += [{"type": "pie", "title": c["label"], "field": c["agg"], "size": 5} for c in k["booleanos"][:2]]
    for i in range(0, len(resto), 2):
        y = _en_fila(resto[i:i + 2], y, 13)
    resumen += resto
    # El mapa: un log con coordenadas se entiende acá y en ningún otro lado. Sin
    # coordenadas pero con el NOMBRE del país, mapa por país (un código no une).
    mapas = [{"type": "map", "title": f"Mapa · {c['label']}", "field": c["path"]} for c in k["geo"][:1]] or \
            [{"type": "region_map", "title": f"Por país · {c['label']}", "field": c["path"]} for c in k["paises"][:1]]
    for m in mapas:
        y = _en_fila([m], y, 18)
    resumen += mapas
    tableros.append({"clave": "resumen", "title": titulo, "titulo": f"{titulo} · Resumen", "id": ids["resumen"],
                     "ip_id": index_pattern_from_name(index_name), "index_fields": k["index_fields"],
                     "panels": resumen})

    # ═══ Problemas: qué falló, cuándo, dónde y a quién ═══
    if critico or nivel:
        cab = encabezado("problemas", "Qué falló, cuándo, dónde y a quién. Para ver uno solo, elegilo en los "
                                      "filtros.")
        y = _en_fila([cab], 0, 6)
        fil = filtros(entidad, principal, componente)
        y = _en_fila(fil, y, 5)
        kpis = []
        if critico:
            kpis.append(metrica(f"Con {critico['label']}", "count", query=q_critico))
        if nivel:
            kpis.append(metrica("Errores y advertencias", "count", query=q_nivel))
        if critico and entidad:
            kpis.append(metrica(f"{entidad['label']} con {critico['label']} (distintos)", "cardinality", entidad, q_critico))
        if critico and principal:
            kpis.append(metrica(f"{principal['label']} con {critico['label']} (distintos)", "cardinality", principal,
                                    q_critico))
        y = fila_de_metricas(kpis, y)
        problemas = [cab] + fil + kpis
        if hay_fecha:
            series = []
            if critico_texto:
                series.append({"type": "histogram", "title": f"{critico['label']} en el tiempo", "metric": "count",
                               "split": critico["agg"], "size": 8, "query": q_critico})
            elif critico:
                series.append({"type": "histogram", "title": f"Con {critico['label']} en el tiempo"
                               + (f" por {principal['label']}" if principal else ""), "metric": "count",
                               "query": q_critico, **({"split": principal["agg"]} if principal else {})})
            if nivel:
                por = componente or nivel
                series.append({"type": "histogram", "title": f"Errores y advertencias por {por['label']}",
                               "metric": "count", "split": por["agg"], "size": 8, "query": q_nivel})
            for s in series:
                y = _en_fila([s], y, 13)
            problemas += series
        barras = []
        if critico_texto:
            barras.append({"type": "bar", "title": f"{critico['label']}: los más frecuentes", "field": critico["agg"],
                           "horizontal": True, "size": 15, "query": q_critico})
        if principal and (critico or nivel):
            barras.append({"type": "bar", "title": (f"Con {critico['label']}" if critico else "Errores y advertencias")
                           + f" por {principal['label']}", "field": principal["agg"], "horizontal": True,
                           "size": 12, "query": q_critico or q_nivel})
        y = _en_fila(barras, y, 15)
        problemas += barras
        if entidad and hay_fecha:
            calor = {"type": "heatmap", "title": (f"Con {critico['label']}" if critico else "Errores y advertencias")
                     + f" por {entidad['label']} y hora", "field": entidad["agg"], "size": 12,
                     "query": q_critico or q_nivel}
            y = _en_fila([calor], y, 15)
            problemas.append(calor)
        if entidad and critico_texto:
            tabla = {"type": "table", "title": f"{entidad['label']} y {critico['label']}", "field": entidad["agg"],
                     "size": 15, "sub_field": critico["agg"], "sub_size": 3, "metricas": [("count", None, "Eventos")],
                     "label_filas": entidad["label"], "label_sub": critico["label"], "query": q_critico}
            y = _en_fila([tabla], y, 15)
            problemas.append(tabla)
        columnas = [c["path"] for c in (entidad, principal, critico, nivel, componente) if c]
        columnas += [c["path"] for c in k["textos_libres"][:1] if c["path"] not in columnas]
        lista = {"type": "search", "title": "Eventos con problemas", "columns": columnas[:6],
                 "query": " or ".join(f"({q})" for q in (q_critico, q_nivel) if q), "sort": [["@timestamp", "desc"]]}
        _en_fila([lista], y, 15)
        problemas.append(lista)
        tableros.append({"clave": "problemas", "titulo": f"{titulo} · Problemas", "id": ids["problemas"],
                         "panels": problemas})

    # ═══ Tiempos: cuánto tarda y quién tiene los más lentos ═══
    if k["duraciones"]:
        durs = k["duraciones"][:2]
        cab = encabezado("tiempos", "Cuánto tarda: el valor típico (p50), los altos (p95 y p99) y quién tiene "
                                    "los más lentos.")
        y = _en_fila([cab], 0, 6)
        fil = filtros(entidad, principal)
        y = _en_fila(fil, y, 5)
        kpis = [metrica(f"{d['label']} (p50, p95 y p99)", "percentiles", d, percents=[50, 95, 99]) for d in durs]
        y = fila_de_metricas(kpis, y)
        tiempos = [cab] + fil + kpis
        if hay_fecha:
            lineas = [{"type": "line", "title": f"{d['label']}: p50, p95 y p99", "metric": "percentiles",
                       "field": d["path"], "percents": [50, 95, 99], "label": d["label"]} for d in durs]
            y = _en_fila(lineas, y, 13)
            tiempos += lineas
            if principal:
                p95 = {"type": "line", "title": f"{duracion['label']} p95 por {principal['label']}",
                       "metric": "percentiles", "field": duracion["path"], "percents": [95],
                       "label": duracion["label"], "split": principal["agg"], "size": 6}
                y = _en_fila([p95], y, 13)
                tiempos.append(p95)
        lento = [("max", duracion["path"], f"{duracion['label']} máximo"),
                 ("avg", duracion["path"], f"{duracion['label']} promedio"), ("count", None, "Eventos")]
        tablas = []
        if entidad:
            tablas.append({"type": "table", "title": f"{entidad['label']} con más {duracion['label']}",
                           "field": entidad["agg"], "size": 15, "metricas": lento, "label_filas": entidad["label"]})
        if principal:
            tablas.append({"type": "table", "title": f"{duracion['label']} por {principal['label']}",
                           "field": principal["agg"], "size": 15, "metricas": lento,
                           "label_filas": principal["label"]})
        y = _en_fila(tablas, y, 15)
        tiempos += tablas
        lista = {"type": "search", "title": f"Eventos con más {duracion['label']}", "query": f"{duracion['path']}:*",
                 "columns": [c["path"] for c in (entidad, principal) if c] + [d["path"] for d in durs],
                 "sort": [[duracion["path"], "desc"]]}
        _en_fila([lista], y, 15)
        tiempos.append(lista)
        tableros.append({"clave": "tiempos", "titulo": f"{titulo} · Tiempos", "id": ids["tiempos"],
                         "panels": tiempos})
    return tableros


def _spec_from_fields(slug: str, index_name: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    """El Resumen del dashboard automático (title/ip_id/index_fields/panels):
    ver `_tableros_desde_campos`."""
    return _tableros_desde_campos(slug, index_name, fields)[0]


def _busqueda_obj(slug: str, busq_id: str, panel: dict[str, Any], ip_id: str) -> dict[str, Any]:
    ssj = json.dumps({"query": {"query": panel.get("query", ""), "language": "kuery"}, "filter": [],
                      "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index"})
    return {"id": busq_id, "type": "search",
            "references": [{"name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern",
                            "id": ip_id}],
            "attributes": {"title": f"[{slug}] {panel['title']}", "description": "", "hits": 0,
                           "columns": panel.get("columns") or [], "sort": panel.get("sort") or [["@timestamp", "desc"]],
                           "version": 1, "kibanaSavedObjectMeta": {"searchSourceJSON": ssj}}}


def _build_tableros(slug: str, tableros: list[dict[str, Any]]) -> str:
    """El NDJSON de los dashboards del dataset: un index pattern y, por
    dashboard, sus visualizaciones (y búsquedas) y el dashboard. Los ids salen
    del título de cada panel; los de los dashboards de al lado llevan su clave
    adelante (dos dashboards pueden tener un panel con el mismo nombre)."""
    principal = tableros[0]
    ip_id = principal.get("ip_id") or f"{slug}-*"
    lines = [json.dumps(_index_pattern_obj(slug, principal.get("index_fields"), ip_id))]
    for n, t in enumerate(tableros):
        meta: list[tuple] = []
        for panel, grid in zip(t["panels"], _flow_grid(t["panels"])):
            clave = panel["title"] if n == 0 else f"{t['clave']}:{panel['title']}"
            if panel["type"] == "search":
                sid = _stable_id(slug, "search", clave)
                lines.append(json.dumps(_busqueda_obj(slug, sid, panel, ip_id)))
                meta.append(("search", sid, grid))
                continue
            vis_id = _stable_id(slug, "vis", clave)
            vis_state = _panel_to_vis_state(panel)
            if panel["type"] == "controls":
                lines.append(json.dumps(_controls_viz_obj(slug, vis_id, panel["title"], vis_state, ip_id,
                                                          len(_normalize_controls(panel["controls"])))))
            else:
                lines.append(json.dumps(_viz_obj(slug, vis_id, panel["title"], vis_state, ip_id,
                                                 panel["type"] != "markdown", panel.get("query", ""))))
            meta.append(("visualization", vis_id, grid))
        lines.append(json.dumps(_dashboard_obj(slug, t["titulo"], t["id"], meta, restaurar_tiempo=(n == 0))))
    return "\n".join(lines)


_ROLES_DE_COLUMNA = ("entity_id", "primary_dimension", "critical_indicator", "success_indicator", "measure", "dimension")


def _columnas(fields: list[dict], spec: dict, n: int = 6) -> list[str]:
    """Las columnas de una búsqueda: primero las de rol (entidad, dimensión
    principal, indicadores, medida), después el campo del volumen y el resto."""
    fields = [f for f in fields or [] if isinstance(f, dict) and (f.get("field_path") or "").strip()
              and (f.get("field_path") or "") != "@timestamp" and (f.get("type") or "") != "date"]
    orden = sorted(fields, key=lambda f: _ROLES_DE_COLUMNA.index(f.get("role")) if f.get("role") in _ROLES_DE_COLUMNA else 99)
    fuera: list[str] = []
    volumen = str((spec or {}).get("volume_field") or "").removesuffix(".keyword")
    for ruta in [f["field_path"] for f in orden[:2]] + ([volumen] if volumen else []) + [f["field_path"] for f in orden]:
        if ruta and ruta not in fuera:
            fuera.append(ruta)
    return fuera[:n]


_PRONOSTICO_DE_FALLAS = re.compile(r"critical|fail|error|denied|block|threat|fraud|downtime|attack", re.I)


def consulta_de_criticos(spec: dict) -> str:
    """El KQL de los eventos críticos, del primer pronóstico de fallas del spec
    (`critical_events` en un dataset nuevo; `failed_count`, `denied_events`,
    `fraud_count`… en los curados): un indicador que se suma (`is_fraud`) vale
    1; uno que se cuenta (el campo `<x>_falla` que arma el filter) está solo
    en los que fallaron; un `filter` es su término."""
    for fc in (spec or {}).get("forecasts") or []:
        if not _PRONOSTICO_DE_FALLAS.search(str(fc.get("feature_name") or "")):
            continue
        tipo, cuerpo = next(iter(next(iter((fc.get("aggregation_query") or {}).values()), {}).items()), ("", {}))
        if tipo == "filter":
            clase, cond = next(iter((cuerpo or {}).items()), ("", {}))
            campo, valor = next(iter((cond or {}).items()), ("", None))
            valores = valor if isinstance(valor, list) else [valor]
            if campo and valores and all(isinstance(v, (str, int, float)) for v in valores):
                return f"{campo}:(" + " or ".join(json.dumps(v, ensure_ascii=False) for v in valores) + ")"
            continue
        campo = str((cuerpo or {}).get("field") or "").removesuffix(".keyword")
        if campo and tipo in ("sum", "value_count"):
            return f"{campo} >= 1" if tipo == "sum" else f"{campo}:*"
    return ""


def con_busquedas_guardadas(ndjson: str, slug: str, spec: "dict | None", fields: "list[dict] | None",
                            label: str = "") -> str:
    """Las búsquedas de Discover del caso, junto a su dashboard: todos los
    eventos con las columnas que importan y, si el caso tiene un indicador de
    falla, solo los críticos. Sobre el index pattern del dashboard; no se
    duplican."""
    objetos = []
    for linea in (ndjson or "").splitlines():
        try:
            objetos.append(json.loads(linea))
        except ValueError:
            continue
    patron = next((o for o in objetos if isinstance(o, dict) and o.get("type") == "index-pattern"
                   and not str(o.get("id", "")).startswith("perfil-")), None)
    if patron is None:
        return ndjson
    columnas = _columnas(fields or [], spec or {})
    nombre = label or slug
    nuevas = []
    for titulo, consulta in ((f"{nombre}: todos los eventos", ""),
                             (f"{nombre}: eventos críticos", consulta_de_criticos(spec or {}))):
        if titulo.endswith("críticos") and not consulta:
            continue
        sid = _stable_id(slug, "search", titulo)
        if any(o.get("id") == sid for o in objetos):
            continue
        ssj = json.dumps({"query": {"query": consulta, "language": "kuery"}, "filter": [],
                          "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index"})
        nuevas.append({"id": sid, "type": "search", "references": [
            {"name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern", "id": patron["id"]}],
            "attributes": {"title": f"[{slug}] {titulo}", "description": "", "hits": 0, "columns": columnas,
                           "sort": [["@timestamp", "desc"]], "version": 1,
                           "kibanaSavedObjectMeta": {"searchSourceJSON": ssj}}})
    if not nuevas:
        return ndjson
    return "\n".join(json.dumps(o) for o in objetos + nuevas)


def busquedas_del_ndjson(ndjson: str) -> list[dict]:
    """`[{id, titulo}]` de las búsquedas guardadas de un NDJSON."""
    fuera = []
    for linea in (ndjson or "").splitlines():
        try:
            o = json.loads(linea)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("type") == "search":
            fuera.append({"id": o["id"], "titulo": str((o.get("attributes") or {}).get("title", ""))})
    return fuera


def con_panel_de_perfil(ndjson: str, slug: str, perfil: "dict | None") -> str:
    """El dashboard del caso con una tabla más: su perfil por entidad (el
    índice `perfil-<slug>` que escribe el Transform), una fila por entidad y
    una columna por medida, ordenada por la primera. Sirve sobre cualquier
    NDJSON (también el de disco) y no lo duplica si ya lo tiene.

    El índice no tiene fecha: su index pattern va sin `timeFieldName`, así el
    rango del dashboard no lo vacía. Hasta que el Transform corre, la tabla
    queda vacía (el dashboard se importa antes de "Provisionar plugins")."""
    import perfiles

    if not perfil or not perfil.get("medidas"):
        return ndjson
    objetos = []
    for linea in (ndjson or "").splitlines():
        try:
            objetos.append(json.loads(linea))
        except ValueError:
            continue
    tablero = next((o for o in objetos if isinstance(o, dict) and o.get("type") == "dashboard"), None)
    vis_id = _stable_id(slug, "vis", "perfil por entidad")
    if tablero is None or any(o.get("id") == vis_id for o in objetos):
        return ndjson
    ip_id = perfiles.indice_destino(slug)
    fechas = set(perfil.get("fechas") or [])
    nombres = perfil.get("nombres") or {}
    medidas = list(perfil["medidas"])
    patron = _index_pattern_obj(slug, [("entidad", "keyword")] + [(m, "date" if m in fechas else "double") for m in medidas],
                                ip_id)
    patron["attributes"].pop("timeFieldName", None)
    etiqueta = perfil.get("etiqueta") or perfil.get("campo") or "Entidad"
    aggs = [_metric_agg("max", m, nombres.get(m, m), str(i + 1)) for i, m in enumerate(medidas)]
    filas = _terms_agg(str(len(medidas) + 1), "entidad", "bucket", 15)
    filas["params"]["orderBy"] = str(medidas.index(perfiles.orden_del_perfil(perfil)) + 1)
    filas["params"]["customLabel"] = etiqueta
    titulo = f"Perfil por {etiqueta}"
    tabla = {"title": titulo, "type": "table", "aggs": aggs + [filas],
             "params": {"perPage": 15, "showPartialRows": False, "showMetricsAtAllLevels": False,
                        "showTotal": False, "totalFunc": "sum", "percentageCol": ""}}
    paneles = json.loads(tablero["attributes"].get("panelsJSON") or "[]")
    abajo = max((p["gridData"]["y"] + p["gridData"]["h"] for p in paneles if p.get("gridData")), default=0)
    i = len(paneles)
    paneles.append({"version": "7.10.0", "gridData": {"x": 0, "y": abajo, "w": 48, "h": 15, "i": str(i)},
                    "panelIndex": str(i), "embeddableConfig": {}, "panelRefName": f"panel_{i}"})
    tablero["attributes"]["panelsJSON"] = json.dumps(paneles)
    tablero["references"] = list(tablero.get("references") or []) + [
        {"name": f"panel_{i}", "type": "visualization", "id": vis_id}]
    otros = [o for o in objetos if o is not tablero]
    return "\n".join(json.dumps(o) for o in otros + [patron, _viz_obj(slug, vis_id, titulo, tabla, ip_id, True), tablero])


def id_del_dashboard(ndjson: str) -> str:
    """El id del dashboard de un NDJSON (para el link directo desde la
    plataforma), o "" si no trae uno."""
    for linea in (ndjson or "").splitlines():
        try:
            obj = json.loads(linea)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "dashboard" and obj.get("id"):
            return str(obj["id"])
    return ""


def build_ndjson_from_fields(slug: str, index_name: str, fields: list[dict[str, Any]], nombre: str = "") -> str:
    """Genera el NDJSON de los dashboards AUTO-derivados de los campos detectados
    (custom): el Resumen y, si aplican, Problemas y Tiempos.

    Mismo formato/back-door que los predefinidos (1 index-pattern + viz +
    dashboards; el Resumen primero). El index-pattern matchea el índice real
    (`index_pattern_from_name`) y trae los `fields` poblados; las viz
    referencian el `field_path` real.
    """
    return _build_tableros(slug, _tableros_desde_campos(slug, index_name, fields, nombre))


# ── Regeneración de los .ndjson de disco ─────────────────────────────────────

# Slugs cuyos .ndjson se (re)generan a disco: todos los que tienen spec.
_DISK_REGEN_SLUGS = list(_DASHBOARD_SPECS)


def write_all_ndjson(out_dir: "str | None" = None) -> list[str]:
    """Escribe docs/dashboards/<slug>.ndjson para los slugs en alcance.

    Devuelve la lista de paths escritos. Usado por `python -m dashboards` para
    regenerar los artefactos de disco que el importer (disk-first) y el Starter
    Kit consumen.
    """
    from pathlib import Path

    base = Path(out_dir) if out_dir else Path(__file__).parent / "docs" / "dashboards"
    base.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for slug in _DISK_REGEN_SLUGS:
        path = base / f"{slug}.ndjson"
        path.write_text(build_ndjson(slug) + "\n", encoding="utf-8")
        written.append(str(path))
    return written


if __name__ == "__main__":
    for p in write_all_ndjson():
        print(f"[dashboards] escrito {p}")
