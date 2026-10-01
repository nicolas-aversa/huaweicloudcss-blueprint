""""Entorno desplegado" en secciones (Resumen, Security Analytics, Anomaly
Detection) en vez de todo apilado: había que bajar mucho para llegar a las
anomalías. Se prueba en node con el código real de la vista."""
import pathlib
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
  { id: 'seguridad', label: 'Security Analytics', icon: 'shield', html: '' },
  { id: 'anomalias', label: 'Anomaly Detection', icon: 'activity', html: '<p>AD</p>' },
];
let h = infraSeccionesHTML(secs, '');
check('pestañas de las secciones con contenido', h.includes('data-infra-tab="resumen"') && h.includes('data-infra-tab="anomalias"'), h);
check('sin plugin, sin pestaña', !h.includes('data-infra-tab="seguridad"') && !h.includes('infra-panel-seguridad'), h);
check('la primera, activa', h.includes('id="infra-tab-resumen" data-infra-tab="resumen" aria-controls="infra-panel-resumen" aria-selected="true"'), h);
check('las otras, ocultas', h.includes('data-infra-panel="anomalias" role="tabpanel" aria-labelledby="infra-tab-anomalias" hidden>'), h);
check('la activa, visible', h.includes('data-infra-panel="resumen" role="tabpanel" aria-labelledby="infra-tab-resumen">'), h);
h = infraSeccionesHTML(secs, 'anomalias');
check('la guardada, activa', h.includes('data-infra-tab="anomalias" aria-controls="infra-panel-anomalias" aria-selected="true"')
  && h.includes('aria-labelledby="infra-tab-resumen" hidden>'), h);
h = infraSeccionesHTML(secs, 'seguridad');
check('guardada pero sin contenido: la primera', h.includes('data-infra-tab="resumen" aria-controls="infra-panel-resumen" aria-selected="true"'), h);
h = infraSeccionesHTML([secs[0], { ...secs[1] }], '');
check('una sola sección: sin pestañas', !h.includes('role="tablist"') && h.includes('<p>pipelines</p>') && !h.includes(' hidden>'), h);

// Elegir: marca la pestaña, muestra su panel y la recuerda.
const tab = (id) => ({ dataset: { infraTab: id }, classList: { on: false, toggle(_, v) { this.on = v; } }, attrs: {},
                       setAttribute(k, v) { this.attrs[k] = v; } });
const panel = (id) => ({ dataset: { infraPanel: id }, hidden: false });
const tabs = [tab('resumen'), tab('anomalias')];
const panels = [panel('resumen'), panel('anomalias')];
const raiz = { querySelectorAll: (sel) => sel === '[data-infra-tab]' ? tabs : panels };
elegirSeccionInfra(raiz, 'anomalias');
check('pestaña marcada', tabs[1].classList.on && tabs[1].attrs['aria-selected'] === 'true' && !tabs[0].classList.on && tabs[0].attrs['aria-selected'] === 'false');
check('panel visible', panels[1].hidden === false && panels[0].hidden === true);
check('recordada', guardado['infra-seccion'] === 'anomalias' && infraSeccionGuardada() === 'anomalias');
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
    # Banner y acciones arriba, siempre visibles; después las secciones.
    assert vista.index('<div class="infra-dash__banner">') < vista.index('id="infra-destroy-btn"') \
        < vista.index("${infraSeccionesHTML([")
    secciones = vista[vista.index("${infraSeccionesHTML(["):vista.index("], infraSeccionGuardada())}")]
    assert secciones.index("{ id: 'resumen'") < secciones.index("{ id: 'seguridad'") < secciones.index("{ id: 'anomalias'")
    resumen = secciones[:secciones.index("{ id: 'seguridad'")]
    for pieza in ("${setupPanel}", '<div class="pipe-list">${pipeRows}</div>', 'id="infra-capabilities-result"'):
        assert pieza in resumen, pieza
    assert "body.querySelector('.infra-tabs')?.addEventListener('click'" in vista
    assert "elegirSeccionInfra(body, t.dataset.infraTab);" in vista


def test_lo_que_se_recuerda_no_es_el_chat():
    """El chat vive solo en memoria (compliance); lo único guardado es qué
    pestaña estaba elegida."""
    f = _funciones(_INDEX.read_text(encoding="utf-8"))
    assert f.count("localStorage.getItem(") == 1 and f.count("localStorage.setItem(") == 1
    assert "_CLAVE_SECCION = 'infra-seccion'" in f
