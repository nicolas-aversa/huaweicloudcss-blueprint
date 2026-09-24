"""El LLM pone la semántica de una tabla y no puede romper nada.

Cada test le da al perfil una respuesta del LLM (buena, mala o ninguna) y mira
que lo válido se aplique, que lo inválido se descarte, y que el .conf y los
tipos sigan siendo los del perfilador pase lo que pase.
"""
import json

import pytest
from fastapi.testclient import TestClient

import conf_lint
import main
import perfilador
import semantica
from maas_integrator import infer_role

TELEMETRIA = """Fecha y Hora,ID Trabajo,Cliente,Estado,Páginas Totales,Velocidad (PPM),Consumo Tinta (ml),Código Error
2026-09-18 11:04:12,TRB-00187,Editorial Sur,COMPLETADO,12000,142,865.5,
2026-09-18 11:31:55,TRB-00188,Gráfica Andes,FALLIDO,8000,118,240.0,E-0412
2026-09-18 12:02:10,TRB-00189,Editorial Sur,COMPLETADO,5000,150,410.25,
2026-09-18 12:30:10,TRB-00190,Imprenta Norte,COMPLETADO,3000,150,210.25,
"""
# Sin header: el perfilador los llama columna_N.
SIN_HEADER = """2026-09-18 11:04:12,POZO-7,Neuquén,1250.5,ACTIVO
2026-09-18 12:04:12,POZO-9,Neuquén,980.0,ACTIVO
2026-09-18 13:04:12,POZO-7,Mendoza,1100.25,PARADO
"""


def _perfil(texto):
    return perfilador.perfilar(texto.strip("\n").split("\n"))


def _responde(datos):
    return lambda _prompt: json.dumps(datos)


def _col(perfil, nombre):
    return next(c for c in perfil.columnas if c.nombre == nombre)


def _tipos(perfil):
    return {c.nombre: (c.tipo, c.formato_fecha) for c in perfil.columnas}


# ── Lo válido se aplica ─────────────────────────────────────────────────────
def test_aplica_roles_dimension_y_unidad_y_devuelve_las_preguntas():
    p = _perfil(TELEMETRIA)
    r = semantica.enriquecer(p, _responde({
        "columnas": {
            "estado": {"rol": "success_indicator", "dimension": True},
            "cliente": {"rol": "primary_dimension", "dimension": True},
            "paginas_totales": {"rol": "measure", "unidad": "páginas"},
            "codigo_error": {"rol": "critical_indicator", "dimension": False},
        },
        "preguntas": ["¿Cuántos trabajos hay por Estado?", 7],
    }))

    assert r.fuente == "llm" and r.preguntas == ["¿Cuántos trabajos hay por Estado?"]
    assert _col(p, "estado").rol == "success_indicator"
    assert _col(p, "cliente").rol == "primary_dimension"
    assert _col(p, "paginas_totales").rol == "measure"
    assert _col(p, "paginas_totales").unidad == "páginas"
    assert _col(p, "codigo_error").rol == "critical_indicator"


@pytest.mark.parametrize("dice, queda", [
    ("las facturas", "las facturas"),
    ("los trabajos de impresión", "los trabajos de impresión"),
    ("  Las Ventas  ", "las ventas"),
    # El modelo describe de más: se queda con el artículo y el sustantivo.
    ("las ventas mensuales por producto y país", "las ventas"),
    ("las facturas de cada mes", "las facturas"),
    ("los trabajos de impresión del turno noche", "los trabajos de impresión"),
    ("facturas", ""),                       # sin artículo no sabemos el género
    ("cada fila es una factura de venta", ""),
    ("los", ""),
    ("", ""),
    (42, ""),
])
def test_que_es_cada_fila_se_valida_antes_de_usarlo(dice, queda):
    """El artículo es el que decide "¿Cuántas facturas?" o "¿Cuántos trabajos?":
    sin uno usable, las filas son "registros"."""
    r = semantica.enriquecer(_perfil(TELEMETRIA), _responde({"filas": dice}))
    assert r.filas == queda


def test_con_header_la_etiqueta_es_la_del_cliente():
    """"Páginas Totales" es cómo le dice el cliente: el LLM no la reescribe."""
    p = _perfil(TELEMETRIA)
    semantica.enriquecer(p, _responde({"columnas": {"paginas_totales": {"etiqueta": "Total de hojas"}}}))
    assert _col(p, "paginas_totales").etiqueta == "Páginas Totales"


def test_un_header_tecnico_si_lo_mejora_el_llm():
    """`CantidadFacturas` salió de un sistema, no de una persona: la etiqueta
    del LLM ("Cantidad de facturas") entra. Antes la regla era por archivo
    —con header, nunca— y quedaba "CantidadFacturas" en cada panel y pregunta."""
    p = _perfil("AnioMes,Country,CantidadFacturas\n2011-09,EIRE,15\n2011-10,France,8\n")
    semantica.enriquecer(p, _responde({"columnas": {
        "aniomes": {"etiqueta": "Año y mes"},
        "country": {"etiqueta": "País"},
        "cantidadfacturas": {"etiqueta": "Cantidad de facturas"},
    }}))
    assert [c.etiqueta for c in p.columnas] == ["Año y mes", "País", "Cantidad de facturas"]


def test_una_clave_de_json_la_mejora_el_llm():
    """En un JSON las claves siempre son nombres técnicos, aunque parezcan
    palabras ("Country"): la etiqueta del LLM entra."""
    p = _perfil('{"src_ip": "10.0.0.1", "Country": "AR", "bytes": 10}\n'
                '{"src_ip": "10.0.0.2", "Country": "UY", "bytes": 12}\n')
    semantica.enriquecer(p, _responde({"columnas": {
        "src_ip": {"etiqueta": "IP de origen"}, "Country": {"etiqueta": "País"},
    }}))
    etiquetas = {c.nombre: c.etiqueta for c in p.columnas}
    assert etiquetas["src_ip"] == "IP de origen"
    assert etiquetas["Country"] == "País"


def test_la_unidad_del_header_le_gana_a_la_del_llm():
    p = _perfil(TELEMETRIA)
    semantica.enriquecer(p, _responde({"columnas": {"consumo_tinta_ml": {"unidad": "litros"}}}))
    assert _col(p, "consumo_tinta_ml").unidad == "ml"


# ── Lo inválido se descarta ─────────────────────────────────────────────────
@pytest.mark.parametrize("columna, rol", [
    ("cliente", "measure"),              # sumar un texto
    ("estado", "timestamp"),             # un texto no es la fecha del evento
    ("paginas_totales", "entity_id"),    # válido para enteros…
    ("consumo_tinta_ml", "primary_dimension"),   # …pero no un float
    ("consumo_tinta_ml", "entity_id"),
    ("consumo_tinta_ml", "critical_indicator"),
    ("cliente", "rol_inventado"),
    ("fecha_y_hora", "success_indicator"),
])
def test_un_rol_que_no_cuadra_con_el_tipo_se_descarta(columna, rol):
    p = _perfil(TELEMETRIA)
    semantica.enriquecer(p, _responde({"columnas": {columna: {"rol": rol}}}))
    esperado = rol if (columna, rol) == ("paginas_totales", "entity_id") else None
    assert _col(p, columna).rol == esperado


def test_la_dimension_principal_es_una_sola_y_tiene_que_agrupar():
    p = _perfil(TELEMETRIA)
    semantica.enriquecer(p, _responde({"columnas": {
        "id_trabajo": {"rol": "primary_dimension"},   # un valor por fila: no agrupa
        "cliente": {"rol": "primary_dimension"},
        "estado": {"rol": "primary_dimension"},       # segunda: se descarta
    }}))
    assert _col(p, "id_trabajo").rol is None
    assert _col(p, "cliente").rol == "primary_dimension"
    assert _col(p, "estado").rol is None


def test_una_dimension_se_apaga_siempre_pero_se_prende_solo_si_los_datos_dejan():
    p = _perfil(TELEMETRIA)
    assert _col(p, "cliente").dimension and not _col(p, "id_trabajo").dimension
    semantica.enriquecer(p, _responde({"columnas": {
        "cliente": {"dimension": False},
        "id_trabajo": {"dimension": True},     # un valor distinto por fila
    }}))
    assert not _col(p, "cliente").dimension
    assert not _col(p, "id_trabajo").dimension


def test_los_tipos_y_el_conf_no_se_tocan():
    p = _perfil(TELEMETRIA)
    antes_tipos, antes_conf = _tipos(p), perfilador.armar_filter(p, "data")
    semantica.enriquecer(p, _responde({"columnas": {
        "paginas_totales": {"tipo": "string", "rol": "measure"},
        "fecha_y_hora": {"tipo": "string", "formato": "dd/MM/yyyy"},
    }}))
    assert _tipos(p) == antes_tipos
    assert perfilador.armar_filter(p, "data") == antes_conf


@pytest.mark.parametrize("respuesta", [
    "esto no es JSON",
    "[1, 2, 3]",
    '{"columnas": "cualquier cosa", "preguntas": "no es una lista"}',
    "```json\n{\"columnas\": {\"estado\": {\"rol\": \"success_indicator\"}}}\n```",
    "<think>mmm {no}</think>Acá va el JSON:\n```json\n"
    "{\"columnas\": {\"estado\": {\"rol\": \"success_indicator\"}}}\n```\nListo.",
])
def test_una_respuesta_rota_no_rompe_nada(respuesta):
    p = _perfil(TELEMETRIA)
    antes = perfilador.armar_filter(p, "data")
    r = semantica.enriquecer(p, lambda _: respuesta)
    assert perfilador.armar_filter(p, "data") == antes
    assert isinstance(r.preguntas, list)
    if "```" in respuesta:
        assert r.fuente == "llm" and _col(p, "estado").rol == "success_indicator"
    elif "{" not in respuesta:
        assert r.fuente == "heuristica"
    else:   # un objeto con basura adentro: se ignora la basura
        assert r.preguntas == [] and all(c.rol is None for c in p.columnas)


def test_la_semantica_va_con_un_modelo_rapido_sin_thinking_y_con_tope(monkeypatch):
    """glm-5.3 razona siempre y tardaba ~95 s: el paso 2 no puede esperar
    eso por etiquetas y preguntas."""
    import maas_integrator

    visto = {}

    class _Cliente:
        def with_options(self, **kw):
            visto["opciones"] = kw
            return self

    def _chat(_cliente, **kw):
        visto["pedido"] = kw
        from types import SimpleNamespace as NS
        return NS(choices=[NS(message=NS(content='{"columnas": {}}'))])

    monkeypatch.setattr(maas_integrator, "_build_client", lambda: _Cliente())
    monkeypatch.setattr(maas_integrator, "_chat", _chat)

    r = semantica.enriquecer(_perfil(TELEMETRIA))
    assert r.fuente == "llm"
    assert visto["pedido"]["model"] == "glm-5.2"
    assert visto["pedido"]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert visto["opciones"] == {"timeout": semantica._TIMEOUT_S, "max_retries": 0}
    assert semantica._TIMEOUT_S <= 60


def test_si_el_llm_falla_quedan_las_heuristicas():
    def _explota(_):
        raise TimeoutError("el modelo tardó demasiado")

    p = _perfil(TELEMETRIA)
    r = semantica.enriquecer(p, _explota)
    assert r.fuente == "heuristica" and "TimeoutError" in r.nota and r.preguntas == []
    roles = {f["raw_name"]: f["role"] for f in perfilador.campos(p, "data")}
    assert roles["estado"] == "success_indicator"
    assert roles["codigo_error"] == "critical_indicator"
    assert roles["fecha_y_hora"] == "timestamp"


# ── CSV sin header: nombres y etiquetas del LLM ─────────────────────────────
def test_sin_header_el_llm_nombra_las_columnas_y_el_conf_sale_con_esos_nombres():
    p = _perfil(SIN_HEADER)
    assert [c.nombre for c in p.columnas][:2] == ["columna_1", "columna_2"]
    semantica.enriquecer(p, _responde({
        "nombres": {"columna_2": "pozo", "columna_3": "Provincia", "columna_4": "caudal_m3",
                    "columna_5": "estado"},
        "columnas": {"columna_2": {"etiqueta": "Pozo", "rol": "entity_id"},
                     "columna_4": {"etiqueta": "Caudal", "unidad": "m3", "rol": "measure"}},
    }))
    nombres = [c.nombre for c in p.columnas]
    assert nombres == ["columna_1", "pozo", "provincia", "caudal_m3", "estado"]
    assert _col(p, "pozo").etiqueta == "Pozo" and _col(p, "pozo").rol == "entity_id"
    assert _col(p, "caudal_m3").unidad == "m3"

    conf = perfilador.armar_filter(p, "data")
    assert '"pozo"' in conf and "columna_2" not in conf
    completo = ('input { s3 { bucket => "b" } }\n' + conf +
                'output { elasticsearch { hosts => [] index => "x" } }\n')
    assert [str(e) for e in conf_lint.lint(completo, marcador_hosts=True)
            if e.nivel == conf_lint.ERROR] == []
    v = perfilador.verificar(p)
    assert v.ok == v.total == 3


@pytest.mark.parametrize("nuevo", ["message", "columna_1", "Pozo Nº 7!", "9pozo", "x" * 50, 12])
def test_un_nombre_invalido_o_repetido_no_se_aplica(nuevo):
    p = _perfil(SIN_HEADER)
    semantica.enriquecer(p, _responde({"nombres": {"columna_2": nuevo}}))
    assert _col(p, "columna_2")


def test_la_fecha_del_evento_sigue_a_su_columna_renombrada():
    p = _perfil(SIN_HEADER)
    assert p.fecha_evento == "columna_1"
    semantica.enriquecer(p, _responde({"nombres": {"columna_1": "medicion"}}))
    assert p.fecha_evento == "medicion"
    assert "[data][medicion]" in perfilador.armar_filter(p, "data")


def test_con_header_no_se_renombra_nada():
    p = _perfil(TELEMETRIA)
    semantica.enriquecer(p, _responde({"nombres": {"cliente": "customer"}}))
    assert _col(p, "cliente")
    # Ni siquiera una columna que el cliente llamó así en su header.
    p = _perfil("columna_1,monto\nPalermo,10\nBelgrano,12\n")
    assert p.header
    semantica.enriquecer(p, _responde({"nombres": {"columna_1": "sucursal"}}))
    assert _col(p, "columna_1")


# ── Heurísticas en castellano ───────────────────────────────────────────────
@pytest.mark.parametrize("path, tipo, rol", [
    ("data.fecha_y_hora", "date", "timestamp"),
    # Del camino del LLM, donde la fecha puede quedar como texto y el rol es
    # lo que le avisa al dashboard que hay serie temporal.
    ("data.fecha", "string", "timestamp"),
    ("data.hora_evento", "string", "timestamp"),
    ("data.monto", "float", "measure"),
    ("data.importe_total", "float", "measure"),
    ("data.consumo_tinta_ml", "float", "measure"),
    ("data.estado", "string", "success_indicator"),
    ("data.resultado", "string", "success_indicator"),
    ("data.codigo_error", "string", "critical_indicator"),
    ("data.motivo_rechazo", "string", "critical_indicator"),
    ("data.cliente", "string", "entity_id"),
    ("data.usuario", "string", "entity_id"),
    ("data.id_cliente", "integer", None),    # un id numérico no se suma
])
def test_los_roles_se_infieren_tambien_en_castellano(path, tipo, rol):
    assert infer_role(path, tipo) == rol


def test_una_hora_que_es_texto_no_es_la_fecha_del_evento():
    """Con rol timestamp el dashboard cree tener serie temporal y grafica la
    hora de ingesta."""
    p = _perfil("turno,hora,litros\nmañana,a primera hora,10\ntarde,después del almuerzo,12\n")
    roles = {f["raw_name"]: f["role"] for f in perfilador.campos(p, "data")}
    assert roles["hora"] is None


# ── De punta a punta por el endpoint ────────────────────────────────────────
client = TestClient(main.app)


def test_el_endpoint_usa_la_semantica_y_devuelve_diez_preguntas(monkeypatch):
    monkeypatch.setattr(semantica, "_llamar_al_llm", _responde({
        "columnas": {"cliente": {"rol": "primary_dimension"}},
        "filas": "los trabajos",
        "preguntas": ["¿Qué Cliente imprimió más Páginas Totales?", "¿Por qué fallan los trabajos?"],
    }))
    r = client.post("/api/v1/onboarding/generate-filter", json={"raw_log": TELEMETRIA})
    body = r.json()

    assert r.status_code == 200, body
    assert body["verificacion"]["semantica"] == "llm"
    roles = {f["raw_name"]: f["role"] for f in body["fields"]}
    assert roles["cliente"] == "primary_dimension"
    assert len(body["questions"]) == 10
    assert body["questions"][0] == "¿Qué Cliente imprimió más Páginas Totales?"
    assert not any("Por qué" in q for q in body["questions"])
    # Y lo que el LLM dijo que es cada fila llega hasta las plantillas.
    assert "¿Cuántos trabajos hay de cada estado?" in body["questions"]


def test_sin_api_key_el_endpoint_sigue_y_las_preguntas_salen_de_las_plantillas():
    r = client.post("/api/v1/onboarding/generate-filter", json={"raw_log": TELEMETRIA})
    body = r.json()
    assert r.status_code == 200, body
    assert body["verificacion"]["semantica"] == "heuristica"
    assert len(body["questions"]) == 10
    # Sin LLM no sabemos que cada fila es un trabajo: son "registros", pero
    # siguen sonando a pregunta y no a título de reporte.
    assert "¿Cuántos registros hay de cada estado?" in body["questions"]
    assert "¿Cuántos registros terminaron en FALLIDO?" in body["questions"]
