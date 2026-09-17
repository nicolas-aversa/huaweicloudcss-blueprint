/**
 * Frente web del panel de la ECS — Cloudflare Worker.
 *
 * Existe porque FunctionGraph ya no ofrece ninguna puerta HTTP gratis: el APIG
 * shared está dado de baja para cuentas nuevas y las HTTP functions solo aceptan
 * triggers de APIG (dedicado, facturado por hora) o APIC (solo AP-Singapore).
 * En LA-Santiago no queda ninguna opción sin costo.
 *
 * Este Worker sirve la página y valida al usuario; la lógica de verdad —prender
 * y apagar— sigue viviendo en la función de FunctionGraph (`index.py`), que se
 * invoca por la API normal.
 *
 * Que esté partido en dos no es casualidad: la credencial que guarda Cloudflare
 * se scopea a "invocar esta función" y nada más. Si se filtra, lo máximo que
 * consigue alguien es prender y apagar la máquina — no tocar la ECS en sí. Los
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
      // Lo único de afuera es Inter, de Google Fonts —la misma fuente que el
      // app, para que el panel se vea de la misma familia—. Todo lo demás va
      // inline. `connect-src` y `form-action` tienen que estar SÍ O SÍ: con
      // `default-src 'none'` solo, el navegador bloquea el propio fetch de la
      // página hacia `?a=status` (connect-src cae al default) y el panel queda
      // en "Consultando…". Y sin `font-src` la fuente falla EN SILENCIO: el
      // navegador cae a la del sistema y nadie se entera.
      "Content-Security-Policy": "default-src 'none'; " +
        "style-src 'unsafe-inline' https://fonts.googleapis.com; " +
        "font-src https://fonts.gstatic.com; " +
        "script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'",
      "Referrer-Policy": "no-referrer",
      ...extra,
    },
  });
}

// ── Páginas ──────────────────────────────────────────────────────────────────
// Los tokens son los del app (static/index.html, `:root`): mismas superficies,
// mismo rojo, misma escala de radios y tipografía. El panel es la card de login
// del app puesta sola en la pantalla — así se lee como parte de la misma
// plataforma y no como un sitio aparte.
const CSS = `
:root {
  --bg-primary:#ffffff; --bg-secondary:#f6f5f1; --bg-tertiary:#efeee8; --bg-subtle:#faf9f6;
  --border-subtle:#e7e5dd; --border-default:#d9d7cd;
  --text-primary:#15161a; --text-secondary:#515463; --text-muted:#8a8b94;
  --accent:#e50000; --accent-hover:#cc0000; --accent-tint:rgba(229,0,0,.08);
  --accent-ring:rgba(229,0,0,.14); --accent-green:#16a34a;
  --shadow-lg:0 12px 32px rgba(20,22,30,.10);
  --radius-sm:6px; --radius-md:8px; --radius-lg:12px;
  --fs-xs:11px; --fs-sm:12px; --fs-base:13px; --fs-md:14px; --fs-lg:16px; --fs-xl:20px;
  --font-sans:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --ease:cubic-bezier(.4,0,.2,1); --t-base:.18s var(--ease);
}
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
  padding:24px; background:var(--bg-secondary); color:var(--text-primary);
  font:var(--fs-base)/1.5 var(--font-sans); -webkit-font-smoothing:antialiased; }
.card { width:100%; max-width:392px; background:var(--bg-primary);
  border:1px solid var(--border-subtle); border-radius:var(--radius-lg);
  padding:32px 30px; box-shadow:var(--shadow-lg); }
.brand { display:flex; align-items:center; gap:11px; margin-bottom:22px; }
.brand__logo { width:40px; height:40px; flex:none; border-radius:var(--radius-md);
  background:var(--accent); color:#fff; display:flex; align-items:center; justify-content:center; }
.brand__logo svg { width:20px; height:20px; stroke:currentColor; fill:none; stroke-width:2;
  stroke-linecap:round; stroke-linejoin:round; }
.brand__text { display:flex; flex-direction:column; line-height:1.2; }
.brand__name { font-size:var(--fs-lg); font-weight:700; letter-spacing:-.01em; }
.brand__sub { font-size:var(--fs-xs); color:var(--text-muted); }
h1 { margin:0 0 6px; font-size:var(--fs-xl); font-weight:700; letter-spacing:-.01em; }
.sub { margin:0 0 22px; color:var(--text-secondary); font-size:var(--fs-base); }
label { display:block; font-size:var(--fs-xs); font-weight:700; color:var(--text-secondary); margin:0 0 6px; }
input { width:100%; padding:11px 13px; margin-bottom:16px; font-size:var(--fs-md); font-family:inherit;
  color:var(--text-primary); background:var(--bg-tertiary);
  border:1px solid var(--border-default); border-radius:var(--radius-sm);
  transition:border-color var(--t-base), box-shadow var(--t-base), background var(--t-base); }
input:focus { outline:none; background:var(--bg-primary); border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-tint); }
.state { display:flex; align-items:center; gap:10px; padding:14px 16px;
  border:1px solid var(--border-subtle); border-radius:var(--radius-md);
  background:var(--bg-subtle); margin-bottom:16px; }
.dot { width:10px; height:10px; border-radius:50%; background:var(--text-muted); flex:none; }
.dot.on { background:var(--accent-green); }
.dot.busy { background:var(--accent); animation:pulse 1.2s infinite; }
@keyframes pulse { 50% { opacity:.35; } }
.state b { font-size:var(--fs-md); font-weight:600; }
button { width:100%; padding:12px; font-size:var(--fs-md); font-weight:700; font-family:inherit;
  border:1px solid transparent; border-radius:var(--radius-sm); cursor:pointer; margin-bottom:10px;
  background:var(--accent); color:#fff;
  transition:background var(--t-base), border-color var(--t-base), color var(--t-base); }
button:hover:not(:disabled) { background:var(--accent-hover); }
button:focus-visible { outline:none; box-shadow:0 0 0 3px var(--accent-ring); }
button.off { background:var(--bg-primary); color:var(--text-primary); border-color:var(--border-default); }
button.off:hover:not(:disabled) { background:var(--bg-tertiary); border-color:var(--text-muted); }
button:disabled { opacity:.4; cursor:not-allowed; }
.msg { margin:14px 0 0; padding:10px 12px; border-radius:var(--radius-sm); font-size:var(--fs-sm);
  background:var(--bg-subtle); border:1px solid var(--border-subtle); color:var(--text-secondary); }
.msg.bad { color:var(--accent-hover); background:var(--accent-tint); border-color:transparent; font-weight:500; }
.url { margin:16px 0 0; text-align:center; font-size:var(--fs-sm); min-height:18px; }
.url a { color:var(--accent); text-decoration:none; font-weight:500; }
.url a:hover { text-decoration:underline; }
`;

// El mismo `#ic-layers` del sprite del app.
const LOGO = `<svg viewBox="0 0 24 24" aria-hidden="true">` +
  `<path d="M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5M3 17l9 5 9-5"/></svg>`;

function shell(cuerpo) {
  return `<!doctype html><html lang=es><head><meta charset=utf-8>` +
    `<meta name=viewport content="width=device-width,initial-scale=1">` +
    `<title>ECS · Blueprint</title>` +
    `<link rel=preconnect href="https://fonts.googleapis.com">` +
    `<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>` +
    `<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">` +
    `<style>${CSS}</style></head>` +
    `<body><div class=card>` +
    `<div class=brand><span class=brand__logo>${LOGO}</span>` +
    `<span class=brand__text><span class=brand__name>Blueprint</span>` +
    `<span class=brand__sub>Huawei Cloud CSS</span></span></div>` +
    `${cuerpo}</div></body></html>`;
}

function paginaLogin(error = "") {
  return shell(
    `<h1>Panel de la ECS</h1>` +
    `<p class=sub>Ingresá la contraseña para prender o apagar la máquina.</p>` +
    (error ? `<p class="msg bad" style="margin:0 0 14px">${escapar(error)}</p>` : "") +
    `<form method=post action="?a=login">` +
    `<label for=password>Contraseña</label>` +
    `<input type=password id=password name=password placeholder="••••••••" autofocus ` +
    `autocomplete="current-password">` +
    `<button type=submit>Entrar</button></form>`);
}

function escapar(txt) {
  return String(txt).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function paginaPanel() {
  return shell(`
<h1>Panel de la ECS</h1>
<p class=sub>La máquina que hostea la plataforma.</p>
<div class=state>
  <div class=dot id=dot></div>
  <b id=estado>Consultando…</b>
</div>
<button id=on disabled>Encender</button>
<button id=off class=off disabled>Apagar</button>
<p class=msg id=msg>&nbsp;</p>
<p class=url id=url></p>
<script>
const $ = id => document.getElementById(id);
const ON = 'ACTIVE', OFF = 'SHUTOFF', TRANSICION = 'TRANSICION';
let polling = null;
// A dónde queremos llegar tras apretar un botón, o null si no hay nada pedido.
// Es la SEGUNDA red: aunque la API todavía no reporte la transición, un ON que
// llega mientras pedimos apagar no se toma por bueno.
let esperando = null;
// Nº de la última consulta pedida: las respuestas viejas se descartan. El
// intervalo es de 5 s y un status encadena dos llamadas con 15 s de timeout,
// así que pueden llegar fuera de orden y hacer RETROCEDER el estado.
let pedido = 0;

function pintar(d) {
  let t = d.ecs;
  // Todavía no llegó a donde pedimos → seguimos en transición.
  if (esperando && t === esperando) esperando = null;
  else if (esperando) t = TRANSICION;

  const quieto = (t === ON || t === OFF);
  $('estado').textContent = t === ON ? 'Encendida'
                          : t === OFF ? 'Apagada'
                          : esperando === ON ? 'Encendiendo…'
                          : esperando === OFF ? 'Apagando…' : 'Cambiando…';
  $('dot').className = 'dot' + (t === ON ? ' on' : quieto ? '' : ' busy');
  $('on').disabled = !quieto || t === ON;
  $('off').disabled = !quieto || t === OFF;
  // El link sale de la config de la función: esta página vive en el dominio del
  // Worker, no en el de la ECS, así que no se puede derivar de location.
  $('url').innerHTML = (t === ON && d.app_url)
    ? '<a href="' + d.app_url + '" target=_blank rel=noopener>Abrir la plataforma</a>' : '';

  // El polling sigue mientras NO esté quieto. Antes esto cortaba apenas veía un
  // estado estable, y como la API devuelve el estado viejo durante el
  // powering-on, mataba el intervalo 1,5 s después de apretar el botón: el panel
  // se quedaba mostrando "Apagada" para siempre.
  if (!quieto) arrancarPolling(); else pararPolling();
}

function arrancarPolling() { if (!polling) polling = setInterval(estado, 5000); }
function pararPolling() { if (polling) { clearInterval(polling); polling = null; } }

function mensaje(texto, malo) {
  $('msg').textContent = texto;
  $('msg').className = 'msg' + (malo ? ' bad' : '');
}

function fallo(e) {
  mensaje(e.message || e, true);
  // Que un error no deje el panel muerto: si la carga inicial fallaba, los
  // botones quedaban deshabilitados y NO había polling (el intervalo solo nacía
  // dentro de pintar o accion), así que no se recuperaba nunca sin recargar.
  arrancarPolling();
}

async function estado() {
  const mio = ++pedido;
  try {
    const r = await fetch('?a=status', { credentials: 'same-origin' });
    if (r.status === 401) { location.reload(); return; }
    const d = await r.json();
    if (mio !== pedido) return;              // llegó tarde: ya hay una más nueva
    if (d.error) throw new Error(d.error);
    pintar(d);
  } catch (e) { if (mio === pedido) fallo(e); }
}

async function accion(cual) {
  $('on').disabled = $('off').disabled = true;
  esperando = cual === 'start' ? ON : OFF;
  mensaje(cual === 'start' ? 'Encendiendo…' : 'Apagando…', false);
  arrancarPolling();
  try {
    const r = await fetch('?a=' + cual, { method: 'POST', credentials: 'same-origin' });
    if (r.status === 401) { location.reload(); return; }
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    mensaje(d.message, false);
    // Si la acción fue un no-op ("ya estaba encendida"), no hay transición que
    // esperar: el estado que reporte la API ya es el bueno.
    if (/Ya estaba/i.test(d.message || '')) esperando = null;
    estado();
  } catch (e) {
    esperando = null;
    fallo(e);
  }
}

$('on').onclick = () => accion('start');
$('off').onclick = () => accion('stop');
// Al volver a la pestaña, refrescar: en el celular los timers se estrangulan o
// se congelan en background y el panel mostraba lo de hace veinte minutos.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) estado();
});
estado();
</script>`);
}
