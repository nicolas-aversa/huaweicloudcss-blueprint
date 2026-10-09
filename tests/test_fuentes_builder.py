"""Dataset nuevo con varios archivos (un SIEM: firewall, WAF, auth…): cada
archivo es una fuente con su análisis, el paso 2 las muestra en pestañas, se
guardan como casos de una misma familia y el grid las muestra y las elige como
una sola tarjeta. Las funciones del front se corren en node."""
import json
import pathlib
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")


def _tramo(html: str, desde: str, hasta: str) -> str:
    i = html.index(desde)
    return html[i:html.index(hasta, i)]


def _correr(tmp_path, js: str) -> dict:
    f = tmp_path / "t.mjs"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(r.stdout)


def test_cada_fuente_guarda_y_recupera_su_analisis(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    muestra = _tramo(html, "    const _MUESTRA_FILAS = 200;", "    // ── Varias fuentes en un mismo dataset")
    fuentes = _tramo(html, "    const _DE_LA_FUENTE = {", "    async function _handleLogFiles(")
    js = """
const document = { getElementById: () => null };
const state = { fuentes: [], fuenteActiva: 0 };
""" + muestra + fuentes + """
const a = _fuenteDeTexto('fortigate.log', '# comentario\\nsrcip=1 a=2\\nsrcip=3 a=4\\n');
const b = _fuenteDeTexto('auth.log', 'linea 1\\nlinea 2');
state.fuentes = [a, b];
cargarFuente(0);
state.fields = [{ field_path: 'srcip' }];
state.seguridad = { reglas: [1] };
guardarFuenteActiva();
cargarFuente(1);
const enLaOtra = { fields: state.fields.length, rawLog: state.rawLog, seg: state.seguridad };
cargarFuente(0);
console.log(JSON.stringify({
  a: { lineas: a.logFileLines, raw: a.rawLog, muestra: a.muestra.split('\\n').length },
  enLaOtra,
  vuelta: { fields: state.fields.length, seg: state.seguridad, activa: state.fuenteActiva },
  nombres: [nombreDeFuente('siem-fortigate.log'), nombreDeFuente('sin_ext'), nombreDeFuente('')],
}));
"""
    r = _correr(tmp_path, js)
    assert r["a"] == {"lineas": 3, "raw": "srcip=1 a=2", "muestra": 3}, "la primera línea que no es comentario"
    assert r["enLaOtra"] == {"fields": 0, "rawLog": "linea 1", "seg": None}, "cada fuente lo suyo"
    assert r["vuelta"] == {"fields": 1, "seg": {"reglas": [1]}, "activa": 0}
    assert r["nombres"] == ["siem-fortigate", "sin_ext", "fuente"]


def test_la_familia_se_elige_y_se_suelta_entera(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    cargar = _tramo(html, "    function loadExample(id) {", "    // Carga TODOS los casos de demo")
    js = """
const state = { selectedExamples: ['otro'] };
const FAMILIAS = { 'fam:siem': { label: 'SIEM', miembros: ['siem-fw', 'siem-waf'] } };
const LOG_EXAMPLES = [{ id: 'otro' }, { id: 'siem-fw' }, { id: 'siem-waf' }];
function _applySelectionState() {}
""" + cargar + """
const pasos = [];
loadExample('fam:siem'); pasos.push(state.selectedExamples.slice());
loadExample('fam:siem'); pasos.push(state.selectedExamples.slice());
state.selectedExamples = ['siem-fw'];
loadExample('fam:siem'); pasos.push(state.selectedExamples.slice());
console.log(JSON.stringify(pasos));
"""
    assert _correr(tmp_path, js) == [["otro", "siem-fw", "siem-waf"], ["otro"], ["siem-fw", "siem-waf"]]


def test_el_front_cablea_las_fuentes():
    """Las piezas que no se pueden correr sin el DOM, en su lugar."""
    html = _INDEX.read_text(encoding="utf-8")
    assert 'id="log-file-input" accept=".log,.txt,.json,.csv,.ndjson,text/*" multiple' in html
    assert "_handleLogFiles(e.dataTransfer?.files)" in html and "_handleLogFiles(e.target.files)" in html
    gen = _tramo(html, "    async function runFilterGeneration() {", "    // Reuso: si el LLM ya analizó")
    assert "await analizarFuentes();" in gen
    guardar = _tramo(html, "    async function guardarFamilia() {", "\n    }\n")
    for clave in ("familia: label", "seguridad: f.seguridad", "log_content: f.logFileContent"):
        assert clave in guardar, clave
    assert "if ((state.fuentes || []).length > 1) {\n        try {\n          await guardarFamilia();" in html
    # Borrar una familia borra todas sus fuentes.
    assert "deleteCustomCase(FAMILIAS[slug].miembros, FAMILIAS[slug].label)" in html
