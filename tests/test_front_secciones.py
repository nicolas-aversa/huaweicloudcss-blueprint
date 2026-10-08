""""Entorno desplegado" sin pestañas: los números, la matriz de casos y
plugins, y abajo accesos y snapshot. Las pestañas Resumen/Plugins dejaban
medio panel vacío (la lista de casos a la izquierda, el detalle al costado).
Se prueba en node con el código real de la vista."""
import pathlib
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


def _vista(html: str) -> str:
    i = html.index("    function renderInfraView(data) {")
    return html[i:html.index("    async function hydrateActiveEnv() {", i)]


def _funciones(html: str) -> str:
    i = html.index("    const _ESTADO_PLUGIN = {")
    return html[i:html.index('    // "Explicar" una anomalía', i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = {};
const _fmtPron = (v) => String(v);
const T = (plugin, estado, extra = {}) => ({ plugin, titulo: plugin, que: '', estado, motivo: '', filas: [], links: [], numero: '', ...extra });
const state = { lastStatus: { plugins: {
  siem: [T('dashboard', 'ok', { numero: 'x' }), T('perfil', 'ok'), T('forecasting', 'parcial')],
  cts: [T('dashboard', 'ok')],
  _cluster: [T('query_insights', 'ok', { numero: 'insights' })],
} } };
const pedidos = [];
globalThis.fetch = async (url) => { pedidos.push(url); return { ok: true, json: async () => ({}) }; };
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
// Un DOM mínimo: la tabla, una fila con su botón y su detalle por caso.
const clases = () => ({ s: new Set(), add(c) { this.s.add(c); }, remove(c) { this.s.delete(c); }, contains(c) { return this.s.has(c); },
                        toggle(c, v) { v ? this.add(c) : this.remove(c); } });
const cuerpo = () => ({
  dataset: {}, _h: '', tarjetas: [],
  set innerHTML(h) {
    this._h = h;
    this.tarjetas = [...h.matchAll(/<article class="plug[^"]*" data-plugin="([^"]+)"/g)].map(m => ({
      dataset: { plugin: m[1] }, vista: 0, foco: 0, scrollIntoView() { this.vista++; }, focus() { this.foco++; } }));
  },
  get innerHTML() { return this._h; },
  querySelector(sel) { return sel === '[data-numero], [data-verif]' && /data-(numero|verif)=/.test(this._h) ? {} : null; },
  querySelectorAll(sel) { return sel === '.plug' ? this.tarjetas : []; },
});
const tabla = { abrir: {}, filas: {}, dets: {} };
for (const slug of ['siem', 'cts', '_cluster']) {
  const fila = { classList: clases() };
  tabla.filas[slug] = fila;
  tabla.abrir[slug] = { dataset: { slug }, attrs: { 'aria-expanded': 'false' },
    getAttribute(k) { return this.attrs[k]; }, setAttribute(k, v) { this.attrs[k] = v; },
    closest(sel) { return sel === '.mtx' ? tabla : sel === '.mtx__fila' ? fila : null; } };
  const c = cuerpo();
  tabla.dets[slug] = { dataset: { det: slug }, hidden: true, c, querySelector(sel) { return sel === '.mtx__det-cuerpo' ? c : null; } };
}
tabla.querySelectorAll = (sel) => Object.values({ '.mtx__det': tabla.dets, '.mtx__abrir': tabla.abrir, '.mtx__fila': tabla.filas }[sel] || {});
const abierto = (s) => !tabla.dets[s].hidden && tabla.abrir[s].attrs['aria-expanded'] === 'true' && tabla.filas[s].classList.contains('is-abierta');

await verPlugins(tabla.abrir.siem);
check('abre el caso', abierto('siem') && state.pluginsCaso === 'siem');
check('con sus tarjetas', tabla.dets.siem.c.tarjetas.length === 3, tabla.dets.siem.c.innerHTML);
check('y sus números', JSON.stringify(pedidos) === JSON.stringify(['/api/v1/plugins/numeros?slug=siem']), JSON.stringify(pedidos));
await verPlugins(tabla.abrir.cts);
check('uno abierto a la vez', abierto('cts') && !abierto('siem') && tabla.dets.siem.hidden);
await verPlugins(tabla.abrir.cts);
check('tocarlo de nuevo lo cierra', !abierto('cts') && state.pluginsCaso === '');
const leidos = pedidos.length;
await verPlugins(tabla.abrir.siem, 'perfil');
const perfil = tabla.dets.siem.c.tarjetas.find(t => t.dataset.plugin === 'perfil');
check('una celda abre el caso en esa tarjeta', abierto('siem') && perfil.vista === 1 && perfil.foco === 1);
check('lo ya leído no se vuelve a pedir', pedidos.length === leidos && leidos === 2, JSON.stringify(pedidos));
await verPlugins(tabla.abrir.siem, 'forecasting');
const fc = tabla.dets.siem.c.tarjetas.find(t => t.dataset.plugin === 'forecasting');
check('otra celda del caso abierto no lo cierra', abierto('siem') && fc.foco === 1);
await verPlugins(tabla.abrir._cluster);
check('todo el cluster, sin slug', pedidos[pedidos.length - 1] === '/api/v1/plugins/numeros' && abierto('_cluster'), JSON.stringify(pedidos));
const sinTabla = { dataset: { slug: 'siem' }, closest: () => null };
await verPlugins(sinTabla);
check('fuera de la matriz, nada', state.pluginsCaso === 'siem' || state.pluginsCaso === '_cluster');
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_abrir_y_cerrar_casos_en_node(tmp_path):
    js = tmp_path / "matriz.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", _funciones(_INDEX.read_text(encoding="utf-8"))), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_la_vista_va_sin_pestanas():
    html = _INDEX.read_text(encoding="utf-8")
    vista = _vista(html)
    # La franja del entorno con sus acciones y la puesta en marcha arriba;
    # después los números, la matriz, y accesos y snapshot lado a lado.
    orden = ['<section class="env-bar">', 'id="infra-destroy-btn"', "${setupPanel}", "${kpis}",
             "${matrizDePluginsHTML(filas, data.plugins)}",
             '<div class="env-extras">${accesosHTML(pipelines)}${snapshotsDeDemoHTML()}</div>']
    posiciones = [vista.index(p) for p in orden]
    assert posiciones == sorted(posiciones), orden
    # El resultado de "Provisionar plugins", dentro de la puesta en marcha.
    k = vista.index("const setupPanel = ")
    assert 'id="infra-capabilities-result"' in vista[k:vista.index("body.innerHTML = `", k)]
    for viejo in ("infraSeccionesHTML", "data-infra-tab", "cargarSeccionInfra", "elegirSeccionInfra", "caso-card", "pipeRows"):
        assert viejo not in html, viejo


def test_no_se_guarda_nada_en_el_navegador():
    """El chat vive solo en memoria (compliance); y sin pestañas ya no hay
    una elegida que recordar."""
    vista = _vista(_INDEX.read_text(encoding="utf-8"))
    assert "localStorage" not in vista and "infra-seccion" not in vista


def test_el_numero_de_plugins_lleva_a_la_matriz():
    vista = _vista(_INDEX.read_text(encoding="utf-8"))
    assert "kpi('plugins', 'Plugins', nPlugins," in vista
    assert "const irALaMatriz = () => body.querySelector('#infra-plugins')?.scrollIntoView({ block: 'start', behavior: 'smooth' });" in vista
    assert "if (k && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); irALaMatriz(); }" in vista, "con teclado también"


_ARNES_REPARTO = r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
globalThis.document = { createElement: () => {
  const el = { className: '', hijos: [], appendChild(h) { this.hijos.push(h); } };
  return el; } };
const tarjeta = (alto, nombre) => ({ offsetHeight: alto, dataset: {}, nombre });
const grid = (ancho, tarjetas) => ({
  clientWidth: ancho, dataset: {}, clases: new Set(), props: {}, hijos: tarjetas,
  classList: { add(c) { grid.ultimo.clases.add(c); } },
  style: { setProperty(k, v) { grid.ultimo.props[k] = v; } },
  querySelectorAll(sel) { return sel === '.plug' ? this.hijos.flatMap(h => h.hijos ? h.hijos : [h]) : []; },
  replaceChildren(...h) { this.hijos = h; },
});
""" + "{FUNCION}" + r"""
const ts = [tarjeta(300, 'a'), tarjeta(100, 'b'), tarjeta(100, 'c'), tarjeta(100, 'd'), tarjeta(100, 'e')];
const g = grid(920, ts); grid.ultimo = g;
repartirTarjetas(g);
const cols = g.hijos.map(c => c.hijos.map(t => t.nombre).join(''));
check('tres columnas al ancho de 920', g.dataset.cols === '3' && g.props['--cols'] === '3' && g.clases.has('plug-grid--cols'), JSON.stringify(g.dataset));
check('a la columna más corta, en orden', JSON.stringify(cols) === JSON.stringify(['a', 'bd', 'ce']), JSON.stringify(cols));
// Que crezcan después no las mueve: con el mismo ancho no se reparte de nuevo.
ts[1].offsetHeight = 900;
repartirTarjetas(g);
check('crecer no las cambia de columna', JSON.stringify(g.hijos.map(c => c.hijos.map(t => t.nombre).join(''))) === JSON.stringify(cols));
check('el orden original queda guardado', ts.map(t => t.dataset.orden).join('') === '01234');
const uno = grid(300, [tarjeta(50, 'x')]); grid.ultimo = uno;
repartirTarjetas(uno);
check('angosto: una columna', uno.dataset.cols === '1');
repartirTarjetas(null);
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_las_tarjetas_se_reparten_una_vez_y_no_saltan(tmp_path):
    """En columnas CSS el navegador las rebalanceaba al llegar los números y la
    verificación: "se mueven los cuadrantes de lugar"."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    const _ANCHO_TARJETA = 290")
    fn = html[i:html.index("    let _repartoPendiente = null;", i)]
    js = tmp_path / "reparto.mjs"
    js.write_text(_ARNES_REPARTO.replace("{FUNCION}", fn), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "column-width" not in html[html.index("    .plug-grid {"):html.index("    .plug-col {")]


def test_el_conteo_y_un_backtest_que_termino_no_repintan_la_vista():
    """Repintar toda la vista cerraba y reabría el caso abierto: sus tarjetas
    se acomodaban de nuevo."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("async function contarDocumentos(")
    fn = html[i:html.index("function buildDeployBodyFromStatus(", i)]
    assert "renderInfraView" not in fn and "if (state.envActive) pintarConteoDeDocumentos();" in fn
    j = html.index("    async function verPlugins(btn, plugin = '') {")
    ver = html[j:html.index('    // "Explicar" una anomalía', j)]
    assert "hydrateActiveEnv" not in ver and "actualizarCeldasDelCaso(slug, tarjetas);" in ver
