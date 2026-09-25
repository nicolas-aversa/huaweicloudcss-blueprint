"""El chat de cada caso sobrevive a que la vista se rearme.

El asistente se reconstruye en cada render de "Entorno desplegado" (volver a
la vista, Verificar, cada paso de la puesta en marcha) y la conversación vivía
solo en el DOM: se borraba. Ahora vive en un store por caso, en memoria, y
cada render la repinta desde ahí.

Las funciones REALES del store se corren en node.
"""
import pathlib
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"

_ARNES = r"""
const icon = (n) => `<i:${n}>`;
const escapeHtml = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
""" + "{STORE}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };

// Vacío: la invitación a preguntar, con el nombre del caso.
check('vacío', capChatLogHTML('ventas', 'Ventas').includes('Preguntame lo que quieras sobre <strong>Ventas</strong>'));
// El saludo trae cinco preguntas para arrancar, como tarjetas.
const saludo = capChatLogHTML('ventas', 'Ventas', ['a', 'b', 'c', 'd', 'e', 'f']);
check('vacío: cinco ideas', (saludo.match(/class="cap-chat__idea lp-q-btn"/g) || []).length === 5, saludo);
check('vacío: la idea es la pregunta', saludo.includes('data-q="e"') && !saludo.includes('data-q="f"'));
check('vacío: para empezar', saludo.includes('Para empezar'));
check('vacío: sin ideas, sin bloque', !capChatLogHTML('ventas', 'Ventas').includes('cap-chat__ideas'));

// Pregunta en A, mientras se espera la respuesta.
capChatAgregar('ventas', 'user', 'hola &lt;b&gt;');
const pend = capChatAgregar('ventas', 'bot', '···');
check('ids distintos', pend.id !== capChats.ventas[0].id);

// "Re-render" (se arma el log desde lo guardado): la conversación sigue ahí.
let a = capChatLogHTML('ventas', 'Ventas');
check('re-render: la pregunta', a.includes('hola &lt;b&gt;'), a);
check('re-render: la espera', a.includes('···'));
check('re-render: sin invitación', !a.includes('Preguntame lo que quieras'));
check('re-render: cada fila con su id', a.includes(`data-msg="${pend.id}"`));

// B tiene su propia conversación, vacía.
check('otro caso vacío', capChatLogHTML('fraude', 'Fraude').includes('<strong>Fraude</strong>'));

// La respuesta llega (quizás con otra pestaña activa): queda en A.
pend.html = '<span class="cap-chat__answer">Hay 42.</span>';
a = capChatLogHTML('ventas', 'Ventas');
check('respuesta guardada en A', a.includes('Hay 42.') && !a.includes('···'), a);
check('no se fue a B', !capChatLogHTML('fraude', 'Fraude').includes('Hay 42.'));

// Un error se pinta como error también al volver.
const e = capChatAgregar('fraude', 'bot', 'No se pudo responder.');
e.err = true;
check('error', capChatLogHTML('fraude', 'Fraude').includes('cap-chat__msg--err'));
check('bot con avatar', capChatFilaHTML(e).includes('<i:spark>'));
check('usuario sin avatar', !capChatFilaHTML(capChats.ventas[0]).includes('<i:spark>'));

// Limpiar solo toca el caso activo.
capChatLimpiar('ventas');
check('limpiar A', capChatLogHTML('ventas', 'Ventas').includes('<strong>Ventas</strong>'));
check('B intacto', capChatLogHTML('fraude', 'Fraude').includes('No se pudo responder.'));

// La memoria que va al backend: turnos contestados, sin errores, los últimos 4.
capChatLimpiar('fraude');
for (let i = 0; i < 6; i++) {
  capChatAgregar('fraude', 'user', 'p' + i);
  const b = capChatAgregar('fraude', 'bot', 'r' + i);
  b.turno = { pregunta: 'p' + i, ppl: 'source=x' + i, respuesta: 'r' + i };
  if (i === 5) b.err = true;
}
capChatAgregar('fraude', 'bot', '···');          // una pendiente, sin turno
const h = capChatHistorial('fraude');
check('historial: los últimos 4 contestados', h.map(t => t.pregunta).join(',') === 'p1,p2,p3,p4',
      h.map(t => t.pregunta).join(','));
check('historial: con la consulta', h[0].ppl === 'source=x1');
check('historial: otro caso vacío', capChatHistorial('nada').length === 0);

// Después de cada respuesta: tres sugeridas que todavía no se preguntaron.
const QS = ['¿Cuántos eventos hay?', '¿Cuál es el pico?', '¿Qué falla más?', '¿Quién compra más?', '¿Cuándo baja?'];
const ideas = (html) => [...html.matchAll(/data-q="([^"]*)"/g)].map(m => m[1]);
capChatAgregar('seguir', 'user', 'x').pregunta = '¿Cuál es el pico?';
const p1 = capChatAgregar('seguir', 'bot', '···');
p1.pendiente = true;
check('esperando: sin sugeridas', !capChatLogHTML('seguir', 'S', QS).includes('cap-chat__ideas'));
p1.pendiente = false;
p1.html = 'Fue el martes.';
let s1 = capChatLogHTML('seguir', 'S', QS);
check('respondida: tres', ideas(s1).length === 3, ideas(s1).join(' | '));
check('respondida: sin la ya preguntada', !ideas(s1).includes('¿Cuál es el pico?'), ideas(s1).join(' | '));
check('respondida: en su orden', ideas(s1).join('|') === '¿Cuántos eventos hay?|¿Qué falla más?|¿Quién compra más?');
check('respondida: podés seguir con', s1.includes('Podés seguir con') && !s1.includes('Para empezar'));
check('respondida: después de la respuesta', s1.indexOf('Fue el martes.') < s1.indexOf('cap-chat__ideas'));
// Escrita a mano, con otras mayúsculas y sin signos: cuenta igual.
capChatAgregar('seguir', 'user', 'x').pregunta = '  cuántos EVENTOS hay ';
capChatAgregar('seguir', 'bot', 'Hay 10.');
s1 = capChatLogHTML('seguir', 'S', QS);
check('a mano: tampoco se repite', ideas(s1).join('|') === '¿Qué falla más?|¿Quién compra más?|¿Cuándo baja?', ideas(s1).join(' | '));
// Un error también deja seguir.
capChatAgregar('seguir', 'user', 'x').pregunta = '¿Qué falla más?';
const p2 = capChatAgregar('seguir', 'bot', 'No se pudo responder.');
p2.err = true;
check('error: con sugeridas', ideas(capChatLogHTML('seguir', 'S', QS)).length === 2);
// Preguntadas todas: no queda el bloque vacío.
for (const q of QS) capChatAgregar('seguir', 'user', 'x').pregunta = q;
capChatAgregar('seguir', 'bot', 'ok');
check('todas preguntadas: sin bloque', !capChatLogHTML('seguir', 'S', QS).includes('cap-chat__ideas'));
// La última es del usuario (todavía sin burbuja de espera): ninguna.
capChatAgregar('seguir', 'user', 'y');
check('última del usuario: sin sugeridas', capChatSeguirHTML('seguir', ['otra']) === '');

console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


def _store(html: str) -> str:
    ini = html.index("    const capChats = {};")
    return html[ini:html.index("    function renderCapabilitiesResult(", ini)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_conversacion_de_cada_caso_sobrevive_al_re_render(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    js = tmp_path / "chat.mjs"
    js.write_text(_ARNES.replace("{STORE}", _store(html)), encoding="utf-8")

    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)

    assert res.returncode == 0, "checks fallidos:\n" + (res.stdout or res.stderr)


def _render(html: str) -> str:
    i = html.index("    function renderCapabilitiesResult(")
    return html[i:html.index("    async function provisionCapabilitiesFromInfra(", i)]


def test_el_asistente_se_arma_desde_lo_guardado():
    fn = _render(_INDEX.read_text(encoding="utf-8"))
    assert "capChatLogHTML(s, SLUG_LABELS[s] || 'tus datos', _questionsFor(s))" in fn
    # La pestaña elegida también sobrevive.
    assert "activeSlugs.indexOf(capChatSlug)" in fn
    assert "capChatSlug = slug;" in fn
    # Los gráficos de lo guardado se vuelven a embeber.
    assert "_renderCapChartInto(lugar, e.result)" in fn


def test_la_respuesta_va_al_caso_de_la_pregunta_y_al_asistente_actual():
    fn = _render(_INDEX.read_text(encoding="utf-8"))
    envio = fn[fn.index("async function sendCapChat(question)"):]
    envio = envio[:envio.index("\n      }\n")]
    assert "const pend = _agregar(slug, 'bot'" in envio
    # Se guarda la respuesta entera y se busca su burbuja en lo que está en
    # pantalla ahora, no en la que había al preguntar.
    assert "pend.html = `<div class=\"cap-chart-host\"></div>" in envio
    assert "pend.result = data.result" in envio
    assert "const el = _msgDe(pend);" in envio
    assert "document.querySelector(`#deploy-capabilities [data-msg=" in fn
    # Una a la vez, aunque el form se rearme.
    assert "capChatBusy" in envio and "chatForm.dataset.busy" not in fn


def test_solo_en_memoria_y_se_borra_con_el_entorno():
    """Las respuestas traen datos del cluster del cliente: no van a disco."""
    html = _INDEX.read_text(encoding="utf-8")
    assert "Storage" not in _store(html) and "Storage" not in _render(html)
    i = html.index("async function destroyEnvironment()")
    destroy = html[i:html.index("\n    }\n", i)]
    assert "delete capChats[k]" in destroy


def test_la_pregunta_manda_la_memoria_de_su_caso():
    fn = _render(_INDEX.read_text(encoding="utf-8"))
    envio = fn[fn.index("async function sendCapChat(question)"):]
    envio = envio[:envio.index("\n      }\n")]
    # El historial se toma ANTES de agregar la pregunta nueva.
    assert envio.index("const history = capChatHistorial(slug);") < envio.index("_agregar(slug, 'user'")
    assert "question, slug, history," in envio
    assert "pend.turno = { pregunta: question, ppl: data.ppl || '', respuesta: data.answer || '' };" in envio


# ── El chat flotante ────────────────────────────────────────────────────────
def test_el_asistente_es_un_panel_flotante_y_no_alarga_la_pagina():
    """Era una tarjeta dentro de la vista: al aparecer (paso 3) la alargaba."""
    fn = _render(_INDEX.read_text(encoding="utf-8"))
    assert "document.body.append(host, lanzador);" in fn
    assert "host.className = 'cap-flotante' + (capChatAbierto ? ' is-abierto' : '');" in fn
    assert "anchor.appendChild(host)" not in fn and "insertAdjacentElement('afterend', host)" not in fn
    # En la página queda una línea con los plugins; sin botón "Abrir asistente":
    # se abre solo al provisionar y, si se cierra, con la burbuja.
    assert "Abrir asistente" not in fn and "cap-abrir" not in fn


def test_el_asistente_se_abre_solo_al_provisionar_los_plugins():
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("async function provisionCapabilitiesFromInfra(")
    fn = html[i:html.index("\n    }\n", i)]
    assert fn.index("capChatAbierto = true;") < fn.index("renderCapabilitiesResult(data.capabilities || {}, mount);")
    assert fn.index("capChatAbierto = true;") > fn.index("if (!res.ok)"), "solo si salió bien"


def test_abrir_y_cerrar_no_pierde_nada():
    fn = _render(_INDEX.read_text(encoding="utf-8"))
    abrir = fn[fn.index("const _abrir = (on) => {"):]
    abrir = abrir[:abrir.index("};")]
    assert "capChatAbierto = on;" in abrir and "classList.toggle('is-abierto', on)" in abrir
    assert "lanzador.addEventListener('click', () => _abrir(!capChatAbierto));" in fn
    assert "#cap-flotante-cerrar')?.addEventListener('click', () => _abrir(false));" in fn


def test_el_caso_se_elige_en_la_cabecera_con_un_desplegable_propio():
    """El <select> nativo abría la lista del sistema, gris y fuera de estilo."""
    html = _INDEX.read_text(encoding="utf-8")
    fn = _render(html)
    assert "<select" not in fn
    assert '<div class="cap-caso" id="cap-chat-caso" data-value=' in fn
    assert 'aria-haspopup="listbox"' in fn and 'role="listbox"' in fn and 'role="option"' in fn
    assert "(host.querySelector('#cap-chat-caso')?.dataset.value) || activeSlugs[0]" in fn
    cambio = fn[fn.index("if (caso) desplegableDeCaso(caso, (slug) => {"):]
    assert "capChatSlug = slug;" in cambio[:400]
    css = html[:html.index("</style>")]
    assert ".cap-caso__opcion[aria-selected=\"true\"] .icon { visibility: visible; }" in css
    lista = css[css.index("    .cap-caso__lista {"):]
    assert "position: absolute" in lista[:lista.index("}")] and "box-shadow: var(--shadow-lg)" in lista[:lista.index("}")]


def test_el_desplegable_con_mouse_y_teclado(tmp_path):
    """En node, con la función real."""
    if shutil.which("node") is None:
        pytest.skip("node no está instalado")
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function desplegableDeCaso(raiz, alElegir) {")
    fn = html[i:html.index("\n    }\n", i) + 7]
    arnes = r"""
const document = { activeElement: null };
function nodo(extra = {}) {
  const clases = new Set(), attrs = {}, oyentes = {};
  return Object.assign({
    clases, attrs, dataset: {},
    classList: { contains: (c) => clases.has(c), toggle: (c, on) => { on ? clases.add(c) : clases.delete(c); } },
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: (k) => attrs[k],
    addEventListener: (t, f) => { (oyentes[t] = oyentes[t] || []).push(f); },
    disparar(t, e = {}) { e.preventDefault = e.preventDefault || (() => {}); (oyentes[t] || []).forEach(f => f(e)); return e; },
    focus() { document.activeElement = this; },
  }, extra);
}
const ops = ['billetera', 'alyc', 'resenas'].map((v, i) => {
  const op = nodo();
  op.dataset.value = v;
  op.attrs['aria-selected'] = String(i === 0);
  op.querySelector = () => ({ textContent: 'Caso ' + v });
  op.closest = () => op;
  return op;
});
const boton = nodo(), actual = { textContent: 'Caso billetera' };
const lista = nodo({ querySelectorAll: () => ops });
lista.clases.add('hidden');
const raiz = nodo({
  querySelector: (sel) => ({ '.cap-caso__boton': boton, '.cap-caso__lista': lista, '.cap-caso__actual': actual })[sel],
  contains: (x) => x === boton || ops.includes(x),
});
raiz.dataset.value = 'billetera';
""" + fn + r"""
const elegidos = [];
desplegableDeCaso(raiz, (v) => elegidos.push(v));
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
const abierta = () => !lista.clases.has('hidden');
check('apretar el botón no le saca el foco', boton.disparar('mousedown', { preventDefault() { this.hecho = true; } }).hecho);
boton.disparar('click');
check('abre', abierta() && boton.attrs['aria-expanded'] === 'true');
check('abre sobre el elegido', document.activeElement === ops[0]);
lista.disparar('keydown', { key: 'ArrowDown' });
check('flecha abajo', document.activeElement === ops[1]);
lista.disparar('keydown', { key: 'ArrowDown' }); lista.disparar('keydown', { key: 'ArrowDown' });
check('no pasa del último', document.activeElement === ops[2]);
lista.disparar('keydown', { key: 'ArrowUp' }); lista.disparar('keydown', { key: 'ArrowUp' });
lista.disparar('keydown', { key: 'ArrowUp' });
check('no pasa del primero', document.activeElement === ops[0]);
lista.disparar('keydown', { key: 'ArrowDown' });
lista.disparar('keydown', { key: 'Enter' });
check('Enter elige', raiz.dataset.value === 'alyc' && elegidos.join() === 'alyc', elegidos.join());
check('cierra y vuelve al botón', !abierta() && document.activeElement === boton);
check('marca el elegido', ops[1].attrs['aria-selected'] === 'true' && ops[0].attrs['aria-selected'] === 'false');
check('muestra su nombre', actual.textContent === 'Caso alyc');
boton.disparar('keydown', { key: 'ArrowDown' });
check('flecha en el botón abre', abierta() && document.activeElement === ops[1]);
lista.disparar('click', { target: ops[1] });
check('elegir el mismo no avisa', elegidos.join() === 'alyc' && !abierta());
boton.disparar('click'); lista.disparar('keydown', { key: 'Escape' });
check('Escape cierra sin elegir', !abierta() && raiz.dataset.value === 'alyc' && document.activeElement === boton);
boton.disparar('click'); raiz.disparar('focusout', { relatedTarget: ops[2] });
check('el foco adentro no cierra', abierta());
raiz.disparar('focusout', { relatedTarget: null });
check('tocar afuera cierra', !abierta());
boton.disparar('click'); boton.disparar('click');
check('el botón alterna', !abierta());
boton.disparar('click'); lista.disparar('click', { target: ops[2] });
check('clic elige', elegidos.join() === 'alyc,resenas' && !abierta());
console.log(fallos.join('\n')); process.exit(fallos.length ? 1 : 0);
"""
    js = tmp_path / "caso.mjs"
    js.write_text(arnes, encoding="utf-8")
    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr


def test_despues_de_cada_respuesta_vuelven_las_sugeridas():
    fn = _render(_INDEX.read_text(encoding="utf-8"))
    envio = fn[fn.index("async function sendCapChat(question)"):]
    envio = envio[:envio.index("\n      }\n")]
    # La pregunta queda anotada (para no volver a sugerirla) y la respuesta,
    # pendiente hasta que llega.
    assert "_agregar(slug, 'user', escapeHtml(question)).pregunta = question;" in envio
    assert "pend.pendiente = true;" in envio
    fin = envio[envio.index("} finally {"):]
    assert "pend.pendiente = false;" in fin and "if (pend.err) _mostrarSiguientes(slug);" in fin
    # Con respuesta, aparecen cuando termina de escribirse.
    tipeo = envio[envio.index("_typeInto(answerEl"):]
    assert "_mostrarSiguientes(slug);" in tipeo[:tipeo.index("});")]
    mostrar = fn[fn.index("const _mostrarSiguientes = (slug) => {"):]
    mostrar = mostrar[:mostrar.index("};")]
    assert "log.querySelectorAll('.cap-chat__ideas').forEach(n => n.remove());" in mostrar
    assert "capChatSeguirHTML(slug, _questionsFor(slug))" in mostrar
    # Con el mensaje nuevo se van.
    agregar = fn[fn.index("const _agregar = (slug, who, html) => {"):]
    assert "log.querySelectorAll('.cap-chat__ideas').forEach(n => n.remove());" in agregar[:agregar.index("};")]


def test_sin_entorno_no_queda_el_asistente():
    """El panel y su botón viven en <body>, fuera de la vista."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("function renderInfraView(")
    vista = html[i:html.index("state.lastStatus = data;", i)]
    assert vista.count("_quitarAsistente();") == 2        # provisionando y sin entorno
    j = html.index("function _quitarAsistente()")
    quitar = html[j:html.index("}", j)]
    assert "'deploy-capabilities'" in quitar and "'cap-lanzador'" in quitar


def test_el_css_del_panel():
    html = _INDEX.read_text(encoding="utf-8")
    css = html[:html.index("</style>")]
    panel = css[css.index("    .cap-flotante {"):]
    panel = panel[:panel.index("}")]
    assert "position: fixed" in panel and "display: none" in panel
    assert ".cap-flotante.is-abierto { display: flex; }" in css
    lanzador = css[css.index("    .cap-lanzador {"):]
    assert "position: fixed" in lanzador[:lanzador.index("}")]
    celu = css[css.index("@media (max-width: 560px) {\n      .cap-flotante"):]
    assert "width: 100vw" in celu[:200]


# ── Panel lateral ───────────────────────────────────────────────────────────
def test_el_asistente_es_un_panel_lateral_que_corre_el_contenido():
    """Primero fue una tarjeta que alargaba la página, después una burbuja: el
    pedido fue un panel lateral."""
    html = _INDEX.read_text(encoding="utf-8")
    css = html[:html.index("</style>")]
    panel = css[css.index("    .cap-flotante {"):]
    panel = panel[:panel.index("}")]
    assert "top: 0; right: 0; bottom: 0" in panel and "width: min(var(--asistente-w), 100vw)" in panel
    assert "body.con-asistente.en-entorno .app-shell { padding-right: var(--asistente-w); }" in css
    assert ".cap-lanzador.is-abierto { display: none; }" in css
    fn = _render(html)
    abrir = fn[fn.index("const _abrir = (on) => {"):]
    assert "document.body.classList.toggle('con-asistente', on);" in abrir[:abrir.index("};")]
    assert "document.body.classList.toggle('con-asistente', capChatAbierto);" in fn
    j = html.index("function _quitarAsistente()")
    assert "document.body.classList.remove('con-asistente');" in html[j:html.index("}", j)]


def test_las_sugerencias_se_toman_por_delegacion_y_no_hay_fila_de_fichas():
    """Las sugeridas se vuelven a pintar (al limpiar y después de cada
    respuesta): con listeners por botón quedaban muertas. La fila de fichas de
    abajo se fue: las sugeridas van en el chat, como tarjetas."""
    html = _INDEX.read_text(encoding="utf-8")
    fn = _render(html)
    assert "const qb = e.target.closest('.lp-q-btn');" in fn
    assert "if (qb && host.contains(qb)) sendCapChat(qb.dataset.q || '');" in fn
    assert "host.querySelectorAll('.lp-q-btn').forEach" not in fn
    for rastro in ("cap-chat__suggest", "is-vacio", "lp-tab-panel", "_syncVacio"):
        assert rastro not in html, rastro
    assert 'class="cap-chat__composer" id="cap-chat-form"' in fn
    assert 'id="cap-chat-clear" title="Nueva conversación"' in fn


def test_el_asistente_vive_solo_en_el_entorno():
    """En el resto de las vistas no están ni el panel ni su botón, y la barra
    vuelve a como estaba; al volver, si estaba abierto, se ve de nuevo."""
    html = _INDEX.read_text(encoding="utf-8")
    css = html[:html.index("</style>")]
    assert "body:not(.en-entorno) .cap-flotante,\n    body:not(.en-entorno) .cap-lanzador { display: none; }" in css
    i = html.index("    function showView(name) {")
    vista = html[i:html.index("\n    }\n", i)]
    assert "document.body.classList.toggle('en-entorno', enEntorno);" in vista
    assert "const enEntorno = name === 'infra';" in vista
    assert "_navConAsistente(enEntorno && capChatAbierto && !!document.getElementById('deploy-capabilities'));" in vista
    # Un re-render desde otra vista (p. ej. al hidratar el estado) no contrae la barra.
    assert "if (capChatAbierto && document.body.classList.contains('en-entorno')) _navConAsistente(true);" in _render(html)


def test_la_vista_previa_del_entorno():
    """?preview=entorno muestra la vista y el chat sin desplegar; nada sale del
    navegador salvo la reproducción de un deploy."""
    html = _INDEX.read_text(encoding="utf-8")
    assert "if (params.get('preview') === 'entorno') { _vistaPreviaEntorno(params); return; }" in html
    i = html.index("function _vistaPreviaEntorno(params)")
    fn = html[i:html.index("\n    }\n", i)]
    for ruta in ("/api/v1/terraform/status", "/api/v1/onboarding/apply-schema",
                 "/api/v1/onboarding/provision-capabilities", "/api/v1/pipelines/health",
                 "/api/v1/capabilities/ppl-chat"):
        assert ruta in fn, ruta
    assert "if (metodo !== 'GET' && u.includes('/api/v1/')) return json(" in fn, "ningún POST real"
    assert "real('/api/v1/dev/deploy-preview?paso=0.03')" in fn
    assert "if (params.get('chat') === '1') capChatAbierto = true;" in fn
