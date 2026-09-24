"""El paso 3 de un dataset nuevo es solo el origen, ejercitado de verdad en node.

Un dataset nuevo termina en "Guardar": el destino (cluster, índice,
contraseña) lo pone quien lo despliegue desde la tarjeta del caso. Pedirlo en
el paso 3 era pedir datos que nadie iba a usar, y la contraseña obligatoria
trababa el paso. La excepción es "Ya están en la nube → Bucket OBS": no se
guarda y se despliega ahora, así que ahí el destino sigue.

Se corren las funciones REALES del HTML contra un DOM mínimo.
"""
import pathlib
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")

_ARNES = r"""
const hecho = {};
const el = (id) => hecho[id] || (hecho[id] = {
  id, textContent: '', hidden: false,
  classList: { toggle(c, on) { if (c === 'hidden') hecho[id].hidden = !!on; } },
});
let chipActivo = 'obs';
const pasos = [1, 2, 3, 4].map(n => ({
  dataset: { step: String(n) }, active: n === 3,
  label: { textContent: '' },
  querySelector() { return this.label; },
}));
const document = {
  getElementById: el,
  querySelector(sel) {
    if (sel === '#live-plugin .src-chip.is-active') return { dataset: { plugin: chipActivo } };
    if (sel === '#pipeline-substeps .nav-substep.active') return pasos.find(p => p.active);
    return null;
  },
  querySelectorAll(sel) { return sel === '#pipeline-substeps .nav-substep' ? pasos : []; },
};
const state = { deployMode: 'demo', sourceMode: 'file' };

const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
const rotulos = () => pasos.map(p => p.label.textContent).join(' | ');

// Demo: se despliega, hay destino.
syncModoGuardar();
check('demo: destino visible', hecho['step3-output-section'].hidden === false);
check('demo: título', hecho['step3-title-text'].textContent === 'Origen y destino');
check('demo: botón', hecho['generate-pipeline-label'].textContent === 'Revisar y desplegar');
check('demo: riel', rotulos() === 'Datos | Estructura | Origen y destino | Desplegar', rotulos());

// Dataset nuevo con archivo: se guarda, sin destino.
state.deployMode = 'custom';
syncModoGuardar();
check('guarda: destino oculto', hecho['step3-output-section'].hidden === true);
check('guarda: título', hecho['step3-title-text'].textContent === 'Origen');
check('guarda: botón', hecho['generate-pipeline-label'].textContent === 'Revisar y guardar');
check('guarda: paso 4', hecho['step4-title-text'].textContent === 'Revisar y guardar');
check('guarda: riel', rotulos() === 'Datos | Estructura | Origen | Guardar', rotulos());
check('guarda: header', hecho['wizard-step-label'].textContent === 'Origen',
      hecho['wizard-step-label'].textContent);
check('guarda: siguiente', hecho['wizard-step-next'].textContent === 'Siguiente: Guardar');

// Ya están en la nube → Kafka: también se guarda.
state.sourceMode = 'live'; chipActivo = 'kafka';
syncModoGuardar();
check('kafka: destino oculto', hecho['step3-output-section'].hidden === true);

// Ya están en la nube → Bucket OBS: no se guarda, se despliega ahora.
chipActivo = 'obs';
syncModoGuardar();
check('obs en vivo: destino visible', hecho['step3-output-section'].hidden === false);
check('obs en vivo: botón', hecho['generate-pipeline-label'].textContent === 'Revisar y desplegar');

// El .conf del paso 4 sin destino termina en el filter.
const conf = 'input {\n  s3 { }\n}\n\nfilter {\n  mutate { }\n}\n\noutput {\n  elasticsearch {\n    index => ""\n  }\n}';
const sin = sinDestino(conf);
check('sin destino: no hay output', !/output\s*\{/.test(sin), sin);
check('sin destino: queda el filter', sin.includes('filter {'));
check('sin destino: explica', sin.includes('se arma al desplegar el caso desde su tarjeta'));
check('sin output: igual', sinDestino('filter {\n}') === 'filter {\n}');

console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


def _funcion(html: str, firma: str) -> str:
    """Una función de nivel superior del script, entera (hasta su `}` de cierre)."""
    ini = html.index(firma)
    fin = html.index("\n    }\n", ini) + len("\n    }\n")
    return html[ini:fin]


def test_el_paso_3_de_un_dataset_nuevo_es_solo_el_origen(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    fuente = "\n".join(_funcion(html, f"    function {f}(") for f in (
        "livePlugin", "esObsEnVivo", "soloGuarda", "syncModoGuardar", "stepLabels", "sinDestino"))
    js = tmp_path / "modo.mjs"
    js.write_text(fuente + "\n" + _ARNES, encoding="utf-8")

    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)

    assert res.returncode == 0, "checks fallidos:\n" + (res.stdout or res.stderr)


def test_guardar_no_valida_el_destino():
    """La contraseña de OpenSearch era obligatoria aunque el campo no se usara."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("      if (s === 3) {")
    paso3 = html[i:html.index("return validateRequired(checks);", i)]
    assert paso3.index("if (soloGuarda())") < paso3.index("output-password")


def test_fuente_pasa_a_origen():
    html = _INDEX.read_text(encoding="utf-8")
    assert '<h3 class="section-subtitle">Origen (input)</h3>' in html
    assert "Fuente (input)" not in html and "Fuente y destino" not in html
    # El paso 1 deja de llamarse "Origen": ese nombre es del paso 3.
    assert '<span class="nav-substep__label">Datos</span>' in html
