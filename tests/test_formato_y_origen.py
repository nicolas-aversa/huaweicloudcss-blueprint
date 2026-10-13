"""La línea chica de cada card del paso 1: por dónde llega el dato (OBS,
Kafka…), en qué (la extensión del archivo o, sin archivo, el formato que lee el
filtro) y de qué sistema sale. La entrada y el formato salen solos (sirve para
cualquier dataset y para los casos ya guardados); el origen lo declaran los
curados y lo escribe el SA al guardar."""
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


def test_los_curados():
    payload = {v["slug"]: v for v in verticals.front_payload()["verticals"]}
    for slug, v in payload.items():
        assert v["entrada"] == "OBS" and v["formato"].startswith(".") and v["origen"], slug
    assert [payload["cts"][k] for k in ("entrada", "formato", "origen")] == ["OBS", ".json.gz", "Huawei Cloud CTS"]
    assert payload["siem"]["formato"] == ".log", "cuatro archivos, una extensión"


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


def _linea(slug):
    e = next(e for e in custom_cases.front_entries() if e["slug"] == slug)
    return [e["entrada"], e["formato"], e["origen"]]


def test_un_dataset_llega_por_obs_con_la_extension_de_su_archivo(store):
    r = TestClient(main.app).post("/api/v1/cases", json=_caso(origen="  API de seguimiento  ",
                                                             log_filename="seguimiento-de-envios.ndjson"))
    assert r.status_code == 200, r.text
    assert r.json()["case"]["origen"] == "API de seguimiento"
    assert _linea("seguimiento-de-envios") == ["OBS", ".ndjson", "API de seguimiento"]


def test_un_caso_guardado_antes_igual_muestra_entrada_y_formato(store):
    custom_cases.save_case(_caso(), '{"evento":"Admitido"}\n', filename="envios.CSV")
    ruta = store / "seguimiento-de-envios.json"
    viejo = json.loads(ruta.read_text(encoding="utf-8"))
    viejo.pop("origen")
    ruta.write_text(json.dumps(viejo), encoding="utf-8")
    assert _linea("seguimiento-de-envios") == ["OBS", ".csv", ""]


def test_una_fuente_en_vivo_dice_su_plugin_y_el_formato_del_filtro(store):
    custom_cases.save_case(_caso(origen="eventos del TMS", sample='{"evento":"Admitido"}', input_config={
        "plugin_type": "kafka", "kafka": {"bootstrap_servers": "10.0.0.5:9092", "topics": ["tracking"]}}), "")
    assert _linea("seguimiento-de-envios") == ["Kafka", "JSON", "eventos del TMS"]


def test_la_card_y_el_modal():
    html = _INDEX.read_text(encoding="utf-8")
    assert "const fuenteDe = (e) => [e.entrada, e.formato, e.origen].filter(Boolean).join(' · ');" in html
    assert '<span class="example-card__fuente" title="${escapeHtml(fuenteDe(e))}">${escapeHtml(fuenteDe(e))}</span>' in html
    assert 'id="save-case-origen" maxlength="40"' in html
    # Las dos formas de guardar (un archivo o varias fuentes) lo mandan.
    assert html.count("origen: document.getElementById('save-case-origen').value.trim(),") == 2
