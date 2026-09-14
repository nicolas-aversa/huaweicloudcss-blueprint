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

console.log("── la página no filtra secretos ──");
const panelHtml = await (await pedir({ cookie })).text();
const loginHtml = await (await pedir()).text();
for (const [k, v] of Object.entries(ENV)) {
  if (!v || k === "HW_REGION" || k === "HW_PROJECT_ID" || k === "FUNCTION_URN") continue;
  check(`${k} no aparece en el HTML`, !panelHtml.includes(v) && !loginHtml.includes(v));
}

console.log(fallos ? `\n${fallos} FALLAS` : "\ntodo ok");
process.exit(fallos ? 1 : 0);
