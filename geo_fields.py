"""Coordenadas en un dataset nuevo: detectarlas y dejarlas listas para un mapa.

Un log con latitud y longitud es un log que se entiende en un mapa, no en dos
gráficos de barras de números decimales. Todo lo necesario ya existía suelto —
`index_template` sabe emitir `geo_point`, `dashboards` sabe armar el `tile_map`,
y el vertical `transacciones-billetera` hace el recorrido completo a mano— pero
nada de eso se activaba solo cuando un SA sube su propio dataset.

Esto es el medio que faltaba, y es determinístico a propósito: al LLM se le
puede pedir que arme el campo, pero no se le puede exigir. Acá se detecta el par
de columnas por nombre y tipo, y se agrega al filter el mismo bloque `ruby` que
ya corre en producción, que valida lo que hay que validar:

  * que los dos valores sean números (una celda vacía o un "N/D" no rompe nada);
  * que estén en rango (|lat|≤90, |lon|≤180);
  * y que no sean (0,0) — el "sin dato" más común, que en un mapa aparece como
    una isla fantasma frente a la costa de África y se lleva toda la atención.

OpenSearch acepta `"lat,lon"` como geo_point, así que el campo sintético se arma
como string y el index template hace el resto.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# El nombre del campo que se crea. El mismo que usa `transacciones-billetera`.
NOMBRE = "geo_location"
ETIQUETA = "Ubicación"

# Nombres de columna que son una latitud o una longitud. Se compara contra el
# último tramo del path (`data.device_lat` → `device_lat`) y contra el nombre
# crudo, así que cubre prefijos (`geo_lat`, `device_latitude`, `lat_origen`).
_LAT = re.compile(r"(^|[_.\-])(lat|latitude|latitud)([_.\-]|$)", re.I)
_LON = re.compile(r"(^|[_.\-])(lon|lng|long|longitude|longitud)([_.\-]|$)", re.I)
# Un solo campo que ya trae "lat,lon".
_PAR = re.compile(r"(^|[_.\-])(geo|geoloc|location|locations|coord|coords|coordinates|coordenadas)([_.\-]|$)", re.I)
_VALOR_PAR = re.compile(r"^\s*-?\d{1,3}(\.\d+)?\s*,\s*-?\d{1,3}(\.\d+)?\s*$")

_NUMERICOS = ("float", "double", "integer", "long", "number")


@dataclass(frozen=True)
class Geo:
    """Qué se encontró. `lat`/`lon` con el par; `unico` con el campo ya combinado."""
    lat: str = ""
    lon: str = ""
    unico: str = ""

    @property
    def es_par(self) -> bool:
        return bool(self.lat and self.lon)


def _path(campo: dict) -> str:
    return str(campo.get("field_path") or campo.get("ecs_path") or campo.get("raw_name") or "").strip()


def _hoja(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _es_numerico(campo: dict) -> bool:
    return str(campo.get("type") or "").lower() in _NUMERICOS


def detectar(fields: list[dict], muestra: str = "") -> Geo | None:
    """El par lat/lon, o el campo que ya viene combinado. `None` si no hay geo.

    Pide que los dos sean numéricos: una columna `latitud` de texto libre es
    cualquier cosa menos una coordenada, y un `geo_point` mal armado rechaza el
    documento entero al indexar.
    """
    lat = lon = ""
    for campo in fields or []:
        path = _path(campo)
        if not path:
            continue
        hoja = _hoja(path)
        if not lat and _LAT.search(hoja) and _es_numerico(campo):
            lat = path
        elif not lon and _LON.search(hoja) and _es_numerico(campo):
            lon = path
    if lat and lon:
        return Geo(lat=lat, lon=lon)

    # Un único campo `"lat,lon"`: el nombre lo sugiere y la muestra lo confirma.
    for campo in fields or []:
        path = _path(campo)
        if not path or _es_numerico(campo):
            continue
        if not _PAR.search(_hoja(path)):
            continue
        valor = str(campo.get("sample") or campo.get("example") or "").strip()
        if _VALOR_PAR.match(valor) or _hay_par_en(muestra, _hoja(path)):
            return Geo(unico=path)
    return None


def _hay_par_en(muestra: str, nombre: str) -> bool:
    """¿La línea de muestra trae `nombre=-34.6,-58.4` (o `"nombre": "…"`)?"""
    if not muestra or not nombre:
        return False
    patron = re.compile(rf"{re.escape(nombre)}\"?\s*[:=]\s*\"?(-?\d{{1,3}}(?:\.\d+)?\s*,\s*-?\d{{1,3}}(?:\.\d+)?)")
    return bool(patron.search(muestra))


def _corchetes(path: str) -> str:
    """`data.device.lat` → `[data][device][lat]`, que es como se escribe en Logstash."""
    return "".join(f"[{tramo}]" for tramo in path.split(".") if tramo)


def _codigo(lat: str, lon: str, dst: str) -> str:
    """El cuerpo del `ruby`. Un número se toma como número: de un JSON, Logstash
    trae los decimales como BigDecimal, y su texto es "-0.3459341e2", que no
    pasaba la validación de antes (pensada para el texto de un CSV): el campo no
    se armaba en ningún documento y el mapa salía vacío, sin un error (visto en
    la ECS con dos casos de JSON). Un texto se valida como antes."""
    return (
        f'        coords = [event.get("{lat}"), event.get("{lon}")].map do |v|\n'
        f"          if v.is_a?(Numeric)\n"
        f"            v.to_f\n"
        f"          elsif v.to_s.strip =~ /\\A-?\\d+(\\.\\d+)?\\z/\n"
        f"            v.to_s.strip.to_f\n"
        f"          end\n"
        f"        end\n"
        f"        latf, lonf = coords\n"
        f"        if latf && lonf && latf.abs<=90 && lonf.abs<=180 && !(latf==0.0 && lonf==0.0)\n"
        f'          event.set("{dst}", "#{{latf}},#{{lonf}}")\n'
        f"        end\n"
    )


def bloque_ruby(geo: Geo, destino: str) -> str:
    """El `ruby` que fusiona lat y lon en `"lat,lon"`: los dos tienen que ser
    números, en rango y no (0,0)."""
    lat, lon, dst = _corchetes(geo.lat), _corchetes(geo.lon), _corchetes(destino)
    return (
        f"\n  if {lat} and {lon} {{\n"
        f"    ruby {{\n"
        f"      code => '\n"
        + _codigo(lat, lon, dst) +
        f"      '\n"
        f"    }}\n"
        f"  }}\n"
    )


# El cuerpo de antes: validaba el texto del valor, y el de un decimal de JSON
# no pasaba. Está en los casos ya guardados y en el `.conf` de las demos.
_CODIGO_VIEJO = re.compile(
    r'(?P<sangria>[ \t]*)lat = event\.get\("(?P<lat>[^"]+)"\)\.to_s\n'
    r'[ \t]*lon = event\.get\("(?P<lon>[^"]+)"\)\.to_s\n'
    r'[ \t]*if lat =~ /\\A-\?\\d\+\(\\\.\\d\+\)\?\\z/ && lon =~ /\\A-\?\\d\+\(\\\.\\d\+\)\?\\z/\n'
    r'[ \t]*latf, lonf = lat\.to_f, lon\.to_f\n'
    r'[ \t]*if latf\.abs<=90 && lonf\.abs<=180 && !\(latf==0\.0 && lonf==0\.0\)\n'
    r'[ \t]*event\.set\("(?P<dst>[^"]+)", "#\{lat\},#\{lon\}"\)\n'
    r'[ \t]*end\n'
    r'[ \t]*end\n')


def actualizar(conf: str) -> tuple[str, int]:
    """Cambia el bloque de antes por el de ahora en un `.conf` ya armado (un
    caso guardado, una demo): `(conf, cuántos cambió)`."""
    return _CODIGO_VIEJO.subn(lambda m: _codigo(m["lat"], m["lon"], m["dst"]), conf or "")


def _campo_geo(path: str) -> dict:
    return {
        "raw_name": _hoja(path),
        "field_path": path,
        "ecs_path": path,
        "type": "geo_point",
        "business_label": ETIQUETA,
        "unit": None,
        # Un punto en el mapa no es una categoría por la que agrupar ni una
        # medida que sumar: sin esto aparecería en los Top-N como una dimensión.
        "dimension": False,
        "role": None,
        "is_ecs": False,
        "ecs_type_official": None,
        "normalized_path": path,
        "ecs_overlay_path": None,
    }


def aplicar(filter_code: str, fields: list[dict], namespace: str = "data") -> tuple[str, list[dict], str]:
    """Deja el filter y los campos listos para el mapa. Devuelve `(filter, fields, nota)`.

    Idempotente: si el `.conf` ya arma un `geo_location` —porque el modelo lo
    hizo solo— no se duplica nada.
    """
    geo = detectar(fields, "")
    if geo is None:
        return filter_code, fields, ""

    if geo.unico:
        for campo in fields:
            if _path(campo) == geo.unico and campo.get("type") != "geo_point":
                campo["type"] = "geo_point"
                campo["dimension"] = False
                return filter_code, fields, f"`{geo.unico}` es una coordenada: la tipé como geo_point"
        return filter_code, fields, ""

    ns = (namespace or "").strip()
    destino = f"{ns}.{NOMBRE}" if ns else NOMBRE
    if any(_path(c) == destino for c in fields) or _corchetes(destino) in (filter_code or ""):
        return filter_code, fields, ""

    cuerpo = (filter_code or "").rstrip()
    if not cuerpo.endswith("}"):
        return filter_code, fields, ""       # no es un `filter { … }`: no lo toco
    nuevo = cuerpo[:-1].rstrip("\n") + "\n" + bloque_ruby(geo, destino) + "}\n"
    return nuevo, [*fields, _campo_geo(destino)], (
        f"armé `{destino}` (geo_point) con `{geo.lat}` y `{geo.lon}`: el dataset sale en un mapa")
