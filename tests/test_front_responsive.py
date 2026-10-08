"""La vista se acomoda al ancho REAL del contenido, y la barra se contrae.

Con el panel del asistente abierto, el banner del entorno calculaba su
ensanchado contra el ancho fijo del contenedor: se achicaba al centro y las
tarjetas quedaban apiladas en una columna. Y la barra izquierda (252 px) no
se podía achicar para dar lugar.
"""
import pathlib
import re

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"
HTML = _INDEX.read_text(encoding="utf-8")
CSS = HTML[:HTML.index("</style>")]


def _regla(selector: str) -> str:
    i = CSS.index(f"    {selector} {{")
    return CSS[i:CSS.index("}", i)]


def test_el_area_de_contenido_es_un_container():
    regla = _regla(".main-content")
    assert "container-type: inline-size" in regla and "container-name: principal" in regla


def test_el_ensanchado_mide_el_contenido_y_nunca_achica():
    i = CSS.index("    #view-home {\n      --ancho-amplio")
    regla = CSS[i:CSS.index("}", i)]
    assert "--ancho-amplio: min(var(--container-wide), calc(100cqi - 56px))" in regla
    # min(0px, …): los márgenes ensanchan (negativos) o quedan en 0; jamás
    # positivos, que era lo que achicaba el banner.
    assert regla.count("min(0px, calc((100% - var(--ancho-amplio)) / 2))") == 2
    assert "var(--container-w) -" not in regla


def test_la_matriz_de_casos_se_desplaza_sin_perder_el_caso():
    # En un teléfono la tabla se desplaza a lo ancho y la columna del caso
    # queda fija; los nombres no se parten.
    assert "overflow-x: auto" in _regla(".mtx-scroll")
    fija = _regla(".mtx__caso, .mtx__th-caso")
    assert "position: sticky" in fija and "left: 0" in fija and "background: var(--bg-primary)" in fija
    assert "white-space: nowrap" in _regla(".mtx__nombre")
    # Accesos y snapshot lado a lado; en un teléfono, uno debajo del otro.
    assert "grid-template-columns: repeat(auto-fit, minmax(min(380px, 100%), 1fr))" in _regla(".env-extras")
    idx = _regla(".pipe-row__idx")
    assert "white-space: nowrap" in idx and "text-overflow: ellipsis" in idx
    assert "white-space: nowrap" in _regla(".pipe-row__prefix")


def test_la_barra_se_contrae_a_iconos():
    # Como el de Cloudflare: un ícono de panel al pie, sin texto (el nombre
    # queda en el tooltip y en aria-label).
    i = HTML.index('<div class="app-nav__pie">')
    pie = HTML[i:HTML.index("</div>", i)]
    assert 'id="nav-toggle"' in pie and '<use href="#ic-panel"/>' in pie
    assert 'aria-label="Contraer la barra lateral"' in pie and "Contraer menú" not in HTML
    assert '<symbol id="ic-panel"' in HTML
    celu = CSS[CSS.index("@media (min-width: 769px) {\n      body.nav-contraida"):]
    celu = celu[:celu.index("\n    }\n")]
    assert "body.nav-contraida { --rail-w: 68px; }" in celu
    for oculto in (".nav-item__label", ".app-nav__brand-text", ".nav-substeps"):
        assert f"body.nav-contraida {oculto}" in celu, oculto
    assert "@media (max-width: 768px) { .app-nav__pie { display: none; } }" in CSS, "en el celular no"


def test_la_preferencia_queda_en_el_navegador_y_los_iconos_tienen_nombre():
    i = HTML.index("(function navContraible() {")
    fn = HTML[i:HTML.index("})();", i)]
    assert "localStorage.getItem('navContraida') === '1'" in fn
    assert "localStorage.setItem('navContraida', on ? '1' : '0')" in fn
    assert re.search(r"try \{ guardado = localStorage", fn), "sin storage, arranca expandida"
    assert "b.title = lbl.textContent.trim();" in fn
    assert "aplicarNavContraida(on);" in fn
    # Con el asistente abierto el botón no hace nada: primero se cierra él.
    assert fn.index("if (btn.getAttribute('aria-disabled') === 'true') return;") < fn.index("aplicarNavContraida(on);")
    assert "navAntesDelAsistente" not in fn[fn.index("btn.addEventListener('click'"):]
    j = HTML.index("function aplicarNavContraida(on) {")
    aplicar = HTML[j:HTML.index("\n    }\n", j)]
    assert "document.body.classList.toggle('nav-contraida', on);" in aplicar
    assert "btn.setAttribute('aria-label', texto);" in aplicar
    assert "const bloqueada = navAntesDelAsistente !== null;" in aplicar
    assert "btn.setAttribute('aria-disabled', String(bloqueada));" in aplicar
    assert "'Cerrá el asistente para expandir la barra'" in aplicar
    assert '.app-nav__toggle[aria-disabled="true"]:hover { opacity: .4; cursor: not-allowed;' in CSS


def test_el_asistente_contrae_la_barra_y_al_cerrarse_la_deja_como_estaba():
    i = HTML.index("function _navConAsistente(abierto) {")
    fn = HTML[i:HTML.index("\n    }\n", i)]
    # Abrir: recuerda cómo estaba (una sola vez) y contrae.
    assert "if (navAntesDelAsistente === null) navAntesDelAsistente = document.body.classList.contains('nav-contraida');" in fn
    assert "aplicarNavContraida(true);" in fn
    # Cerrar: olvida (así el botón se desbloquea) y vuelve a como estaba.
    assert fn.index("navAntesDelAsistente = null;") < fn.index("aplicarNavContraida(antes);")
    # Lo llaman abrir/cerrar, el re-armado con el panel abierto y la salida del asistente.
    assert "_navConAsistente(on);" in HTML
    assert "if (capChatAbierto && document.body.classList.contains('en-entorno')) _navConAsistente(true);" in HTML
    j = HTML.index("function _quitarAsistente()")
    assert "_navConAsistente(false);" in HTML[j:HTML.index("\n    }\n", j)]


def test_abrir_y_cerrar_el_asistente_con_la_barra_de_cada_forma(tmp_path):
    """En node, con las funciones reales: la barra vuelve a como estaba."""
    import shutil
    import subprocess
    import pytest
    if shutil.which("node") is None:
        pytest.skip("node no está instalado")
    i = HTML.index("    function aplicarNavContraida(on) {")
    aplicar = HTML[i:HTML.index("\n    }\n", i) + 7]
    j = HTML.index("    let navAntesDelAsistente = null;")
    nav = HTML[j:HTML.index("\n    }\n", HTML.index("function _navConAsistente", j)) + 7]
    arnes = r"""
const clases = new Set();
const boton = { attrs: {}, title: '', setAttribute(k, v) { this.attrs[k] = String(v); } };
const document = {
  body: { classList: { toggle(c, on) { on ? clases.add(c) : clases.delete(c); }, contains: (c) => clases.has(c) } },
  getElementById: () => boton,
};
const bloqueado = () => boton.attrs['aria-disabled'] === 'true';
""" + aplicar + nav + r"""
const fallos = [];
const check = (n, c) => { if (!c) fallos.push(n); };
// Expandida: abrir la contrae, cerrar la expande.
check('sin asistente: libre', (aplicarNavContraida(false), !bloqueado()) && boton.title === 'Contraer la barra lateral');
_navConAsistente(true);  check('abrir contrae', clases.has('nav-contraida'));
check('abierto: bloqueado y dice por qué', bloqueado() && boton.title === 'Cerrá el asistente para expandir la barra');
_navConAsistente(true);  check('re-render abierto sigue contraída', clases.has('nav-contraida'));
_navConAsistente(false); check('cerrar vuelve a expandida', !clases.has('nav-contraida'));
check('cerrado: libre otra vez', !bloqueado() && boton.title === 'Contraer la barra lateral');
// Ya contraída por la persona: cerrar el asistente la deja contraída.
aplicarNavContraida(true);
_navConAsistente(true); _navConAsistente(false);
check('contraída antes, contraída después', clases.has('nav-contraida'));
check('contraída y cerrado: libre para expandir', !bloqueado() && boton.title === 'Expandir la barra lateral');
// Cerrar sin haber abierto no toca nada.
aplicarNavContraida(false); _navConAsistente(false);
check('cerrar sin abrir no toca', !clases.has('nav-contraida'));
console.log(fallos.join('\n')); process.exit(fallos.length ? 1 : 0);
"""
    js = tmp_path / "nav.mjs"
    js.write_text(arnes, encoding="utf-8")
    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr


def test_el_entorno_usa_todo_el_ancho():
    """Es un tablero: con 880 px y 9 casos todo iba en una columna angosta y la
    mitad de la pantalla quedaba vacía. Usa el área de contenido (hasta 1.640
    px), con márgenes que ensanchan y nunca achican, como el Home."""
    i = CSS.index("    #view-infra {\n      --ancho-entorno")
    regla = CSS[i:CSS.index("}", i)]
    assert "--ancho-entorno: min(1640px, calc(100cqi - 56px))" in regla
    assert regla.count("min(0px, calc((100% - var(--ancho-entorno)) / 2))") == 2
    # El Home sigue con su propio ancho.
    assert CSS.count("--ancho-amplio:") == 1


def test_el_boton_de_contraer_no_se_mueve_y_cambia_la_linea():
    """Como en Cloudflare: el botón queda en el mismo lugar; el ícono espeja
    la línea (izquierda expandida, derecha contraída)."""
    assert ".app-nav__pie { flex-shrink: 0; padding: 8px 18px;" in CSS
    assert "body.nav-contraida .app-nav__pie" not in CSS, "contraída no se recentra"
    celu = CSS[CSS.index("@media (min-width: 769px) {\n      body.nav-contraida"):]
    celu = celu[:celu.index("\n    }\n")]
    assert "body.nav-contraida .app-nav__toggle .icon { transform: scaleX(-1); }" in celu


def test_la_scrollbar_es_fina_y_roja():
    assert "::-webkit-scrollbar { width: 6px; height: 6px; }" in CSS
    assert "::-webkit-scrollbar-thumb { background: var(--accent); border-radius: 999px; }" in CSS
    # Firefox (sin los pseudo-elementos): las propiedades estándar, solo ahí,
    # porque en Chrome anularían los pseudo-elementos.
    ff = CSS[CSS.index("@supports not selector(::-webkit-scrollbar) {"):]
    assert "scrollbar-width: thin; scrollbar-color: var(--accent) transparent;" in ff[:200]


def test_las_pestanas_de_casos_no_se_salen_con_muchos_casos():
    """Con 9 casos las etiquetas se partían en dos líneas y la última pestaña
    se salía de la tarjeta: una sola fila, sin cortes, que se desplaza."""
    html = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    i = html.index("    .case-tabs {")
    tabs = html[i:html.index("}", i)]
    tab = html[html.index("    .case-tab {"):html.index("}", html.index("    .case-tab {"))]
    assert "overflow-x: auto" in tabs and "border-bottom: 2px" not in tabs
    # Arriba, un indicador de 2 px en la posición de la activa (no la barra de
    # desplazamiento, que es gruesa y no dice dónde estás); la línea de base abajo.
    assert "transform" not in tabs and "transform" not in tab
    assert "box-shadow: inset 0 -2px 0 var(--border-subtle)" in tabs
    assert "scrollbar-width: none" in tabs and ".case-tabs::-webkit-scrollbar { display: none; }" in html
    # El riel propio: fino, y deslizarlo elige la pestaña de esa posición.
    assert "riel.addEventListener('pointerdown'" in html and "if (tab && !tab.classList.contains('is-active')) tab.click();" in html
    assert "white-space: nowrap" in tab and "flex: 1 0 auto" in tab and "margin-bottom: -2px" not in tab
    assert html.count("tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });") == 2


def test_el_riel_marca_la_posicion(tmp_path):
    """El segmento mide una pestaña sobre el total y está donde está la activa;
    deslizarlo elige la pestaña que queda debajo."""
    import json
    import shutil
    import subprocess
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no está instalado")
    html = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    i = html.index("    function posicionDelRiel(")
    fn = html[i:html.index("    function _rielDe(", i)]
    js = tmp_path / "t.mjs"
    js.write_text(fn + """
console.log(JSON.stringify({
  pos: [posicionDelRiel(4, 0), posicionDelRiel(4, 3), posicionDelRiel(8, 4), posicionDelRiel(1, 0)],
  tab: [pestanaEnElRiel(8, 0), pestanaEnElRiel(8, 0.56), pestanaEnElRiel(8, 1), pestanaEnElRiel(8, 1.4), pestanaEnElRiel(8, -1)],
}));
""", encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    salida = json.loads(r.stdout)
    assert salida["pos"][:2] == [{"ancho": 25, "izquierda": 0}, {"ancho": 25, "izquierda": 75}]
    assert salida["pos"][2]["ancho"] == 12.5 and abs(salida["pos"][2]["izquierda"] - 50) < 0.01, "la 5.ª de 8"
    assert salida["pos"][3] == {"ancho": 100, "izquierda": 0}
    assert salida["tab"] == [0, 4, 7, 7, 0]
