"""El runbook para el cliente: los pasos y los archivos para desplegar un
dataset en su propio cluster, sin la plataforma y sin desplegarlo antes. Se
exporta desde el ··· de cada caso, en el paso 1."""
import io
import json
import pathlib
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import conf_lint
import dimensionamiento as dm
import exportar
import main
import plan_de_cluster
import runbook

HOTEL = [{"field_path": "@timestamp", "type": "date", "role": "timestamp"},
         {"field_path": "comentario", "type": "text"},
         {"field_path": "estado", "type": "keyword", "role": "primary_dimension", "business_label": "Estado"},
         {"field_path": "huesped", "type": "keyword", "role": "entity_id", "entity": True, "business_label": "Huésped"},
         {"field_path": "tarifa", "type": "double", "role": "measure", "business_label": "Tarifa"}]
AHORA = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"
SECRETOS = re.compile(r"HPUA|vJfk|VTd4|Huawei1234|k72q30|nvapi-[A-Za-z0-9_-]{20}|enc:")


def _ds(slug="hotel", plugins="# plugins\nPOST x", **kw):
    return {"slug": slug, "label": kw.get("label", "Hotel"), "excluir": [], "index_pattern": f"{slug}-*",
            "plan": plan_de_cluster.plan(slug, HOTEL, "Hotel"),
            "template": "PUT _index_template/x\n{}\n", "plugins": plugins,
            "logstash": 'input {}\nfilter {}\noutput { elasticsearch { hosts => ["https://<ENDPOINT_DEL_CLUSTER>:9200"] '
                        'user => "<USUARIO>" password => "<CONTRASEÑA>" } }\n',
            "dashboards": '{"type":"dashboard"}\n'}


# ── El script en dos ────────────────────────────────────────────────────────
def test_el_template_va_antes_que_los_datos_y_los_plugins_despues():
    template, plugins = exportar.partes("hotel", HOTEL, label="Hotel")
    assert "PUT _index_template/hotel" in template and "_plugins/" not in template
    assert plugins.startswith("# Plugins de OpenSearch para «Hotel». Corré cada request en orden,")
    assert "PUT _index_template" not in plugins and "_plugins/_ml/agents/_register" in plugins
    todo = exportar.devtools("hotel", HOTEL, label="Hotel")
    cuerpo = plugins.split("\n\n", 1)[1]
    assert template.strip() in todo and cuerpo.strip() in todo, "las dos partes son el script entero"


def test_sin_plugins_el_segundo_archivo_queda_vacio(monkeypatch):
    monkeypatch.setattr(exportar, "_armar", lambda slug, fields, **kw: (["# encabezado", "PUT _index_template/x\n{}"],
                                                                        [("template", 1)]))
    assert exportar.partes("x", []) == ("# encabezado\n\nPUT _index_template/x\n{}\n", "")


# ── El ZIP ──────────────────────────────────────────────────────────────────
def test_un_dataset_los_pasos_en_orden_y_sus_archivos():
    z = zipfile.ZipFile(io.BytesIO(runbook.paquete("Hotel", [_ds()], ahora=AHORA)))
    assert z.namelist() == ["LEEME.md", "1-index-template.txt", "2-logstash.conf", "3-dashboards.ndjson", "4-plugins.txt"]
    md = z.read("LEEME.md").decode()
    assert md.startswith("# Runbook: Hotel\n") and "2026-10-13 12:00 UTC" in md
    pasos = ["### 1. Index template (Dev Tools)", "### 2. Logstash", "### 3. Dashboards", "### 4. Plugins (Dev Tools)"]
    assert all(p in md for p in pasos) and [md.index(p) for p in pasos] == sorted(md.index(p) for p in pasos)
    assert "corré `1-index-template.txt`" in md and "`GET hotel-*/_count` tiene que crecer" in md
    assert "| `<ENDPOINT_DEL_CLUSTER>` | el endpoint de tu cluster CSS" in md, "lo que completás, en palabras"
    assert "## Qué vas a tener" in md and "| Plugin | ¿Se configura? | Por qué |" in md
    assert "## Para producción" not in md and "el tamaño" not in md, "sin volumen, sin dimensionamiento"


def test_con_volumen_dimensiona_y_con_varios_una_carpeta_por_dataset():
    dim = dm.dimensionar(400, 5_000_000, 90, True)
    z = zipfile.ZipFile(io.BytesIO(runbook.paquete("Firewall", [_ds("fw-a", label="A"), _ds("fw-b", plugins="", label="B")],
                                                   dimensionamiento=dim, ahora=AHORA)))
    assert "fw-a/1-index-template.txt" in z.namelist() and "fw-b/2-logstash.conf" in z.namelist()
    assert "fw-b/4-plugins.txt" not in z.namelist(), "sin plugins, sin archivo"
    md = z.read("LEEME.md").decode()
    assert "el `1-index-template.txt` de cada carpeta (`fw-a/`, `fw-b/`)" in md
    assert "corré `fw-a/4-plugins.txt` request por request" in md, "solo donde hay plugins, con su carpeta"
    assert "## Qué vas a tener: A" in md and "## Qué vas a tener: B" in md
    assert "## Para producción" in md and "5.000.000 eventos por día" in md and "(el tamaño, en «Para producción»)" in md


def test_los_marcadores():
    assert runbook.marcadores("a <MAAS_API_KEY> b <AGENT_ID>", "<MAAS_API_KEY> <x> <CONTRASEÑA>") == \
        ["MAAS_API_KEY", "AGENT_ID", "CONTRASEÑA"]
    md = runbook.documento("Hotel", [{**_ds(), "plugins": "POST _plugins/_ml/agents/<AGENT_ID>/_execute <MAAS_API_KEY>"}], ahora=AHORA)
    assert "| `<…_ID>` | lo devuelve la respuesta de un request anterior" in md
    assert "Una API key de MaaS, y que el cluster pueda salir a MaaS" in md


# ── El endpoint ─────────────────────────────────────────────────────────────
@pytest.fixture
def caso(monkeypatch, tmp_path):
    import custom_cases
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    casos = {
        "hotel": {"label": "Hotel", "fields": HOTEL, "index_base": "hotel", "sample": "x" * 400,
                  "filter_code": 'filter {\n  json { source => "message" }\n}',
                  "volumen": {"eventos_por_dia": 1_000_000, "alta_disponibilidad": True},
                  "input_config": {"plugin_type": "obs", "obs": {"bucket": "demoscss", "prefix": "hotel-logs/",
                                                                 "access_key_id": "enc:xxx", "secret_access_key": "enc:yyy",
                                                                 "multiline_pattern": "^\\d{4}-"}}},
        "kafka": {"label": "Pagos", "fields": HOTEL, "index_base": "pagos",
                  "filter_code": "filter {\n}",
                  "input_config": {"plugin_type": "kafka", "kafka": {"bootstrap_servers": "kafka.cliente:9093", "topics": ["pagos"],
                                                                     "security_protocol": "SASL_SSL", "sasl_username": "svc",
                                                                     "sasl_password": "enc:zzz"}}},
    }
    monkeypatch.setattr(custom_cases, "get_case", lambda slug: casos.get(slug))
    return casos


def test_el_endpoint_arma_el_zip_sin_secretos_ni_el_bucket_de_la_plataforma(caso):
    r = TestClient(main.app).get("/api/v1/runbook", params={"slugs": "hotel"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="runbook-hotel.zip"' in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    conf = z.read("2-logstash.conf").decode()
    assert [s.nombre for s in conf_lint.secciones(conf)] == ["input", "filter", "output"]
    assert 'bucket => "<BUCKET>"' in conf and "demoscss" not in conf and "hotel-logs" not in conf, "el origen es del cliente"
    assert 'access_key_id => "<ACCESS_KEY_ID>"' in conf and 'secret_access_key => "<SECRET_ACCESS_KEY>"' in conf
    assert "^\\d{4}-" in conf, "lo que es de los datos (dónde empieza cada registro) se queda"
    assert 'json { source => "message" }' in conf and 'index => "hotel-%{+YYYY.MM}"' in conf
    assert 'hosts => ["https://<ENDPOINT_DEL_CLUSTER>:9200"]' in conf and 'password => "<CONTRASEÑA>"' in conf
    assert "PUT _index_template/hotel" in z.read("1-index-template.txt").decode()
    assert json.loads(z.read("3-dashboards.ndjson").decode().splitlines()[0])
    md = z.read("LEEME.md").decode()
    assert "1.000.000 eventos por día" in md, "con el volumen guardado del caso"
    todo = b"".join(z.read(n) for n in z.namelist()).decode()
    assert not SECRETOS.search(todo)


def test_una_fuente_en_vivo_conserva_lo_que_no_es_secreto(caso):
    z = zipfile.ZipFile(io.BytesIO(TestClient(main.app).get("/api/v1/runbook?slugs=kafka").content))
    conf = z.read("2-logstash.conf").decode()
    assert 'bootstrap_servers => "kafka.cliente:9093"' in conf and '"pagos"' in conf
    assert "<SASL_PASSWORD>" in conf and "enc:" not in conf


def test_varios_y_errores(caso):
    c = TestClient(main.app)
    z = zipfile.ZipFile(io.BytesIO(c.get("/api/v1/runbook", params={"slugs": "hotel,kafka", "nombre": "Mi PoC"}).content))
    assert {"hotel/2-logstash.conf", "kafka/2-logstash.conf"} <= set(z.namelist())
    assert z.read("LEEME.md").decode().startswith("# Runbook: Mi PoC\n")
    assert c.get("/api/v1/runbook?slugs=no-existe").status_code == 404
    assert c.get("/api/v1/runbook?slugs=,").status_code == 400


def test_sin_comandos_en_entorno_desplegado():
    """Los comandos de Dev Tools sirven para que el cliente lo despliegue: van
    en el runbook. En Entorno desplegado (la demo) no están."""
    c = TestClient(main.app)
    assert c.get("/api/v1/plugins/comandos?plugin=forecasting").status_code == 404
    assert c.get("/api/v1/dev/comandos-preview?plugin=forecasting").status_code == 404
    html = _INDEX.read_text(encoding="utf-8")
    assert "Ver cómo se hace en Dev Tools" not in html and "Llevarse:" not in html


# ── El ··· de las tarjetas del paso 1 ───────────────────────────────────────
_ARNES = r"""
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const icon = (n) => `<i data-i="${n}"></i>`;
const FAMILIAS = { 'fam:fw': { label: 'Firewall', miembros: ['fw-a', 'fw-b'] } };
const LOG_EXAMPLES = [{ id: 'siem', label: 'SIEM' }, { id: 'mio', label: 'Mío', custom: true }];
const hechos = [];
const toast = (m, t) => hechos.push(['toast', t, m]);
const detailError = (d) => new Error((d.detail && d.detail.message) || 'error');
globalThis.fetch = async (u) => { hechos.push(['fetch', u]); return u.includes('no-existe')
  ? { ok: false, json: async () => ({ detail: { message: 'No hay un dataset' } }) }
  : { ok: true, headers: { get: () => 'attachment; filename="runbook-firewall.zip"' }, blob: async () => 'zip' }; };
globalThis.URL = { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} };
const descargas = [];
globalThis.document = { createElement: () => ({ click() { descargas.push(this.download); }, remove() {} }),
                        body: { appendChild() {} } };
globalThis.setTimeout = () => {};
""" + "{FUNCION}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
await exportarRunbook('fam:fw');
check('una familia, todas sus fuentes y su nombre', hechos[0][1] === '/api/v1/runbook?slugs=fw-a%2Cfw-b&nombre=Firewall', JSON.stringify(hechos));
check('se descarga con su nombre', descargas[0] === 'runbook-firewall.zip', JSON.stringify(descargas));
await exportarRunbook('no-existe');
check('el error, dicho', hechos.some(h => h[0] === 'toast' && h[1] === 'error' && h[2].includes('No hay un dataset')), JSON.stringify(hechos));
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_exportar_el_runbook_desde_el_navegador(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    async function exportarRunbook(id) {")
    js = tmp_path / "runbook.mjs"
    js.write_text(_ARNES.replace("{FUNCION}", html[i:html.index("    document.addEventListener('click', (e) => {", i)]),
                  encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_el_menu_de_cada_tarjeta():
    html = _INDEX.read_text(encoding="utf-8")
    assert 'data-menu-case="${escapeHtml(e.id)}" role="button" tabindex="0" aria-haspopup="menu"' in html
    assert "data-delete-case" not in html, "borrar está en el ···, no en un ✕ aparte"
    assert "if (item.dataset.accion === 'runbook') return exportarRunbook(id);" in html
    assert "const propio = !!FAMILIAS[id] || !!(e && e.custom);" in html, "borrar, solo lo propio"
