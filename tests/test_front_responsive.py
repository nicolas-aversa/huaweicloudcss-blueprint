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
    i = CSS.index("    #view-home,\n    #view-infra .infra-dash__banner {")
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
    assert 'id="nav-toggle"' in HTML and "Contraer menú" in HTML
    celu = CSS[CSS.index("@media (min-width: 769px) {\n      body.nav-contraida"):]
    celu = celu[:celu.index("\n    }\n")]
    assert "body.nav-contraida { --rail-w: 68px; }" in celu
    for oculto in (".nav-item__label", ".app-nav__brand-text", ".nav-substeps", ".app-nav__env-detail"):
        assert f"body.nav-contraida {oculto}" in celu, oculto
    assert "@media (max-width: 768px) { .app-nav__toggle { display: none; } }" in CSS, "en el celular no"


def test_la_preferencia_queda_en_el_navegador_y_los_iconos_tienen_nombre():
    i = HTML.index("(function navContraible() {")
    fn = HTML[i:HTML.index("})();", i)]
    assert "localStorage.getItem('navContraida') === '1'" in fn
    assert "localStorage.setItem('navContraida', on ? '1' : '0')" in fn
    assert re.search(r"try \{ guardado = localStorage", fn), "sin storage, arranca expandida"
    assert "b.title = lbl.textContent.trim();" in fn
    assert "document.body.classList.toggle('nav-contraida', on);" in fn
