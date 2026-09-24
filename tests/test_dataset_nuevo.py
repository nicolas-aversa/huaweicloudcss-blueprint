"""El flujo "Dataset nuevo" de punta a punta: de la muestra al template.

Estos tests pasan por el endpoint real (`/generate-filter`) y por el builder
del index template, porque los dos bugs que motivaron este archivo vivían en las
costuras y no en ninguna función suelta:

  * `dimension` y `role` se perdían en la respuesta del endpoint (el modelo de
    Pydantic no los declaraba y los descartaba), así que ningún dataset nuevo
    llegaba al paso 2 —ni al caso guardado, ni al dashboard— con sus roles;
  * el `date` filter del .conf y el `format` del template decían cosas
    distintas, y una fecha como `2026-09-18 11:04:12` se descartaba en silencio.
"""
import pytest
from fastapi.testclient import TestClient

import custom_cases
import main
from index_template import build_index_template

client = TestClient(main.app)

TELEMETRIA = (
    "Fecha y Hora,ID Trabajo,Cliente,Estado,Páginas Totales,Consumo Tinta (ml)\n"
    "2026-09-18 11:04:12,TRB-00187,Editorial Sur,COMPLETADO,12000,865.5\n"
    "2026-09-18 11:31:55,TRB-00188,Gráfica Andes,FALLIDO,8000,240.0\n"
    "2026-09-18 12:02:10,TRB-00189,Editorial Sur,COMPLETADO,5000,410.25\n"
)


def _generar(raw_log, **kw):
    r = client.post("/api/v1/onboarding/generate-filter", json={"raw_log": raw_log, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _no_llamar_al_llm(monkeypatch):
    def _explota(*a, **k):
        raise AssertionError("una tabla con filas no tiene por qué ir al LLM")
    monkeypatch.setattr(main, "generate_logstash_filter", _explota)


# ── Quién arma el .conf ─────────────────────────────────────────────────────
def test_una_tabla_la_arma_el_perfilador_sin_llamar_al_llm(monkeypatch):
    _no_llamar_al_llm(monkeypatch)

    body = _generar(TELEMETRIA)

    assert body["verificacion"]["fuente"] == "perfilador"
    assert body["verificacion"]["formato"] == "delimitado"
    assert body["verificacion"]["filas"] == 3 and body["verificacion"]["filas_ok"] == 3
    assert "csv {" in body["filter_code"]
    assert '"yyyy-MM-dd HH:mm:ss"' in body["filter_code"]


def test_solo_el_header_va_al_llm(monkeypatch):
    """Sin filas de datos no hay nada que perfilar: los tipos saldrían todos
    string. Ahí el LLM, que al menos infiere por los nombres."""
    llamado = {}

    def _fake(raw_log, **kw):
        llamado["raw"] = raw_log
        return {"filter_code": 'filter { csv { columns => ["a"] } }', "fields": []}

    monkeypatch.setattr(main, "generate_logstash_filter", _fake)

    body = _generar("Fecha y Hora,Cliente,Monto")

    assert body["verificacion"]["fuente"] == "llm"
    assert llamado["raw"] == "Fecha y Hora,Cliente,Monto"


@pytest.mark.parametrize("muestra", [
    "<34>Oct 11 22:14:15 mymachine su: 'su root' failed for lonvick on /dev/pts/8\n"
    "<34>Oct 11 22:14:16 mymachine su: 'su root' failed for lonvick on /dev/pts/9\n",
    # Los `|` del encabezado CEF parten cada línea en 8 columnas "consistentes".
    "CEF:0|Security|threatmanager|1.0|100|worm stopped|10|\n"
    "CEF:0|Security|threatmanager|1.0|101|worm started|5|\n",
    # El `,123` de los milisegundos daría una tabla de tres columnas.
    "2026-09-18 11:04:12,123 INFO [main] com.x.Y - arrancó, listo\n"
    "2026-09-18 11:04:13,456 WARN [main] com.x.Y - lento, 3s\n",
], ids=["syslog", "cef", "log4j"])
def test_un_formato_conocido_mantiene_su_generador_curado(monkeypatch, muestra):
    """Syslog, CEF, Apache y log4j tienen generadores con los grok de su
    formato: no se reemplazan por una tabla."""
    llamado = {}

    def _fake(raw_log, **kw):
        llamado["sí"] = True
        return {"filter_code": 'filter { grok { match => { "message" => "%{SYSLOGLINE}" } } }', "fields": []}

    monkeypatch.setattr(main, "generate_logstash_filter", _fake)

    body = _generar(muestra)

    assert llamado, "un log con formato propio tiene que ir por su generador, no por el perfilador"
    assert body["verificacion"]["fuente"] == "llm"


def test_jdbc_y_el_feedback_no_pasan_por_el_perfilador(monkeypatch):
    llamados = []
    monkeypatch.setattr(main, "generate_logstash_filter",
                        lambda raw_log, **kw: llamados.append(kw) or {"filter_code": "filter { }", "fields": []})

    _generar(TELEMETRIA, input_type="jdbc")
    _generar(TELEMETRIA, feedback="corregí la fecha", previous_filter="filter { }")

    assert len(llamados) == 2


# ── Lo que viaja al paso 2 ──────────────────────────────────────────────────
def test_role_y_dimension_llegan_al_paso_2(monkeypatch):
    """El bug: el modelo de la respuesta no declaraba `dimension` ni `role`, y
    Pydantic descarta lo que no conoce. Ningún dataset nuevo llegaba al paso 2
    —ni al caso guardado, ni al dashboard, ni a las capabilities— con sus roles."""
    monkeypatch.setattr(main, "generate_logstash_filter", lambda raw_log, **kw: {
        "filter_code": "filter { }",
        "fields": [{"raw_name": "estado", "field_path": "data.estado", "type": "string",
                    "business_label": "Estado", "dimension": True, "role": "success_indicator"}]})

    campo = _generar("una línea sin formato conocido")["fields"][0]

    assert campo["dimension"] is True
    assert campo["role"] == "success_indicator"


def test_el_formato_de_la_fecha_y_las_etiquetas_llegan_al_paso_2(monkeypatch):
    _no_llamar_al_llm(monkeypatch)

    campos = {f["field_path"]: f for f in _generar(TELEMETRIA)["fields"]}

    fecha = campos["fecha_y_hora"]
    assert fecha["type"] == "date" and fecha["date_format"] == "yyyy-MM-dd HH:mm:ss"
    assert fecha["role"] == "timestamp"
    assert campos["paginas_totales"]["business_label"] == "Páginas Totales"
    assert campos["consumo_tinta_ml"]["unit"] == "ml"
    assert campos["estado"]["dimension"] is True
    assert set(campos["estado"]["frecuentes"]) == {"COMPLETADO", "FALLIDO"}


# ── El template dice lo mismo que el filter ─────────────────────────────────
def _mapping(campos, path):
    props = build_index_template(campos, "data", "t-%{+YYYY.MM}")["template"]["mappings"]["properties"]
    nodo = props
    for tramo in path.split("."):
        nodo = nodo[tramo]
        if "properties" in nodo and tramo != path.split(".")[-1]:
            nodo = nodo["properties"]
    return nodo


def test_el_template_lee_la_fecha_con_el_formato_del_filter(monkeypatch):
    """El `date` filter llevaba bien la fecha a @timestamp, pero el campo
    original tenía un `format` que no la aceptaba y se descartaba en silencio."""
    _no_llamar_al_llm(monkeypatch)
    campos = _generar(TELEMETRIA)["fields"]

    m = _mapping(campos, "fecha_y_hora")

    assert m["type"] == "date"
    assert m["format"].split("||")[0] == "yyyy-MM-dd HH:mm:ss"


def test_las_palabras_clave_de_logstash_se_traducen():
    for joda, opensearch in (("ISO8601", "strict_date_optional_time"),
                             ("UNIX", "epoch_second"), ("UNIX_MS", "epoch_millis")):
        m = _mapping([{"field_path": "data.f", "type": "date", "date_format": joda}], "data.f")
        formatos = m["format"].split("||")
        assert formatos[0] == opensearch, joda
        assert formatos.count(opensearch) == 1, "no se repite el mismo formato"


def test_una_fecha_sin_formato_conocido_igual_entra_con_espacio():
    """Red para una fecha que tipó el LLM sin decir el formato: lo más común en
    un CSV —con espacio en vez de `T`— antes no entraba en ningún formato."""
    m = _mapping([{"field_path": "data.f", "type": "date"}], "data.f")

    formatos = m["format"].split("||")
    assert "yyyy-MM-dd HH:mm:ss" in formatos and "dd/MM/yyyy" in formatos
    # Y el orden viejo se respeta: el compacto va antes que epoch.
    assert formatos.index("yyyyMMddHHmmssSSS") < formatos.index("epoch_millis")


def test_el_caso_guardado_conserva_el_formato_de_fecha():
    limpios = custom_cases._clean_fields([
        {"field_path": "data.f", "type": "date", "date_format": "dd/MM/yyyy HH:mm",
         "frecuentes": [], "sample": "18/09/2026 11:04"}])

    assert limpios[0]["date_format"] == "dd/MM/yyyy HH:mm"


# ── El LLM ve más de una línea ──────────────────────────────────────────────
def test_el_llm_ve_hasta_cinco_lineas(monkeypatch):
    """Con una sola línea el modelo tipaba adivinando: un campo que en esa
    línea venía vacío, o un código que casualmente era un número."""
    import types
    import maas_integrator as mi

    visto = {}

    def _create(**kw):
        visto["user"] = kw["messages"][1]["content"]
        msg = types.SimpleNamespace(content='{"filter_code": "filter { }", "fields": []}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr(mi, "_build_client", lambda: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create))))
    monkeypatch.setattr(mi, "get_pipeline_model", lambda: "glm")

    lineas = [f"linea rara numero {i} sin formato conocido" for i in range(8)]
    mi.generate_logstash_filter("\n".join(lineas))

    assert all(f"numero {i} " in visto["user"] for i in range(5))
    assert "numero 5 " not in visto["user"]
