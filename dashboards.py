"""
dashboards.py
=============

Generador de dashboards baseline por caso de uso para OpenSearch Dashboards.

Dos formatos de spec conviven:

- **Rich** (`"panels"`): dashboards profesionales modelados en los *sample data*
  de OpenSearch (eCommerce / Flights / [Logs] Web Traffic), mapeados a los campos
  REALES de cada tipo.
- **Legacy** (`"visualizations"`): el generador simple original.

Los specs de cada vertical salen del registro declarativo `verticals/`
(`_verticals.dashboard_specs()`); acá solo queda `_LEGACY_DASHBOARD_SPECS` con
los que NO son un vertical de demo (hoy `firewall`, naming ECS previo al SIEM).

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
    aggs = [_metric_agg(metric_agg, metric_field, None), _datehist_agg("2")]
    if split_field:
        aggs.append(_terms_agg("3", split_field, "group", size))
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
    if t == "metric":
        return _vs_metric(title, panel["agg"], panel.get("field"), panel.get("label"))
    if t == "pie":
        return _vs_pie(title, panel["field"], panel.get("size", 10))
    if t == "bar":
        return _vs_bar(title, panel["field"], panel.get("horizontal", False), panel.get("size", 10),
                       panel.get("metric", "count"), panel.get("agg_field"))
    if t in ("area", "line"):
        return _vs_timeseries(title, t, panel.get("metric", "count"),
                              panel.get("field"), panel.get("split"), panel.get("size", 5))
    if t == "table":
        return _vs_table(title, panel["field"], panel.get("size", 10),
                         panel.get("metric", "count"), panel.get("agg_field"))
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
    "area": (24, 14), "line": (24, 14), "tagcloud": (16, 15), "map": (24, 18),
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
                   panels_meta: list[tuple[str, list[int]]]) -> dict[str, Any]:
    panels = []
    refs = []
    for i, (vis_id, grid) in enumerate(panels_meta):
        x, y, w, h = grid
        pname = f"panel_{i}"
        panels.append({
            "version": "7.10.0", "gridData": {"x": x, "y": y, "w": w, "h": h, "i": str(i)},
            "panelIndex": str(i), "embeddableConfig": {}, "panelRefName": pname,
        })
        refs.append({"name": pname, "type": "visualization", "id": vis_id})
    return {
        "attributes": {
            "title": f"[{slug}] {title}", "hits": 0, "description": "",
            "panelsJSON": json.dumps(panels),
            "optionsJSON": json.dumps({"useMargins": True, "hidePanelTitles": False}),
            "version": 1, "timeRestore": True, "timeFrom": "2025-07-01T00:00:00.000Z", "timeTo": "2026-07-02T00:00:00.000Z",
            "refreshInterval": {"pause": True, "value": 0},
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

# Legacy: dashboards que NO pertenecen a un vertical de demo (ej. `firewall`,
# naming ECS previo al SIEM). Los de los verticales se leen del registro.
_LEGACY_DASHBOARD_SPECS: dict[str, dict[str, Any]] = {
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
    **_LEGACY_DASHBOARD_SPECS,
    **_verticals.dashboard_specs(),
}


# ── Builder legacy (formato "visualizations") ────────────────────────────────

def _generate_id() -> str:
    return str(uuid.uuid4())


def _legacy_index_pattern(slug: str) -> dict[str, Any]:
    return {
        "type": "index-pattern", "id": f"{slug}-*",
        "attributes": {"title": f"{slug}-*", "timeFieldName": "@timestamp", "fields": json.dumps([])},
        "references": [],
    }


def _legacy_vis_state(vis_type: str, field: str | None) -> dict[str, Any]:
    if vis_type == "histogram":
        return {
            "type": "histogram",
            "aggs": [
                {"id": "1", "enabled": True, "type": "count", "schema": "metric", "params": {}},
                {"id": "2", "enabled": True, "type": "date_histogram", "schema": "segment",
                 "params": {"field": "@timestamp", "interval": "auto", "drop_partials": False,
                            "min_doc_count": 1, "extended_bounds": {}}},
            ],
            "params": {"type": "histogram", "addLegend": True, "addTooltip": True,
                       "legendPosition": "right", "grid": {"categoryLines": False},
                       "categoryAxes": _category_axis(), "valueAxes": _value_axis()},
        }
    if vis_type == "pie":
        return {
            "type": "pie",
            "aggs": [_metric_agg("count", None, None), _terms_agg("2", field or "", "segment", 10)],
            "params": {"type": "pie", "addLegend": True, "addTooltip": True,
                       "legendPosition": "right", "isDonut": True,
                       "labels": {"show": False, "values": True, "last_level": True}},
        }
    if vis_type == "metric":
        return {
            "type": "metric",
            "aggs": [_metric_agg("avg", field, None)],
            "params": {"addLegend": False, "addTooltip": True,
                       "metric": {"percentageMode": False, "useRanges": False,
                                  "colorSchema": "Green to Red", "metricColorMode": "None",
                                  "colorsRange": [{"from": 0, "to": 10000}],
                                  "labels": {"show": True}, "invertColors": False,
                                  "style": {"bgFill": "#000", "fontSize": 60}}},
        }
    return {"type": vis_type, "aggs": [], "params": {}}


def _legacy_build_ndjson(slug: str, spec: dict[str, Any]) -> str:
    lines: list[str] = []
    ip_id = f"{slug}-*"
    lines.append(json.dumps(_legacy_index_pattern(slug)))

    vis_ids: list[str] = []
    for vis_spec in spec["visualizations"]:
        vis_id = _stable_id(slug, "vis", vis_spec["title"])
        vis_ids.append(vis_id)
        vis_state = _legacy_vis_state(vis_spec["type"], vis_spec.get("field"))
        vis_state["title"] = f"{slug}-{vis_spec['title']}"
        lines.append(json.dumps(_viz_obj(slug, vis_id, vis_spec["title"], vis_state, ip_id, True)))

    panels_meta = [(vid, [(i % 2) * 24, (i // 2) * 15, 24, 15]) for i, vid in enumerate(vis_ids)]
    dash_id = _stable_id(slug, "dashboard", spec["title"])
    lines.append(json.dumps(_dashboard_obj(slug, spec["title"], dash_id, panels_meta)))
    return "\n".join(lines)


# ── API pública ──────────────────────────────────────────────────────────────

def build_ndjson(slug: str) -> str:
    """Genera el NDJSON baseline para el caso dado (1 index-pattern + N viz + 1
    dashboard, una línea por saved object). Despacha rich vs legacy según el spec."""
    spec = _DASHBOARD_SPECS.get(slug)
    if not spec:
        raise ValueError(f"No hay spec para el slug '{slug}'. Slugs válidos: {list(_DASHBOARD_SPECS)}")
    if "panels" in spec:
        return _build_rich_ndjson(slug, spec)
    return _legacy_build_ndjson(slug, spec)


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


def _spec_from_fields(slug: str, index_name: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Arma un spec rich (title/ip_id/index_fields/panels) a partir de los campos
    detectados, eligiendo viz por tipo y calculando el layout en la grilla de 48."""
    from index_template import index_pattern_from_name

    index_fields: list[tuple[str, str]] = [("@timestamp", "date")]
    # (path, es, agg_path, label, dimension, role)
    typed: list[tuple[str, str, str, str, bool, "str | None"]] = []
    has_event_date = False
    for f in fields or []:
        path = _field_path(f)
        if not path or path == "@timestamp":
            continue
        wiz_type = (f.get("type") or "").strip()
        es = _WIZ_TO_ES.get(wiz_type, "keyword")
        if es == "date":
            has_event_date = True
        index_fields.append((path, es))
        agg_path = path
        if es == "text":
            agg_path = f"{path}.keyword"
            index_fields.append((agg_path, "keyword"))
        dim = f.get("dimension")
        if not isinstance(dim, bool):
            try:
                from maas_integrator import is_dimension
                dim = is_dimension(path, wiz_type)
            except Exception:  # noqa: BLE001
                dim = es in ("keyword", "text", "boolean")
        role = (f.get("role") or "").strip() or None
        typed.append((path, es, agg_path, (f.get("business_label") or path), bool(dim), role))

    ip_fields = [(p, ap, lbl) for (p, es, ap, lbl, _d, _r) in typed if es == "ip"]
    num_fields = [(p, ap, lbl) for (p, es, ap, lbl, _d, _r) in typed if es in ("long", "double")]
    cat_fields = [(p, ap, lbl) for (p, es, ap, lbl, d, _r) in typed
                  if es in ("keyword", "text", "boolean") and d]

    # Selección por role (fallback a primer campo del tipo).
    primary_cat = next((t for t in typed if t[5] == "primary_dimension"), None)
    primary_cat = (primary_cat[0], primary_cat[2], primary_cat[3]) if primary_cat else (cat_fields[0] if cat_fields else None)
    primary_num = next((t for t in typed if t[5] == "measure"), None)
    primary_num = (primary_num[0], primary_num[2], primary_num[3]) if primary_num else (num_fields[0] if num_fields else None)
    entity_field = next((t for t in typed if t[5] == "entity_id"), None)
    entity_field = (entity_field[0], entity_field[2], entity_field[3]) if entity_field else None

    _seen: set[str] = set()
    def _uniq(base: str, path: str) -> str:  # noqa: E306 — títulos únicos (ids = uuid5(título))
        t = base if base not in _seen else f"{base} ({path})"
        _seen.add(t)
        return t

    title = slug.replace("-", " ").replace("_", " ").title()
    panels: list[dict[str, Any]] = [
        {"type": "markdown", "title": "Header", "grid": [0, 0, 48, 4],
         "md": f"## {title}\nDashboard auto-generado de tu log (campos detectados)."},
    ]

    # Fila de métricas (w12 h8, hasta 4): Total + cardinality de entity_id +
    # cardinality de IP + suma de la medida principal.
    mx = 0
    panels.append({"type": "metric", "title": "Total", "agg": "count", "grid": [mx, 4, 12, 8]})
    mx += 12
    if entity_field:
        p, ap, lbl = entity_field
        panels.append({"type": "metric", "title": _uniq(f"Únicos {lbl}", p), "agg": "cardinality",
                       "field": ap, "label": f"Únicos {lbl}", "grid": [mx, 4, 12, 8]})
        mx += 12
    elif ip_fields:
        p, ap, lbl = ip_fields[0]
        panels.append({"type": "metric", "title": _uniq(f"Únicos {lbl}", p), "agg": "cardinality",
                       "field": ap, "label": f"Únicos {lbl}", "grid": [mx, 4, 12, 8]})
        mx += 12
    if primary_num and mx < 36:
        p, ap, lbl = primary_num
        panels.append({"type": "metric", "title": _uniq(f"Suma {lbl}", p), "agg": "sum",
                       "field": p, "label": f"Suma {lbl}", "grid": [mx, 4, 12, 8]})
        mx += 12

    # Serie temporal SOLO si el log trae su propia fecha.
    y = 12
    if has_event_date:
        panels.append({"type": "area", "title": "Eventos en el tiempo", "metric": "count",
                       "grid": [0, y, 48, 12]})
        y += 12

    # Pie chart de la dimensión primaria (role → primer cat_fields).
    if primary_cat:
        p0, ap0, lbl0 = primary_cat
        panels.append({"type": "pie", "title": _uniq(f"Distribución por {lbl0}", p0),
                       "field": ap0, "grid": [0, y, 24, 12]})
        if len(cat_fields) > 1:
            p1, ap1, lbl1 = cat_fields[1] if cat_fields[0] == primary_cat else cat_fields[0]
            panels.append({"type": "bar", "title": _uniq(f"Top {lbl1}", p1),
                           "field": ap1, "horizontal": True, "grid": [24, y, 24, 12]})
        y += 12
    elif cat_fields:
        p0, ap0, lbl0 = cat_fields[0]
        panels.append({"type": "pie", "title": _uniq(f"Distribución por {lbl0}", p0),
                       "field": ap0, "grid": [0, y, 24, 12]})
        if len(cat_fields) > 1:
            p1, ap1, lbl1 = cat_fields[1]
            panels.append({"type": "bar", "title": _uniq(f"Top {lbl1}", p1),
                           "field": ap1, "horizontal": True, "grid": [24, y, 24, 12]})
        y += 12

    # Breakdowns: categóricos (horizontal_bar top-N) + IPs (table). 2 por fila, w24 h15.
    breakdowns: list[dict[str, Any]] = []
    for (p, ap, lbl) in cat_fields[:8]:
        breakdowns.append({"type": "bar", "title": _uniq(f"Top {lbl}", p), "field": ap, "horizontal": True})
    for (p, ap, lbl) in ip_fields[:2]:
        breakdowns.append({"type": "table", "title": _uniq(f"Top {lbl}", p), "field": ap})
    col = 0
    for b in breakdowns[:10]:
        b["grid"] = [col * 24, y, 24, 15]
        panels.append(b)
        col += 1
        if col == 2:
            col = 0
            y += 15
    if col == 1:
        y += 15

    # Numérico over time: role measure → primer numérico. Solo con fecha real.
    if primary_num and has_event_date:
        p, ap, lbl = primary_num
        panels.append({"type": "line", "title": _uniq(f"{lbl} en el tiempo", p), "metric": "sum",
                       "field": p, "grid": [0, y, 48, 12]})
        y += 12

    # Cross-tab: "Top dimensión por medida" — role primary_dimension × role measure.
    if primary_cat and primary_num:
        p_cat, ap_cat, lbl_cat = primary_cat
        p_num, ap_num, lbl_num = primary_num
        panels.append({"type": "bar", "title": _uniq(f"Top {lbl_cat} por {lbl_num}", p_cat),
                       "field": ap_cat, "horizontal": True, "metric": "sum", "agg_field": p_num,
                       "grid": [0, y, 48, 15]})
        y += 15

    return {
        "title": title,
        "ip_id": index_pattern_from_name(index_name),
        "index_fields": index_fields,
        "panels": panels,
    }


def build_ndjson_from_fields(slug: str, index_name: str, fields: list[dict[str, Any]]) -> str:
    """Genera el NDJSON de un dashboard AUTO-derivado de los campos detectados (custom).

    Mismo formato/back-door que los predefinidos (1 index-pattern + N viz + 1
    dashboard). El index-pattern matchea el índice real (`index_pattern_from_name`)
    y trae los `fields` poblados; las viz referencian el `field_path` real.
    """
    spec = _spec_from_fields(slug, index_name, fields)
    return _build_rich_ndjson(slug, spec)


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
