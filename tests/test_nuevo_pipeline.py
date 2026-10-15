"""«Nuevo pipeline» (agregar un log al entorno desplegado) arranca sin nada
elegido. Visto: con el caso recién guardado todavía marcado en el paso 1,
`resetFlow` borraba sus campos pero dejaba la tarjeta elegida, y el paso 2
decía «No se detectaron campos estructurados en el log»."""
import pathlib
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_nuevo_pipeline_suelta_la_seleccion(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function resetFlow(reuse) {")
    fuente = html[i:html.index("    // ── Router de vistas a nivel app", i)]
    js = tmp_path / "reset.mjs"
    js.write_text(r"""
const state = { selectedExamples: ['pedidos-omnicanal'], fields: [{ field_path: 'x' }], rawLog: 'x' };
const elements = { rawLog: { value: 'x' } };
let aplicada = 0, limpiado = 0;
function clearMapping() { limpiado++; state.fields = []; }
function _applySelectionState() { aplicada++; }
const document = { getElementById: () => null };
""" + fuente + r"""
const f = [];
resetFlow(false);
if (state.selectedExamples.length !== 1 || aplicada) f.push('sin reuse no toca la selección');
resetFlow(true);
if (state.selectedExamples.length !== 0) f.push('elegidos: ' + JSON.stringify(state.selectedExamples));
if (aplicada !== 1) f.push('la grilla tiene que repintarse sin la tarjeta marcada');
console.log(f.join('\n')); process.exit(f.length ? 1 : 0);
""", encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_el_paso_2_recupera_los_campos_del_caso_elegido():
    """Si otro camino borra los campos con un solo caso elegido, el paso 2 usa
    los del caso (los tiene el catálogo) en vez de mostrar la tabla vacía."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("      if (isPredefinedDemo) {")
    bloque = html[i:html.index("renderMappingTable(state.fields);", i)]
    assert "EXAMPLE_DATA[state.selectedExamples[0]]" in bloque
    assert "state.fields = elegido.fields;" in bloque and "state.filterCode = elegido.filterCode" in bloque
