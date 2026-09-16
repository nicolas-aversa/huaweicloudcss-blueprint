"""`caseMeta` del front, ejercitada de verdad en node.

Es la única función que decide **de qué bucket y con qué prefijo lee cada caso**,
y la comparten el preview del paso 4 y el body del deploy. Si se equivoca, el
wizard muestra una cosa y despliega otra — o manda CTS a buscar sus trazas al
bucket de demos, donde no están, y la pipeline no ingiere nada.

El resto del front no tiene tests: esta función los merece porque el error es
silencioso y apunta a datos reales. Se extrae del `index.html` y se corre con
node stubbeando lo poco que toca (el DOM, `LOG_EXAMPLES`, `demoBucket`).
"""
import pathlib
import shutil
import subprocess

import pytest

_RAIZ = pathlib.Path(__file__).resolve().parent.parent
_INDEX = _RAIZ / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node no está instalado")


_ARNES = r"""
const DOM = {};
const document = { getElementById: id => DOM[id] || null };
const state = { fields: [] };
function demoBucket() { return 'demos-del-sa'; }

// Tal como los emite `front_payload()`: solo CTS trae origen propio.
const LOG_EXAMPLES = [
  { id: 'cts', label: 'Traces de CTS', indexBase: 'cts',
    obsBucket: 'mi-tracker-cts', obsPrefix: 'CloudTraces/' },
  { id: 'siem', label: 'SIEM', indexBase: 'siem', obsBucket: '', obsPrefix: '' },
];
const EXAMPLE_DATA = { cts: { fields: [1, 2, 3] }, siem: { fields: [1] } };

const fallos = [];
const check = (nombre, cond, extra) => {
  if (!cond) fallos.push(nombre + (extra === undefined ? '' : ' -> ' + extra));
};

// Un vertical con origen propio gana sobre todo lo demás.
let m = caseMeta('cts');
check('cts bucket', m.bucket === 'mi-tracker-cts', m.bucket);
check('cts prefix', m.prefix === 'CloudTraces/', m.prefix);
check('cts ownBucket', m.ownBucket === 'mi-tracker-cts', m.ownBucket);
check('cts index', m.index === 'cts-%{+YYYY.MM}', m.index);

// Un caso de demo normal: bucket del SA y prefijo TOP-LEVEL por tipo.
m = caseMeta('siem');
check('siem bucket', m.bucket === 'demos-del-sa', m.bucket);
check('siem prefix', m.prefix === 'siem-logs/', m.prefix);
check('siem ownBucket vacio', m.ownBucket === '', JSON.stringify(m.ownBucket));

// Lo que el operador tipee NO puede desviar un caso con origen propio.
DOM['input-bucket'] = { value: 'lo-que-escribio-el-operador' };
check('cts ignora el input', caseMeta('cts').bucket === 'mi-tracker-cts',
      caseMeta('cts').bucket);
check('siem respeta el input',
      caseMeta('siem').bucket === 'lo-que-escribio-el-operador',
      caseMeta('siem').bucket);

// Custom sigue leyendo del form y nunca trae bucket propio.
DOM['input-prefix'] = { value: 'mis-logs/' };
DOM['output-index'] = { value: 'custom-%{+YYYY.MM}' };
m = caseMeta('custom');
check('custom prefix del form', m.prefix === 'mis-logs/', m.prefix);
check('custom sin bucket propio', m.ownBucket === '', JSON.stringify(m.ownBucket));
check('custom isCustom', m.isCustom === true);

console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


def _extraer_case_meta() -> str:
    """El cuerpo de `caseMeta` tal como está en el index.html."""
    html = _INDEX.read_text(encoding="utf-8")
    ini = html.index("    function caseMeta(id) {")
    # Hasta el cierre de la función, anclado en su return.
    ret = html.index("return { bucket, prefix, index, fieldsCount, ownBucket,", ini)
    return html[ini:html.index("\n    }", ret) + 6]


def test_case_meta_resuelve_el_origen_de_cada_caso(tmp_path):
    js = tmp_path / "case_meta.mjs"
    js.write_text(_extraer_case_meta() + _ARNES, encoding="utf-8")

    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)

    assert res.returncode == 0, "checks fallidos:\n" + (res.stdout or res.stderr)
