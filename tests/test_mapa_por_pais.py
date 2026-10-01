"""Mapa por país (Region Map) en los dashboards de SIEM y FortiAnalyzer: los
datos traen el NOMBRE del país de origen, sin coordenadas."""
import json

import pytest

import dashboards


def _region_maps(slug):
    objs = [json.loads(l) for l in dashboards.build_ndjson(slug).splitlines() if l.strip()]
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


@pytest.mark.parametrize("slug, campo, filtro", [
    ("siem", "source.geo.country_name", "security.denied:1"),
    ("fortianalyzer", "srccountry", ""),
])
def test_el_dashboard_trae_su_mapa(slug, campo, filtro):
    mapas = _region_maps(slug)
    assert len(mapas) == 1
    vs = json.loads(mapas[0]["attributes"]["visState"])
    assert vs["aggs"][1]["params"]["field"] == campo
    fuente = json.loads(mapas[0]["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])
    assert fuente["query"]["query"] == filtro


def test_los_otros_casos_no_tienen_mapa_por_pais():
    assert _region_maps("transacciones-billetera") == []
