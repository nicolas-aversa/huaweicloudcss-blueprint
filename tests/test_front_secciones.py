""""Entorno desplegado" en secciones (Resumen, Plugins, Documentos) en vez de
todo apilado. Se prueba en node con el código real de la vista."""
import pathlib
import re
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


def _funciones(html: str) -> str:
    i = html.index("    const _CLAVE_SECCION = 'infra-seccion';")
    return html[i:html.index("    function renderInfraView(data) {", i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
let guardado = {};
let romper = false;
globalThis.localStorage = {
  getItem: (k) => { if (romper) throw new Error('bloqueado'); return guardado[k] ?? null; },
  setItem: (k, v) => { if (romper) throw new Error('bloqueado'); guardado[k] = v; },
};
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
const secs = [
  { id: 'resumen', label: 'Resumen', icon: 'database', html: '<p>pipelines</p>' },
  { id: 'documentos', label: 'Documentos', icon: 'file', html: '' },
  { id: 'plugins', label: 'Plugins', icon: 'layers', html: '<p>AD</p>' },
];
let h = infraSeccionesHTML(secs, '');
check('pestañas de las secciones con contenido', h.includes('data-infra-tab="resumen"') && h.includes('data-infra-tab="plugins"'), h);
check('sin plugin, sin pestaña', !h.includes('data-infra-tab="documentos"') && !h.includes('infra-panel-documentos'), h);
check('la primera, activa', h.includes('id="infra-tab-resumen" data-infra-tab="resumen" aria-controls="infra-panel-resumen" aria-selected="true"'), h);
check('las otras, ocultas', h.includes('data-infra-panel="plugins" role="tabpanel" aria-labelledby="infra-tab-plugins" hidden>'), h);
check('la activa, visible', h.includes('data-infra-panel="resumen" role="tabpanel" aria-labelledby="infra-tab-resumen">'), h);
h = infraSeccionesHTML(secs, 'plugins');
check('la guardada, activa', h.includes('data-infra-tab="plugins" aria-controls="infra-panel-plugins" aria-selected="true"')
  && h.includes('aria-labelledby="infra-tab-resumen" hidden>'), h);
h = infraSeccionesHTML(secs, 'documentos');
check('guardada pero sin contenido: la primera', h.includes('data-infra-tab="resumen" aria-controls="infra-panel-resumen" aria-selected="true"'), h);
h = infraSeccionesHTML([secs[0], { ...secs[1] }], '');
check('una sola sección: sin pestañas', !h.includes('role="tablist"') && h.includes('<p>pipelines</p>') && !h.includes(' hidden>'), h);

// Elegir: marca la pestaña, muestra su panel y la recuerda.
const tab = (id) => ({ dataset: { infraTab: id }, classList: { on: false, toggle(_, v) { this.on = v; } }, attrs: {},
                       setAttribute(k, v) { this.attrs[k] = v; } });
const panel = (id) => ({ dataset: { infraPanel: id }, hidden: false });
const tabs = [tab('resumen'), tab('plugins')];
const panels = [panel('resumen'), panel('plugins')];
const raiz = { querySelectorAll: (sel) => sel === '[data-infra-tab]' ? tabs : panels };
elegirSeccionInfra(raiz, 'plugins');
check('pestaña marcada', tabs[1].classList.on && tabs[1].attrs['aria-selected'] === 'true' && !tabs[0].classList.on && tabs[0].attrs['aria-selected'] === 'false');
check('panel visible', panels[1].hidden === false && panels[0].hidden === true);
check('recordada', guardado['infra-seccion'] === 'plugins' && infraSeccionGuardada() === 'plugins');
// El contador de cada pestaña (sin contador, nada).
h = infraSeccionesHTML([{ ...secs[0] }, { id: 'plugins', label: 'Plugins', icon: 'layers', cuenta: 9, html: '<p>AD</p>' }], '');
check('contador', h.includes('<span class="infra-tab__cuenta">9</span>') && h.split('infra-tab__cuenta').length === 2, h);

// Cada pestaña carga lo suyo al abrirla, una sola vez por render.
const clicks = [];
const boton = (n, slug) => ({ click: () => clicks.push(n), dataset: { slug } });
const panelDocs = { dataset: {}, querySelector: (sel) => sel === '#infra-documentos-actualizar' ? boton('documentos') : null };
const casos = [boton('siem', 'siem'), boton('pozos', 'produccion-pozos')];
const panelPlug = () => ({ dataset: {}, querySelector: (sel) => sel === '.plug__caso' ? casos[0] : null,
                           querySelectorAll: (sel) => sel === '.plug__caso' ? casos : [] });
let pp = panelPlug();
const raizC = { querySelector: (sel) => ({ '[data-infra-panel="documentos"]': panelDocs, '[data-infra-panel="plugins"]': pp })[sel] || null };
globalThis.state = {};
cargarSeccionInfra(raizC, 'documentos');
cargarSeccionInfra(raizC, 'documentos');
cargarSeccionInfra(raizC, 'plugins');
cargarSeccionInfra(raizC, 'resumen');
cargarSeccionInfra(raizC, undefined);
check('carga sola y una vez', JSON.stringify(clicks) === JSON.stringify(['documentos', 'siem']), JSON.stringify(clicks));
// Plugins: después de un refresco, el caso que se estaba mirando.
state.pluginsCaso = 'produccion-pozos';
pp = panelPlug();
cargarSeccionInfra(raizC, 'plugins');
check('vuelve al caso que se miraba', clicks[clicks.length - 1] === 'pozos', JSON.stringify(clicks));

romper = true;
check('sin storage no rompe', infraSeccionGuardada() === '');
elegirSeccionInfra(raiz, 'resumen');
check('sin storage igual cambia', panels[0].hidden === false && panels[1].hidden === true);
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_las_secciones_en_node(tmp_path):
    js = tmp_path / "secciones.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", _funciones(_INDEX.read_text(encoding="utf-8"))), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_la_vista_arma_sus_secciones_debajo_del_banner():
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function renderInfraView(data) {")
    vista = html[i:html.index("    async function hydrateActiveEnv() {", i)]
    # La franja del entorno con sus acciones y la puesta en marcha arriba,
    # siempre visibles; después las secciones.
    assert vista.index('<section class="env-bar">') < vista.index('id="infra-destroy-btn"') \
        < vista.index("${setupPanel}") < vista.index("${infraSeccionesHTML([")
    secciones = vista[vista.index("${infraSeccionesHTML(["):vista.index("], infraSeccionGuardada())}")]
    assert secciones.index("{ id: 'resumen'") < secciones.index("{ id: 'plugins'") < secciones.index("{ id: 'documentos'")
    assert [m for m in re.findall(r"\{ id: '(\w+)'", secciones)] == ["resumen", "plugins", "documentos"], \
        "las de cada plugin salieron: los resultados se miran en Dashboards"
    resumen = secciones[:secciones.index("{ id: 'plugins'")]
    for pieza in ("${kpis}", '<div class="pipe-list">${pipeRows}</div>', "${accesosHTML(pipelines)}"):
        assert pieza in resumen, pieza
    # El resultado de "Provisionar plugins", dentro de la puesta en marcha.
    k = vista.index("const setupPanel = ")
    setup = vista[k:vista.index("body.innerHTML = `", k)]
    assert 'id="infra-capabilities-result"' in setup
    assert "body.querySelector('.infra-tabs')?.addEventListener('click'" in vista
    assert "elegirSeccionInfra(body, t.dataset.infraTab);" in vista


def test_lo_que_se_recuerda_no_es_el_chat():
    """El chat vive solo en memoria (compliance); lo único guardado es qué
    pestaña estaba elegida."""
    f = _funciones(_INDEX.read_text(encoding="utf-8"))
    assert f.count("localStorage.getItem(") == 1 and f.count("localStorage.setItem(") == 1
    assert "_CLAVE_SECCION = 'infra-seccion'" in f


def test_los_numeros_y_los_chips_llevan_a_plugins():
    """El indicador de Plugins y los chips de cada caso abren la pestaña
    Plugins, en la tarjeta de ese caso."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function renderInfraView(data) {")
    vista = html[i:html.index("    async function hydrateActiveEnv() {", i)]
    for ir in ("kpi('plugins', 'Plugins', nPlugins,", "['Anomalías', enPlugins]", "[`Pronósticos · ${fc}`, enPlugins]",
               'data-ir="${ir}" data-slug="${escapeHtml(p.slug)}"'):
        assert ir in vista, ir
    irA = vista[vista.index("const irASeccion = (id) => {"):vista.index("body.querySelector('.infra-tabs')?.addEventListener")]
    assert "elegirSeccionInfra(body, id);" in irA and "cargarSeccionInfra(body, id);" in irA
    assert "if (k.dataset.slug) state.pluginsCaso = k.dataset.slug;" in vista
    assert "if (caso && !caso.classList.contains('is-active')) caso.click();" in vista
    assert "if (k && (e.key === 'Enter' || e.key === ' '))" in vista, "con teclado también"
    # La pestaña que quedó elegida carga al entrar.
    assert "cargarSeccionInfra(body, body.querySelector('[data-infra-tab].is-active')?.dataset.infraTab);" in vista
