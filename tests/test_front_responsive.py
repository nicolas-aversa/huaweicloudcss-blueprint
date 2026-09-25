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


def test_las_filas_de_pipelines_bajan_de_renglon_sin_partir_nombres():
    assert "flex-wrap: wrap" in _regla(".pipe-row")
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
    for oculto in (".nav-item__label", ".app-nav__brand-text", ".nav-substeps", ".app-nav__env-detail"):
        assert f"body.nav-contraida {oculto}" in celu, oculto
    assert "@media (max-width: 768px) { .app-nav__pie { display: none; } }" in CSS, "en el celular no"


def test_la_preferencia_queda_en_el_navegador_y_los_iconos_tienen_nombre():
    i = HTML.index("(function navContraible() {")
    fn = HTML[i:HTML.index("})();", i)]
    assert "localStorage.getItem('navContraida') === '1'" in fn
    assert "localStorage.setItem('navContraida', on ? '1' : '0')" in fn
    assert re.search(r"try \{ guardado = localStorage", fn), "sin storage, arranca expandida"
    assert "b.title = lbl.textContent.trim();" in fn
    assert "aplicarNavContraida(on);" in fn and "navAntesDelAsistente = null;" in fn
    j = HTML.index("function aplicarNavContraida(on) {")
    aplicar = HTML[j:HTML.index("\n    }\n", j)]
    assert "document.body.classList.toggle('nav-contraida', on);" in aplicar
    assert "btn.setAttribute('aria-label', texto);" in aplicar


def test_el_asistente_contrae_la_barra_y_al_cerrarse_la_deja_como_estaba():
    i = HTML.index("function _navConAsistente(abierto) {")
    fn = HTML[i:HTML.index("\n    }\n", i)]
    # Abrir: recuerda cómo estaba (una sola vez) y contrae.
    assert "if (navAntesDelAsistente === null) navAntesDelAsistente = document.body.classList.contains('nav-contraida');" in fn
    assert "aplicarNavContraida(true);" in fn
    # Cerrar: vuelve a como estaba, y olvida.
    assert "aplicarNavContraida(navAntesDelAsistente);" in fn and "navAntesDelAsistente = null;" in fn
    # Lo llaman abrir/cerrar, el re-armado con el panel abierto y la salida del asistente.
    assert "_navConAsistente(on);" in HTML
    assert "if (capChatAbierto) _navConAsistente(true);" in HTML
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
const document = {
  body: { classList: { toggle(c, on) { on ? clases.add(c) : clases.delete(c); }, contains: (c) => clases.has(c) } },
  getElementById: () => ({ setAttribute() {}, set title(v) {} }),
};
""" + aplicar + nav + r"""
const fallos = [];
const check = (n, c) => { if (!c) fallos.push(n); };
// Expandida: abrir la contrae, cerrar la expande.
_navConAsistente(true);  check('abrir contrae', clases.has('nav-contraida'));
_navConAsistente(true);  check('re-render abierto sigue contraída', clases.has('nav-contraida'));
_navConAsistente(false); check('cerrar vuelve a expandida', !clases.has('nav-contraida'));
// Ya contraída por la persona: cerrar el asistente la deja contraída.
aplicarNavContraida(true);
_navConAsistente(true); _navConAsistente(false);
check('contraída antes, contraída después', clases.has('nav-contraida'));
// Cerrar sin haber abierto no toca nada.
aplicarNavContraida(false); _navConAsistente(false);
check('cerrar sin abrir no toca', !clases.has('nav-contraida'));
console.log(fallos.join('\n')); process.exit(fallos.length ? 1 : 0);
"""
    js = tmp_path / "nav.mjs"
    js.write_text(arnes, encoding="utf-8")
    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr


def test_el_entorno_tiene_el_ancho_de_las_demas_paginas():
    """Ni el banner ni la vista se ensanchan: al contraer la barra, "Entorno
    desplegado" quedaba más ancho que el resto de las páginas."""
    assert CSS.count("--ancho-amplio:") == 1
    i = CSS.index("--ancho-amplio:")
    selector = CSS[CSS.rindex("*/", 0, i) + 2:i]
    assert selector.strip() == "#view-home {", selector
    assert "#view-infra" not in selector and "infra-dash__banner" not in selector


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
