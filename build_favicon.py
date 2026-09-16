"""Genera el favicon de la app componiendo los tres logos que ya viven en static/.

CSS de base (la lupa violeta, que ya es una insignia cuadrada) con OpenSearch y
Logstash más chicos superpuestos abajo, como dos "hijos" de la marca.

Se corre a mano cuando cambian los logos:

    py build_favicon.py

Escribe `static/favicon.png` (180 px, para la pestaña y para pantallas retina).
El navegador lo reescala a 16 px solo; a ese tamaño los dos logos chicos se leen
como acentos de color más que como marcas distinguibles — es el compromiso que se
eligió a cambio de tener un tab distinto a cualquier otro.
"""
import pathlib

from PIL import Image

STATIC = pathlib.Path(__file__).parent / "static"
LADO = 180                      # tamaño de salida; el browser reescala
CHICO = 0.34                    # lado de los logos superpuestos, relativo al total
MARGEN = 0.03                   # respiro contra el borde, relativo al total
SOLAPE = 0.28                   # cuánto se pisan entre sí, relativo a su propio lado


def _cuadrado(nombre: str, lado: int) -> Image.Image:
    """Carga un PNG en RGBA, recortado a su contenido y encajado en un cuadrado.

    El recorte importa: `logstash.png` es 461×541 con aire alrededor, así que sin
    esto quedaría visualmente más chico que los otros dos aun con el mismo lado.
    """
    img = Image.open(STATIC / nombre).convert("RGBA")
    caja = img.getbbox()         # descarta el alfa transparente del borde
    if caja:
        img = img.crop(caja)
    ancho, alto = img.size
    escala = lado / max(ancho, alto)
    img = img.resize((max(1, round(ancho * escala)), max(1, round(alto * escala))),
                     Image.LANCZOS)
    lienzo = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    lienzo.paste(img, ((lado - img.width) // 2, (lado - img.height) // 2), img)
    return lienzo


def build() -> Image.Image:
    base = _cuadrado("css.png", LADO)

    lado_chico = round(LADO * CHICO)
    margen = round(LADO * MARGEN)
    # Los dos chicos van juntos y superpuestos en la esquina inferior derecha,
    # como un par de insignias. Repartidos uno en cada esquina tapaban la lente de
    # la lupa y el conjunto se leía como un par de anteojos.
    solape = round(lado_chico * SOLAPE)
    y = LADO - lado_chico - margen
    posiciones = [
        ("logstash.png", (LADO - lado_chico - margen, y)),
        # OpenSearch se dibuja último para quedar ENCIMA: su silueta es más
        # reconocible y conviene que no la pise el otro.
        ("opensearch.png", (LADO - lado_chico * 2 - margen + solape, y)),
    ]
    for nombre, pos in posiciones:
        logo = _cuadrado(nombre, lado_chico)
        # Disco blanco detrás: los dos logos tienen fondo transparente y sobre el
        # violeta de CSS perderían contraste (el navy de OpenSearch desaparece).
        disco = Image.new("RGBA", (lado_chico, lado_chico), (0, 0, 0, 0))
        mascara = Image.new("L", (lado_chico * 4, lado_chico * 4), 0)
        from PIL import ImageDraw
        ImageDraw.Draw(mascara).ellipse((0, 0, lado_chico * 4 - 1, lado_chico * 4 - 1),
                                        fill=255)
        disco.paste((255, 255, 255, 255), (0, 0),
                    mascara.resize((lado_chico, lado_chico), Image.LANCZOS))
        # El logo, achicado dentro del disco para que no toque el borde.
        interior = round(lado_chico * 0.68)
        chico = _cuadrado(nombre, interior)
        disco.paste(chico, ((lado_chico - interior) // 2, (lado_chico - interior) // 2),
                    chico)
        base.paste(disco, pos, disco)
    return base


if __name__ == "__main__":
    salida = STATIC / "favicon.png"
    build().save(salida)
    print(f"escrito {salida} ({salida.stat().st_size // 1024} KB)")
