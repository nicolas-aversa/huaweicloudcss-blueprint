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
check('vacío', capChatLogHTML('ventas', 'Ventas').includes('Preguntame sobre Ventas'));

// Pregunta en A, mientras se espera la respuesta.
capChatAgregar('ventas', 'user', 'hola &lt;b&gt;');
const pend = capChatAgregar('ventas', 'bot', '···');
check('ids distintos', pend.id !== capChats.ventas[0].id);

// "Re-render" (se arma el log desde lo guardado): la conversación sigue ahí.
let a = capChatLogHTML('ventas', 'Ventas');
check('re-render: la pregunta', a.includes('hola &lt;b&gt;'), a);
check('re-render: la espera', a.includes('···'));
check('re-render: sin invitación', !a.includes('Preguntame sobre'));
check('re-render: cada fila con su id', a.includes(`data-msg="${pend.id}"`));

// B tiene su propia conversación, vacía.
check('otro caso vacío', capChatLogHTML('fraude', 'Fraude').includes('Preguntame sobre Fraude'));

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
check('limpiar A', capChatLogHTML('ventas', 'Ventas').includes('Preguntame sobre Ventas'));
check('B intacto', capChatLogHTML('fraude', 'Fraude').includes('No se pudo responder.'));

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
    assert "capChatLogHTML(s, SLUG_LABELS[s] || 'tus datos')" in fn
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
