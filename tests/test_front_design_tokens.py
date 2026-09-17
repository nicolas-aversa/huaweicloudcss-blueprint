"""Audit del sistema de diseño del front.

El CSS de la app no se desarmó porque alguien escribiera CSS feo: se desarmó
porque nada impedía que cada componente inventara su propia tipografía, su propio
radio y su propia transición. Cuando se midió había **20 tamaños de fuente**
distintos (incluidos `12.5px`, `11.5px` y `9.5px`), **9 pesos** y **12
transiciones**.

Estos tests son presupuestos: cuentan los valores crudos que quedan fuera de los
tokens y fallan si crecen. Los números bajan a medida que se migran vistas — lo
que NO pueden hacer es subir. Si estás acá porque un test falló y agregaste un
valor nuevo, la pregunta no es "¿subo el presupuesto?" sino "¿por qué este
componente necesita un tamaño que la escala no tiene?".
"""
import pathlib
import re

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


@pytest.fixture(scope="module")
def css() -> str:
    """El bloque <style>, sin el `:root` donde los tokens SÍ definen valores."""
    html = _INDEX.read_text(encoding="utf-8")
    style = html[html.index("<style>"):html.index("</style>")]
    fin_root = style.index("}", style.index(":root {"))
    return style[:style.index(":root {")] + style[fin_root:]


def _crudos(css: str, prop: str, permitidos: set[str]) -> list[str]:
    """Valores de `prop` que no salen de un token ni están permitidos."""
    out = []
    for valor in re.findall(prop + r":\s*([^;{}]+)[;}]", css):
        v = valor.strip()
        if "var(--" in v or v in permitidos:
            continue
        out.append(v)
    return out


# Los presupuestos son el estado ALCANZADO, no una meta. Bajan con cada vista
# migrada; que suban significa que se agregó un valor fuera de la escala.
#
# Tanda A: 143 / 20 / 12. Tanda B: 2 / 0 / 0.
#
# Los dos font-size que quedan son deliberados y no son texto de interfaz:
#   * `clamp(30px, 4.8vw, 58px)` del título del hero — fluido por diseño, ninguna
#     escala fija puede expresarlo;
#   * `34px` del KPI del asistente — un número de display.
# Si el presupuesto tiene que subir de 2, el que está mal es el componente.
PRESUPUESTO_FS = 2
PRESUPUESTO_RADIO = 0
PRESUPUESTO_TRANSICION = 0


def test_los_tamanos_de_fuente_salen_de_la_escala(css):
    crudos = _crudos(css, "font-size", {"inherit", "0", "1em", "100%"})
    assert len(crudos) <= PRESUPUESTO_FS, (
        f"{len(crudos)} font-size fuera de la escala (presupuesto {PRESUPUESTO_FS}). "
        f"Usá --fs-xs..--fs-2xl. Los nuevos: {sorted(set(crudos))[:12]}")


def test_no_vuelven_los_pesos_intermedios(css):
    """550, 650 y 750 se colaron porque Inter se cargaba como fuente variable.
    La escala tiene tres pesos y el `<link>` ya solo pide esos tres: un peso
    intermedio hoy lo sintetiza el navegador y se ve como falsa negrita."""
    html = _INDEX.read_text(encoding="utf-8")
    pesos = set(re.findall(r"font-weight:\s*(\d+)", css))
    cargados = set(re.search(r"family=Inter:wght@([\d;]+)", html).group(1).split(";"))

    assert pesos <= cargados, (
        f"el CSS usa pesos que el <link> de Inter no carga: "
        f"{sorted(pesos - cargados)}. El navegador los sintetiza y se ven como "
        f"falsa negrita — o se agregan al link, o se usan los de la escala.")
    assert pesos <= {"400", "500", "600", "700"}, (
        f"pesos fuera de la escala: {sorted(pesos)}. Usá --fw-normal/--fw-medium/"
        f"--fw-semi.")


def test_los_radios_salen_de_la_escala(css):
    crudos = _crudos(css, "border-radius", {"50%", "0", "inherit", "999px"})
    assert len(crudos) <= PRESUPUESTO_RADIO, (
        f"{len(crudos)} border-radius crudos (presupuesto {PRESUPUESTO_RADIO}). "
        f"Usá --radius-sm..--radius-xl. Los nuevos: {sorted(set(crudos))[:12]}")


def test_las_transiciones_salen_de_los_dos_tokens(css):
    crudos = _crudos(css, "transition", {"none"})
    assert len(crudos) <= PRESUPUESTO_TRANSICION, (
        f"{len(crudos)} transiciones crudas (presupuesto {PRESUPUESTO_TRANSICION}). "
        f"Usá --t-fast o --t-base. Las nuevas: {sorted(set(crudos))[:6]}")


def test_nada_se_levanta_al_hover(css):
    """Cloudflare no levanta nada al pasar el cursor. Con varios elementos juntos
    —doce cards en el grid de ejemplos— el `translateY` más la sombra hacía que el
    conjunto se leyera inquieto.

    En vez de listar selectores uno por uno, se verifica que NINGUNA regla `:hover`
    tenga un transform: así también queda cubierto el componente que se agregue
    mañana. Los `translateY` fuera de `:hover` (keyframes, centrados) no se tocan."""
    hovers = re.findall(r"([^{}]*:hover[^{]*)\{([^}]*)\}", css)
    culpables = [sel.strip().splitlines()[-1].strip()
                 for sel, cuerpo in hovers if "translateY" in cuerpo]
    assert not culpables, (
        f"estas reglas se levantan al hover: {culpables}. El hover se marca con "
        f"fondo o borde, no moviendo el elemento de lugar.")


def test_el_favicon_esta_declarado():
    """Sin esto el navegador muestra el globo genérico, que fue el síntoma."""
    html = _INDEX.read_text(encoding="utf-8")
    assert 'rel="icon"' in html and "favicon.png" in html
    assert (_INDEX.parent / "favicon.png").is_file(), (
        "falta static/favicon.png — regeneralo con `py build_favicon.py`")


def test_los_comentarios_abren_y_cierran_en_orden():
    """Tokeniza el `<style>` buscando `/*` y `*/` huérfanos.

    Este es EL chequeo. Los otros dos no alcanzaron: un `*/` suelto no desbalancea
    el conteo (sigue habiendo tantas aperturas como cierres), no desbalancea las
    llaves, y no produce ningún comentario "largo con llaves adentro". Pero el
    navegador lo toma como el arranque de un selector y **consume hasta el `{`
    siguiente**, tirando esa regla entera a la basura.

    Pasó exactamente eso: un `*/` huérfano se comió `.app-view { display: none; }`
    y todas las vistas de la app se apilaron una debajo de otra, con el CSS
    aparentemente sano por cualquier otra medida.
    """
    html = _INDEX.read_text(encoding="utf-8")
    style = html[html.index("<style>"):html.index("</style>")]
    base = html[:html.index("<style>")].count("\n") + 1

    problemas, dentro, i = [], False, 0
    while i < len(style) - 1:
        par = style[i:i + 2]
        if par == "/*":
            if dentro:
                problemas.append(f"L{base + style[:i].count(chr(10))}: `/*` dentro de un comentario")
            dentro, i = True, i + 2
            continue
        if par == "*/":
            if not dentro:
                problemas.append(f"L{base + style[:i].count(chr(10))}: `*/` huérfano, sin apertura")
            dentro, i = False, i + 2
            continue
        i += 1
    if dentro:
        problemas.append("el último comentario del bloque nunca cierra")

    assert not problemas, "comentarios CSS mal formados:\n  " + "\n  ".join(problemas)


def test_todas_las_vistas_arrancan_ocultas():
    """`.app-view { display: none }` es lo único que impide que las 7 vistas se
    dibujen apiladas. Si el parser la descarta, la app se ve como una sola página
    larguísima con todas las secciones pegadas — y ningún test lo notaba."""
    html = _INDEX.read_text(encoding="utf-8")
    style = html[html.index("<style>"):html.index("</style>")]
    sin_comentarios = re.sub(r"/\*.*?\*/", "", style, flags=re.DOTALL)

    # Las reglas cuya pérdida rompe la app de forma evidente. Cada una se fue
    # alguna vez sin que ningún test lo notara.
    for selector, porque in (
        (r"\.app-view", "las 7 vistas se apilan una debajo de otra"),
        (r"\.step-pane", "los 4 pasos del wizard se apilan"),
        (r"\.hidden", "nada de lo que se oculta se oculta"),
    ):
        m = re.search(rf"(?m)^\s*{selector}\s*\{{([^}}]*)\}}", sin_comentarios)
        assert m, f"desapareció la regla base {selector} → {porque}"
        assert "display" in m.group(1) and "none" in m.group(1), \
            f"{selector} perdió `display: none` → {porque}: {m.group(1).strip()[:80]}"

    # Y en el markup, una sola vista arranca activa.
    activas = re.findall(r'<div class="app-view[^"]*\bactive\b[^"]*" id="(view-[\w-]+)"', html)
    assert activas == ["view-home"], f"vistas activas al cargar: {activas}"


def test_ningun_comentario_se_traga_css():
    """Un `*/` perdido convierte el resto del archivo en comentario.

    Pasó de verdad: un script de limpieza borró una regla cuyo comentario estaba
    pegado arriba, se llevó el `*/` y dejó el `{` del cuerpo. Desde ahí el
    navegador se comió 130 líneas de CSS — entre ellas `.icon`, así que TODOS los
    íconos de la app salieron como manchas negras y las flechas de los botones
    ocuparon media pantalla.

    Contar `/*` contra `*/` NO alcanza: cuando falta un cierre, el `/*` siguiente
    hace de cierre y el conteo queda balanceado igual, con CSS comido en el medio.
    La señal real es un comentario largo que contiene llaves.
    """
    html = _INDEX.read_text(encoding="utf-8")
    style = html[html.index("<style>"):html.index("</style>")]

    culpables = []
    for m in re.finditer(r"/\*.*?\*/", style, re.DOTALL):
        cuerpo = m.group(0)
        # Los comentarios cortos SÍ pueden citar CSS de ejemplo; un comentario de
        # 5+ líneas con llaves adentro es una regla que se perdió.
        if cuerpo.count("\n") >= 5 and ("{" in cuerpo or "}" in cuerpo):
            linea = style[:m.start()].count("\n") + html[:html.index("<style>")].count("\n") + 1
            culpables.append(f"L{linea}: {cuerpo.splitlines()[0][:70]}")

    assert not culpables, (
        "hay comentarios que se están tragando reglas CSS:\n  " + "\n  ".join(culpables))


def test_la_regla_base_de_los_iconos_esta_viva():
    """`.icon` da tamaño Y `fill: none` a los 37 símbolos del sprite.

    Sin ella un `<svg class="icon">` sin otra regla cae al tamaño por defecto del
    elemento reemplazado (300×150) y se pinta con `fill: black`. Es el síntoma que
    delató el comentario roto: flechas negras gigantes en los botones del home.
    """
    html = _INDEX.read_text(encoding="utf-8")
    style = html[html.index("<style>"):html.index("</style>")]
    sin_comentarios = re.sub(r"/\*.*?\*/", "", style, flags=re.DOTALL)

    # Anclada al inicio de línea: `.btn-sm .icon {` también empieza con `.icon`
    # tras un espacio, y esa NO es la regla base.
    m = re.search(r"^\s*\.icon\s*\{([^}]*)\}", sin_comentarios, re.MULTILINE)
    assert m, "desapareció la regla base .icon (o quedó dentro de un comentario)"
    cuerpo = m.group(1)
    for prop in ("width", "height", "fill", "stroke"):
        assert prop in cuerpo, f".icon perdió `{prop}`: {cuerpo.strip()[:120]}"


def test_no_quedo_rastro_del_pie_del_nav():
    """La región y el puntito verde se sacaron del rail; su CSS y el JS que los
    alimentaba se van con ellos, o queda código muerto apuntando a la nada."""
    html = _INDEX.read_text(encoding="utf-8")
    assert "app-nav__footer" not in html


def test_el_deploy_no_hardcodea_fresh_deploy():
    """`fresh_deploy: true` en todo deploy del wizard hacía que agregar un caso a
    un entorno con tres pipelines dejara UNA: el backend descartaba el registro
    y Terraform destruía el resto. Con entorno activo se agrega, no se pisa."""
    html = _INDEX.read_text(encoding="utf-8")
    assert not re.search(r"fresh_deploy:\s*true", html), "fresh_deploy hardcodeado en true"
    assert "fresh_deploy: !state.envActive" in html


def test_el_rail_oculta_la_barra_pero_sigue_scrolleando(css):
    """Con los sub-pasos del wizard abiertos el rail no entraba en una laptop y
    aparecía la barra de scroll al lado del menú. Se oculta la barra pero NO el
    scroll: en una pantalla más chica todavía tiene que poder bajarse con la
    rueda. Las dos cosas juntas, o se rompe una."""
    m = re.search(r"^\s*\.app-nav__items\s*\{([^}]*)\}", css, re.MULTILINE)
    assert m, "desapareció la regla base .app-nav__items"
    cuerpo = m.group(1)
    assert re.search(r"overflow-y:\s*auto", cuerpo), "el rail dejó de scrollear"
    assert re.search(r"scrollbar-width:\s*none", cuerpo), "la barra vuelve a verse (Firefox)"
    assert re.search(r"\.app-nav__items::-webkit-scrollbar\s*\{[^}]*display:\s*none", css), \
        "la barra vuelve a verse (Chrome/Edge/Safari)"
