"""Un dataset con coordenadas tiene que terminar en un mapa.

Todo el andamiaje existía suelto —`index_template` sabe emitir `geo_point`,
`dashboards` sabe armar el `tile_map`, y `transacciones-billetera` hace el
recorrido completo a mano—, pero para un dataset nuevo las coordenadas quedaban
como dos columnas de números decimales. Estos tests cubren el medio que faltaba.
"""
import conf_lint
import geo_fields


def _campo(path, tipo="float", **kw):
    base = {"raw_name": path.rsplit(".", 1)[-1], "field_path": path, "type": tipo,
            "business_label": path, "dimension": False, "role": None}
    base.update(kw)
    return base


FILTER = ('filter {\n'
          '  csv { separator => "," columns => ["a"] target => "data" }\n'
          '  mutate { remove_field => ["message"] }\n'
          '}')


# ── Detección ───────────────────────────────────────────────────────────────
def test_detecta_el_par_lat_lon_con_o_sin_prefijo():
    for nlat, nlon in (("lat", "lon"), ("latitude", "longitude"), ("latitud", "longitud"),
                       ("geo_lat", "geo_long"), ("device_lat", "device_lng"),
                       ("lat_origen", "lon_origen")):
        geo = geo_fields.detectar([_campo(f"data.{nlat}"), _campo(f"data.{nlon}")])
        assert geo is not None and geo.es_par, f"{nlat}/{nlon}"
        assert geo.lat == f"data.{nlat}" and geo.lon == f"data.{nlon}"


def test_no_confunde_una_columna_cualquiera_con_una_coordenada():
    """`longitud_texto` de un producto, o una latitud que vino como texto: un
    geo_point mal armado rechaza el documento ENTERO al indexar."""
    assert geo_fields.detectar([_campo("data.latencia"), _campo("data.longitud_cable")]) is None
    assert geo_fields.detectar([_campo("data.lat", "string"), _campo("data.lon", "string")]) is None
    assert geo_fields.detectar([_campo("data.lat")]) is None, "una sola mitad no alcanza"
    assert geo_fields.detectar([]) is None
    # Las DOS tienen que ser numéricas: con una sola de texto ya no es un punto.
    assert geo_fields.detectar([_campo("data.lat", "string"), _campo("data.lon")]) is None
    assert geo_fields.detectar([_campo("data.lat"), _campo("data.lon", "string")]) is None


def test_un_geo_point_no_es_una_dimension_para_agrupar():
    """Agrupar por coordenada da una barra por documento."""
    from maas_integrator import is_dimension

    assert is_dimension("data.geo_location", "geo_point") is False
    assert is_dimension("data.estado", "string") is True


def test_detecta_un_campo_que_ya_viene_combinado():
    campos = [_campo("data.coordenadas", "string", sample="-34.60,-58.38")]
    geo = geo_fields.detectar(campos)
    assert geo is not None and geo.unico == "data.coordenadas" and not geo.es_par

    # También si el valor sale de la línea de muestra en vez del campo.
    geo = geo_fields.detectar([_campo("data.location", "string")],
                              muestra='ts=1 location=-34.6,-58.4 estado=OK')
    assert geo is not None and geo.unico == "data.location"

    # Un campo que se llama parecido pero no trae coordenadas no cuenta.
    assert geo_fields.detectar([_campo("data.location", "string", sample="Buenos Aires")]) is None


# ── El filter que se agrega ─────────────────────────────────────────────────
def test_aplicar_arma_el_campo_y_el_filter_sigue_compilando():
    campos = [_campo("data.lat"), _campo("data.lon")]

    nuevo, salida, nota = geo_fields.aplicar(FILTER, campos, "data")

    assert conf_lint.lint_filtro(nuevo) == [], conf_lint.lint_filtro(nuevo)
    assert "[data][geo_location]" in nuevo
    assert "geo_point" in nota or "mapa" in nota
    geo = [c for c in salida if c["type"] == "geo_point"]
    assert len(geo) == 1
    assert geo[0]["field_path"] == "data.geo_location"
    assert geo[0]["dimension"] is False, "un punto en el mapa no es una categoría"
    # Las columnas originales se conservan: sirven en una tabla.
    assert {c["field_path"] for c in salida} >= {"data.lat", "data.lon"}


def test_el_bloque_valida_rangos_y_descarta_el_cero_cero():
    """(0,0) es el "sin dato" más común y en un mapa es una isla fantasma frente
    a África que se lleva toda la atención."""
    nuevo, _, _ = geo_fields.aplicar(FILTER, [_campo("data.lat"), _campo("data.lon")], "data")

    assert "latf.abs<=90" in nuevo and "lonf.abs<=180" in nuevo
    assert "!(latf==0.0 && lonf==0.0)" in nuevo
    assert "to_f" in nuevo


def test_aplicar_es_idempotente():
    campos = [_campo("data.lat"), _campo("data.lon")]
    una, campos1, _ = geo_fields.aplicar(FILTER, campos, "data")
    dos, campos2, nota = geo_fields.aplicar(una, campos1, "data")

    assert dos == una and nota == ""
    assert len([c for c in campos2 if c["type"] == "geo_point"]) == 1


def test_si_el_modelo_ya_armo_el_campo_no_se_duplica():
    """El prompt ahora le ofrece `geo_point`: si lo hizo solo, no se pisa."""
    propio = FILTER[:-1] + '  mutate { add_field => { "[data][geo_location]" => "1,2" } }\n}'

    nuevo, salida, nota = geo_fields.aplicar(propio, [_campo("data.lat"), _campo("data.lon")], "data")

    assert nuevo == propio and nota == ""
    assert not [c for c in salida if c["type"] == "geo_point"]


def test_sin_coordenadas_no_toca_nada():
    campos = [_campo("data.monto"), _campo("data.estado", "string")]

    nuevo, salida, nota = geo_fields.aplicar(FILTER, campos, "data")

    assert nuevo == FILTER and salida == campos and nota == ""


def test_el_namespace_vacio_deja_el_campo_arriba():
    """Los casos predefinidos parsean a top-level."""
    nuevo, salida, _ = geo_fields.aplicar(FILTER, [_campo("lat"), _campo("lon")], "")

    assert "[geo_location]" in nuevo
    assert [c for c in salida if c["type"] == "geo_point"][0]["field_path"] == "geo_location"


def test_un_campo_combinado_solo_se_retipa():
    """Ya es un `"lat,lon"`: no hace falta tocar el filter, solo decir qué es."""
    campos = [_campo("data.coords", "string", sample="-34.6,-58.4")]

    nuevo, salida, nota = geo_fields.aplicar(FILTER, campos, "data")

    assert nuevo == FILTER, "no hay nada que fusionar"
    assert salida[0]["type"] == "geo_point" and salida[0]["dimension"] is False
    assert "geo_point" in nota
