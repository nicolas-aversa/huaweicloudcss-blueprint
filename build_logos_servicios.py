"""Baja los logos oficiales de los servicios de Huawei Cloud que muestra el
progreso del deploy (CSS, NAT Gateway, EIP, VPC).

Salen de las bibliotecas de draw.io de huaweicloud-latam, grupo `color`: según
su README, "los íconos que ves en la Consola y en las páginas de producto". Cada
ícono viene embebido como `data:image/svg+xml;base64` y se busca por su título.
No hay ícono de Security Group: ese grupo usa el de VPC.

Se corre a mano cuando cambian los íconos:

    py build_logos_servicios.py

Escribe `static/servicios/{css,nat,eip,vpc}.svg`, con un `<title>` agregado para
que un lector de pantalla los nombre.
"""
import base64
import json
import pathlib
import re
import urllib.parse
import urllib.request

REPO = "https://raw.githubusercontent.com/huaweicloud-latam/drawio-libraries/HEAD/color/"
DESTINO = pathlib.Path(__file__).parent / "static" / "servicios"

# archivo de salida → (biblioteca de draw.io, título del ícono, nombre accesible)
LOGOS = {
    "css": ("HWC Analytics (color).xml", "Cloud Search Service (CSS)", "CSS"),
    "nat": ("HWC Networking (color).xml", "NAT Gateway", "NAT Gateway"),
    "eip": ("HWC Networking (color).xml", "Elastic IP (EIP)", "EIP"),
    "vpc": ("HWC Networking (color).xml", "Virtual Private Cloud (VPC)", "VPC"),
}


def _biblioteca(nombre: str) -> list[dict]:
    with urllib.request.urlopen(REPO + urllib.parse.quote(nombre), timeout=30) as r:
        texto = r.read().decode("utf-8")
    return json.loads(texto[texto.index("["):texto.rindex("]") + 1])


def svg_de(item: dict, titulo: str) -> str:
    """El SVG del ícono, con `<title>` como primer hijo."""
    datos = item["data"]
    crudo = datos.split(",", 1)[1]
    svg = (base64.b64decode(crudo).decode("utf-8") if ";base64" in datos.split(",", 1)[0]
           else urllib.parse.unquote(crudo))
    svg = re.sub(r"<desc>.*?</desc>\s*", "", svg, flags=re.S)   # "Created with Pixso."
    return re.sub(r"(<svg\b[^>]*>)", rf'\1<title>{titulo}</title>', svg, count=1).strip() + "\n"


def main() -> None:
    DESTINO.mkdir(exist_ok=True)
    cache: dict[str, list[dict]] = {}
    for archivo, (biblioteca, titulo_drawio, titulo) in LOGOS.items():
        if biblioteca not in cache:
            cache[biblioteca] = _biblioteca(biblioteca)
        items = cache[biblioteca]
        item = next(i for i in items if i.get("title") == titulo_drawio)
        (DESTINO / f"{archivo}.svg").write_text(svg_de(item, titulo), encoding="utf-8")
        print(f"{archivo}.svg <- {biblioteca} :: {titulo_drawio}")


if __name__ == "__main__":
    main()
