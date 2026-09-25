"""La cuenta va arriba de la barra, como en Cloudflare; abajo, solo contraer.

Antes, arriba estaba la marca ("CSS Blueprint · Huawei Cloud") y abajo, debajo
del botón de contraer, una caja con el mail y el botón de salir.
"""
import pathlib
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"
HTML = _INDEX.read_text(encoding="utf-8")
CSS = HTML[:HTML.index("</style>")]


def _nav() -> str:
    i = HTML.index('<nav class="app-nav" id="app-nav">')
    return HTML[i:HTML.index("</nav>", i)]


def test_la_cuenta_esta_arriba_en_el_lugar_de_la_marca():
    nav = _nav()
    marca = nav[:nav.index('<div class="app-nav__items">')]
    assert 'id="nav-user"' in marca and 'aria-haspopup="menu"' in marca
    assert 'id="nav-user-email"' in marca and '<use href="#ic-selector"/>' in marca
    # El menú, con el mail completo y "Cerrar sesión".
    i = marca.index('id="nav-user-menu"')
    menu = marca[i:]
    assert 'id="nav-user-email-full"' in menu and 'id="nav-logout"' in menu and "Cerrar sesión" in menu
    assert '<symbol id="ic-selector"' in HTML


def test_abajo_queda_solo_el_boton_de_contraer():
    nav = _nav()
    pie = nav[nav.index('<div class="app-nav__pie">'):]
    assert 'id="nav-toggle"' in pie
    assert "nav-user" not in pie and "nav-logout" not in pie
    assert "app-nav__user-avatar" not in HTML and ".app-nav__user {" not in CSS


def test_con_sesion_la_cuenta_reemplaza_a_la_marca():
    i = HTML.index("_fetch('/auth/me'")
    js = HTML[i:HTML.index("}).catch(", i)]
    assert "['nav-marca-logo', 'nav-marca-texto'].forEach(" in js
    assert "el.classList.add('hidden')" in js and "box.classList.remove('hidden');" in js
    assert "menuDeCuenta(box, document.getElementById('nav-user-menu'));" in js
    # Sin sesión (single-user) no se toca: queda la marca.
    assert js.index("if (!d || !d.auth) return;") < js.index("nav-marca-logo")


def test_el_menu_no_lo_recorta_la_barra_y_contraida_queda_el_logo():
    i = CSS.index("    .app-nav__menu {")
    menu = CSS[i:CSS.index("}", i)]
    assert "position: fixed;" in menu, "la barra tiene overflow hidden: absolute lo recortaría"
    celu = CSS[CSS.index("@media (min-width: 769px) {\n      body.nav-contraida"):]
    celu = celu[:celu.index("\n    }\n")]
    assert "body.nav-contraida .app-nav__cuenta-flechas," in celu
    assert "body.nav-contraida .app-nav__user-email { display: none; }" in celu


def test_la_vista_previa_trae_una_sesion_de_ejemplo():
    i = HTML.index("function _vistaPreviaEntorno(params) {")
    fn = HTML[i:HTML.index("\n    }\n", i)]
    assert "if (u.includes('/auth/me')) return json({ auth: true, logged_in: true," in fn
    assert "if (u.includes('/auth/logout')) return json({ ok: true });" in fn


def test_el_menu_abre_cierra_con_afuera_y_con_escape(tmp_path):
    """En node, con la función real."""
    if shutil.which("node") is None:
        pytest.skip("node no está instalado")
    i = HTML.index("      function menuDeCuenta(btn, menu) {")
    fn = HTML[i:HTML.index("\n      }\n", i) + 9]
    arnes = r"""
const oyentes = {};
const document = { addEventListener: (t, f) => { (oyentes[t] = oyentes[t] || []).push(f); } };
function el() {
  const clases = new Set(), attrs = {}, propios = {};
  return {
    clases, attrs, enfocado: false,
    classList: { contains: (c) => clases.has(c), toggle: (c, on) => { on ? clases.add(c) : clases.delete(c); } },
    setAttribute: (k, v) => { attrs[k] = v; },
    addEventListener: (t, f) => { (propios[t] = propios[t] || []).push(f); },
    disparar(t, e) { (propios[t] || []).forEach(f => f(e)); },
    contains(x) { return x === this || x === adentro; },
    focus() { this.enfocado = true; },
  };
}
const adentro = {};
const btn = el(), menu = el();
menu.clases.add('hidden');
""" + fn + r"""
menuDeCuenta(btn, menu);
const fallos = [];
const check = (n, c) => { if (!c) fallos.push(n); };
const clicBoton = () => { const e = { stopPropagation() {} }; btn.disparar('click', e); };
const clicDoc = (target) => (oyentes.click || []).forEach(f => f({ target }));
const tecla = (key) => (oyentes.keydown || []).forEach(f => f({ key }));
clicBoton(); check('abre', !menu.clases.has('hidden') && btn.attrs['aria-expanded'] === 'true');
clicDoc(adentro); check('clic adentro no cierra', !menu.clases.has('hidden'));
clicDoc({}); check('clic afuera cierra', menu.clases.has('hidden') && btn.attrs['aria-expanded'] === 'false');
clicBoton(); clicBoton(); check('el botón alterna', menu.clases.has('hidden'));
clicBoton(); tecla('Enter'); check('otra tecla no cierra', !menu.clases.has('hidden'));
tecla('Escape'); check('Escape cierra y vuelve al botón', menu.clases.has('hidden') && btn.enfocado);
console.log(fallos.join('\n')); process.exit(fallos.length ? 1 : 0);
"""
    js = tmp_path / "cuenta.mjs"
    js.write_text(arnes, encoding="utf-8")
    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
