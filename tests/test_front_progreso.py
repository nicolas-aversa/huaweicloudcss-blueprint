"""La barra del deploy: una global y una por componente, ejercitadas en node.

Se corren las funciones REALES del HTML (`progresoHTML`, `progresoEvento`,
`progresoFin`) contra un DOM mínimo, con la secuencia de eventos que manda el
backend: `plan` (los componentes, antes de empezar), `item` (uno que cambió) y
`progress` (el global).
"""
import pathlib
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")

_DOM = r"""
// Lo justo del DOM que usa el widget: clases, hijos, texto, estilo.
class El {
  constructor(cls = '') { this._cls = new Set(); this.className = cls; this.children = [];
                          this.dataset = {}; this.style = {}; this.textContent = ''; }
  set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._cls].join(' '); }
  get classList() {
    const s = this._cls;
    return { add: c => s.add(c), remove: c => s.delete(c), contains: c => s.has(c),
             toggle: (c, on) => { if (on === undefined) on = !s.has(c); on ? s.add(c) : s.delete(c); } };
  }
  set innerHTML(h) { this.children = [...String(h).matchAll(/class="([^"]+)"/g)].map(m => new El(m[1])); }
  appendChild(e) { this.children.push(e); return e; }
  _todos() { return this.children.flatMap(c => [c, ...c._todos()]); }
  querySelector(sel) { return this._todos().find(e => e._cls.has(sel.slice(1))) || null; }
  querySelectorAll(sel) { return this._todos().filter(e => e._cls.has(sel.slice(1))); }
}
const document = { createElement: () => new El() };
const escapeHtml = s => String(s);
"""

_ARNES = r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };

const raiz = new El();
raiz.innerHTML = progresoHTML('Iniciando…');
const fila = key => raiz.querySelector('.deploy-progress__list').children.find(li => li.dataset.key === key);
const texto = (key, cls) => fila(key).querySelector(cls).textContent;

// El plan: todos los componentes de entrada, en espera.
progresoEvento(raiz, { type: 'plan', items: [
  { key: 'nat', label: 'NAT gateway', percent: 0, done: false, estado: 'En espera' },
  { key: 'opensearch', label: 'CSS OpenSearch cluster', percent: 0, done: false, estado: 'En espera' },
  { key: 'pipeline:fintech', label: 'Pipeline · fintech', percent: 0, done: false, estado: 'En espera' },
]});
check('plan: tres filas', raiz.querySelector('.deploy-progress__list').children.length === 3);
check('plan: en espera', fila('opensearch').classList.contains('is-waiting'));
check('plan: texto', texto('opensearch', '.deploy-progress__estado') === 'En espera');
check('plan: nombre completo al pasar el mouse',
      fila('opensearch').querySelector('.deploy-progress__label').title === 'CSS OpenSearch cluster');

// Un componente que avanza.
progresoEvento(raiz, { type: 'item', key: 'opensearch', label: 'CSS OpenSearch cluster',
                       percent: 34.4, done: false, estado: 'Creando' });
check('item: ya no espera', !fila('opensearch').classList.contains('is-waiting'));
check('item: qué hace y cuánto', texto('opensearch', '.deploy-progress__estado') === 'Creando · 34%',
      texto('opensearch', '.deploy-progress__estado'));
check('item: su barra', fila('opensearch').querySelector('.deploy-progress__minifill').style.width === '34%');

// Otro que termina.
progresoEvento(raiz, { type: 'item', key: 'nat', label: 'NAT gateway',
                       percent: 100, done: true, estado: 'listo' });
check('listo: tilde', fila('nat').classList.contains('is-done'));
check('listo: texto', texto('nat', '.deploy-progress__estado') === 'Listo');

// Un componente que el plan no traía aparece igual.
progresoEvento(raiz, { type: 'item', key: 'otros', label: 'Otros recursos',
                       percent: 10, done: false, estado: 'Creando' });
check('nuevo: aparece', !!fila('otros'));

// El global.
progresoEvento(raiz, { type: 'progress', percent: 41.6, phase: 'CSS OpenSearch cluster',
                       message: 'Creando CSS OpenSearch cluster…' });
check('global: barra', raiz.querySelector('.deploy-progress__fill').style.width === '41.6%');
check('global: %', raiz.querySelector('.deploy-progress__pct').textContent === '42%');
check('global: fase', raiz.querySelector('.deploy-progress__phase').textContent === 'Creando CSS OpenSearch cluster…');

// Se corta: lo que no terminó queda interrumpido, lo que terminó sigue listo.
progresoFin(raiz, false);
check('corte: interrumpido', fila('opensearch').classList.contains('is-stalled'));
check('corte: texto', texto('opensearch', '.deploy-progress__estado') === 'Interrumpido');
check('corte: el listo sigue listo', fila('nat').classList.contains('is-done')
      && !fila('nat').classList.contains('is-stalled'));
check('corte: no marca 100%', raiz.querySelector('.deploy-progress__pct').textContent === '42%');

// Un plan nuevo en el mismo contenedor (otro deploy) reemplaza las filas.
const reuso = new El();
reuso.innerHTML = progresoHTML();
progresoEvento(reuso, { type: 'plan', items: [{ key: 'x', label: 'X', percent: 0, done: false, estado: 'En espera' },
                                              { key: 'y', label: 'Y', percent: 0, done: false, estado: 'En espera' }] });
progresoEvento(reuso, { type: 'plan', items: [{ key: 'z', label: 'Z', percent: 0, done: false, estado: 'En espera' }] });
const keys = reuso.querySelector('.deploy-progress__list').children.map(li => li.dataset.key).join(',');
check('plan nuevo: reemplaza', keys === 'z', keys);

// Termina bien: todo listo y al 100%.
const otra = new El();
otra.innerHTML = progresoHTML();
progresoEvento(otra, { type: 'plan', items: [{ key: 'a', label: 'A', percent: 50, done: false, estado: 'Creando' }] });
progresoFin(otra, true);
check('fin: 100%', otra.querySelector('.deploy-progress__pct').textContent === '100%');
check('fin: listo', otra.querySelector('.deploy-progress__item').classList.contains('is-done'));

console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


def _funcion(html: str, firma: str) -> str:
    ini = html.index(firma)
    return html[ini:html.index("\n    }\n", ini) + len("\n    }\n")]


def test_la_barra_global_y_las_de_cada_componente(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    fuente = "\n".join(_funcion(html, f"    function {f}(") for f in (
        "progresoHTML", "_progresoItem", "progresoEvento", "progresoFin"))
    js = tmp_path / "progreso.mjs"
    js.write_text(_DOM + fuente + "\n" + _ARNES, encoding="utf-8")

    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)

    assert res.returncode == 0, "checks fallidos:\n" + (res.stdout or res.stderr)


def test_los_tres_lugares_usan_el_mismo_widget():
    """La puesta en marcha, "Iniciar ingesta" y la reconexión tras un F5: antes
    las dos últimas eran una línea "mensaje (N%)" en un recuadro azul."""
    html = _INDEX.read_text(encoding="utf-8")
    assert "${progresoHTML('Iniciando…', 'deploy-progress')}" in html

    ingesta = _funcion(html, "    async function infraStartIngestion(")
    reparar = _funcion(html, "    async function infraRepararDnat(")
    reconexion = _funcion(html, "    async function _reconnectDeployJob(")
    seguir = _funcion(html, "    async function _seguirDeploy(")
    assert "progresoEvento(prog, d)" in seguir
    for nombre, fn in (("ingesta", ingesta), ("dnat", reparar), ("reconexión", reconexion)):
        assert "_progresoEnStatus(" in fn, nombre
        assert "progresoEvento(prog, d)" in fn or "_seguirDeploy(" in fn, nombre
        assert "(${Math.round(d.percent)}%)" not in fn, f"{nombre}: volvió la línea con el %"


def test_trabajando_no_es_un_recuadro_azul():
    html = _INDEX.read_text(encoding="utf-8")
    css = html[:html.index("</style>")]
    regla = css[css.index("    .status-loading {"):]
    regla = regla[:regla.index("}")]
    assert "59, 130, 246" not in regla and "#3b82f6" not in regla
    assert "var(--bg-primary)" in regla and "var(--border-subtle)" in regla
