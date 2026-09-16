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


def test_no_quedo_rastro_del_pie_del_nav():
    """La región y el puntito verde se sacaron del rail; su CSS y el JS que los
    alimentaba se van con ellos, o queda código muerto apuntando a la nada."""
    html = _INDEX.read_text(encoding="utf-8")
    assert "app-nav__footer" not in html
