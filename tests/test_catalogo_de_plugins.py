"""Elegir qué plugins armar, ya con el entorno desplegado: cada uno con sus
comandos de Dev Tools a la vista y un botón que aplica solo ese. Antes,
"Provisionar plugins" armaba todo (también lo que en una demo sobra, como el
ciclo de vida de los índices)."""
import json
import pathlib
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import exportar
import main

F = lambda path, tipo="keyword", **kw: {"field_path": path, "type": tipo, **kw}  # noqa: E731
CAMPOS = [F("@timestamp", "date", role="timestamp"), F("comentario", "text"), F("cliente", role="entity_id"),
          F("monto", "float", role="measure"), F("dni", sensitive=True), F("estado", role="primary_dimension")]


def test_las_secciones_por_plugin():
    secs = exportar.secciones("hotel", CAMPOS, label="Hotel")
    for p in ("template", "agente", "forecasting", "anomalias", "busqueda", "reporte", "ciclo_de_vida"):
        assert p in secs, p
    assert secs["forecasting"].startswith("# ── Pronósticos") and "_plugins/_forecast/forecasters" in secs["forecasting"]
    assert "_plugins/_forecast" not in secs["anomalias"], "cada uno con lo suyo"
    assert "_plugins/_reports/definition" in secs["reporte"]
    # El script entero es lo mismo que todas las secciones (más el encabezado).
    todo = exportar.devtools("hotel", CAMPOS, label="Hotel")
    assert all(texto in todo for texto in secs.values())


def _endpoint(monkeypatch, registro):
    llamadas = {"casos": [], "perfil": [], "reporte": [], "paso": [], "busqueda": [], "canal": 0}
    escrito = {}
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda td: json.loads(json.dumps(registro)))
    monkeypatch.setattr(main, "_write_pipelines_registry", lambda td, reg: escrito.update(reg))
    monkeypatch.setattr(main, "_provision_capabilities", lambda c, slug, *a, apagados=None, **k:
                        llamadas["casos"].append((slug, k.get("solo_conversacional", False), set(apagados or ()))) or {})
    monkeypatch.setattr(main, "_provisionar_perfil", lambda b, u, p, slug, *a: llamadas["perfil"].append(slug) or {"ok": True, "reason": "ok"})
    monkeypatch.setattr(main, "_provisionar_analista", lambda *a, **k: {"ok": True, "reason": "ok"})
    monkeypatch.setattr(main, "_provisionar_reporte", lambda b, u, p, slug, *a: llamadas["reporte"].append(slug) or {"ok": True, "reason": "ok"})
    monkeypatch.setattr(main, "_provisionar_el_paso_del_tiempo", lambda b, u, p, slug, ip, entry, *a: llamadas["paso"].append((slug, set(entry["excluir"]))))
    monkeypatch.setattr(main, "_provisionar_la_busqueda", lambda c, u, p, h, td, slugs, pipe, *a: llamadas["busqueda"].append({s: set(pipe[s]["excluir"]) for s in slugs}))
    monkeypatch.setattr(main, "_provisionar_canal", lambda *a: llamadas.__setitem__("canal", llamadas["canal"] + 1))
    for f in ("_registrar_capacidades", "_asegurar_ppl_v3", "_revisar_meses_de_seguridad", "_registrar_agente_del_run"):
        monkeypatch.setattr(main, f, lambda *a, **k: "")
    return llamadas, escrito


REGISTRO = {"siem": {"index": "siem-%{+YYYY_MM}", "dashboard_id": "D1", "excluir": ["perfil"]},
            "ventas-ecommerce": {"index": "ventas-ecommerce-%{+YYYY.MM}", "dashboard_id": "D2"}}


def test_aplicar_un_plugin_no_toca_los_demas(monkeypatch):
    llamadas, escrito = _endpoint(monkeypatch, REGISTRO)
    r = TestClient(main.app).post("/api/v1/onboarding/provision-capabilities", json={
        "opensearch_password": "pw", "slugs": ["siem", "ventas-ecommerce"], "plugins": ["perfil"]})
    assert r.status_code == 200
    assert not llamadas["casos"], "ni asistente, ni pronósticos, ni anomalías"
    assert llamadas["perfil"] == ["siem", "ventas-ecommerce"], "lo pedido, aunque se haya apagado en el paso 2"
    assert escrito["siem"]["excluir"] == [], "pedirlo lo prende"
    assert not llamadas["reporte"] and llamadas["canal"] == 0
    assert all("ciclo_de_vida" in ex and "rollup" in ex for _, ex in llamadas["paso"]), "el ciclo de vida, apagado"
    assert all("busqueda" in ex for por_caso in llamadas["busqueda"] for ex in por_caso.values())


def test_la_alerta_sola_va_sobre_el_detector_que_ya_esta(monkeypatch):
    llamadas, _ = _endpoint(monkeypatch, REGISTRO)
    TestClient(main.app).post("/api/v1/onboarding/provision-capabilities", json={
        "opensearch_password": "pw", "slugs": ["siem"], "plugins": ["alertas"]})
    (slug, solo_conv, apagados), = llamadas["casos"]
    assert slug == "siem" and not solo_conv and "alertas" not in apagados and "anomalias" in apagados
    assert llamadas["canal"] == 1, "el canal de la alerta"


def test_rehacer_un_plugin_no_baja_el_caso(monkeypatch):
    llamadas, _ = _endpoint(monkeypatch, REGISTRO)
    bajados = []
    monkeypatch.setattr(main, "_teardown_orphans_by_name", lambda *a: bajados.append("huerfanos"))
    monkeypatch.setattr(main, "_teardown_slug_caps", lambda *a: bajados.append("caso"))
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {"siem": {"agent_id": "A", "forecaster_ids": ["F"]}})
    TestClient(main.app).post("/api/v1/onboarding/provision-capabilities", json={
        "opensearch_password": "pw", "slugs": ["siem"], "plugins": ["perfil"], "force": True})
    assert not bajados, "el asistente y los pronósticos no se tocan"
    assert llamadas["perfil"] == ["siem"]


def test_los_recomendados_respetan_el_paso_2(monkeypatch):
    llamadas, escrito = _endpoint(monkeypatch, REGISTRO)
    TestClient(main.app).post("/api/v1/onboarding/provision-capabilities", json={
        "opensearch_password": "pw", "slugs": ["siem", "ventas-ecommerce"], "recomendados": True})
    assert llamadas["perfil"] == ["ventas-ecommerce"] and not escrito, "lo apagado en el paso 2 sigue apagado"
    assert ("siem", True, set()) in llamadas["casos"], "el asistente, sí"
    assert ("ventas-ecommerce", False, {"ciclo_de_vida", "rollup"}) in llamadas["casos"]
    assert all({"ciclo_de_vida", "rollup"} <= ex for _, ex in llamadas["paso"]), "lo que en una demo sobra, no"
    assert llamadas["reporte"] == ["siem", "ventas-ecommerce"] and llamadas["canal"] == 1


def test_sin_lista_todo_como_antes(monkeypatch):
    llamadas, escrito = _endpoint(monkeypatch, REGISTRO)
    TestClient(main.app).post("/api/v1/onboarding/provision-capabilities", json={
        "opensearch_password": "pw", "slugs": ["siem", "ventas-ecommerce"]})
    assert ("siem", True, set()) in llamadas["casos"], "el asistente primero"
    assert llamadas["perfil"] == ["ventas-ecommerce"], "lo apagado en el paso 2 sigue apagado"
    assert llamadas["reporte"] == ["siem", "ventas-ecommerce"] and not escrito


def test_los_comandos_de_un_plugin(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    (tmp_path / main._PIPELINES_REGISTRY_NAME).write_text(json.dumps({
        "siem": {"index": "siem-%{+YYYY_MM}"}, "produccion-pozos": {"index": "produccion-pozos-%{+YYYY.MM}"}}),
        encoding="utf-8")
    d = TestClient(main.app).get("/api/v1/plugins/comandos?plugin=forecasting").json()
    assert [c["slug"] for c in d["casos"]] == ["siem", "produccion-pozos"]
    assert all("POST _plugins/_forecast/forecasters" in c["texto"] for c in d["casos"])
    assert "siem-*" in d["casos"][0]["texto"], "con el índice real del entorno"
    busq = TestClient(main.app).get("/api/v1/plugins/comandos?plugin=busqueda").json()
    assert [c["slug"] for c in busq["casos"]] == ["siem"], "pozos no tiene texto libre"


def test_el_catalogo_dice_donde_aplica_y_donde_esta(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    registro = {"siem": {"index": "siem-%{+YYYY_MM}", "dashboard_id": "D"}, "produccion-pozos": {"index": "pp-%{+YYYY.MM}"}}
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {"siem": {"agent_id": "A", "forecaster_ids": ["F"]}})
    monkeypatch.setattr(main, "_read_estados", lambda td: {"siem": {"busqueda": {"ok": True}, "rollup": {"ok": False}}})
    monkeypatch.setattr(main, "_read_security", lambda td: {"siem": {"detectores": {"x": {}}}})
    cat = {c["plugin"]: c for c in main._catalogo_de_plugins(tmp_path, registro)}
    assert list(cat)[:4] == ["agente", "forecasting", "anomalias", "alertas"]
    assert cat["forecasting"]["aplica"] == ["siem", "produccion-pozos"] and cat["forecasting"]["aplicado"] == ["siem"]
    assert cat["busqueda"]["aplica"] == ["siem"] and cat["busqueda"]["aplicado"] == ["siem"]
    assert cat["reporte"]["aplica"] == ["siem"], "el reporte, donde hay dashboard"
    assert cat["rollup"]["aplicado"] == [], "falló: no está aplicado"
    assert cat["security_analytics"]["aplicado"] == ["siem"]


# ── El front ────────────────────────────────────────────────────────────────
_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"


def _catalogo_js() -> str:
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    const _PARA_QUE_SIRVE = {")
    j = html.index("    function ayudaHTML(plugin, izquierda = false) {")
    k = html.index("    // ── Plugins para el cluster: elegir qué armar")
    return html[i:j] + html[j:html.index("    const _tarjetasDe = ", j)] + html[k:html.index("    // `verificar`: cada tarjeta reserva", k)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_el_catalogo_en_node(tmp_path):
    js = tmp_path / "catalogo.mjs"
    js.write_text(r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = { siem: 'SIEM', 'produccion-pozos': 'Producción de pozos' };
const state = {};
""" + _catalogo_js() + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
const C = (plugin, aplica, aplicado, recomendado = true) => ({ plugin, aplica, aplicado, recomendado });
const cat = [C('rollup', [], [], false), C('forecasting', ['siem', 'produccion-pozos'], ['siem']),
             C('agente', ['siem'], ['siem']), C('ciclo_de_vida', ['siem'], [], false),
             C('security_analytics', ['siem'], []), C('no-existe', ['siem'], [])];
check('sin catálogo, nada', catalogoHTML([]) === '' && catalogoHTML(null) === '');
const h = catalogoHTML(cat);
check('lo desconocido no va', !h.includes('no-existe'), h);
check('lo que no aplica, al final', h.indexOf('Resumen por hora') > h.indexOf('Ciclo de vida'), h);
check('a medias: cuántos y dónde falta', h.includes('title="Falta en: Producción de pozos">En 1 de 2 casos</span>'), h);
check('a medias: completar', h.includes('class="btn btn-secondary btn-sm cat__aplicar" data-plugin="forecasting">Completar</button>'), h);
check('listo: sin botón para aplicar', h.includes('<span class="sev sev--ok">Listo</span>') && !h.includes('data-plugin="agente">Aplicar'), h);
check('lo opcional lo dice', (h.match(/>Opcional</g) || []).length === 2, h);
check('Security Analytics, en el paso 1', h.includes('Se arma en el paso 1, antes que los datos.')
  && !h.includes('cat__aplicar" data-plugin="security_analytics"'), h);
check('sin casos, sin comandos', !h.includes('cat__ver" data-plugin="rollup"') && h.includes('No aplica a estos casos'), h);
check('los comandos, cerrados', h.includes('aria-expanded="false" aria-controls="cat-det-forecasting"')
  && h.includes('id="cat-det-forecasting" data-det="forecasting" hidden>'), h);
check('cada uno con su ?', h.includes('Pronósticos<span class="ayuda"'), h);
state.catalogoAbierto = 'forecasting';
const abierto = catalogoHTML(cat);
check('abierto, a todo el ancho y a la vista', abierto.includes('class="cat__item is-abierto"')
  && abierto.includes('id="cat-det-forecasting" data-det="forecasting">'), abierto);
state.catalogoAbierto = null;
state.provisionandoPlugins = true; state.aplicandoPlugin = 'ciclo_de_vida';
const aplicando = catalogoHTML(cat);
check('aplicando: lo dice y no deja lanzar otro', aplicando.includes('data-plugin="ciclo_de_vida" disabled>Aplicando…')
  && aplicando.includes('data-plugin="forecasting" disabled>Completar'), aplicando);
check('escapado', catalogoHTML([C('agente', ['<x>'], [])]).includes('&lt;x&gt;'));
// Dev Tools, desde la URL de Dashboards.
check('dev tools', devToolsDe('https://1.2.3.4:5601/app/home#/') === 'https://1.2.3.4:5601/app/dev_tools#/console', devToolsDe('https://1.2.3.4:5601/app/home#/'));
check('sin dashboards, sin link', devToolsDe('') === '');
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
""", encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_el_catalogo_esta_conectado():
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function renderInfraView(data) {")
    vista = html[i:html.index("    async function hydrateActiveEnv() {", i)]
    assert "${catalogoHTML(data.catalogo)}" in vista
    assert "provisionCapabilitiesFromInfra(e.currentTarget, pipelines, false, { recomendados: true })" in vista
    assert "if (aplicar) return aplicarPlugin(aplicar, pipelines, data.catalogo);" in vista
    assert "if (ver) return verComandos(ver);" in vista
    # Los pedidos llevan la lista o los recomendados.
    assert "...(plugins ? { plugins } : {})," in html and "...(recomendados ? { recomendados: true } : {})," in html
    # De a uno: dos a la vez se pisan.
    assert "if (state.provisionandoPlugins) {" in html
    assert "fetch(`/api/v1/plugins/comandos?plugin=${encodeURIComponent(plugin)}`)" in html
    # La alerta sin detector, con el detector.
    assert "? ['anomalias', 'alertas'] : [c.plugin];" in html
