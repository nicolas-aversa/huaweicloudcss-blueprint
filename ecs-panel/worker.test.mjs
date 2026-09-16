/**
 * Ejercita el Worker de verdad: Web Crypto real, fetch interceptado.
 *
 * Lo importante acá es lo que los tests de Python no pueden ver: que la cookie
 * que firma el Worker sea EXACTAMENTE la misma que valida el panel (mismo
 * formato que auth.py), que una contraseña incorrecta no entregue sesión, y que
 * las acciones sin cookie no lleguen nunca a Huawei.
 */
import worker from "./worker.js";

const ENV = {
  PANEL_PASSWORD: "una-password-larga-y-aleatoria",
  PANEL_SECRET: "un-secreto-largo-para-firmar",
  HW_USER: "panel-bot", HW_DOMAIN: "cuenta", HW_PASSWORD: "pw",
  HW_REGION: "la-south-2", HW_PROJECT_ID: "proj-1",
  FUNCTION_URN: "urn:fss:la-south-2:proj:function:default:ecs-panel:latest",
};

let llamadas = [];
let estadoFalso = { ecs: "SHUTOFF", ports: false, app_url: "https://1.2.3.4.sslip.io" };

globalThis.fetch = async (url, opts = {}) => {
  llamadas.push({ url: String(url), method: opts.method });
  if (String(url).includes("/v3/auth/tokens")) {
    return new Response("{}", { status: 201, headers: { "X-Subject-Token": "TOK-IAM" } });
  }
  if (String(url).includes("/invocations")) {
    if (opts.headers["X-Auth-Token"] !== "TOK-IAM") {
      return new Response("sin token", { status: 401 });
    }
    const accion = JSON.parse(opts.body).action;
    if (accion === "start") { estadoFalso = { ...estadoFalso, ecs: "ACTIVE", ports: true }; }
    if (accion === "stop") { estadoFalso = { ...estadoFalso, ecs: "SHUTOFF", ports: false }; }
    const cuerpo = accion === "status" ? estadoFalso : { ok: true, message: "listo", ecs: estadoFalso.ecs };
    return new Response(JSON.stringify({ result: JSON.stringify(cuerpo) }), { status: 200 });
  }
  return new Response("?", { status: 404 });
};

const BASE = "https://panel.example.workers.dev/";
const pedir = (opts = {}) => worker.fetch(new Request(BASE + (opts.qs || ""), {
  method: opts.method || "GET",
  headers: opts.cookie ? { Cookie: "panel=" + opts.cookie } : {},
  body: opts.body,
}), ENV);

let fallos = 0;
function check(nombre, cond, extra = "") {
  if (!cond) { fallos++; console.log("  FALLA   " + nombre + (extra ? " — " + extra : "")); }
  else console.log("  ok      " + nombre);
}
const cookieDe = res => (res.headers.get("Set-Cookie") || "").split("panel=")[1]?.split(";")[0] || "";

console.log("── sin sesión ──");
let r = await pedir();
check("la raíz devuelve el login", (await r.clone().text()).includes("type=password"));
check("el login no expone el panel", !(await r.text()).includes("Encender"));

llamadas = [];
for (const a of ["status", "start", "stop"]) {
  r = await pedir({ qs: "?a=" + a, method: "POST" });
  check(`${a} sin cookie → 401`, r.status === 401);
}
check("NINGUNA acción sin sesión tocó Huawei", llamadas.length === 0, JSON.stringify(llamadas));

console.log("── login ──");
const form = p => { const f = new URLSearchParams(); f.set("password", p); return f; };
r = await pedir({ qs: "?a=login", method: "POST", body: form("incorrecta") });
check("password incorrecta → 401", r.status === 401);
check("password incorrecta no entrega cookie", !r.headers.get("Set-Cookie"));

r = await pedir({ qs: "?a=login", method: "POST", body: form(ENV.PANEL_PASSWORD) });
check("password correcta → 200", r.status === 200);
const cookie = cookieDe(r);
check("entrega una cookie", !!cookie);
const sc = r.headers.get("Set-Cookie") || "";
check("la cookie es HttpOnly + Secure + SameSite=Strict",
  sc.includes("HttpOnly") && sc.includes("Secure") && sc.includes("SameSite=Strict"));
check("la respuesta ya es el panel", (await r.text()).includes("Encender"));

console.log("── con sesión ──");
r = await pedir({ qs: "?a=status", cookie });
let d = await r.json();
check("status responde el estado", d.ecs === "SHUTOFF" && d.ports === false, JSON.stringify(d));
check("status trae el link a la plataforma", d.app_url === "https://1.2.3.4.sslip.io");

r = await pedir({ qs: "?a=start", cookie, method: "GET" });
check("start por GET → 405 (un prefetch no puede encender)", r.status === 405);

r = await pedir({ qs: "?a=start", cookie, method: "POST" });
check("start por POST funciona", (await r.json()).ok === true);
d = await (await pedir({ qs: "?a=status", cookie })).json();
check("tras encender: ACTIVE + puertos abiertos", d.ecs === "ACTIVE" && d.ports === true);

await pedir({ qs: "?a=stop", cookie, method: "POST" });
d = await (await pedir({ qs: "?a=status", cookie })).json();
check("tras apagar: SHUTOFF + puertos cerrados", d.ecs === "SHUTOFF" && d.ports === false);

console.log("── cookies falsas ──");
for (const [nombre, mala] of [
  ["firma inválida", cookie.split(".")[0] + ".0000"],
  ["sin punto", "basura"],
  ["vacía", ""],
  ["payload alterado", btoa("panel|99999999999").replace(/=+$/, "") + "." + cookie.split(".")[1]],
]) {
  r = await pedir({ qs: "?a=status", cookie: mala });
  check(`${nombre} → 401`, r.status === 401);
}

console.log("── el token IAM se cachea ──");
llamadas = [];
for (let i = 0; i < 4; i++) await pedir({ qs: "?a=status", cookie });
const tokens = llamadas.filter(l => l.url.includes("/v3/auth/tokens")).length;
check("un solo pedido de token para 4 llamadas", tokens === 0 || tokens === 1, `fueron ${tokens}`);

console.log("── la CSP no puede bloquear a la propia página ──");
// Con `default-src 'none'` a secas, el navegador bloquea el fetch de la página
// hacia ?a=status y el panel queda colgado en "Consultando…". Esto no se ve sin
// un browser de verdad, así que al menos se fija que las directivas estén.
r = await pedir({ cookie });
const csp = r.headers.get("Content-Security-Policy") || "";
check("declara connect-src (el fetch de status)", /connect-src\s+'self'/.test(csp), csp);
check("declara form-action (el POST del login)", /form-action\s+'self'/.test(csp), csp);
check("sigue sin permitir recursos externos", csp.includes("default-src 'none'"), csp);

console.log("── la página no filtra secretos ──");
const panelHtml = await (await pedir({ cookie })).text();
const loginHtml = await (await pedir()).text();
for (const [k, v] of Object.entries(ENV)) {
  if (!v || k === "HW_REGION" || k === "HW_PROJECT_ID" || k === "FUNCTION_URN") continue;
  check(`${k} no aparece en el HTML`, !panelHtml.includes(v) && !loginHtml.includes(v));
}

console.log("── el JS del panel, contra una ECS que TARDA ──");
// Hasta acá nada ejecutaba el script del panel: vive dentro de un template
// string y los tests solo miraban la capa HTTP. Por eso sobrevivió el bug de
// "el panel se congela": el doble de fetch de arriba hace la transición
// INSTANTÁNEA, que es justo lo contrario de la realidad, y encima la afirma como
// correcta. Acá se extrae el script y se corre con un DOM mínimo, timers
// controlados y un backend que tarda tres consultas en encender.
{
  const js = panelHtml.split("<script>")[1].split("</script>")[0];

  const nodos = {};
  const nodo = () => ({ textContent: "", className: "", innerHTML: "", disabled: false });
  for (const id of ["estado", "puertos", "dot", "on", "off", "url", "msg"]) nodos[id] = nodo();

  let ahora = 0;
  const timers = [];        // {id, cuando, cada, fn}
  let sigId = 1;
  const avanzar = async (ms) => {
    const hasta = ahora + ms;
    for (;;) {
      const t = timers.filter(t => t.cuando <= hasta).sort((a, b) => a.cuando - b.cuando)[0];
      if (!t) break;
      ahora = t.cuando;
      if (t.cada) t.cuando += t.cada; else timers.splice(timers.indexOf(t), 1);
      t.fn();
      await new Promise(r => setImmediate(r));   // dejar correr los await del fetch
    }
    ahora = hasta;
  };

  // La ECS tarda: durante el powering-on la API sigue diciendo SHUTOFF, y el
  // backend lo traduce a TRANSICION (que es el arreglo de index.py).
  let consultas = 0, encendiendo = false;
  const backend = {
    status: () => {
      if (!encendiendo) return { ecs: "SHUTOFF", ports: false, app_url: "https://x" };
      consultas++;
      return consultas < 3
        ? { ecs: "TRANSICION", ports: true, app_url: "https://x" }
        : { ecs: "ACTIVE", ports: true, app_url: "https://x" };
    },
    start: () => { encendiendo = true; return { ok: true, message: "Encendiendo.", ecs: "TRANSICION" }; },
  };

  const sandbox = {
    document: {
      getElementById: id => nodos[id] || nodo(),
      addEventListener: () => {},
      hidden: false,
    },
    location: { reload: () => {} },
    setInterval: (fn, cada) => { const id = sigId++; timers.push({ id, cuando: ahora + cada, cada, fn }); return id; },
    clearInterval: id => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
    setTimeout: (fn, ms) => { const id = sigId++; timers.push({ id, cuando: ahora + (ms || 0), fn }); return id; },
    fetch: async (url) => new Response(JSON.stringify(
      String(url).includes("a=start") ? backend.start() : backend.status())),
    Response,
  };
  const { runInNewContext } = await import("node:vm");
  runInNewContext(js, sandbox);
  await new Promise(r => setImmediate(r));       // el estado() inicial

  check("arranca mostrando el estado real", nodos.estado.textContent === "Apagada",
        nodos.estado.textContent);

  nodos.on.onclick();                            // apretar "Encender"
  await new Promise(r => setImmediate(r));
  check("al apretar dice que está encendiendo", /Encendiendo/.test(nodos.estado.textContent),
        nodos.estado.textContent);

  // EL BUG: a los 1,5 s la API todavía reporta el estado viejo. Antes, eso
  // alcanzaba para que el panel lo diera por definitivo y matara el polling.
  await avanzar(1500);
  check("no se da por encendida antes de tiempo", nodos.estado.textContent !== "Encendida",
        nodos.estado.textContent);
  check("el polling sigue vivo tras el primer chequeo", timers.some(t => t.cada),
        "no quedó ningún intervalo: el panel se congeló");

  await avanzar(20000);                          // dejar que termine de arrancar
  check("termina reflejando que está encendida", nodos.estado.textContent === "Encendida",
        nodos.estado.textContent);
  check("y ahí sí corta el polling", !timers.some(t => t.cada));
  check("habilita el botón de apagar", nodos.off.disabled === false);

  // ── Contra el backend VIEJO, que miente ──────────────────────────────────
  // El Worker y la función de FunctionGraph se despliegan por separado: hasta
  // que no se actualice la función, el panel va a hablar con una que reporta
  // SHUTOFF durante todo el powering-on. El frente tiene que aguantar eso solo,
  // sin ayuda del backend. Es el escenario exacto del bug original.
  for (const id of Object.keys(nodos)) Object.assign(nodos[id], nodo());
  timers.length = 0;
  ahora = 0;
  let arrancado = false, vistas = 0;
  // Contexto nuevo: el script declara `const $`, y reusar el sandbox anterior
  // choca con la declaración del primer run.
  const sandbox2 = { ...sandbox };
  sandbox2.fetch = async (url) => {
    if (String(url).includes("a=start")) {
      arrancado = true;
      return new Response(JSON.stringify({ ok: true, message: "Encendiendo.", ecs: "SHUTOFF" }));
    }
    // El contrato viejo: NUNCA dice TRANSICION. Miente con SHUTOFF hasta el final.
    vistas++;
    return new Response(JSON.stringify(
      { ecs: arrancado && vistas > 3 ? "ACTIVE" : "SHUTOFF", ports: arrancado, app_url: "https://x" }));
  };
  runInNewContext(js, sandbox2);
  await new Promise(r => setImmediate(r));

  nodos.on.onclick();
  await new Promise(r => setImmediate(r));
  await avanzar(1500);
  check("con el backend viejo tampoco se da por apagada", nodos.estado.textContent !== "Apagada",
        nodos.estado.textContent);
  check("con el backend viejo el polling sigue vivo", timers.some(t => t.cada),
        "se congeló: es el bug original");

  await avanzar(30000);
  check("con el backend viejo igual llega a Encendida", nodos.estado.textContent === "Encendida",
        nodos.estado.textContent);
}

console.log(fallos ? `\n${fallos} FALLAS` : "\ntodo ok");
process.exit(fallos ? 1 : 0);
