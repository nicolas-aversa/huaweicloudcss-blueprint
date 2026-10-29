"""Agregar una pipeline a un entorno activo no pide el usuario ni la contraseña.

La pipeline nueva escribe en el cluster que ya corre: el host lo pone
Terraform y la contraseña, el backend (la del cluster). El paso 3 la pedía
igual, obligatoria, y la que se tipeaba viajaba a Terraform: una distinta de
la real hacía que intentara cambiarle la contraseña al cluster.

Se corren las funciones REALES del HTML contra un DOM mínimo.
"""
import pathlib
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")

_ARNES = r"""
const valores = { 'output-protocol': 'http', 'output-hosts': '', 'output-index': 'mi-log-%{+YYYY.MM}',
                  'output-user': 'admin', 'output-password': 'Otra1234' };
const hecho = {};
const el = (id) => hecho[id] || (hecho[id] = {
  id, value: valores[id] ?? '', hidden: false,
  classList: { toggle(c, on) { if (c === 'hidden') hecho[id].hidden = !!on; } },
});
const document = { getElementById: el };
const state = { envActive: false, deployMode: 'demo', inputPlugin: 'beats', outputPlugins: ['opensearch'],
                selectedExamples: ['ventas'] };
let pedidos = [];
const validateRequired = (checks) => { pedidos = checks.map(c => c.id); return true; };
const clearAllFieldErrors = () => {};
const soloGuarda = () => false;
const demoBucket = () => 'bucket';
const toast = () => {};

const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };

// Desde cero: se piden, y va la que se tipeó.
syncDestinoDelEntorno();
check('cero: se ven', !hecho['output-credenciales'].hidden && hecho['output-del-entorno'].hidden);
validateStep(3);
check('cero: se validan', pedidos.includes('output-user') && pedidos.includes('output-password'), pedidos);
check('cero: va la tipeada', collectOutputConfig('opensearch').password === 'Otra1234');

// Con el entorno andando: ni se ven ni se validan, y la del form no viaja
// aunque el campo la tenga (la del primer deploy, o una tipeada antes).
state.envActive = true;
syncDestinoDelEntorno();
check('entorno: no se ven', hecho['output-credenciales'].hidden && !hecho['output-del-entorno'].hidden);
validateStep(3);
check('entorno: el índice sí', pedidos.includes('output-index'), pedidos);
check('entorno: no se validan', !pedidos.includes('output-user') && !pedidos.includes('output-password'), pedidos);
const c = collectOutputConfig('opensearch');
check('entorno: viaja vacía', c.password === '' && c.user === 'admin', JSON.stringify(c));
check('entorno: el índice viaja', c.index === 'mi-log-%{+YYYY.MM}', c.index);

console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


def _funcion(html: str, firma: str) -> str:
    """Una función de nivel superior del script, entera (hasta su `}` de cierre)."""
    ini = html.index(firma)
    fin = html.index("\n    }\n", ini) + len("\n    }\n")
    return html[ini:fin]


def test_al_entorno_activo_no_se_piden_ni_viajan_las_credenciales(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    fuente = "\n".join(_funcion(html, f"    function {f}(") for f in (
        "syncDestinoDelEntorno", "validateStep", "collectOutputConfig"))
    js = tmp_path / "destino.mjs"
    js.write_text(fuente + "\n" + _ARNES, encoding="utf-8")

    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)

    assert res.returncode == 0, "checks fallidos:\n" + (res.stdout or res.stderr)


def test_cambia_con_el_entorno():
    """El entorno se conoce después de pintar el paso 3 (llega del estado del
    servidor) y el form se re-crea al cambiar de plugin: las dos cosas
    vuelven a acomodarlo."""
    html = _INDEX.read_text(encoding="utf-8")
    render = _funcion(html, "    function renderOutputConfig(")
    assert "syncDestinoDelEntorno();" in render
    i = html.index("state.envActive = active;")
    assert "syncDestinoDelEntorno();" in html[i:i + 200]
