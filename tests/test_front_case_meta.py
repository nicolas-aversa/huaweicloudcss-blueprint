"""El origen de cada caso, del backend al `.conf`, ejercitado de verdad en node.

`caseMeta` es la única función que decide **de qué bucket y con qué prefijo lee
cada caso**, y la comparten el preview del paso 4 y el body del deploy. Si se
equivoca, el wizard muestra una cosa y despliega otra — o manda CTS a buscar sus
trazas al bucket de demos, donde no están, y la pipeline no ingiere nada.

Este test ya existía y **no atrapó exactamente ese bug**: stubeaba `LOG_EXAMPLES`
a mano, con un comentario que decía "tal como los emite `front_payload()`", e
incluía el `obsBucket` que el front en realidad perdía al proyectar el payload.
El stub reemplazaba justo la pieza rota. Ahora el arnés corre la cadena
completa: el payload REAL de `verticals.front_payload()` → el
`applyVerticalsPayload` REAL extraído del HTML → `caseMeta`.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

import verticals

_RAIZ = pathlib.Path(__file__).resolve().parent.parent
_INDEX = _RAIZ / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node no está instalado")


_ARNES = r"""
const DOM = {};
const document = { getElementById: id => DOM[id] || null };
const state = { fields: [], selectedExamples: [], obsCreds: { ak: 'AK' }, osPassword: '',
                outputPlugins: ['elasticsearch'], rawLog: '' };
const window = { __VERTICALS__: PAYLOAD };
function demoBucket() { return 'demos-del-sa'; }
function hwRegion() { return 'la-south-2'; }
function secretFieldValue() { return ''; }
function collectOutputConfig() { return { user: 'admin', password: 'p', ssl: true }; }
function slugFromIndex(i) { return (i || '').split('-')[0]; }
function pipelineNamespace() { return 'data'; }
// Un <input> como el del paso 3: `dataset` y `readOnly` de verdad, que es lo
// que usa `_applyCaseSourceDest` para no pisar el bucket compartido.
const campo = (value) => ({ value, dataset: {}, readOnly: false, title: '' });

const fallos = [];
const check = (nombre, cond, extra) => {
  if (!cond) fallos.push(nombre + (extra === undefined ? '' : ' -> ' + extra));
};

// La proyección del payload no puede perder campos: perdió `obsBucket`/
// `obsPrefix` (CTS leyendo del bucket equivocado) y `caseType`/`inputPlugin`
// (un caso Kafka mostrando el form de OBS), las dos veces en silencio.
const visibles = PAYLOAD.verticals.filter(v => !v.hidden);
const perdidos = [];
for (const v of visibles) {
  const e = LOG_EXAMPLES.find(x => x.id === v.slug) || {};
  for (const k of Object.keys(v)) if (!(k in e)) perdidos.push(v.slug + '.' + k);
}
check('proyeccion sin perdida', perdidos.length === 0, perdidos.join(', '));
check('cts tiene card', !!LOG_EXAMPLES.find(e => e.id === 'cts'));

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
DOM['input-bucket'] = campo('lo-que-escribio-el-operador');
check('cts ignora el input', caseMeta('cts').bucket === 'mi-tracker-cts',
      caseMeta('cts').bucket);
check('siem respeta el input',
      caseMeta('siem').bucket === 'lo-que-escribio-el-operador',
      caseMeta('siem').bucket);

// `casoConOrigenPropio` es de donde sale el origen en el camino de UN caso,
// que viaja sin `cases[]` y antes leía el `<input>` del paso 3 — un re-render
// (guardar ⚙ Configuración) bastaba para devolverlo al bucket de demos.
state.selectedExamples = ['cts'];
check('single cts', (casoConOrigenPropio() || {}).bucket === 'mi-tracker-cts',
      JSON.stringify(casoConOrigenPropio()));
check('single cts prefix', (casoConOrigenPropio() || {}).prefix === 'CloudTraces/');
state.selectedExamples = ['siem'];
check('single siem sin origen propio', casoConOrigenPropio() === null);
state.selectedExamples = ['custom'];
check('custom sin origen propio', casoConOrigenPropio() === null);
state.selectedExamples = [];
check('sin seleccion', casoConOrigenPropio() === null);

// `collectInputConfig` arma el input del `.conf` que se despliega. Con CTS
// seleccionado tiene que ignorar lo que haya en el form —que es justamente lo
// que quedaba mal tras cualquier re-render— y usar el origen del caso.
state.deployMode = 'demo';
state.inputPlugin = 'obs';
state.readExistingMode = true;
DOM['input-bucket'] = campo('demos-del-sa');
DOM['input-prefix'] = campo('cts-logs/');
state.selectedExamples = ['cts'];
let cfg = collectInputConfig();
check('conf de cts: bucket', cfg.bucket === 'mi-tracker-cts', cfg.bucket);
check('conf de cts: prefix', cfg.prefix === 'CloudTraces/', cfg.prefix);
check('conf de cts: read-only', cfg.delete === false && cfg.watch_for_new_files === false);
state.selectedExamples = ['siem'];
cfg = collectInputConfig();
check('conf de siem: bucket del form', cfg.bucket === 'demos-del-sa', cfg.bucket);
check('conf de siem: prefix del form', cfg.prefix === 'cts-logs/', cfg.prefix);

// El redeploy tras un F5 se rearma desde /terraform/status, sin el form: el
// bucket estaba fijo en el de demos y mandaba a CTS a buscar sus trazas ahí.
state.selectedExamples = [];
let body = buildDeployBodyFromStatus({ pipelines: [{ slug: 'cts', index: 'cts-%{+YYYY.MM}', obs_prefix: 'CloudTraces/' }] });
check('redeploy de cts: bucket', body.obs_bucket === 'mi-tracker-cts', body.obs_bucket);
check('redeploy de cts: prefix', body.obs_prefix === 'CloudTraces/', body.obs_prefix);
body = buildDeployBodyFromStatus({ pipelines: [{ slug: 'siem', index: 'siem-%{+YYYY.MM}', obs_prefix: 'siem-logs/' }] });
check('redeploy de siem: bucket de demos', body.obs_bucket === 'demos-del-sa', body.obs_bucket);

// ── Varios casos a la vez, uno de ellos CTS ────────────────────────────────
// El bucket de CTS no es el de los demás. Mientras lo fue, los casos de demo
// salían leyendo `mi-tracker-cts` —donde no está su dataset— y el guard que
// sube los datasets que faltan se los escribía ahí, encima de las trazas.
state.selectedExamples = ['cts', 'siem'];
DOM['input-bucket'] = campo('demos-del-sa');
DOM['input-prefix'] = campo('siem-logs/');
DOM['output-index'] = campo('siem-%{+YYYY.MM}');

check('multi: sin origen propio del deploy', casoConOrigenPropio() === null,
      JSON.stringify(casoConOrigenPropio()));

// La pestaña de CTS muestra su bucket, bloqueado, sin pisar el compartido.
_applyCaseSourceDest('cts');
check('tab cts: muestra el suyo', DOM['input-bucket'].value === 'mi-tracker-cts',
      DOM['input-bucket'].value);
check('tab cts: bloqueado', DOM['input-bucket'].readOnly === true);
check('tab cts: guarda el compartido', DOM['input-bucket'].dataset.shared === 'demos-del-sa',
      DOM['input-bucket'].dataset.shared);
check('tab cts: siem sigue en el suyo', caseMeta('siem').bucket === 'demos-del-sa',
      caseMeta('siem').bucket);
check('tab cts: el body no se lleva el ajeno',
      collectDeployBody(false).obs_bucket === 'demos-del-sa',
      collectDeployBody(false).obs_bucket);
check('tab cts: cada caso con su bucket',
      JSON.stringify(collectDeployBody(false).cases.map(c => c.slug + '=' + c.obs_bucket))
        === JSON.stringify(['cts=mi-tracker-cts', 'siem=']),
      JSON.stringify(collectDeployBody(false).cases.map(c => c.slug + '=' + c.obs_bucket)));

// Y al volver a una pestaña sin origen propio, el campo vuelve a ser editable.
_applyCaseSourceDest('siem');
check('tab siem: vuelve el compartido', DOM['input-bucket'].value === 'demos-del-sa',
      DOM['input-bucket'].value);
check('tab siem: editable', DOM['input-bucket'].readOnly === false);
check('tab siem: sin resto guardado', DOM['input-bucket'].dataset.shared === undefined);

// El input del `.conf` compartido tampoco se lleva el bucket de CTS.
cfg = collectInputConfig();
check('multi: input del form', cfg.bucket === 'demos-del-sa', cfg.bucket);

// Redeploy tras un F5 con las dos pipelines: ídem.
body = buildDeployBodyFromStatus({ pipelines: [
  { slug: 'cts', index: 'cts-%{+YYYY.MM}', obs_prefix: 'CloudTraces/' },
  { slug: 'siem', index: 'siem-%{+YYYY.MM}', obs_prefix: 'siem-logs/' },
]});
check('redeploy multi: bucket compartido', body.obs_bucket === 'demos-del-sa', body.obs_bucket);
check('redeploy multi: cts con el suyo', body.cases[0].obs_bucket === 'mi-tracker-cts');
check('redeploy multi: siem sin propio', body.cases[1].obs_bucket === '');

// Dataset nuevo con archivo: el paso 3 ya no tiene campos de bucket (el
// archivo va al bucket de demos). Leer el form vacío mandaba `bucket => ""` y
// "Revisar y guardar" fallaba.
state.selectedExamples = ['custom'];
state.deployMode = 'custom';
state.sourceMode = 'file';
state.obsCreds = { ak: 'AK' };
DOM['input-bucket'] = null;
DOM['input-prefix'] = null;
cfg = collectInputConfig();
check('archivo: bucket de demos', cfg.bucket === 'demos-del-sa', cfg.bucket);
check('archivo: prefijo del caso', cfg.prefix === '‹slug›-logs/', cfg.prefix);
state.sourceMode = 'live';
DOM['input-bucket'] = campo('bucket-del-cliente');
DOM['input-prefix'] = campo('logs/');
cfg = collectInputConfig();
check('en la nube: lo del form', cfg.bucket === 'bucket-del-cliente', cfg.bucket);
state.deployMode = 'demo';

// Custom sigue leyendo del form y nunca trae bucket propio.
state.selectedExamples = [];
DOM['input-bucket'] = campo('demos-del-sa');
DOM['input-prefix'] = campo('mis-logs/');
DOM['output-index'] = campo('custom-%{+YYYY.MM}');
state.selectedExamples = [];
m = caseMeta('custom');
check('custom prefix del form', m.prefix === 'mis-logs/', m.prefix);
check('custom sin bucket propio', m.ownBucket === '', JSON.stringify(m.ownBucket));
check('custom isCustom', m.isCustom === true);

console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


def _trozo(html: str, desde: str, hasta: str) -> str:
    ini = html.index(desde)
    return html[ini:html.index(hasta, ini)]


def _fuente_del_front() -> str:
    """El catálogo y todo lo que resuelve el origen, tal cual está en el HTML.

    Son las cuatro piezas de la cadena, y cada una falló por su cuenta alguna
    vez: la proyección del payload, `caseMeta`, el input que termina en el
    `.conf` y el body que se rearma tras un refresh.
    """
    html = _INDEX.read_text(encoding="utf-8")
    partes = [
        # Las declaraciones + applyVerticalsPayload (hasta su invocación al boot).
        _trozo(html, "    let _VDATA = ", "    applyVerticalsPayload(window.__VERTICALS__);"),
        _trozo(html, "    function sharedBucket() {", "    // Configuration file REAL de un tipo"),
        _trozo(html, "    function _applyCaseSourceDest(id) {",
               "    // Selecciona el plugin de input"),
        _trozo(html, "    // Collect input config", "    // Collect output config"),
        _trozo(html, "    function collectDeployBody(startIngestion) {",
               "    // Extrae un mensaje legible del `detail`"),
        _trozo(html, "    function buildDeployBodyFromStatus(data) {",
               "    // Paso: aplica index template"),
    ]
    return "\n".join(partes)


def test_case_meta_resuelve_el_origen_de_cada_caso(tmp_path):
    js = tmp_path / "case_meta.mjs"
    # El payload REAL del backend, no un stub: si el front deja de copiar un
    # campo, o el backend deja de emitirlo, este test se entera.
    payload = json.dumps(verticals.front_payload(), ensure_ascii=False)
    js.write_text(f"const PAYLOAD = {payload};\n" + _fuente_del_front()
                  + "\napplyVerticalsPayload(PAYLOAD);\n" + _ARNES, encoding="utf-8")

    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)

    assert res.returncode == 0, "checks fallidos:\n" + (res.stdout or res.stderr)


def test_el_backend_declara_el_origen_propio_de_cts():
    """La otra mitad del contrato: si esto cambia, el test de arriba miente."""
    cts = next(v for v in verticals.front_payload()["verticals"] if v["slug"] == "cts")

    assert cts["obsBucket"] == "mi-tracker-cts"
    assert cts["obsPrefix"] == "CloudTraces/"
    assert not cts["hidden"], "sin card, el caso no se puede desplegar desde el grid"
