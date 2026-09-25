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
// El saludo trae hasta cuatro preguntas para arrancar, como tarjetas.
const saludo = capChatLogHTML('ventas', 'Ventas', ['a', 'b', 'c', 'd', 'e']);
check('vacío: cuatro ideas', (saludo.match(/class="cap-chat__idea lp-q-btn"/g) || []).length === 4, saludo);
check('vacío: la idea es la pregunta', saludo.includes('data-q="a"') && !saludo.includes('data-q="e"'));
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


def test_el_caso_se_elige_en_la_cabecera():
    fn = _render(_INDEX.read_text(encoding="utf-8"))
    assert '<select class="cap-flotante__caso" id="cap-chat-caso" aria-label="Caso">' in fn
    assert "(host.querySelector('#cap-chat-caso')?.value) || activeSlugs[0]" in fn
    cambio = fn[fn.index("host.querySelector('#cap-chat-caso')?.addEventListener('change'"):]
    assert "capChatSlug = slug;" in cambio[:600]


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
    assert "body.con-asistente .app-shell { padding-right: var(--asistente-w); }" in css
    assert ".cap-lanzador.is-abierto { display: none; }" in css
    fn = _render(html)
    abrir = fn[fn.index("const _abrir = (on) => {"):]
    assert "document.body.classList.toggle('con-asistente', on);" in abrir[:abrir.index("};")]
    assert "document.body.classList.toggle('con-asistente', capChatAbierto);" in fn
    j = html.index("function _quitarAsistente()")
    assert "document.body.classList.remove('con-asistente');" in html[j:html.index("}", j)]


def test_las_sugerencias_se_toman_por_delegacion_y_se_ocultan_al_arrancar():
    """Las del saludo se vuelven a pintar al limpiar: con listeners por botón
    quedaban muertas. Abajo, la fila aparece recién con conversación."""
    html = _INDEX.read_text(encoding="utf-8")
    fn = _render(html)
    assert "const qb = e.target.closest('.lp-q-btn');" in fn
    assert "if (qb && host.contains(qb)) sendCapChat(qb.dataset.q || '');" in fn
    assert "host.querySelectorAll('.lp-q-btn').forEach" not in fn
    assert "host.classList.toggle('is-vacio', !(capChats[_activeChatSlug()] || []).length)" in fn
    assert ".cap-flotante.is-vacio .cap-chat__suggest-row { display: none; }" in html
    # Sin scrollbar a la vista, y el campo con el enviar adentro.
    assert "scrollbar-width: none" in html and 'class="cap-chat__composer" id="cap-chat-form"' in fn
    assert 'id="cap-chat-clear" title="Nueva conversación"' in fn


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
