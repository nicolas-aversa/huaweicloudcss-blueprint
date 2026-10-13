"""La línea chica de cada card del paso 1: en qué formato llega el dato y de
qué sistema sale. Los curados lo declaran; los del Builder sacan el formato del
filtro (sirve para cualquier archivo y para los casos ya guardados) y el origen
lo escribe el SA al guardar."""
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

import auth
import custom_cases
import main
import verticals

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


@pytest.mark.parametrize("filtro, formato", [
    ('filter { csv { columns => ["a","b"] } }', "CSV"),
    ('filter { json { source => "message" target => "data" } }', "JSON"),
    ('filter { kv { source => "message" } }', "clave=valor"),
    ('filter { grok { match => { "message" => "%{SYSLOGTIMESTAMP:ts} %{GREEDYDATA:m}" } } }', "Syslog"),
    ('filter { xml { source => "message" target => "doc" } }', "XML"),
    ('filter { grok { match => { "message" => "%{GREEDYDATA:raw}" } } }', "Texto"),
    ('filter { json { source => "message" } kv { source => "[data][detalle]" } }', "JSON, clave=valor"),
    ("", ""),
])
def test_el_formato_sale_del_filtro(filtro, formato):
    assert custom_cases.formato_del_filtro(filtro) == formato


def test_los_curados_dicen_formato_y_origen():
    for v in verticals.all_verticals():
        assert v.get("formato") and v.get("origen"), v["slug"]
    payload = {v["slug"]: v for v in verticals.front_payload()["verticals"]}
    assert (payload["cts"]["formato"], payload["cts"]["origen"]) == ("JSON", "Huawei Cloud CTS")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(main, "_upload_case_dataset", lambda slug: (False, "test: sin subir"))
    return tmp_path / "cases"


def _caso(**kw):
    base = {"label": "Seguimiento de envíos", "group": "logistica", "icon": "truck",
            "filter_code": 'filter { json { source => "message" target => "data" } }',
            "fields": [{"raw_name": "evento", "field_path": "data.evento", "type": "string"}],
            "log_content": '{"evento":"Admitido"}\n{"evento":"Entregado"}\n'}
    base.update(kw)
    return base


def test_al_guardar_queda_el_origen_y_el_formato(store):
    r = TestClient(main.app).post("/api/v1/cases", json=_caso(origen="  API de seguimiento  "))
    assert r.status_code == 200, r.text
    caso = r.json()["case"]
    assert (caso["formato"], caso["origen"]) == ("JSON", "API de seguimiento")
    entrada = next(e for e in custom_cases.front_entries() if e["slug"] == "seguimiento-de-envios")
    assert (entrada["formato"], entrada["origen"]) == ("JSON", "API de seguimiento")


def test_un_caso_guardado_antes_igual_muestra_su_formato(store):
    custom_cases.save_case(_caso(), '{"evento":"Admitido"}\n')
    ruta = store / "seguimiento-de-envios.json"
    viejo = json.loads(ruta.read_text(encoding="utf-8"))
    viejo.pop("formato"), viejo.pop("origen")
    ruta.write_text(json.dumps(viejo), encoding="utf-8")
    entrada = next(e for e in custom_cases.front_entries() if e["slug"] == "seguimiento-de-envios")
    assert (entrada["formato"], entrada["origen"]) == ("JSON", "")


def test_la_card_y_el_modal():
    html = _INDEX.read_text(encoding="utf-8")
    assert "const fuenteDe = (e) => [e.formato, e.origen].filter(Boolean).join(' · ');" in html
    assert '<span class="example-card__fuente" title="${escapeHtml(fuenteDe(e))}">${escapeHtml(fuenteDe(e))}</span>' in html
    assert 'id="save-case-origen" maxlength="40"' in html
    # Las dos formas de guardar (un archivo o varias fuentes) lo mandan.
    assert html.count("origen: document.getElementById('save-case-origen').value.trim(),") == 2
