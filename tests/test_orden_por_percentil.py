"""Los grupos de un panel no se ordenan por un percentiles sin nombrar cuál.

"Latencia p95 por mercado" (fix-oms) salía "Error" en Dashboards: su terms
ordenaba por "1", un percentiles, y OpenSearch responde "Invalid aggregation
order path [1]. When ordering on a multi-value metrics aggregation a metric name
must be specified". Con "1.95" la misma consulta anda (probado en el cluster).
"""
import json

import pytest

import dashboards
import dashboards_guardados as dg


def _vs(metrica: dict, orden: str = "1") -> dict:
    return {"type": "line", "aggs": [
        {"id": "1", "type": metrica.pop("type"), "schema": "metric", "params": metrica},
        {"id": "3", "type": "terms", "schema": "group", "params": {"field": "mercado", "orderBy": orden}},
        {"id": "2", "type": "date_histogram", "schema": "segment", "params": {"field": "@timestamp"}}]}


def _orden(vs: dict) -> str:
    return dashboards.orden_valido(vs)["aggs"][1]["params"]["orderBy"]


@pytest.mark.parametrize("metrica, orden", [
    ({"type": "percentiles", "field": "lat", "percents": [95]}, "1.95"),
    ({"type": "percentiles", "field": "lat", "percents": [50, 95, 99]}, "1.99"),   # el más alto
    ({"type": "percentiles", "field": "lat"}, "1.99"),                            # los de Dashboards
    ({"type": "percentiles", "field": "lat", "percents": [99.9]}, "1[99.9]"),
])
def test_por_un_percentil_se_nombra_cual(metrica, orden):
    assert _orden(_vs(metrica)) == orden


@pytest.mark.parametrize("metrica, orden", [
    ({"type": "count"}, "1"),
    ({"type": "avg", "field": "lat"}, "1"),
    ({"type": "percentiles", "field": "lat", "percents": [95]}, "_key"),
    ({"type": "percentiles", "field": "lat", "percents": [95]}, "_count"),
    ({"type": "percentiles", "field": "lat", "percents": [95]}, "1.95"),          # ya nombrado
])
def test_lo_que_ya_se_puede_ordenar_no_se_toca(metrica, orden):
    assert _orden(_vs(metrica, orden)) == orden


def test_el_generador_ya_no_lo_arma():
    vs = dashboards._panel_to_vis_state({"type": "line", "title": "Latencia p95 por mercado",
                                         "metric": "percentiles", "field": "fix.latencia_ms",
                                         "percents": [95], "label": "Latencia", "split": "fix.contraparte"})
    assert [(a["type"], a["params"].get("orderBy")) for a in vs["aggs"]] == [
        ("percentiles", None), ("terms", "1.95"), ("date_histogram", None)]


def test_una_copia_guardada_rota_se_carga_arreglada(tmp_path, monkeypatch):
    monkeypatch.setattr(dg, "_dir", lambda: tmp_path)
    rota = _vs({"type": "percentiles", "field": "lat", "percents": [95]})
    sana = _vs({"type": "count"})
    dg.guardar("fix-oms", [
        {"type": "visualization", "id": "rota", "attributes": {"title": "p95", "visState": json.dumps(rota)},
         "references": []},
        {"type": "visualization", "id": "sana", "attributes": {"title": "n", "visState": json.dumps(sana)},
         "references": []},
        {"type": "dashboard", "id": "d", "attributes": {"title": "D", "panelsJSON": "[]"}, "references": []},
    ], "alguien")

    cargado = {o["id"]: o for o in map(json.loads, dg.ndjson("fix-oms").splitlines())}

    assert json.loads(cargado["rota"]["attributes"]["visState"])["aggs"][1]["params"]["orderBy"] == "1.95"
    # Lo demás, tal cual se guardó.
    assert cargado["sana"]["attributes"]["visState"] == json.dumps(sana)
    assert cargado["d"]["attributes"] == {"title": "D", "panelsJSON": "[]"}
    # Y lo guardado no se reescribe: se corrige al cargarlo.
    assert json.loads(dg.leer("fix-oms")["visualization:rota"]["attributes"]["visState"]) == rota
