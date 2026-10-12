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
def test_que_podes_mostrar_en_node(tmp_path):
    js = tmp_path / "funciones.mjs"
    js.write_text(r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = { siem: 'SIEM', 'produccion-pozos': 'Producción de pozos' };
const state = {};
const _ESTADO_PLUGIN = { ok: ['Listo', 'sev--ok'], parcial: ['Parcial', 'sev--high'], falla: ['Falló', 'sev--critical'],
                         en_curso: ['En curso', 'sev--low'], excluido: ['Excluido', ''], sin_probar: ['Sin probar', 'sev--low'] };
const _tarjetasDe = (plugins, slug) => (Array.isArray((plugins || {})[slug]) ? plugins[slug] : []);
const pluginsDetalleHTML = (ts) => `<div class="plug-grid">${ts.map(t => t.plugin).join(',')}</div>`;
""" + _catalogo_js() + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
const C = (plugin, aplica, aplicado, recomendado = true) => ({ plugin, aplica, aplicado, recomendado });
const T = (plugin, estado, extra = {}) => ({ plugin, titulo: plugin, estado, motivo: '', filas: [], links: [], ...extra });
const data = {
  pipelines: [{ slug: 'siem' }, { slug: 'produccion-pozos' }],
  catalogo: [C('agente', ['siem', 'produccion-pozos'], ['siem', 'produccion-pozos']),
             C('forecasting', ['siem', 'produccion-pozos'], ['siem']),
             C('security_analytics', ['siem'], []), C('rollup', [], [], false),
             C('ciclo_de_vida', ['siem', 'produccion-pozos'], [], false), C('anomalias', ['siem'], [])],
  plugins: {
    siem: [T('dashboard', 'ok', { links: [{ texto: 'Abrir el tablero', url: 'https://d/siem' }] }),
           T('forecasting', 'parcial', { motivo: 'backtest parcial' })],
    'produccion-pozos': [T('dashboard', 'ok')],
    _cluster: [T('query_insights', 'ok'), T('embeddings', 'falla')],
  },
};
check('sin nada, nada', funcionesHTML({ pipelines: [], catalogo: [], plugins: {} }) === '');
const h = funcionesHTML(data);
const tile = (html, id) => (html.match(new RegExp(`<div class="fn-tile[^"]*" data-fn="${id}">[\\s\\S]*?</div>`)) || [''])[0];
check('una tarjeta con ícono, nombre corto y estado', tile(h, 'forecasting').includes('<span class="fn-tile__icono"><svg data-i="trending"></svg></span>'
  + '<span class="fn-tile__titulo">Pronósticos</span><span class="fn-tile__estado">En 1 de 2 casos</span>'), tile(h, 'forecasting'));
check('el nombre técnico no va en la tarjeta (va en su panel)', !h.includes('Forecasting'), h);
check('a medias, en amarillo, con su acción', tile(h, 'forecasting').startsWith('<div class="fn-tile fn--parcial" data-fn="forecasting">')
  && tile(h, 'forecasting').includes('class="btn btn-secondary btn-sm cat__aplicar" data-plugin="forecasting">Completar</button>'), tile(h, 'forecasting'));
check('el asistente se prueba', tile(h, 'agente').includes('<button type="button" class="btn btn-secondary btn-sm fn__probar">Probar</button>')
  && tile(h, 'agente').includes('Lista · 2 casos'), tile(h, 'agente'));
check('sin activar, en gris, con Activar', tile(h, 'anomalias').startsWith('<div class="fn-tile fn--off"')
  && tile(h, 'anomalias').includes('Sin activar') && tile(h, 'anomalias').includes('data-plugin="anomalias">Activar</button>'), tile(h, 'anomalias'));
check('las acciones, en secundario (lo primario es el paso 3)', !h.includes('btn-primary'), h);
check('Security Analytics no se activa desde acá', !h.includes('data-plugin="security_analytics"') && h.includes('data-fn="security_analytics"'), h);
check('los tableros, de las tarjetas', tile(h, 'dashboard').includes('Lista · 2 casos'), tile(h, 'dashboard'));
check('lo que no aplica no se muestra', !h.includes('data-fn="rollup"') && !h.includes('data-fn="busqueda"'), h);
check('lo opcional y lo del cluster, no acá', !h.includes('data-fn="ciclo_de_vida"') && !h.includes('data-fn="query_insights"'), h);
const extra = funcionesHTML(data, { extra: true });
check('van en "Otras funciones"', extra.includes('id="infra-funciones-extra"') && extra.includes('>Otras funciones<')
  && tile(extra, 'ciclo_de_vida').includes('data-plugin="ciclo_de_vida">Activar</button>') && tile(extra, 'query_insights').includes('Lista'), extra);
check('cuántas listas', h.includes('<span class="fn-cab__n">2 de 5 listas</span>'), h);
check('el panel, cerrado', h.includes('<div class="fn-panel" hidden></div>') && h.includes('aria-expanded="false"'), h);
const sinTableros = funcionesHTML({ ...data, plugins: {} });
check('los tableros aplican siempre: sin tarjetas, sin activar (y sin botón: van en el paso 1)',
  tile(sinTableros, 'dashboard').includes('Sin activar') && !sinTableros.includes('data-plugin="dashboard"'), tile(sinTableros, 'dashboard'));
state.provisionandoPlugins = true; state.aplicandoPlugin = 'forecasting';
const activando = funcionesHTML(data);
check('activando: lo dice y no deja lanzar otro', activando.includes('data-plugin="forecasting" disabled>Activando…')
  && activando.includes('data-plugin="anomalias" disabled>Activar'), activando);
state.provisionandoPlugins = false; state.aplicandoPlugin = '';
// Con "Activar los plugins" corriendo: lo que falta está en cola, no "sin activar".
const enCurso = funcionesHTML({ ...data, activando: { desde: 1 } });
check('activando: en cola, sin botón', tile(enCurso, 'forecasting').includes('Activando… 1 de 2')
  && tile(enCurso, 'anomalias').includes('<span class="fn-tile__estado">Activando…</span>')
  && !enCurso.includes('cat__aplicar" data-plugin="forecasting"') && !enCurso.includes('cat__aplicar" data-plugin="anomalias"'), enCurso);
check('activando: lo que no arma el paso 3 sigue como está', tile(funcionesHTML({ ...data, activando: { desde: 1 } }, { extra: true }), 'ciclo_de_vida').includes('Sin activar'));
check('activando: lo dice arriba', enCurso.includes('Activando: se completan solas'), enCurso);
const unaSola = funcionesHTML({ ...data, activando: { desde: 1, plugins: ['forecasting'] } });
check('activando una sola: solo esa en cola', tile(unaSola, 'forecasting').includes('Activando… 1 de 2')
  && tile(unaSola, 'anomalias').includes('data-plugin="anomalias">Activar</button>') && !unaSola.includes('Activando: se completan solas'), unaSola);
state.funcionAbierta = 'forecasting';
const abierta = funcionesHTML(data);
check('abierta: marcada y con su panel a la vista', abierta.includes('<div class="fn-tile fn--parcial is-abierta" data-fn="forecasting">')
  && abierta.includes('<div class="fn-panel"></div>') && funcionesHTML(data, { extra: true }).includes('<div class="fn-panel" hidden></div>'), abierta);
state.funcionAbierta = null;
// El detalle.
const d = detalleDeFuncionHTML(funcionDe('forecasting', data));
check('qué hace y para el negocio', d.includes('<strong>Qué hace.</strong>') && d.includes('<strong>Para el negocio.</strong>'), d);
check('cada caso con su estado y su motivo', d.includes('<span class="fn__caso-nombre">SIEM</span><span class="sev sev--high">Parcial</span>')
  && d.includes('<span class="fn__motivo">backtest parcial</span>'), d);
check('el que falta, sin activar', d.includes('<span class="fn__caso-nombre">Producción de pozos</span><span class="sev sev--low">Sin activar</span>'), d);
check('sus comandos, a demanda', d.includes('class="btn btn-ghost btn-sm fn__comandos" data-plugin="forecasting"')
  && d.includes('<div class="cat__det" data-det="forecasting" hidden></div>'), d);
const tab = detalleDeFuncionHTML(funcionDe('dashboard', data));
check('el link a Dashboards de cada caso', tab.includes('href="https://d/siem" target="_blank" rel="noopener">Abrir el tablero'), tab);
check('sin catálogo, sin comandos', !tab.includes('fn__comandos'), tab);
const bus = funcionDe('busqueda', data);
check('lo del cluster va con su función, si tiene algo que decir', bus.cluster.map(t => t.plugin).join() === 'embeddings', JSON.stringify(bus.cluster));
const busOk = funcionDe('busqueda', { ...data, plugins: { ...data.plugins, _cluster: [T('embeddings', 'ok')] } });
check('el modelo listo no se muestra', busOk.cluster.length === 0, JSON.stringify(busOk.cluster));
check('Query Insights, siempre (es la función)', funcionDe('query_insights', data).cluster.map(t => t.plugin).join() === 'query_insights');
const conX = detalleDeFuncionHTML(funcionDe('agente', { ...data, catalogo: [C('agente', ['<x>'], [])] }));
check('escapado', conX.includes('&lt;x&gt;') && !conX.includes('<x>'), conX);
// Dev Tools, desde la URL de Dashboards.
check('dev tools', devToolsDe('https://1.2.3.4:5601/app/home#/') === 'https://1.2.3.4:5601/app/dev_tools#/console');
check('sin dashboards, sin link', devToolsDe('') === '');
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
""", encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_que_podes_mostrar_esta_conectado():
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function renderInfraView(data) {")
    vista = html[i:html.index("    async function hydrateActiveEnv() {", i)]
    assert "${funcionesHTML(data)}" in vista and "catalogoHTML" not in html and "verComandos" not in html
    assert "provisionCapabilitiesFromInfra(e.currentTarget, pipelines, false, { recomendados: true })" in vista
    assert "if (aplicar) return aplicarPlugin(aplicar, pipelines, data.catalogo);" in vista
    assert "if (e.target.closest('.fn__probar')) return abrirAsistente();" in vista
    assert "if (fila) return verFuncion(fila, data);" in vista
    assert "if (!det.hidden) pintarComandos(det, det.dataset.det);" in vista
    assert "body.querySelector('#infra-preguntar')?.addEventListener('click', abrirAsistente);" in vista
    # El paso 3 activa todo lo que hace falta: sin "recomendados" ni "elegí cuáles" (traían dudas).
    assert "'infra-capabilities-btn', activando ? 'Activando…' : 'Activar los plugins', 'Volver a activar los plugins'" in vista
    assert "recomendados" not in vista.replace("{ recomendados: true }", "") and "setup-paso__link" not in html
    assert "la búsqueda por significado" in vista
    # Los pedidos llevan la lista o los recomendados.
    assert "...(plugins ? { plugins } : {})," in html and "...(recomendados ? { recomendados: true } : {})," in html
    # De a uno: dos a la vez se pisan.
    assert "if (state.provisionandoPlugins) {" in html
    assert "fetch(`/api/v1/plugins/comandos?plugin=${encodeURIComponent(plugin)}`)" in html
    # La alerta sin detector, con el detector.
    assert "? ['anomalias', 'alertas'] : [c.plugin];" in html


def test_el_estado_dice_si_hay_una_activacion_en_curso(monkeypatch):
    """"Activar los plugins" arma los casos de a uno: a mitad de camino la
    vista decía "3 de 3 · Todo hecho" y "Sin activar" de lo que estaba en cola."""
    monkeypatch.setattr(main.runs, "running_id", lambda kind=None: "R1" if kind == "capabilities" else None)
    monkeypatch.setattr(main.runs, "get", lambda rid: {"id": rid, "created": 1700000000, "plugins": ["perfil"]})
    assert main._activando() == {"desde": 1700000000, "plugins": ["perfil"]}
    monkeypatch.setattr(main.runs, "running_id", lambda kind=None: None)
    assert main._activando() is None
    html = _INDEX.read_text(encoding="utf-8")
    assert "const capsDone = !activando && !!(data.capabilities && Object.keys(data.capabilities).length);" in html
    assert "vigilarActivacion(!!data.activando && !state.provisionandoPlugins);" in html
