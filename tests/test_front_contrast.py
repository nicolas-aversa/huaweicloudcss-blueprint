"""Contraste de los tokens de color contra los fondos reales de la app.

Este archivo existe por un error concreto. Al pasar el acento del bordó al rojo
oficial de Huawei, el razonamiento fue "rojo sobre blanco da 4.85:1, pasa AA, listo".
Pero esta app casi nunca pinta texto sobre blanco puro: el canvas es un off-white
cálido y el modismo dominante es `background: var(--accent-tint); color: <acento>`.
Sobre ese tint el rojo oficial da **4.20:1**, debajo del 4.5 que pide WCAG AA para
texto normal — y el bug habría pasado desapercibido, porque a ojo "se ve rojo".

De ahí la regla que estos tests fijan: **el acento pinta fondos, bordes e íconos; el
texto lo pinta el tono oscuro**. Si alguien cambia la paleta y rompe eso, falla acá y
no en una auditoría de accesibilidad seis meses después.

`tests/test_front_design_tokens.py` no puede atrapar esto: su fixture recorta el
`:root` entero antes de medir, justamente porque ahí los tokens SÍ definen valores
crudos.
"""
import pathlib
import re

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"

# WCAG 2.1, 1.4.3: 4.5:1 para texto normal, 3:1 para texto grande (≥24px, o ≥18.66px
# en negrita) y para componentes gráficos (1.4.11, que cubre los íconos).
AA_TEXTO = 4.5
AA_GRANDE = 3.0


def _luminancia(rgb: tuple[float, float, float]) -> float:
    """Luminancia relativa (WCAG 2.1, definición de `relative luminance`)."""
    canal = []
    for c in rgb:
        c = c / 255
        canal.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = canal
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(frente: tuple, fondo: tuple) -> float:
    a, b = _luminancia(frente), _luminancia(fondo)
    claro, oscuro = max(a, b), min(a, b)
    return (claro + 0.05) / (oscuro + 0.05)


def _hex(valor: str) -> tuple[float, float, float]:
    v = valor.strip().lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def _sobre(frente: tuple, alpha: float, fondo: tuple) -> tuple[float, float, float]:
    """Compone un color semitransparente sobre un fondo opaco.

    Hace falta porque `--accent-tint` es un rgba: el fondo EFECTIVO detrás del texto
    no es el token, es el token ya mezclado con la superficie de abajo.
    """
    return tuple(f * alpha + b * (1 - alpha) for f, b in zip(frente, fondo))


@pytest.fixture(scope="module")
def tokens() -> dict[str, str]:
    """Los tokens de color del `:root`, tal como los lee el navegador.

    Los comentarios se borran ANTES de parsear: el `:root` está muy comentado y una
    prosa como "…5.06 sobre --bg-tertiary: pasa contra todos los fondos…" parsea
    como una declaración cuyo valor se extiende hasta el `;` siguiente — que es el
    de la declaración REAL de abajo, y se la traga. Pasó con `--accent-hover`.
    """
    html = _INDEX.read_text(encoding="utf-8")
    root = html[html.index(":root {"):html.index("}", html.index(":root {"))]
    root = re.sub(r"/\*.*?\*/", "", root, flags=re.DOTALL)
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"(--[\w-]+):\s*([^;]+);", root)}


def _rgba(valor: str) -> tuple[tuple, float]:
    m = re.match(r"rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+))?\s*\)", valor)
    assert m, f"no pude parsear el color {valor!r}"
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))), float(m.group(4) or 1)


def test_el_acento_de_texto_pasa_AA_sobre_todos_los_fondos(tokens):
    """`--accent-hover` es el color de TEXTO del acento y tiene que pasar 4.5:1
    contra cada superficie donde la app lo pinta — no solo contra blanco."""
    texto = _hex(tokens["--accent-hover"])
    blanco = _hex(tokens["--bg-primary"])
    tint, alpha = _rgba(tokens["--accent-tint"])

    fondos = {
        "--bg-primary": _hex(tokens["--bg-primary"]),
        "--bg-secondary": _hex(tokens["--bg-secondary"]),
        "--bg-tertiary": _hex(tokens["--bg-tertiary"]),
        "--bg-subtle": _hex(tokens["--bg-subtle"]),
        # El caso que motivó el archivo: el acento sobre su propio tint.
        "--accent-tint sobre blanco": _sobre(tint, alpha, blanco),
    }
    malos = {n: round(_ratio(texto, f), 2)
             for n, f in fondos.items() if _ratio(texto, f) < AA_TEXTO}
    assert not malos, (
        f"--accent-hover ({tokens['--accent-hover']}) no llega a {AA_TEXTO}:1 sobre "
        f"{malos}. Es el color con el que se pinta TEXTO: o se oscurece el token, o "
        f"se aclara el fondo. No lo arregles bajando el umbral.")


def test_el_texto_blanco_sobre_el_acento_pasa_AA(tokens):
    """Tiles, chips activos, burbujas del chat y `.btn-primary` pintan blanco sobre
    el acento pleno."""
    ratio = _ratio((255, 255, 255), _hex(tokens["--accent"]))
    assert ratio >= AA_TEXTO, (
        f"blanco sobre --accent ({tokens['--accent']}) da {ratio:.2f}:1, debajo de "
        f"{AA_TEXTO}. Lo usan .btn-primary, .cap-chat__msg--user y los tiles.")


def test_el_acento_pleno_sirve_al_menos_como_componente_grafico(tokens):
    """Los ~14 íconos SVG que heredan `currentColor` desde `--accent` viven sobre el
    tint. Como componentes gráficos el umbral es 3:1 (WCAG 1.4.11), no 4.5."""
    acento = _hex(tokens["--accent"])
    tint, alpha = _rgba(tokens["--accent-tint"])
    fondo = _sobre(tint, alpha, _hex(tokens["--bg-primary"]))
    ratio = _ratio(acento, fondo)
    assert ratio >= AA_GRANDE, (
        f"--accent sobre su propio tint da {ratio:.2f}:1, debajo de {AA_GRANDE} — "
        f"los íconos de los tiles dejan de distinguirse del fondo.")


def test_no_quedan_rojos_fuera_de_los_tokens():
    """La app llegó a tener CUATRO rojos: el bordó del acento, el oficial, un rojo
    Tailwind para errores y un rosa-rojo en los toasts. Cada uno entró por un camino
    distinto y ninguno estaba en un token."""
    html = _INDEX.read_text(encoding="utf-8")
    prohibidos = {
        "#7f0001 / #660001": r"7f0001|660001",
        "rgb bordó": r"127, ?0, ?1\b",
        "#dc2626 (rojo Tailwind)": r"dc2626",
        "rgb Tailwind": r"220, ?38, ?38",
        "#e11d48 (rosa-rojo del toast)": r"e11d48",
    }
    encontrados = {n: len(re.findall(p, html)) for n, p in prohibidos.items()
                   if re.search(p, html)}
    assert not encontrados, (
        f"volvieron rojos fuera de la paleta: {encontrados}. Usá --accent (fondos, "
        f"bordes, íconos) o --accent-hover (texto).")


def test_los_gradientes_de_marca_estan_aplanados():
    """Había 7 gradientes `--accent → --accent-red`. Cuando los dos tokens pasaron a
    ser el mismo rojo quedaron de un color hacia sí mismo: invisibles, pero pagando
    el costo de pintar un degradado. Aplanados también van con el resto del rediseño,
    que sigue a Cloudflare y no usa gradientes."""
    html = _INDEX.read_text(encoding="utf-8")
    style = html[html.index("<style>"):html.index("</style>")]
    rojos = re.findall(r"linear-gradient\([^;]*--accent[,)][^;]*", style)
    assert not rojos, f"volvió un gradiente sobre el acento: {rojos}"
    # El de progreso de deploy es verde y sí tiene dos paradas reales: no se toca.
    assert "--accent-green" in style


def test_la_paleta_de_los_graficos_no_repite_la_marca():
    """`_vegaTheme()` armaba la paleta categórica con `[--accent, --accent-red, …]`.
    Con los dos tokens iguales, las series 1 y 2 de una barra apilada o una dona
    salían del mismo color. Es el efecto de segundo orden más caro del cambio de
    paleta y no se ve en ninguna captura del CSS."""
    html = _INDEX.read_text(encoding="utf-8")
    m = re.search(r"palette: \[([^\]]+)\]", html)
    assert m, "desapareció la paleta de _vegaTheme()"
    entradas = [e.strip().strip("'\"") for e in m.group(1).split(",")]
    assert len(entradas) == len(set(entradas)), (
        f"la paleta de gráficos tiene colores repetidos: {entradas}. Dos series "
        f"consecutivas se van a ver iguales.")
    assert "accent2" not in html, (
        "volvió `accent2`: era --accent-red, que hoy es el mismo color que --accent.")
