/**
 * Frente web del panel de la ECS — Cloudflare Worker.
 *
 * Existe porque FunctionGraph ya no ofrece ninguna puerta HTTP gratis: el APIG
 * shared está dado de baja para cuentas nuevas y las HTTP functions solo aceptan
 * triggers de APIG (dedicado, facturado por hora) o APIC (solo AP-Singapore).
 * En LA-Santiago no queda ninguna opción sin costo.
 *
 * Este Worker sirve la página y valida al usuario; la lógica de verdad —prender,
 * apagar, abrir y cerrar los puertos del security group— sigue viviendo en la
 * función de FunctionGraph (`index.py`), que se invoca por la API normal.
 *
 * Que esté partido en dos no es casualidad: la credencial que guarda Cloudflare
 * se scopea a "invocar esta función" y nada más. Si se filtra, lo máximo que
 * consigue alguien es prender y apagar la máquina — no tocar ECS ni VPC. Los
 * permisos anchos se quedan del lado de Huawei, en la agency de la función.
 *
 * Secrets (wrangler secret put <NOMBRE>):
 *   PANEL_PASSWORD   la contraseña del panel
 *   PANEL_SECRET     clave para firmar la cookie (string largo al azar)
 *   HW_USER          usuario IAM dedicado, con permiso SOLO de invocar la función
 *   HW_DOMAIN        cuenta (domain name) de ese usuario
 *   HW_PASSWORD      su contraseña
 *
 * Vars (wrangler.toml):
 *   HW_REGION        p. ej. la-south-2
 *   HW_PROJECT_ID    project id de esa región
 *   FUNCTION_URN     URN de la función de FunctionGraph
 */

const SESSION_TTL = 7 * 24 * 3600;      // una semana: se loguea una vez desde el celular
const TOKEN_MARGEN = 5 * 60;            // renovar el token IAM antes de que venza

// El token IAM dura 24 h. Cachearlo en el módulo evita pedir uno nuevo en cada
// click; el isolate puede reciclarse en cualquier momento, así que esto es un
// oportunismo, no un invariante: si se pierde, se pide otro.
let tokenCache = { token: "", venceEn: 0 };

export default {
  async fetch(request, env) {
    try {
      return await manejar(request, env);
    } catch (err) {
      return json({ error: String(err && err.message || err) }, 502);
    }
  },
};

async function manejar(request, env) {
  const url = new URL(request.url);
  const accion = url.searchParams.get("a") || "";
  const metodo = request.method.toUpperCase();

  if (metodo === "POST" && accion === "login") return login(request, env);

  const autenticado = await tokenValido(env.PANEL_SECRET, cookieDe(request));

  if (!autenticado) {
    // Las acciones responden JSON (las llama el fetch de la página); la página,
    // HTML. Un 401 en JSON le dice al front que recargue y muestre el login.
    if (["status", "start", "stop"].includes(accion)) {
      return json({ error: "Sesión vencida. Recargá la página." }, 401);
    }
    return html(paginaLogin());
  }

  if (["status", "start", "stop"].includes(accion)) {
    // start y stop solo por POST: por GET las dispararía un prefetch del navegador.
    if (accion !== "status" && metodo !== "POST") {
      return json({ error: "Usá POST para esta acción." }, 405);
    }
    const res = await invocar(env, accion);
    return json(res, res.error ? 502 : 200);
  }

  return html(paginaPanel());
}

// ── Login y sesión ──────────────────────────────────────────────────────────
async function login(request, env) {
  const form = await request.formData().catch(() => null);
  const dada = form ? String(form.get("password") || "") : "";
  if (!(await igualesEnTiempoConstante(dada, env.PANEL_PASSWORD || ""))) {
    return html(paginaLogin("Contraseña incorrecta."), 401);
  }
  const cookie = `panel=${await firmarToken(env.PANEL_SECRET)}; Max-Age=${SESSION_TTL}` +
    `; Path=/; HttpOnly; Secure; SameSite=Strict`;
  return html(paginaPanel(), 200, { "Set-Cookie": cookie });
}

/**
 * Comparación que no filtra la contraseña por cuánto tarda en fallar.
 * Se comparan los HMAC y no los strings: así el tiempo tampoco depende del
 * largo, que es lo que delata `a.length !== b.length`.
 */
async function igualesEnTiempoConstante(a, b) {
  if (!b) return false;
  const [ha, hb] = await Promise.all([sha256(a), sha256(b)]);
  let iguales = 0;
  for (let i = 0; i < ha.length; i++) iguales |= ha[i] ^ hb[i];
  return iguales === 0;
}

async function sha256(txt) {
  return new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(txt)));
}

async function hmac(secreto, msg) {
  const clave = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secreto),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const firma = await crypto.subtle.sign("HMAC", clave, new TextEncoder().encode(msg));
  return [...new Uint8Array(firma)].map(b => b.toString(16).padStart(2, "0")).join("");
}

// Mismo formato que la sesión de la plataforma (auth.py): payload en base64url,
// punto, HMAC-SHA256 en hex.
async function firmarToken(secreto) {
  const cuerpo = b64url(`panel|${Math.floor(Date.now() / 1000) + SESSION_TTL}`);
  return `${cuerpo}.${await hmac(secreto, cuerpo)}`;
}

async function tokenValido(secreto, token) {
  if (!token || !token.includes(".") || !secreto) return false;
  const corte = token.lastIndexOf(".");
  const cuerpo = token.slice(0, corte), firma = token.slice(corte + 1);
  if (!(await igualesEnTiempoConstante(firma, await hmac(secreto, cuerpo)))) return false;
  try {
    const vence = Number(deB64url(cuerpo).split("|")[1]);
    return Number.isFinite(vence) && vence >= Math.floor(Date.now() / 1000);
  } catch { return false; }
}

function b64url(txt) {
  return btoa(txt).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function deB64url(txt) {
  return atob(txt.replace(/-/g, "+").replace(/_/g, "/"));
}

function cookieDe(request) {
  const crudo = request.headers.get("Cookie") || "";
  for (const parte of crudo.split(";")) {
    const [nombre, ...resto] = parte.trim().split("=");
    if (nombre === "panel") return resto.join("=");
  }
  return "";
}

// ── Huawei: token IAM + invocación de la función ────────────────────────────
async function tokenIAM(env) {
  const ahora = Math.floor(Date.now() / 1000);
  if (tokenCache.token && tokenCache.venceEn - TOKEN_MARGEN > ahora) return tokenCache.token;

  const res = await fetch(`https://iam.${env.HW_REGION}.myhuaweicloud.com/v3/auth/tokens`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      auth: {
        identity: {
          methods: ["password"],
          password: {
            user: {
              name: env.HW_USER,
              domain: { name: env.HW_DOMAIN },
              password: env.HW_PASSWORD,
            },
          },
        },
        scope: { project: { id: env.HW_PROJECT_ID } },
      },
    }),
  });
  // El token viaja en un HEADER de respuesta, no en el body.
  const token = res.headers.get("X-Subject-Token");
  if (!res.ok || !token) {
    throw new Error(`IAM rechazó las credenciales (HTTP ${res.status}). ` +
      `Revisá HW_USER / HW_DOMAIN / HW_PASSWORD.`);
  }
  tokenCache = { token, venceEn: ahora + 24 * 3600 };
  return token;
}

async function invocar(env, accion) {
  let token;
  try {
    token = await tokenIAM(env);
  } catch (err) {
    return { error: err.message };
  }

  const url = `https://functiongraph.${env.HW_REGION}.myhuaweicloud.com` +
    `/v2/${env.HW_PROJECT_ID}/fgs/functions/${env.FUNCTION_URN}/invocations`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Auth-Token": token },
    body: JSON.stringify({ action: accion }),
  });

  const crudo = await res.text();
  if (!res.ok) {
    // Un 401 acá casi siempre es el token vencido antes de tiempo: tirar el
    // cache para que el próximo intento pida uno nuevo.
    if (res.status === 401) tokenCache = { token: "", venceEn: 0 };
    return { error: `FunctionGraph respondió ${res.status}: ${crudo.slice(0, 200)}` };
  }
  try {
    // La API envuelve el return de la función; algunas versiones lo dan como
    // string y otras ya parseado.
    const sobre = JSON.parse(crudo);
    const cuerpo = sobre.result ?? sobre.body ?? sobre;
    return typeof cuerpo === "string" ? JSON.parse(cuerpo) : cuerpo;
  } catch {
    return { error: `Respuesta inesperada de la función: ${crudo.slice(0, 200)}` };
  }
}

// ── Respuestas ───────────────────────────────────────────────────────────────
function json(datos, status = 200) {
  return new Response(JSON.stringify(datos), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function html(cuerpo, status = 200, extra = {}) {
  return new Response(cuerpo, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      // La página no carga nada de afuera: todo va inline.
      "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
      "Referrer-Policy": "no-referrer",
      ...extra,
    },
  });
}

// ── Páginas ──────────────────────────────────────────────────────────────────
const CSS = `
:root { color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --fg:#16181d;
  --muted:#6b7280; --line:#e5e7eb; --on:#16a34a; --off:#6b7280; --busy:#d97706;
  --danger:#dc2626; }
@media (prefers-color-scheme: dark) { :root { --bg:#0f1115; --card:#181b21;
  --fg:#e8eaed; --muted:#9aa0a6; --line:#2a2e36; } }
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; display:flex; align-items:center;
  justify-content:center; padding:24px; background:var(--bg); color:var(--fg);
  font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; }
.card { width:100%; max-width:420px; background:var(--card); border-radius:16px;
  padding:28px 24px; box-shadow:0 1px 3px rgba(0,0,0,.1); }
h1 { margin:0 0 4px; font-size:20px; }
.sub { margin:0 0 24px; color:var(--muted); font-size:14px; }
.state { display:flex; align-items:center; gap:10px; padding:16px;
  border:1px solid var(--line); border-radius:12px; margin-bottom:20px; }
.dot { width:12px; height:12px; border-radius:50%; background:var(--off); flex:none; }
.dot.on { background:var(--on); }
.dot.busy { background:var(--busy); animation:pulse 1.2s infinite; }
@keyframes pulse { 50% { opacity:.35; } }
.state b { font-size:15px; }
.state span { display:block; color:var(--muted); font-size:13px; }
button { width:100%; padding:15px; font-size:16px; font-weight:600;
  font-family:inherit; border:0; border-radius:12px; cursor:pointer;
  margin-bottom:10px; background:var(--on); color:#fff; }
button.off { background:var(--card); color:var(--fg); border:1px solid var(--line); }
button:disabled { opacity:.4; cursor:not-allowed; }
input { width:100%; padding:14px; font-size:16px; font-family:inherit;
  border:1px solid var(--line); border-radius:12px; margin-bottom:12px;
  background:var(--bg); color:var(--fg); }
.msg { margin:14px 0 0; padding:12px; border-radius:10px; font-size:14px;
  background:var(--bg); color:var(--muted); }
.msg.bad { color:var(--danger); }
.url { margin-top:20px; text-align:center; font-size:13px; }
.url a { color:var(--muted); }
`;

function shell(cuerpo) {
  return `<!doctype html><html lang=es><head><meta charset=utf-8>` +
    `<meta name=viewport content="width=device-width,initial-scale=1">` +
    `<title>ECS · Plataforma CSS</title><style>${CSS}</style></head>` +
    `<body><div class=card>${cuerpo}</div></body></html>`;
}

function paginaLogin(error = "") {
  return shell(
    `<h1>Panel de la ECS</h1>` +
    `<p class=sub>Ingresá la contraseña para continuar.</p>` +
    `<form method=post action="?a=login">` +
    `<input type=password name=password placeholder="Contraseña" autofocus ` +
    `autocomplete="current-password">` +
    `<button type=submit>Entrar</button></form>` +
    (error ? `<p class="msg bad">${escapar(error)}</p>` : ""));
}

function escapar(txt) {
  return String(txt).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function paginaPanel() {
  return shell(`
<h1>Panel de la ECS</h1>
<p class=sub>Plataforma CSS Accelerator</p>
<div class=state>
  <div class=dot id=dot></div>
  <div><b id=estado>Consultando…</b><span id=puertos>&nbsp;</span></div>
</div>
<button id=on disabled>Encender</button>
<button id=off class=off disabled>Apagar</button>
<p class=msg id=msg>&nbsp;</p>
<p class=url id=url></p>
<script>
const $ = id => document.getElementById(id);
// Cualquier estado que no sea ACTIVE ni SHUTOFF es una transición: se sigue poleando.
const ON = 'ACTIVE', OFF = 'SHUTOFF';
let polling = null;

function pintar(d) {
  const t = d.ecs, quieto = (t === ON || t === OFF);
  $('estado').textContent = t === ON ? 'Encendida' : t === OFF ? 'Apagada' : t;
  $('puertos').textContent = d.ports ? 'Puertos 80/443 abiertos' : 'Puertos cerrados';
  $('dot').className = 'dot' + (t === ON ? ' on' : quieto ? '' : ' busy');
  $('on').disabled = !quieto || t === ON;
  $('off').disabled = !quieto || t === OFF;
  // El link sale de la config de la función: esta página vive en el dominio del
  // Worker, no en el de la ECS, así que no se puede derivar de location.
  $('url').innerHTML = (t === ON && d.app_url)
    ? '<a href="' + d.app_url + '" target=_blank rel=noopener>Abrir la plataforma</a>' : '';
  if (!quieto && !polling) polling = setInterval(estado, 5000);
  if (quieto && polling) { clearInterval(polling); polling = null; }
}

function fallo(e) {
  $('msg').textContent = e.message || e;
  $('msg').className = 'msg bad';
}

async function estado() {
  try {
    const r = await fetch('?a=status', { credentials: 'same-origin' });
    if (r.status === 401) { location.reload(); return; }
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    pintar(d);
  } catch (e) { fallo(e); }
}

async function accion(cual) {
  $('on').disabled = $('off').disabled = true;
  $('msg').className = 'msg';
  $('msg').textContent = cual === 'start' ? 'Encendiendo…' : 'Apagando…';
  try {
    const r = await fetch('?a=' + cual, { method: 'POST', credentials: 'same-origin' });
    if (r.status === 401) { location.reload(); return; }
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    $('msg').textContent = d.message;
    // El start/stop de Huawei es asíncrono: el estado real llega poleando.
    if (!polling) polling = setInterval(estado, 5000);
    setTimeout(estado, 1500);
  } catch (e) { fallo(e); }
}

$('on').onclick = () => accion('start');
$('off').onclick = () => accion('stop');
estado();
</script>`);
}
