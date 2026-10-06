"""Mapa por país (Region Map) en el dashboard de un dataset que trae el NOMBRE
del país (sin coordenadas)."""
import json

import pytest

import dashboards


def _region_maps(fields):
    ndjson = dashboards.build_ndjson_from_fields("x", "x-%{+YYYY.MM}", fields)
    objs = [json.loads(l) for l in ndjson.splitlines() if l.strip()]
    return [o for o in objs if o.get("type") == "visualization"
            and json.loads(o["attributes"]["visState"]).get("type") == "region_map"]


def test_el_mapa_se_une_por_nombre_con_la_capa_de_paises():
    vs = dashboards._vs_region_map("Por país", "source.geo.country_name")
    assert vs["type"] == "region_map"
    terms = vs["aggs"][1]
    assert terms["type"] == "terms" and terms["schema"] == "segment" and terms["params"]["field"] == "source.geo.country_name"
    p = vs["params"]
    # Si no se fija, Dashboards toma el primer campo de la capa: iso2, y no une nada.
    assert p["selectedJoinField"] == {"type": "name", "name": "name", "description": "Name"}
    assert p["selectedLayer"]["layerId"] == "elastic_maps_service.World Countries"
    assert p["selectedLayer"]["id"] == "world_countries" and p["selectedLayer"]["format"] == {"type": "geojson"}
    assert [f["name"] for f in p["selectedLayer"]["fields"]] == ["iso2", "iso3", "name"]
    assert p["layerChosenByUser"] == "default"


def test_el_dashboard_trae_su_mapa():
    """Un log con el NOMBRE del país (FortiGate: `srccountry`) tiene su mapa."""
    campos = [{"field_path": "srcip", "type": "ip"},
              {"field_path": "srccountry", "type": "string", "dimension": True, "business_label": "País de origen"}]
    mapas = _region_maps(campos)
    assert len(mapas) == 1
    vs = json.loads(mapas[0]["attributes"]["visState"])
    assert vs["aggs"][1]["params"]["field"] == "srccountry"


def test_sin_pais_no_hay_mapa_por_pais():
    assert _region_maps([{"field_path": "monto", "type": "float"},
                         {"field_path": "canal", "type": "string", "dimension": True}]) == []
