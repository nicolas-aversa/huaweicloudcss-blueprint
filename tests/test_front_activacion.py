"""El botón de "Activar los plugins" no se queda en "Activando…" para siempre.

La activación es un pedido de minutos y el botón se liberaba solo cuando
llegaba su respuesta. Una red que corta las conexiones largas sin avisar la
dejaba sin llegar nunca: en la ECS, la activación había terminado hacía 51
minutos y el botón seguía en "Activando…", sin poder volver a activar.

Se corre la función REAL del HTML (`provisionCapabilitiesFromInfra`) en node,
con un `fetch` cuya respuesta a la activación no llega nunca y un estado del
servidor que la muestra en curso y después terminada.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")

_ENTORNO = r"""
let ahora = 1_000_000;
Date.now = () => ahora;
let intervalos = [];
globalThis.setInterval = (fn) => { intervalos.push(fn); return intervalos.length; };
globalThis.clearInterval = (id) => { intervalos[id - 1] = null; };
const tick = () => new Promise(r => setTimeout(r, 0));
async function pasaElIntervalo(ms) {
  ahora += ms;
  for (const fn of intervalos) if (fn) await fn();
  await tick();
}

const state = { osUser: 'admin', lastStatus: {} };
let capChatAbierto = false;
const _NOMBRE_DEL_PLUGIN = {};
const escapeHtml = s => String(s);
const document = { getElementById: () => null, querySelectorAll: () => [] };
let avisos = [], hidratado = 0;
const toast = (m) => avisos.push(m);
const hydrateActiveEnv = async () => { hidratado++; };
const renderInfraView = () => {};
const renderCapabilitiesResult = () => {};
const showStatus = () => {};
const firmaDePlugins = p => JSON.stringify(p || null);

// El servidor: la activación responde (o no) y el estado dice si está en curso.
let estados = [], respuesta = null;
globalThis.fetch = (url, opciones = {}) => {
  if (url === '/api/v1/terraform/status') {
    const data = estados.length ? estados.shift() : { activando: null };
    return Promise.resolve({ ok: true, json: async () => data });
  }
  return new Promise((resolver, rechazar) => {
    if (respuesta) return resolver(respuesta);
    opciones.signal.addEventListener('abort', () =>
      rechazar(Object.assign(new Error('abortado'), { name: 'AbortError' })));
  });
};
"""

_ARNES = r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
const nuevoBoton = () => ({ disabled: false, innerHTML: 'Volver a activar los plugins', isConnected: true });
const casos = [{ slug: 'fix-oms', has_capabilities: true }];
let terminado = false;

// 1. La respuesta no llega nunca: el servidor la muestra en curso y después no.
{
  const btn = nuevoBoton();
  estados = [{ activando: { desde: 1, plugins: null } }, { activando: null }];
  terminado = false;
  const p = provisionCapabilitiesFromInfra(btn, casos, true).then(() => { terminado = true; });
  await tick();
  check('1: activando', state.provisionandoPlugins === true && btn.disabled && btn.innerHTML === 'Aplicando…');
  await pasaElIntervalo(30000);
  check('1: en curso en el servidor, sigue esperando', !terminado && state.provisionandoPlugins === true);
  await pasaElIntervalo(30000);
  await p;
  check('1: terminó sin la respuesta', terminado && state.provisionandoPlugins === false);
  check('1: el botón vuelve', !btn.disabled && btn.innerHTML === 'Volver a activar los plugins', btn.innerHTML);
  check('1: avisa', avisos.some(a => /terminó/.test(a) && /Actividad/.test(a)), avisos.join(' | '));
  check('1: repinta con el estado', hidratado === 1, hidratado);
  check('1: deja de mirar', intervalos.every(fn => fn === null));
}

// 2. El servidor todavía no la registró (baja lo anterior primero): no se corta
//    enseguida, pero tampoco se espera para siempre.
{
  intervalos = []; avisos = []; hidratado = 0;
  const btn = nuevoBoton();
  estados = [{ activando: null }, { activando: null }];
  terminado = false;
  const p = provisionCapabilitiesFromInfra(btn, casos, true).then(() => { terminado = true; });
  await tick();
  await pasaElIntervalo(30000);
  await pasaElIntervalo(30000);
  check('2: sin verla en curso, a los 60 s sigue', !terminado && state.provisionandoPlugins === true);
  await pasaElIntervalo(10 * 60 * 1000);
  await p;
  check('2: a los 10 min se suelta', terminado && state.provisionandoPlugins === false && !btn.disabled);
}

// 3. Una activación larga que sigue en curso no se corta.
{
  intervalos = []; avisos = []; hidratado = 0;
  const btn = nuevoBoton();
  estados = Array.from({ length: 40 }, () => ({ activando: { desde: 1, plugins: null } }));
  terminado = false;
  const p = provisionCapabilitiesFromInfra(btn, casos, true).then(() => { terminado = true; });
  await tick();
  for (let i = 0; i < 40; i++) await pasaElIntervalo(30000);
  check('3: 20 minutos en curso, sigue esperando', !terminado && state.provisionandoPlugins === true);
  estados = [{ activando: null }];
  await pasaElIntervalo(30000);
  await p;
  check('3: cuando termina, se suelta', terminado && !btn.disabled);
}

// 4. La respuesta llega: el camino de siempre, sin el aviso de "terminó".
{
  intervalos = []; avisos = []; hidratado = 0;
  const btn = nuevoBoton();
  respuesta = { ok: true, json: async () => ({ status: 'success', capabilities: {} }) };
  await provisionCapabilitiesFromInfra(btn, casos, true);
  check('4: listo', state.provisionandoPlugins === false && !btn.disabled && hidratado === 1);
  check('4: sin el aviso de la respuesta perdida', !avisos.some(a => /terminó/.test(a)), avisos.join(' | '));
  respuesta = null;
}

console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


def _funcion(html: str, firma: str) -> str:
    ini = html.index(firma)
    return html[ini:html.index("\n    }\n", ini) + len("\n    }\n")]


def _constante(html: str, nombre: str) -> str:
    return re.search(rf"^\s*const {nombre} = [^\n]+;$", html, re.M).group(0)


def test_el_boton_no_queda_activando_si_la_respuesta_no_llega(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    fuente = "\n".join([_constante(html, "_REFRESCO_PLUGINS_MS"), _constante(html, "_SIN_RESPUESTA_MS"),
                        _funcion(html, "    async function refrescarPluginsEnCurso("),
                        _funcion(html, "    function activacionTerminada("),
                        _funcion(html, "    async function provisionCapabilitiesFromInfra(")])
    js = tmp_path / "activacion.mjs"
    js.write_text(_ENTORNO + fuente + "\n" + _ARNES, encoding="utf-8")

    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)

    assert res.returncode == 0, "checks fallidos:\n" + (res.stdout or res.stderr)
