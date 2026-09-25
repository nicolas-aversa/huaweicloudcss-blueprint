"""El punto de "Entorno desplegado": amarillo mientras Terraform aplica.

Y la tarjeta "Activo · proyecto · N pipelines" ya no está en la barra.
"""
import pathlib
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"
HTML = _INDEX.read_text(encoding="utf-8")
CSS = HTML[:HTML.index("</style>")]


def _fn(firma: str) -> str:
    i = HTML.index(firma)
    return HTML[i:HTML.index("\n    }\n", i)]


def test_la_tarjeta_del_entorno_ya_no_esta_en_la_barra():
    assert "nav-env" not in HTML and "app-nav__env" not in HTML


def test_amarillo_con_su_token():
    assert "--accent-yellow: #e0a100;" in HTML
    assert ".nav-dot.is-aplicando { background: var(--accent-yellow); box-shadow: 0 0 0 3px var(--accent-yellow-tint); }" in CSS


def test_la_vista_pinta_el_punto_con_la_misma_funcion():
    vista = _fn("function renderInfraView(")
    assert "pintarPuntoEntorno();" in vista
    assert vista.index("state.envActive = active;") < vista.index("pintarPuntoEntorno();")
    assert "nav-infra-dot" not in vista, "un solo lugar decide el punto"


@pytest.mark.parametrize("firma", [
    "async function runDeploy(",
    "async function infraStartIngestion(",
    "async function infraRepararDnat(",
    "async function destroyEnvironment(",
    "async function _reconnectDeployJob(",
    "async function _vistaPreviaDeploy(",
])
def test_cada_corrida_de_terraform_lo_prende_y_lo_apaga(firma):
    fn = _fn(firma)
    assert fn.count("terraformEnCurso(true);") == 1, firma
    assert fn.count("terraformEnCurso(false);") == 1, firma
    prende = fn.index("terraformEnCurso(true);")
    apaga = fn.index("terraformEnCurso(false);")
    assert prende < apaga
    if firma != "async function _vistaPreviaDeploy(":
        # En un finally: un error no lo deja amarillo para siempre.
        assert fn.rindex("} finally {", 0, apaga) > prende, firma


def test_el_punto_en_node(tmp_path):
    """Con las funciones reales: verde, amarillo, superpuestas y sin entorno."""
    if shutil.which("node") is None:
        pytest.skip("node no está instalado")
    i = HTML.index("    let terraformCorriendo = 0;")
    fns = HTML[i:HTML.index("\n    }\n", HTML.index("function pintarPuntoEntorno", i)) + 7]
    arnes = r"""
const clases = new Set(['hidden']);
const dot = { title: '', classList: { toggle: (c, on) => { on ? clases.add(c) : clases.delete(c); } } };
const document = { getElementById: (id) => (id === 'nav-infra-dot' ? dot : null) };
const state = { envActive: false };
""" + fns + r"""
const fallos = [];
const check = (n, c) => { if (!c) fallos.push(n + ' -> ' + [...clases].join(',')); };
pintarPuntoEntorno();
check('sin entorno ni terraform: oculto', clases.has('hidden'));
terraformEnCurso(true);
check('deploy nuevo: amarillo', !clases.has('hidden') && clases.has('is-aplicando') && dot.title === 'Terraform aplicando…');
terraformEnCurso(true); terraformEnCurso(false);
check('superpuestas: sigue amarillo', clases.has('is-aplicando'));
state.envActive = true;
terraformEnCurso(false);
check('terminó con entorno: verde', !clases.has('hidden') && !clases.has('is-aplicando') && dot.title === 'Entorno activo');
terraformEnCurso(false);
check('de más no baja de cero', terraformCorriendo === 0);
terraformEnCurso(true);
check('después de de más, vuelve a amarillo', clases.has('is-aplicando'));
terraformEnCurso(false);
state.envActive = false; pintarPuntoEntorno();
check('destruido: oculto', clases.has('hidden'));
console.log(fallos.join('\n')); process.exit(fallos.length ? 1 : 0);
"""
    js = tmp_path / "punto.mjs"
    js.write_text(arnes, encoding="utf-8")
    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
