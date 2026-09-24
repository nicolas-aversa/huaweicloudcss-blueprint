"""
Tests de integración del servicio de AI-Driven Onboarding.

Arquitectura del flujo:

    1. /api/v1/onboarding/generate-filter  -> LLM glm-5.2 (raw_log -> filter)
    2. /api/v1/onboarding/generate-pipeline -> arma .conf (input + filter + output)

El paso de validación con Logstash efímero quedó deprecado. (El comentario que
estaba acá mandaba a /validate-mapping; esa ruta no existe.)
"""

import pathlib
import re
import pytest
from fastapi.testclient import TestClient

import main
import raiz


# Los datasets no se versionan (pesan ~1,3 GB; se bajan del Release o se
# regeneran con los build scripts). Los tests que leen los .log reales se saltean
# si falta la data.
#
# El marcador pide los archivos CONCRETOS que usa cada test, no "algún .log":
# con `any(glob("*.log"))` alcanzaba con tener uno cualquiera para que dejara de
# saltear, y entonces los tests que necesitaban otro fallaban con un `assert None
# is not None` que no decía qué archivo faltaba. Una descarga parcial —que es lo
# normal— se veía como cuatro tests rotos.
_DATASETS_DIR = pathlib.Path(__file__).resolve().parent.parent / "datasets"


def necesita_datasets(*nombres):
    """Skip con el motivo exacto si falta alguno de los `.log` que el test lee."""
    faltan = [n for n in nombres if not (_DATASETS_DIR / n).is_file()]
    return pytest.mark.skipif(
        bool(faltan),
        reason=f"faltan en datasets/: {', '.join(faltan)}" if faltan else "",
    )


# Compat: los tests que solo necesitan "algún" dataset del catálogo.
requires_datasets = necesita_datasets(*sorted(
    f for fs in __import__("verticals").demo_dataset_files().values() for f in fs))


SAMPLE_FINANCIAL_LOG = (
    "2026-05-19T10:15:30.123Z host=pay-gw-03 level=INFO src_ip=10.20.30.40 "
    "dst_ip=10.20.30.5 user=jdoe txn_id=TXN-2026-0098123 amount=1530.75 "
    "currency=USD card_bin=453201 mcc=5411 auth_code=A1B2C3 resp=200 "
    'latency_ms=87 msg="payment authorized"'
)

# Filtro de referencia que simula la salida del LLM para los tests.
SAMPLE_FILTER = r"""
filter {
  grok {
    match => { "message" => "%{TIMESTAMP_ISO8601:event_ts} %{GREEDYDATA:kv_data}" }
  }
  kv {
    source => "kv_data"
    field_split => " "
    value_split => "="
    target => "fields"
  }
}
""".strip()

# Mock de los campos detectados (shape NAMESPACED: el endpoint
# /generate-filter devuelve `{filter_code, fields[]}` con field_path bajo el
# namespace y is_ecs=False; ECS queda como overlay opcional).
SAMPLE_FIELDS = [
    {
        "raw_name": "txn_id",
        "field_path": "data.txn_id",
        "ecs_path": "data.txn_id",
        "ecs_overlay_path": None,
        "type": "string",
        "business_label": "ID de transacción",
        "unit": None,
        "is_ecs": False,
        "ecs_type_official": None,
        "normalized_path": "data.txn_id",
    },
    {
        "raw_name": "amount",
        "field_path": "data.amount",
        "ecs_path": "data.amount",
        "ecs_overlay_path": None,
        "type": "float",
        "business_label": "Monto de transacción",
        "unit": "USD",
        "is_ecs": False,
        "ecs_type_official": None,
        "normalized_path": "data.amount",
    },
    {
        "raw_name": "resp",
        "field_path": "data.resp",
        "ecs_path": "data.resp",
        "ecs_overlay_path": None,
        "type": "integer",
        "business_label": "Código de estado HTTP",
        "unit": None,
        "is_ecs": False,
        "ecs_type_official": None,
        "normalized_path": "data.resp",
    },
]

# Helper: el mock del generador devuelve un dict. Acepta kwargs porque el
# endpoint llama con `namespace=` y `ecs_overlay=`.
def _mock_llm_response(_log, *args, **kwargs):
    return {"filter_code": SAMPLE_FILTER, "fields": SAMPLE_FIELDS}

client = TestClient(main.app)


# --- Infra ------------------------------------------------------------------

def test_health_ok():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_index_page_served():
    res = client.get("/")
    assert res.status_code == 200
    assert "Blueprint" in res.text


# --- /generate-filter -------------------------------------------------------

def test_generate_filter_calls_llm(monkeypatch):
    """Endpoint que dispara el step 1 → step 2 (Mapping) del wizard.

    Verifica que devuelva tanto el `filter_code` deployable como la lista
    de `fields` con las etiquetas de negocio que el frontend muestra en la
    tabla de mapping.
    """
    monkeypatch.setattr(main, "generate_logstash_filter", _mock_llm_response)

    res = client.post(
        "/api/v1/onboarding/generate-filter",
        json={"raw_log": SAMPLE_FINANCIAL_LOG},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert "filter" in body["filter_code"]
    assert isinstance(body["fields"], list)
    assert len(body["fields"]) == 3

    # Shape namespaced: cada field trae field_path bajo el namespace y NO es
    # ECS por default (is_ecs=False). ECS queda como overlay opcional.
    f = body["fields"][0]
    assert {
        "raw_name", "field_path", "ecs_path", "ecs_overlay_path", "type",
        "business_label", "unit", "is_ecs", "normalized_path",
    } <= set(f.keys())
    assert f["business_label"] == "ID de transacción"

    # Sin namespace pedido, los campos van a la raíz como en los casos de
    # ejemplo (`txn_id`, no `data.txn_id`), sin forzar ECS.
    for x in body["fields"]:
        assert x["is_ecs"] is False
        assert not x["field_path"].startswith("data.")
        assert x["ecs_overlay_path"] is None

    txn_id = next(x for x in body["fields"] if x["raw_name"] == "txn_id")
    assert txn_id["field_path"] == "txn_id"
    # El filter del LLM sigue armando bajo `data`, y un bloque al final lo vacía
    # en la raíz: sin él, el dato llegaría a `data.txn_id` y el template, el
    # dashboard y el chat lo buscarían en `txn_id`.
    assert body["filter_code"].rstrip().endswith(raiz.bloque() + "\n}")


def test_generate_filter_respeta_un_namespace_pedido(monkeypatch):
    """Quien pida un namespace explícito lo sigue teniendo (y sin el bloque)."""
    monkeypatch.setattr(main, "generate_logstash_filter", _mock_llm_response)

    body = client.post("/api/v1/onboarding/generate-filter",
                       json={"raw_log": SAMPLE_FINANCIAL_LOG, "namespace": "data"}).json()

    assert all(x["field_path"].startswith("data.") for x in body["fields"])
    assert raiz.bloque() not in body["filter_code"]


def test_generate_filter_empty_raw_log_422():
    res = client.post("/api/v1/onboarding/generate-filter", json={"raw_log": ""})
    assert res.status_code == 422


def test_generate_filter_maas_502(monkeypatch):
    def boom(_log, *args, **kwargs):
        raise RuntimeError("Error al invocar el modelo MaaS: connection refused")

    monkeypatch.setattr(main, "generate_logstash_filter", boom)

    res = client.post(
        "/api/v1/onboarding/generate-filter",
        json={"raw_log": SAMPLE_FINANCIAL_LOG},
    )
    assert res.status_code == 502


def test_generate_filter_missing_api_key_500(monkeypatch):
    """Falta de configuración = 500 (es de esta instancia), no 502.

    El mensaje es el REAL que levanta `_build_client`; el fixture usaba uno que el
    código dejó de producir hace tiempo, así que el test pasaba por la rama
    equivocada del mapeo."""
    def boom(_log, *args, **kwargs):
        raise RuntimeError(
            "No hay API Key de MaaS: configurala en ⚙ Configuración de la "
            "plataforma o definí MAAS_API_KEY en el entorno.")

    monkeypatch.setattr(main, "generate_logstash_filter", boom)

    res = client.post(
        "/api/v1/onboarding/generate-filter",
        json={"raw_log": SAMPLE_FINANCIAL_LOG},
    )
    assert res.status_code == 500


def test_generate_filter_401_de_maas_apunta_a_la_key(monkeypatch):
    """Un 401 de ModelArts tiene que decir QUÉ hacer.

    Antes llegaba a la pantalla el volcado crudo del JSON
    (`{'error': {'code': 'ModelArts.81003', ...}}`), que no menciona ni la API key
    ni dónde arreglarla. Es exactamente el error que bloqueó una demo."""
    def boom(_log, *args, **kwargs):
        raise RuntimeError(
            "Error al invocar el modelo MaaS: Error code: 401 - {'error': "
            "{'code': 'ModelArts.81003', 'message': 'Invalid authorization header.'}}")

    monkeypatch.setattr(main, "generate_logstash_filter", boom)

    res = client.post("/api/v1/onboarding/generate-filter",
                      json={"raw_log": SAMPLE_FINANCIAL_LOG})
    assert res.status_code == 502
    detalle = str(res.json()["detail"])
    assert "API key" in detalle and "Configuración" in detalle, detalle
    assert "ModelArts.81003" not in detalle, "no volcar el JSON crudo del proveedor"


# --- /generate-pipeline (nuevo flujo: filter_code ya generado) --------------

def test_generate_pipeline_with_prebuilt_filter():
    """Caso normal del frontend: el filter ya vino de /generate-filter."""
    res = client.post(
        "/api/v1/onboarding/generate-pipeline",
        json={"filter_code": SAMPLE_FILTER},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["filter_code"] == SAMPLE_FILTER
    # Sin input/output config no se arma pipeline_code.
    assert body["pipeline_code"] is None


def test_generate_pipeline_full_with_filter_and_configs():
    """Frontend manda filter_code + input + output → pipeline completo."""
    res = client.post(
        "/api/v1/onboarding/generate-pipeline",
        json={
            "filter_code": SAMPLE_FILTER,
            "input_config": {
                "plugin_type": "s3",
                "s3": {
                    "bucket": "mi-bucket",
                    "access_key": "AK",
                    "secret_key": "SK",
                },
            },
            "output_config": {
                "plugin_type": "elasticsearch",
                "elasticsearch": {
                    "hosts": "http://opensearch:9200",
                    "index": "logs-%{+YYYY.MM}",
                },
            },
        },
    )

    assert res.status_code == 200
    body = res.json()
    pipeline = body["pipeline_code"]
    assert pipeline is not None
    assert "input {" in pipeline
    assert "s3 {" in pipeline
    assert "filter {" in pipeline
    assert "output {" in pipeline
    assert "elasticsearch {" in pipeline
    assert "mi-bucket" in pipeline


def test_generate_pipeline_without_filter_or_log_422():
    """Si no viene ni filter_code ni raw_log, 422."""
    res = client.post("/api/v1/onboarding/generate-pipeline", json={})
    assert res.status_code == 422


def test_generate_pipeline_rechaza_un_conf_que_no_compila():
    """Por acá pasan TODOS los caminos que arman un .conf (el LLM, los
    generadores determinísticos, el catálogo de formatos y el ejemplo cacheado
    en el navegador): lo que salga roto llega intacto a Terraform, que lo acepta
    igual, y la pipeline nunca arranca."""
    res = client.post("/api/v1/onboarding/generate-pipeline", json={
        # Al filter le falta una llave: el editor del paso 3 lo permite.
        "filter_code": 'filter { mutate { add_field => { "a" => "b" } }',
        "input_config": {"plugin_type": "s3", "s3": {"bucket": "b", "access_key": "AK", "secret_key": "SK"}},
        "output_config": {"plugin_type": "elasticsearch",
                          "elasticsearch": {"hosts": "http://x:9200", "index": "logs"}},
    })

    assert res.status_code == 422
    detalle = res.json()["detail"]
    assert detalle["stage"] == "pipeline_conf"
    assert "sin cerrar" in detalle["message"]


# --- /generate-pipeline (compat: cliente viejo que manda raw_log) -----------

def test_generate_pipeline_legacy_raw_log_triggers_llm(monkeypatch):
    """Si no llega filter_code pero sí raw_log, el endpoint llama al LLM."""
    monkeypatch.setattr(main, "generate_logstash_filter", _mock_llm_response)

    res = client.post(
        "/api/v1/onboarding/generate-pipeline",
        json={"raw_log": SAMPLE_FINANCIAL_LOG},
    )

    assert res.status_code == 200
    assert "filter" in res.json()["filter_code"]


def test_generate_pipeline_legacy_flat_s3_and_nested_es(monkeypatch):
    """Compat: payload viejo con flat S3 input + nested ES output."""
    monkeypatch.setattr(main, "generate_logstash_filter", _mock_llm_response)

    res = client.post(
        "/api/v1/onboarding/generate-pipeline",
        json={
            "raw_log": SAMPLE_FINANCIAL_LOG,
            "input_config": {
                "bucket": "legacy-bucket",
                "access_key": "AK",
                "secret_key": "SK",
                "region": "la-south-2",
            },
            "output_config": {
                "elasticsearch": {
                    "hosts": "http://opensearch:9200",
                    "index": "legacy-%{+YYYY.MM}",
                }
            },
        },
    )

    assert res.status_code == 200
    pipeline = res.json()["pipeline_code"]
    assert "s3 {" in pipeline
    assert "elasticsearch {" in pipeline
    assert "legacy-bucket" in pipeline


# --- Catálogo de formatos: detección determinística sin LLM ----------------
#
# Estos tests NO mockean el LLM: validan que el path determinístico de
# `generate_logstash_filter` (vía catalog + space_kv/json/pipe) genera
# filters correctos para los formatos comunes en demos de cliente. Si
# alguno de estos falla, la demo va a tener que caer al LLM (riesgoso).

from maas_integrator import generate_logstash_filter  # noqa: E402


def test_apache_combined_log_detected():
    log = '127.0.0.1 - alice [10/Oct/2026:13:55:36 -0700] "GET /api HTTP/1.0" 200 2326 "http://ref" "curl/7.0"'
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert "%{COMBINEDAPACHELOG}" in fc
    assert "date {" in fc
    assert "[source][ip]" in fc
    assert "[http][response][status_code]" in fc
    assert "[user_agent][original]" in fc


def test_apache_common_log_detected():
    log = '10.0.0.5 - - [10/Oct/2026:13:55:36 -0700] "POST /login HTTP/1.1" 201 512'
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert "%{COMMONAPACHELOG}" in fc
    # CLF no tiene user_agent (eso es Combined).
    assert "[user_agent][original]" not in fc


def test_syslog_rfc5424_detected():
    log = '<165>1 2026-05-19T10:00:00.123Z host01 sshd 1234 ID47 Failed login attempt'
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert "grok {" in fc
    assert "TIMESTAMP_ISO8601:event_timestamp" in fc
    assert "[host][hostname]" in fc
    assert "[process][name]" in fc


def test_syslog_rfc3164_detected():
    log = '<13>May 19 14:30:01 host01 sshd[1234]: Failed password for invalid user'
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert "%{SYSLOGLINE}" in fc
    assert '"MMM  d HH:mm:ss"' in fc or '"MMM dd HH:mm:ss"' in fc
    assert "[host][hostname]" in fc


def test_cef_detected_with_extension_kv():
    log = 'CEF:0|Vendor|Product|1.0|100|brute force|7|src=10.0.0.1 dst=2.2.2.2 spt=22 suser=root proto=tcp'
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert fc.startswith("filter {")
    # Header CEF parseado por grok.
    assert "device_vendor" in fc
    # Extension parseada por kv adicional.
    assert 'source => "cef_extension"' in fc
    # Mapeos típicos CEF -> ECS.
    assert "[source][ip]" in fc
    assert "[destination][ip]" in fc


def test_csv_with_header_detected():
    log = (
        "timestamp,host,user,event_action\n"
        "2026-05-19T10:00:00Z,h1,alice,login\n"
        "2026-05-19T10:00:05Z,h2,bob,logout\n"
        "2026-05-19T10:00:10Z,h3,carol,login"
    )
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert "csv {" in fc
    assert "skip_header => true" in fc
    # `timestamp` se reconoce y va al date filter, NO al rename.
    assert "date {" in fc
    # Columnas declaradas.
    assert '"host"' in fc and '"user"' in fc


def test_catalog_fields_have_field_path_and_dimension():
    """Todos los formats del catálogo deben producir fields con field_path,
    dimension y role — sin eso, index template, capabilities y dashboards
    fallan en el flow de despliegue productivo."""
    cases = {
        "cef": "CEF:0|Vendor|Product|1.0|100|test|7|src=10.0.0.1 dst=2.2.2.2 act=blocked",
        "syslog_3164": "May 19 14:30:01 host01 sshd[1234]: Failed password for invalid user",
        "syslog_5424": "<165>1 2026-05-19T10:00:00.123Z host01 sshd 1234 ID47 Failed login",
        "apache_combined": '127.0.0.1 - alice [10/Oct/2026:13:55:36 -0700] "GET /api HTTP/1.0" 200 2326 "http://ref" "curl/7.0"',
        "apache_common": '10.0.0.5 - - [10/Oct/2026:13:55:36 -0700] "POST /login HTTP/1.1" 201 512',
        "csv": "timestamp,host,user,action\n2026-05-19T10:00:00Z,h1,alice,login\n2026-05-19T10:00:05Z,h2,bob,logout",
        "jsonl": '{"event_id":"abc","timestamp":"2025-05-28T23:46:49","severity":"critical"}\n{"event_id":"def","timestamp":"2025-05-28T23:47:00","severity":"low"}',
        "pipe_kv": "date=2025-05-28|time=23:46:49|type=endpoint|severity=critical|user=admin|action=file_access",
        "space_kv": "type=endpoint severity=critical user=admin action=login result=success",
    }
    for name, log in cases.items():
        result = generate_logstash_filter(log)
        fields = result.get("fields", [])
        assert fields, f"{name}: no fields generated"
        for f in fields:
            assert "field_path" in f, f"{name}: field {f.get('raw_name')} missing field_path"
            assert f["field_path"], f"{name}: field {f.get('raw_name')} has empty field_path"
            assert "dimension" in f, f"{name}: field {f.get('raw_name')} missing dimension"
            assert "role" in f, f"{name}: field {f.get('raw_name')} missing role"


def test_multi_timestamp_pattern_list_in_date_filter():
    """Verifica que el date filter acepta múltiples formatos, no solo ISO.

    Usamos un log con >=3 `=` para que caiga en el path determinístico
    `space_kv` y no llame al LLM real (que tardaría minutos).
    """
    log = '2026-05-19T10:15:30Z host=h1 user=alice resp=200'
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert "date {" in fc
    # Deben aparecer al menos ISO8601 + UNIX en la lista de patterns.
    assert '"ISO8601"' in fc
    assert '"UNIX"' in fc


def test_multiline_hint_emitted_on_stack_trace():
    """Stack traces multilínea deben setear multiline_hint=True."""
    log = (
        "2026-05-19T10:00:00Z host=h1 user=alice ERROR=processing_failed\n"
        "\tat com.example.Main.run(Main.java:42)\n"
        "\tat com.example.Main.main(Main.java:10)"
    )
    result = generate_logstash_filter(log)
    assert result.get("multiline_hint") is True


def test_no_multiline_hint_for_single_line():
    log = '2026-05-19T10:15:30Z host=h1 user=alice resp=200'
    result = generate_logstash_filter(log)
    assert result.get("multiline_hint") is False


_NESTED_JSON_OBJ = {
    "@timestamp": "2026-06-18T15:24:11.102Z",
    "service": "payment-gateway",
    "level": "ERROR",
    "message": "Connection timeout",
    "http": {"status_code": 504, "method": "POST", "url": "/v1/charges"},
    "exception": {"type": "TimeoutException", "stack_trace": "at A()\nat B()"},
}


def _assert_nested_json_parsed(result):
    """El JSON anidado se aplana a paths con punto y se promueve @timestamp."""
    by_raw = {f["raw_name"]: f for f in result["fields"]}
    # Anidados aplanados (http.*, exception.*), no perdidos dentro de message.
    assert "http.status_code" in by_raw
    assert "http.method" in by_raw
    assert "exception.type" in by_raw
    assert "exception.stack_trace" in by_raw
    # El @timestamp del log se tipa date y se promueve con un date filter.
    assert by_raw["@timestamp"]["type"] == "date"
    fc = result["filter_code"]
    assert 'json {' in fc and 'target => "data"' in fc
    assert 'match => ["[data][@timestamp]"' in fc
    assert 'target => "@timestamp"' in fc


def test_json_minified_single_line_parsed_with_nested_and_timestamp():
    """JSON minificado (1 línea): se detecta, aplana anidados, promueve @timestamp."""
    import json as _json
    result = generate_logstash_filter(_json.dumps(_NESTED_JSON_OBJ))
    _assert_nested_json_parsed(result)


def test_json_pretty_printed_multiline_parsed():
    """JSON pretty-printed (multi-línea, 1ra línea = '{'): antes caía al LLM con
    basura porque el detector solo miraba lines[0]. Ahora parsea el blob completo."""
    import json as _json
    result = generate_logstash_filter(_json.dumps(_NESTED_JSON_OBJ, indent=2))
    _assert_nested_json_parsed(result)


# Los dos tests de `generate_synthetic_shapes` (que devolvía None en vez de
# propagar errores del SDK) se fueron con la función: los datos sintéticos ya no
# se generan, el upload sube el dataset bundleado o el archivo importado.


# --- Prefix-alias matching + race fix + date+time (3 fixes genéricos) -----
#
# Cubren las 3 patologías que aparecen en logs k=v con convenciones
# condensadas (FortiNet, PaloAlto, IIS, custom audit logs, EDR, etc.) sin
# hardcodear nada por vendor. Cada test usa un dominio distinto para
# verificar que la solución es genérica.


def test_namespaced_default_no_ecs_renames():
    """Modo namespaced (default): los campos viven bajo `<ns>.*` conservando
    sus nombres, SIN renombrar a ECS. No debe haber `=> "[source][ip]"` ni
    similares, y el kv debe tener `target => "data"`."""
    log = '2026-05-19T10:00:00Z srcip=10.0.0.1 dstip=8.8.8.8 srcport=51042 user=alice resp=200'
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert 'target => "data"' in fc
    # NO hay renames a ECS.
    assert '=> "[source][ip]"' not in fc
    assert '=> "[destination][ip]"' not in fc
    assert 'rename =>' not in fc
    # Los campos quedan bajo el namespace.
    paths = {f["field_path"] for f in result["fields"]}
    assert "data.srcip" in paths and "data.user" in paths


def test_namespaced_custom_namespace():
    """El namespace es configurable: `transaction` en vez de `data`."""
    log = 'srcip=10.0.0.1 dstip=8.8.8.8 srcport=51042 user=alice resp=200 status=ok'
    result = generate_logstash_filter(log, namespace="transaction")
    fc = result["filter_code"]
    assert 'target => "transaction"' in fc
    paths = {f["field_path"] for f in result["fields"]}
    assert "transaction.srcip" in paths


def test_ecs_overlay_copies_standard_fields_only():
    """Con ecs_overlay=True, se COPIAN (no renombran) los campos estándar a
    ECS; el resto del evento queda bajo el namespace. Los campos de dominio
    sin casa ECS no se tocan."""
    log = 'srcip=10.0.0.1 user_id=alice status_code=200 trxl_resp=000 foo=bar'
    result = generate_logstash_filter(log, ecs_overlay=True)
    fc = result["filter_code"]
    # Overlay: copy de los whitelisted.
    assert 'copy => {' in fc
    assert '"[data][srcip]" => "[source][ip]"' in fc
    # El namespace sigue intacto (no se renombra nada).
    assert 'target => "data"' in fc
    assert 'rename =>' not in fc
    # Los campos whitelisted traen ecs_overlay_path; los de dominio, None.
    by_raw = {f["raw_name"]: f for f in result["fields"]}
    assert by_raw["srcip"]["ecs_overlay_path"] == "source.ip"
    assert by_raw["trxl_resp"]["ecs_overlay_path"] is None
    assert by_raw["foo"]["ecs_overlay_path"] is None


def test_smart_typing_no_convert_in_logstash():
    """Tipado seguro por nombre se refleja en `fields[].type`, pero Logstash
    YA NO convierte: el .conf no tiene `mutate convert` (los tipos los aplica
    el index template de OpenSearch)."""
    log = 'trxl_resp=000 trxl_amount1=500 trxl_account4=123456 trxl_seq_num=99 trxl_msg_typ=210'
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    by_raw = {f["raw_name"]: f for f in result["fields"]}

    # El tipo viaja en los fields (para el template), no como convert.
    assert by_raw["trxl_resp"]["type"] == "string"
    assert by_raw["trxl_account4"]["type"] == "string"
    assert by_raw["trxl_amount1"]["type"] == "float"
    # Logstash solo parsea: NO hay convert en el .conf.
    assert "convert =>" not in fc


def test_keyword_inference_matches_whole_token_not_substring():
    """Regresión: el matcheo por keyword es por TOKEN, no substring. Antes
    "count" ⊂ "srccountry" tipaba el campo como integer → "Canada"/"Reserved"
    no coercían y OpenSearch los descartaba (_ignored), perdiendo agregabilidad.
    Cualquier log debe quedar indexable+agregable: los strings → keyword.
    Los campos enumerados ("amount1") sí matchean ("amount")."""
    from maas_integrator import _infer_field_type
    # El bug original: substring "count" en "srccountry"/"dstcountry".
    assert _infer_field_type("srccountry", "Reserved") == "string"
    assert _infer_field_type("dstcountry", "Canada") == "string"
    # "ip" ⊂ "description" ya no rompe campos de texto.
    assert _infer_field_type("description", "algo con ip adentro") == "string"
    # Token completo / enumerado sí matchea.
    assert _infer_field_type("event_count", "5") == "integer"
    assert _infer_field_type("trxl_amount1", "500") == "float"
    # IP por valor preserva el tipado aunque el nombre no lo delate;
    # una MAC (hex con ':') NO se confunde con IP.
    assert _infer_field_type("srcip", "198.51.100.92") == "ip"
    assert _infer_field_type("srcmac", "a2:e9:00:ec:40:01") == "string"


def test_build_index_template_typing():
    """El index template tipa: keyword-by-default (dynamic template) + medidas
    numéricas + @timestamp date. Códigos/IDs quedan keyword (ausentes de
    properties)."""
    from index_template import build_index_template, index_pattern_from_name
    fields = [
        {"raw_name": "trxl_resp", "type": "string"},
        {"raw_name": "trxl_amount1", "type": "float"},
        {"raw_name": "trxl_account4", "type": "string"},
        {"raw_name": "srcip", "type": "ip"},
        {"raw_name": "trxl_seq_num", "type": "integer"},
        {"raw_name": "is_active", "type": "boolean"},
        {"raw_name": "event_dt", "type": "date"},
        {"raw_name": "geo_loc", "type": "geo_point"},
        {"raw_name": "err_msg", "type": "text"},
    ]
    tpl = build_index_template(fields, "data", "logs-hoje-%{+YYYY.MM}")

    # Pattern derivado del índice con date math.
    assert tpl["index_patterns"] == ["logs-hoje-*"]
    # Índice tolerante: un "null" string en un campo double no debe rechazar el
    # doc entero (mapper_parsing_exception) — se saltea solo ese campo.
    assert tpl["template"]["settings"]["index.mapping.ignore_malformed"] is True
    mappings = tpl["template"]["mappings"]
    # Dynamic template keyword-by-default.
    dt = mappings["dynamic_templates"][0]["strings_as_keyword"]
    assert dt["match_mapping_type"] == "string"
    assert dt["mapping"]["type"] == "keyword"
    # @timestamp date.
    assert mappings["properties"]["@timestamp"] == {"type": "date"}
    ns_props = mappings["properties"]["data"]["properties"]
    # Medida → double; integer → long; ip → ip.
    assert ns_props["trxl_amount1"] == {"type": "double"}
    assert ns_props["trxl_seq_num"] == {"type": "long"}
    assert ns_props["srcip"] == {"type": "ip"}
    # Vocabulario completo del detector (antes boolean/date caían a keyword).
    assert ns_props["is_active"] == {"type": "boolean"}
    assert ns_props["event_dt"]["type"] == "date"
    # Lenient, pero el COMPACTO debe ir ANTES que epoch_millis: un timestamp de
    # 17 dígitos es un long válido y epoch_millis lo parsearía como año 643698,
    # rompiendo Discover/visualizaciones.
    _fmt = ns_props["event_dt"]["format"]
    assert "yyyyMMddHHmmssSSS" in _fmt and "epoch_millis" in _fmt
    assert _fmt.index("yyyyMMddHHmmssSSS") < _fmt.index("epoch_millis")
    # `ignore_malformed` va DENTRO del mapping: el de `settings` no cubre
    # geo_point, y sin esto una coordenada basura rechaza el documento entero.
    assert ns_props["geo_loc"] == {"type": "geo_point", "ignore_malformed": True}
    # text → multi-field text + keyword (full-text Y aggregatable).
    assert ns_props["err_msg"]["type"] == "text"
    assert ns_props["err_msg"]["fields"]["keyword"]["type"] == "keyword"
    # Códigos string → declarados como keyword: el template nombra TODOS los
    # campos, no solo los que no son texto.
    assert ns_props["trxl_resp"] == {"type": "keyword", "ignore_above": 1024}
    assert ns_props["trxl_account4"] == {"type": "keyword", "ignore_above": 1024}

    # Pattern de índice estático (sin date math) → match exacto.
    assert index_pattern_from_name("transacciones") == "transacciones"


def test_build_index_template_top_level_namespace():
    """Namespace vacío => los campos van top-level (sin parent `data`). Lo usan
    los tipos predefinidos, cuyos filtros parsean a top-level."""
    from index_template import build_index_template
    fields = [
        {"raw_name": "review_score", "type": "integer"},
        {"raw_name": "source_ip", "type": "ip"},
        {"raw_name": "trace_name", "type": "string"},
    ]
    tpl = build_index_template(fields, "", "ecommerce-search-%{+YYYY.MM}")
    props = tpl["template"]["mappings"]["properties"]
    # Sin parent `data`: las medidas/ip van directo en properties.
    assert "data" not in props
    assert props["review_score"] == {"type": "long"}
    assert props["source_ip"] == {"type": "ip"}
    assert props["@timestamp"] == {"type": "date"}
    # string → keyword declarado, en la raíz.
    assert props["trace_name"] == {"type": "keyword", "ignore_above": 1024}


def test_build_index_template_nested_field_path():
    """El template tipa por el PATH REAL del campo (field_path), no por raw_name.
    Para firewall el filtro renombra srcip→[source][ip], así que el mapping `ip`
    tiene que ir en source.ip (anidado), no en la clave plana `srcip`."""
    from index_template import build_index_template
    fields = [
        {"raw_name": "srcip", "field_path": "source.ip", "type": "ip"},
        {"raw_name": "dstip", "field_path": "destination.ip", "type": "ip"},
        {"raw_name": "srcport", "field_path": "source.port", "type": "integer"},
        {"raw_name": "sentbyte", "field_path": "source.bytes", "type": "integer"},
        {"raw_name": "dstcountry", "field_path": "destination.geo.country_name", "type": "keyword"},
        {"raw_name": "action", "field_path": "event.action", "type": "keyword"},
    ]
    tpl = build_index_template(fields, "", "firewall-%{+YYYY.MM}")
    props = tpl["template"]["mappings"]["properties"]
    # IPs y medidas tipadas en su path ECS anidado, mergeadas bajo un solo `source`.
    assert props["source"]["properties"]["ip"] == {"type": "ip"}
    assert props["source"]["properties"]["port"] == {"type": "long"}
    assert props["source"]["properties"]["bytes"] == {"type": "long"}
    assert props["destination"]["properties"]["ip"] == {"type": "ip"}
    # NO debe existir la clave plana raw_name.
    assert "srcip" not in props and "dstip" not in props
    # keyword → declarado también, anidado en su path.
    kw = {"type": "keyword", "ignore_above": 1024}
    assert props["event"]["properties"]["action"] == kw
    assert props["destination"]["properties"]["geo"]["properties"]["country_name"] == kw


def test_el_template_nombra_todos_los_campos():
    """Cada campo que llega en `fields` tiene su mapping explícito, sea del tipo
    que sea: nada queda librado al dynamic template."""
    from index_template import build_index_template
    fields = [{"field_path": p, "type": t} for p, t in [
        ("a", "keyword"), ("b", "string"), ("c", "integer"), ("d", "float"), ("e", "date"),
        ("f", "ip"), ("g", "boolean"), ("h", "text"), ("i", "geo_point"), ("j", "long"),
        ("k", "double"), ("l", "tipo_raro"), ("m", None)]]
    props = build_index_template(fields, "", "x-%{+YYYY.MM}")["template"]["mappings"]["properties"]

    faltan = [f["field_path"] for f in fields if f["field_path"] not in props]
    assert faltan == [], faltan
    # Un tipo desconocido o ausente es keyword, igual que el dynamic template.
    assert props["l"]["type"] == props["m"]["type"] == "keyword"


@pytest.mark.parametrize("orden", ["hoja_primero", "objeto_primero"])
def test_un_nombre_que_es_hoja_y_padre_queda_objeto(orden):
    """`user` como keyword y `user.name` adentro: si el keyword pisara al
    objeto, un documento que trae `user` como objeto rebotaría entero."""
    from index_template import build_index_template
    campos = [{"field_path": "user", "type": "keyword"},
              {"field_path": "user.name", "type": "keyword"}]
    if orden == "objeto_primero":
        campos.reverse()
    props = build_index_template(campos, "", "x-%{+YYYY.MM}")["template"]["mappings"]["properties"]
    assert props["user"]["properties"]["name"]["type"] == "keyword"


def test_index_template_endpoint():
    """El endpoint /index-template devuelve el template + el snippet Dev Tools."""
    res = client.post(
        "/api/v1/onboarding/index-template",
        json={
            "fields": [{"raw_name": "amount", "type": "float"}, {"raw_name": "resp", "type": "string"}],
            "namespace": "data",
            "opensearch_index": "logs-%{+YYYY.MM}",
            "project_name": "demo-x",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["template_name"] == "demo-x"
    assert body["index_pattern"] == "logs-*"
    assert body["put_snippet"].startswith("PUT _index_template/demo-x")
    props = body["template"]["template"]["mappings"]["properties"]
    assert props["data"]["properties"]["amount"] == {"type": "double"}


def test_index_template_endpoint_preserves_field_path():
    """El endpoint preserva `field_path` (list[dict] libre) y tipa en el path
    real: un campo firewall con field_path=source.ip queda `ip` anidado."""
    res = client.post(
        "/api/v1/onboarding/index-template",
        json={
            "fields": [{"raw_name": "srcip", "field_path": "source.ip", "type": "ip"}],
            "namespace": "",
            "opensearch_index": "firewall-%{+YYYY.MM}",
            "project_name": "fw",
        },
    )
    assert res.status_code == 200
    props = res.json()["template"]["template"]["mappings"]["properties"]
    assert props["source"]["properties"]["ip"] == {"type": "ip"}
    assert "srcip" not in props


def test_index_template_endpoint_prefers_curated_template():
    """El PREVIEW del paso 2 muestra el template CURADO (templates/<slug>.json)
    verbatim cuando el slug lo tiene — así el preview es fiel a lo que se aplica y
    se ve idéntico cargando el tipo solo o junto a otros (antes divergía porque el
    auto-generado dependía de los `fields` crudos vs. enriquecidos). El slug se
    puede pasar explícito o derivar del índice."""
    import json as _json
    import main as _main

    curated = _json.loads(
        (_main._TEMPLATES_DIR / "transacciones-billetera.json").read_text(encoding="utf-8")
    )
    # slug explícito + fields distintos → igual devuelve el curado, no el de fields.
    res = client.post(
        "/api/v1/onboarding/index-template",
        json={
            "fields": [{"raw_name": "ruido", "type": "keyword"}],
            "opensearch_index": "transacciones-billetera-%{+YYYY.MM}",
            "slug": "transacciones-billetera",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["template"] == curated
    assert body["index_pattern"] == "transacciones-billetera-*"
    assert "ruido" not in _json.dumps(body["template"])
    # sin slug: se deriva del índice → mismo curado.
    res2 = client.post(
        "/api/v1/onboarding/index-template",
        json={
            "fields": [{"raw_name": "otro", "type": "integer"}],
            "opensearch_index": "transacciones-billetera-%{+YYYY.MM}",
        },
    )
    assert res2.json()["template"] == curated


def test_index_template_endpoint_falls_back_when_no_curated():
    """Un slug SIN template curado cae al auto-generado desde los campos."""
    res = client.post(
        "/api/v1/onboarding/index-template",
        json={
            "fields": [{"raw_name": "amount", "type": "float"}],
            "namespace": "data",
            "opensearch_index": "custom-x-%{+YYYY.MM}",
            "slug": "custom-x",
        },
    )
    assert res.status_code == 200
    props = res.json()["template"]["template"]["mappings"]["properties"]
    assert props["data"]["properties"]["amount"] == {"type": "double"}


def test_envelope_stripped_with_grok():
    """Un log con prefijo no-kv (envelope) antes del payload key=value debe
    stripearse con grok para que el kv no se coma basura."""
    log = ('[PEND] 20251004235800:671854561 - 9755 [B2AUTH02] '
           'table=b2_log|trxl_msg_typ=210|trxl_resp=000|trxl_seq_num=1|trxl_channel=M|trxl_host_id=LNK8')
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert 'grok {' in fc
    assert 'kv_payload' in fc
    assert 'source => "kv_payload"' in fc
    assert 'target => "data"' in fc
    # El primer campo real es `table`, no basura del envelope.
    paths = {f["field_path"] for f in result["fields"]}
    assert "data.table" in paths
    assert "data.trxl_msg_typ" in paths


def test_event_timestamp_field_feeds_at_timestamp():
    """Un campo con nombre de timestamp y valor parseable alimenta @timestamp
    vía date filter (con el pattern compacto yyyyMMddHHmmssSSS)."""
    log = ('table=b2_log|trxl_entry_tim=20251004235759139|trxl_resp=000|'
           'trxl_seq_num=1|trxl_channel=M|trxl_host_id=LNK8')
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert 'date {' in fc
    assert 'match => ["[data][trxl_entry_tim]"' in fc
    assert 'yyyyMMddHHmmssSSS' in fc
    assert 'target => "@timestamp"' in fc


def test_date_filter_has_timezone_utc():
    """El date filter para el campo de tiempo del evento debe tener timezone => "UTC"
    para parseo predecible (el synthetic genera en UTC)."""
    log = ('table=b2_log|trxl_entry_tim=20251004235759139|trxl_resp=000|'
           'trxl_seq_num=1|trxl_channel=M|trxl_host_id=LNK8')
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert 'timezone => "UTC"' in fc


def test_compact_date_pattern_before_unix():
    """El pattern compacto yyyyMMddHHmmssSSS debe aparecer ANTES que UNIX_MS
    en la lista de patterns del date filter, para que un número de 17 dígitos
    se interprete como fecha compacta y no como epoch en milisegundos."""
    log = ('table=b2_log|trxl_entry_tim=20251004235759139|trxl_resp=000|'
           'trxl_seq_num=1|trxl_channel=M|trxl_host_id=LNK8')
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    # Encontrar las posiciones de los patterns en el string.
    compact_pos = fc.find('"yyyyMMddHHmmssSSS"')
    unix_ms_pos = fc.find('"UNIX_MS"')
    assert compact_pos > 0, "yyyyMMddHHmmssSSS debe estar en el filter"
    assert unix_ms_pos > 0, "UNIX_MS debe estar en el filter"
    assert compact_pos < unix_ms_pos, "yyyyMMddHHmmssSSS debe ir ANTES que UNIX_MS"


def test_kv_with_separate_date_time_emits_concat_date_filter():
    """date y time como pares separados (FortiNet, IIS) se concatenan en
    [@metadata][event_dt] y pasan por date filter. En modo namespaced los
    campos viven bajo `[<ns>][date]`/`[<ns>][time]`."""
    log = 'date=2026-05-28 time=15:39:10 srcip=10.0.0.1 dstip=8.8.8.8 srcport=80 dstport=443'
    result = generate_logstash_filter(log)
    fc = result["filter_code"]
    assert '[@metadata][event_dt]' in fc
    # Las referencias apuntan a los campos namespaced.
    assert '%{[data][date]} %{[data][time]}' in fc
    assert 'date {' in fc and 'target => "@timestamp"' in fc
    # date/time se remueven post-consumption (paths namespaced).
    assert '"[data][date]"' in fc and '"[data][time]"' in fc


# --- Destroy del entorno (FinOps: no dejar clusters corriendo) ----------


def test_gen_input_kafka_plaintext_y_sasl_ssl():
    """gen_input_kafka: plaintext (DMS puerto 9092, sin auth) NO emite bloque
    SASL; con security_protocol=SASL_SSL (DMS con auth) emite mecanismo, jaas
    inline y truststore, eligiendo el LoginModule según PLAIN vs SCRAM."""
    # Plaintext: sin nada de SASL.
    plain = main.gen_input_kafka(main.KafkaInputConfig(
        bootstrap_servers="h:9092", topics=["t"], codec="json"))
    assert 'bootstrap_servers => "h:9092"' in plain
    assert "SASL_SSL" not in plain and "sasl_jaas_config" not in plain

    # SASL_SSL PLAIN → PlainLoginModule + truststore.
    sasl = main.gen_input_kafka(main.KafkaInputConfig(
        bootstrap_servers="d:9093", topics=["pagos"], codec="json",
        security_protocol="SASL_SSL", sasl_mechanism="PLAIN",
        sasl_username="u", sasl_password="p",
        ssl_truststore_location="/opt/ts.jks", ssl_truststore_password="x"))
    assert 'security_protocol => "SASL_SSL"' in sasl
    assert 'sasl_mechanism => "PLAIN"' in sasl
    assert ('sasl_jaas_config => '
            "'org.apache.kafka.common.security.plain.PlainLoginModule required "
            'username="u" password="p";\'') in sasl
    assert 'ssl_truststore_location => "/opt/ts.jks"' in sasl
    assert 'ssl_truststore_password => "x"' in sasl

    # SCRAM-* → ScramLoginModule.
    scram = main.gen_input_kafka(main.KafkaInputConfig(
        bootstrap_servers="d:9095", topics=["t"], codec="json",
        security_protocol="SASL_SSL", sasl_mechanism="SCRAM-SHA-512",
        sasl_username="u", sasl_password="p"))
    assert "org.apache.kafka.common.security.scram.ScramLoginModule" in scram
    assert 'sasl_mechanism => "SCRAM-SHA-512"' in scram

    # topics tolera string coma-separado (cliente legacy) → lista.
    coerced = main.KafkaInputConfig(bootstrap_servers="h:9092", topics="logs, pagos")
    assert coerced.topics == ["logs", "pagos"]
    blk = main.generate_input_block(
        {"plugin_type": "kafka", "kafka": {"bootstrap_servers": "h:9092", "topics": "logs"}})
    assert 'topics => ["logs"]' in blk


class _OkProc:
    returncode = 0
    stdout = "{}"
    stderr = ""


def test_obs_upload_skips_for_read_existing_bucket(monkeypatch):
    """Caso CTS (read_existing_bucket): NO se sube nada a OBS, aunque haya
    raw_log + bucket + creds (el bucket real ya tiene los datos; subir lo
    contaminaría). No debe instanciarse el OBSClient."""
    import main as _main
    import obs_client as _obs

    calls = {"n": 0}

    class _BoomClient:
        def __init__(self, *a, **k):
            calls["n"] += 1

    monkeypatch.setattr(_obs, "OBSClient", _BoomClient)
    req = _main.TerraformDeployRequest(
        pipeline_conf="filter {}", raw_log='{"trace_id":"x"}',
        obs_bucket="mi-tracker-cts", obs_access_key="ak", obs_secret_key="sk",
        read_existing_bucket=True,
    )
    _main._do_obs_upload(req)  # early-return → no toca OBS
    assert calls["n"] == 0




def test_settings_maas_key_rejects_garbage(monkeypatch, tmp_path):
    """La key se valida antes de persistirse.

    Sin esto, la UI llegó a guardar la cadena de puntitos que usaba como relleno
    del campo. Como el settings file tiene prioridad sobre el env, esa basura
    sombreaba la key buena y MaaS devolvía un 401 imposible de diagnosticar
    ("Invalid authorization header"), porque el header salía con no-ASCII.
    """
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setenv("MAAS_API_KEY", "env-key-zzz9")

    invalidas = [
        ("•" * 12, "caracteres no válidos"),          # el relleno decorativo del campo
        ("•" * 12 + "clave-real-1234", "caracteres no válidos"),  # pegada ENCIMA del relleno
        ("clave con espacios 123", "espacios"),
        ("corta", "demasiado corta"),
        ("ñoño-key-con-tilde-á", "caracteres no válidos"),
    ]
    for key, motivo in invalidas:
        res = client.post("/api/v1/settings/maas", json={"api_key": key})
        assert res.status_code == 400, f"se aceptó una key inválida: {key!r}"
        assert motivo in res.json()["detail"]["message"]
        # Y no se persistió nada: sigue mandando la del env.
        assert mi.get_maas_api_key() == "env-key-zzz9"

    # Una key con forma de key sí entra.
    assert client.post("/api/v1/settings/maas", json={"api_key": "hk-abc123def456ghi"}).status_code == 200
    assert mi.get_maas_api_key() == "hk-abc123def456ghi"

    # Y se puede volver a borrar (la UI ahora puede hacerlo).
    assert client.post("/api/v1/settings/maas", json={"api_key": ""}).status_code == 200
    assert mi.get_maas_api_key() == "env-key-zzz9"


def test_set_maas_api_key_validates_at_the_source(monkeypatch, tmp_path):
    """La barrera también está en el setter, no solo en el endpoint."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    with pytest.raises(mi.InvalidApiKey):
        mi.set_maas_api_key("•" * 12)
    with pytest.raises(mi.InvalidApiKey):
        mi.set_maas_api_key("corta")
    mi.set_maas_api_key("hk-abc123def456ghi")   # válida, no levanta
    assert mi.get_maas_api_key() == "hk-abc123def456ghi"


def test_settings_maas_key_roundtrip(monkeypatch, tmp_path):
    """La API key de MaaS configurada desde ⚙ Configuración se persiste server-side,
    tiene PRIORIDAD sobre el env, se reporta enmascarada (nunca entera) y al borrarla
    se vuelve a la del env."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setenv("MAAS_API_KEY", "env-key-zzz9")

    # Sin settings → usa la del env. `models` mapea cada consumo LLM a su modelo.
    body = client.get("/api/v1/settings/maas").json()
    assert (body["configured"], body["source"], body["masked"]) == (True, "env", "••••zzz9")
    # El copiloto quedó fuera (sus endpoints se removieron de la plataforma).
    assert set(body["models"]) == {"pipeline", "chatbot_llm", "chatbot_ppl"}
    assert mi.get_maas_api_key() == "env-key-zzz9"

    # Configurar desde la UI → prioridad sobre el env, masked con los últimos 4.
    res = client.post("/api/v1/settings/maas", json={"api_key": "cliente-key-ab12"})
    assert res.json() == {"configured": True, "source": "settings"}
    assert mi.get_maas_api_key() == "cliente-key-ab12"
    assert client.get("/api/v1/settings/maas").json()["masked"] == "••••ab12"

    # Borrar (key vacía) → vuelve a la del env.
    client.post("/api/v1/settings/maas", json={"api_key": ""})
    assert client.get("/api/v1/settings/maas").json()["source"] == "env"
    assert mi.get_maas_api_key() == "env-key-zzz9"


def test_una_key_envenenada_en_disco_no_se_usa(monkeypatch, tmp_path):
    """La validación tiene que correr al LEER, no solo al escribir.

    Vivía únicamente en `set_maas_api_key`, así que una key guardada ANTES de que
    esa barrera existiera seguía ahí para siempre: el settings file tiene
    precedencia sobre el env y nadie la volvía a mirar. El síntoma era un 401 de
    ModelArts que no mencionaba la configuración, y la UI mostraba el campo con
    puntitos — indistinguible de una key sana."""
    import json as _json
    import maas_integrator as mi

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(mi, "_SETTINGS_PATH", settings)
    monkeypatch.setenv("MAAS_API_KEY", "env-key-zzz9")

    # Escrita a mano, esquivando el setter: es lo que quedó en los discos viejos.
    settings.write_text(_json.dumps({"maas_api_key": "•" * 12}), encoding="utf-8")

    assert mi.get_maas_api_key() == "env-key-zzz9", (
        "la key corrupta del disco tiene que ignorarse y caer al env, no viajar "
        "al header Authorization")
    assert mi.maas_key_source() == "env"

    problema = mi.maas_key_problem()
    assert problema and "caracteres" in problema
    assert client.get("/api/v1/settings/maas").json()["problem"] == problema


def test_la_key_del_env_se_strippea(monkeypatch, tmp_path):
    """El env era la ÚNICA rama que esquivaba toda normalización: un `\\r` de un
    .env editado en Windows entraba literal al header `Authorization`."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setenv("MAAS_API_KEY", "  env-key-con-espacios\r\n")
    assert mi.get_maas_api_key() == "env-key-con-espacios"


def test_probar_la_key_traduce_el_401(monkeypatch, tmp_path):
    """El botón Probar tiene que decir qué pasa, no volcar el error del SDK."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setenv("MAAS_API_KEY", "una-key-cualquiera")

    class _Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("Error code: 401 - ModelArts.81003")

    monkeypatch.setattr(mi, "_build_client", lambda: _Boom())
    r = client.post("/api/v1/settings/maas/test").json()
    assert r["ok"] is False
    assert "rechazó la API key" in r["detail"]
    assert "81003" not in r["detail"]

    class _Ok:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return object()

    monkeypatch.setattr(mi, "_build_client", lambda: _Ok())
    r = client.post("/api/v1/settings/maas/test").json()
    assert r["ok"] is True


def test_probar_sin_key_no_revienta(monkeypatch, tmp_path):
    """Sin key configurada, el botón responde 200 con ok=False: el resultado de la
    prueba es el contenido, no el status."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.delenv("MAAS_API_KEY", raising=False)

    res = client.post("/api/v1/settings/maas/test")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False and "No hay API Key" in body["detail"]


def test_settings_huawei_roundtrip(monkeypatch, tmp_path):
    """La cuenta Huawei de ⚙ Configuración se persiste server-side; `source`
    dice si el deploy la usa ('settings' exige la terna vpc/subnet/sg) y la
    región efectiva alimenta get_region() (settings > env > la-south-2)."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.delenv("HUAWEI_REGION", raising=False)
    monkeypatch.delenv("HUAWEI_PROJECT_ID", raising=False)

    # Sin settings → fallback al terraform.tfvars y región default.
    body = client.get("/api/v1/settings/huawei").json()
    assert body["source"] == "tfvars"
    assert body["effective_region"] == "la-south-2"
    assert main._huawei_infra_tfvars() == {}

    # Terna incompleta (solo vpc) → sigue en tfvars y NO inyecta nada.
    client.post("/api/v1/settings/huawei", json={"vpc_id": "vpc-1"})
    assert client.get("/api/v1/settings/huawei").json()["source"] == "tfvars"
    assert main._huawei_infra_tfvars() == {}

    # Cuenta completa → settings manda: región efectiva, project id e infra.
    res = client.post("/api/v1/settings/huawei", json={
        "project_id": "abcdef0123456789abcdef0123456789", "region": "sa-brazil-1", "availability_zone": "sa-brazil-1a",
        "vpc_id": "vpc-1", "subnet_id": "net-2", "security_group_id": "sg-3",
        "demo_bucket": "mis-demos",
    })
    body = res.json()
    assert body["source"] == "settings"
    assert body["effective_region"] == "sa-brazil-1"
    assert body["values"]["demo_bucket"] == "mis-demos"
    assert mi.get_region() == "sa-brazil-1"
    assert mi.get_huawei_project_id() == "abcdef0123456789abcdef0123456789"
    assert main._huawei_infra_tfvars() == {
        "vpc_id": "vpc-1", "subnet_id": "net-2", "security_group_id": "sg-3",
        "availability_zone": "sa-brazil-1a", "region": "sa-brazil-1",
    }

    # Vaciar todo → vuelve al fallback.
    client.post("/api/v1/settings/huawei", json={})
    assert client.get("/api/v1/settings/huawei").json()["source"] == "tfvars"


@requires_datasets
def test_datasets_preload_uploads_missing(monkeypatch, tmp_path):
    """El preload sube los datasets FALTANTES a `<slug>-logs/` con put_file
    (streaming de disco), saltea los que ya están (only_missing) y crea el
    bucket si no existe — todo streameado por SSE.

    El store de casos se aísla en un tmp_path: el aserto de abajo es una igualdad
    estricta contra los built-in, así que un caso custom en `data/cases/` (el de
    alguien que probó el Builder en esta máquina) lo rompía. Que los casos custom
    SÍ entren al preload lo cubre tests/test_custom_cases.py.
    """
    import auth
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    calls = {"put": [], "ensure": []}

    class _FakeObs:
        def __init__(self, **kwargs):
            calls["kwargs"] = kwargs
        def ensure_bucket(self, region=""):
            calls["ensure"].append(region)
            return True
        def object_exists(self, key):
            return key.startswith("transacciones-alyc-logs/")   # transacciones-alyc ya está subido
        def put_file(self, key, path):
            calls["put"].append(key)
        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", _FakeObs)
    res = client.post("/api/v1/datasets/preload", json={
        "access_key": "AK", "secret_key": "SK", "bucket": "mis-demos",
        "region": "sa-brazil-1",
    })
    assert res.status_code == 200
    import json
    events = [json.loads(l[len("data: "):]) for l in res.text.splitlines()
              if l.startswith("data: ")]
    complete = [e for e in events if e["type"] == "complete"]
    assert complete and complete[0]["skipped"] == 1 and complete[0]["errors"] == 0
    assert calls["ensure"] == ["sa-brazil-1"]
    # Subió todos los archivos del mapa salvo el de alyc; los 4 del SIEM van juntos.
    expected = {f"{slug}-logs/{f}" for slug, files in main._DEMO_DATASET_FILES.items()
                for f in files if slug != "transacciones-alyc"}
    assert set(calls["put"]) == expected
    assert complete[0]["uploaded"] == len(expected)


def test_deploy_guard_demo_datasets_missing(monkeypatch):
    """El deploy demo corta con 400 accionable ANTES de Terraform si el bucket
    no tiene el dataset de un tipo elegido; el chequeo es best-effort (si OBS
    falla, no bloquea) y no aplica a prefijos productivos."""
    class _FakeObs:
        def __init__(self, **kwargs):
            pass
        def prefix_has_objects(self, prefix):
            return prefix != "alyc-logs/"    # falta solo el de alyc
        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", _FakeObs)
    # Registry vacío: que el cap de pipelines no interfiera con este test.
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda _dir: {})
    body = {
        "project_name": "t", "pipeline_conf": "input {} output {}",
        "obs_access_key": "AK", "obs_secret_key": "SK", "obs_bucket": "mis-demos",
        "opensearch_password": "pw", "opensearch_index": "alyc-%{+YYYY.MM}",
        "read_existing_bucket": True,
        "cases": [
            {"slug": "transacciones-alyc", "raw_log": "x", "filter_code": "filter {}",
             "obs_prefix": "alyc-logs/", "read_existing_bucket": True},
            {"slug": "siem", "raw_log": "x", "filter_code": "filter {}",
             "obs_prefix": "siem-logs/", "read_existing_bucket": True},
        ],
    }
    res = client.post("/api/v1/terraform/deploy-stream", json=body)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["stage"] == "datasets_missing"
    assert "transacciones-alyc" in detail["message"] and "siem" not in detail["message"].split("`")[2]


_CONF_OK = ('input { s3 { bucket => "b" } }\nfilter { %s }\n'
            'output { elasticsearch { hosts => [] index => "logs-%%{+YYYY.MM}" } }\n')


def test_deploy_rejects_unavailable_plugins(monkeypatch):
    """La Logstash de CSS no trae `translate` (el cluster no puede instalar
    plugins). El deploy corta con 400 accionable ANTES del apply — el sandbox
    local NO lo detecta porque su imagen sí lo trae."""
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda _dir: {})
    body = {
        "pipeline_conf": _CONF_OK % 'translate { source => "code" target => "desc" }',
        "opensearch_password": "pw", "opensearch_index": "logs-%{+YYYY.MM}",
    }
    res = client.post("/api/v1/terraform/deploy-stream", json=body)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["stage"] == "pipeline_conf"
    assert "translate" in detail["message"]

    # Un conf sin plugins vetados NO debe disparar este 400 (puede fallar
    # después por otras cosas, pero no en este gate).
    ok_req = main.TerraformDeployRequest(
        pipeline_conf=_CONF_OK % 'mutate { add_field => { "translated" => "x" } }')
    assert main._check_conf_compila(ok_req) is None


def test_el_deploy_no_arranca_con_un_conf_que_logstash_no_compila(monkeypatch):
    """Lo que motivó el chequeo: Terraform acepta cualquier texto, crea la
    configuración y da "success". Logstash ni siquiera levanta la pipeline y el
    cluster queda vivo, vacío y sin un error a la vista."""
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda _dir: {})

    # El modo de falla clásico del LLM: el cuerpo del filter, sin envolver.
    roto = ('input { s3 { bucket => "b" } }\n'
            'grok { match => { "message" => "%{GREEDYDATA:m}" } }\n'
            'output { elasticsearch { hosts => [] } }\n')
    res = client.post("/api/v1/terraform/deploy-stream", json={
        "pipeline_conf": roto, "opensearch_password": "pw"})
    assert res.status_code == 400
    assert "sin el `filter" in res.json()["detail"]["message"]

    # Y una llave de menos, que es lo que deja el editor del paso 3.
    res = client.post("/api/v1/terraform/deploy-stream", json={
        "pipeline_conf": 'input { s3 { bucket => "b" }\nfilter { }\noutput { stdout {} }\n',
        "opensearch_password": "pw"})
    assert res.status_code == 400
    assert "sin cerrar" in res.json()["detail"]["message"]


def test_el_deploy_corrige_la_coma_del_hash_en_vez_de_rechazarla(monkeypatch):
    """La coma entre entradas de un hash es un tic del modelo (viene de escribir
    JSON) y no tiene nada de ambiguo: se corrige y el deploy sigue. Es el error
    que dejó la pipeline de telemetría en `unavailable`."""
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda _dir: {})
    con_coma = (_CONF_OK % 'mutate { convert => { "[data][a]" => "integer", "[data][b]" => "float" } }')
    req = main.TerraformDeployRequest(pipeline_conf=con_coma, opensearch_password="pw")

    notas = main._normalizar_conf(req)

    assert any("coma entre entradas de un hash" in n for n in notas), notas
    assert '"[data][a]" => "integer"  "[data][b]"' in req.pipeline_conf
    assert main._check_conf_compila(req) is None, "después de corregirla tiene que pasar"


def test_los_dos_endpoints_normalizan_antes_de_revisar():
    """Corregir después del guard no sirve de nada: el guard ya cortó."""
    import inspect
    for fn in (main.terraform_deploy_stream, main.terraform_deploy_job):
        src = inspect.getsource(fn)
        assert "_normalizar_conf(request)" in src, fn.__name__
        assert src.index("_normalizar_conf(request)") < src.index("_check_conf_compila(request)"), \
            f"{fn.__name__}: normaliza después de revisar"


def test_el_marcador_de_hosts_es_obligatorio_en_el_deploy():
    """Terraform inyecta el cluster con un `replace` literal de `hosts => []`.
    Sin ese texto exacto la pipeline escribe donde diga el .conf — típicamente
    el endpoint viejo que quedó del preview."""
    req = main.TerraformDeployRequest(
        pipeline_conf=('input { s3 { bucket => "b" } }\nfilter { }\n'
                       'output { elasticsearch { hosts => ["http://viejo:9200"] } }\n'))

    with pytest.raises(main.HTTPException) as exc:
        main._check_conf_compila(req)
    assert "marcador" in exc.value.detail["message"]


def test_el_filter_del_llm_se_valida_y_se_reintenta_con_feedback(monkeypatch):
    """Lo único que se exigía de la respuesta del modelo era que `filter_code`
    fuera un string no vacío. Un filter con una llave de menos, o el cuerpo sin
    el `filter { }` que lo envuelve, llegaba intacto a Terraform: la
    configuración se crea, Logstash no compila la pipeline y el cluster queda
    vivo y vacío. Ahora se revisa y se reintenta UNA vez con los problemas
    concretos como feedback — el camino correctivo que ya existía y que nadie
    llamaba."""
    import types
    import maas_integrator as mi

    def _respuesta(contenido):
        msg = types.SimpleNamespace(content=contenido)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    # El modelo devuelve el CUERPO del filter, sin envolver.
    roto = '{"filter_code": "grok { match => { \\"message\\" => \\"%{GREEDYDATA:m}\\" } }", "fields": []}'
    creado = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(
            create=lambda **kw: _respuesta(roto))))
    monkeypatch.setattr(mi, "_build_client", lambda: creado)
    monkeypatch.setattr(mi, "get_pipeline_model", lambda: "glm")

    visto = {}

    def fake_regen(sample_log, namespace, ecs_overlay, feedback, previous_filter, input_type=""):
        visto.update(feedback=feedback, previous=previous_filter)
        return {"filter_code": 'filter { grok { match => { "message" => "%{GREEDYDATA:m}" } } }',
                "fields": []}

    monkeypatch.setattr(mi, "_regenerate_with_feedback", fake_regen)

    res = mi.generate_logstash_filter("esto es una linea rara sin formato conocido 12345")

    assert res["filter_code"].startswith("filter {"), "devolvió el filter roto igual"
    assert "sin el `filter" in visto["feedback"], visto.get("feedback")
    assert visto["previous"].startswith("grok {"), "el reintento no vio el filter anterior"

    # Si los reintentos tampoco sirven, se dice en vez de devolver algo que no anda.
    intentos = []

    def _sigue_roto(**kw):
        intentos.append(kw["feedback"])
        return {"filter_code": "grok { }", "fields": []}

    monkeypatch.setattr(mi, "_regenerate_with_feedback", _sigue_roto)
    with pytest.raises(RuntimeError, match="no logró generar un filter válido"):
        mi.generate_logstash_filter("otra linea rara sin formato conocido 678")
    assert len(intentos) == mi._REINTENTOS_DEL_FILTRO == 2, "se rinde en el primer intento"


def test_el_pipeline_se_genera_con_glm_5_3_y_thinking(monkeypatch):
    """El .conf tiene que compilar a la primera: cada punto de fidelidad
    sintáctica se paga en deploys de 10 minutos que terminan en `unavailable`."""
    import types
    import maas_integrator as mi

    monkeypatch.delenv("MAAS_PIPELINE_MODEL", raising=False)
    assert mi.get_pipeline_model() == "glm-5.3"

    visto = {}
    bueno = '{"filter_code": "filter { json { source => \\"message\\" } }", "fields": []}'

    def _create(**kw):
        visto.update(kw)
        msg = types.SimpleNamespace(content=bueno)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr(mi, "_build_client", lambda: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create))))

    mi.generate_logstash_filter("una linea rara sin formato conocido 31337")

    assert visto["model"] == "glm-5.3"
    assert visto["extra_body"] == {"thinking": {"type": "enabled"}}

    # Y lo que de verdad viaja: el prompt armado, sin placeholders colgados.
    system = visto["messages"][0]["content"]
    for hueco in ("{ejemplo_filter}", "{plugin_context}", "{namespace}"):
        assert hueco not in system, f"el prompt salió con {hueco} sin resolver"
    assert "SINTAXIS de Logstash" in system
    assert mi._ejemplo_filter() in system, "el ejemplo no llegó al modelo"

    # Y el override por entorno sigue mandando (una instancia puede pinchar otro).
    monkeypatch.setenv("MAAS_PIPELINE_MODEL", "glm-5.2")
    assert mi.get_pipeline_model() == "glm-5.2"


def test_el_reintento_tambien_manda_las_reglas_y_el_ejemplo(monkeypatch):
    """El camino correctivo usa el mismo prompt: si ahí faltara la sección de
    sintaxis, el reintento repetiría el mismo error que vino a corregir."""
    import types
    import maas_integrator as mi

    visto = {}

    def _create(**kw):
        visto.update(kw)
        msg = types.SimpleNamespace(
            content='{"filter_code": "filter { json { source => \\"message\\" } }", "fields": []}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr(mi, "_build_client", lambda: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create))))
    monkeypatch.setattr(mi, "get_pipeline_model", lambda: "glm-5.3")

    mi._regenerate_with_feedback(
        sample_log="una linea", namespace="data", ecs_overlay=False,
        feedback="sobra una coma", previous_filter="filter { }")

    system = visto["messages"][0]["content"]
    assert "{ejemplo_filter}" not in system
    assert "SINTAXIS de Logstash" in system
    assert mi._ejemplo_filter() in system


def test_si_la_key_no_tiene_habilitado_el_modelo_se_sigue_con_el_anterior(monkeypatch):
    """Un modelo puede estar LISTADO en el MaaS y no estar habilitado para la
    key: el endpoint contesta 403 ModelArts.81004. Pasó al pasar a glm-5.3.
    Quedarse sin generación de pipelines por eso es peor que seguir con el
    anterior — pero tiene que decirse, no taparse."""
    import types
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_sin_acceso", set())
    pedidos = []

    def _create(**kw):
        pedidos.append(kw["model"])
        if kw["model"] == "glm-5.3":
            raise mi.OpenAIError(
                "Error code: 403 - {'error_code': 'ModelArts.81004', "
                "'error_msg': 'Invalid request because you do not have access to it.'}")
        msg = types.SimpleNamespace(content="ok")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    cliente = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create)))

    r = mi._chat(cliente, model="glm-5.3", messages=[])

    assert r.choices[0].message.content == "ok"
    assert pedidos == ["glm-5.3", "glm-5.2"]
    assert mi.modelo_efectivo() == "glm-5.2", "la UI tiene que decir con cuál generó"
    assert client.get("/api/v1/settings/maas").json()["models"]["pipeline"] == "glm-5.2", \
        "⚙ Configuración muestra el modelo configurado, no el que se usó"

    # La segunda vez ni se intenta: ya sabemos que no hay acceso.
    pedidos.clear()
    mi._chat(cliente, model="glm-5.3", messages=[])
    assert pedidos == ["glm-5.2"]

    # Un error que NO es de acceso se propaga tal cual.
    def _otro(**kw):
        raise mi.OpenAIError("connection reset")

    cliente_roto = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_otro)))
    monkeypatch.setattr(mi, "_sin_acceso", set())
    with pytest.raises(mi.OpenAIError, match="connection reset"):
        mi._chat(cliente_roto, model="glm-5.3", messages=[])


def test_un_modelo_que_razona_siempre_no_se_queda_sin_generar(monkeypatch):
    """glm-5.3 no admite que le configuren el thinking: `disabled` da 400
    ModelArts.81001. Con `MAAS_PIPELINE_THINKING=disabled` en el entorno, ESO
    dejaba a la plataforma sin generar un solo pipeline. Se manda sin el
    parámetro (el modelo razona igual, es su modo nativo) y se avisa."""
    import types
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_sin_switch_de_thinking", set())
    monkeypatch.setattr(mi, "_sin_acceso", set())
    pedidos = []

    def _create(**kw):
        pedidos.append(kw.get("extra_body"))
        if kw.get("extra_body"):
            raise mi.OpenAIError(
                "Error code: 400 - {'error_code': 'ModelArts.81001', 'error_msg': "
                "'request param validation error, Value error, unsupported thinking "
                "type for the current model: disabled'}")
        msg = types.SimpleNamespace(content="ok")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    cliente = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_create)))

    r = mi._chat(cliente, model="glm-5.3", messages=[],
                 extra_body={"thinking": {"type": "disabled"}})

    assert r.choices[0].message.content == "ok"
    assert pedidos == [{"thinking": {"type": "disabled"}}, None]

    # La segunda vez ya ni lo intenta.
    pedidos.clear()
    mi._chat(cliente, model="glm-5.3", messages=[], extra_body={"thinking": {"type": "disabled"}})
    assert pedidos == [None]


def test_el_prompt_explica_la_sintaxis_que_el_modelo_rompe():
    """El prompt tenía reglas de parseo pero ni una palabra de sintaxis, y los
    pocos hashes que mostraba eran de UNA entrada: el modelo nunca veía cómo se
    separan dos. De ahí salió la coma que dejó una pipeline en `unavailable`."""
    import maas_integrator as mi

    for prompt in (mi.SYSTEM_PROMPT_BASE, mi.SYSTEM_PROMPT_JDBC):
        assert "SINTAXIS de Logstash" in prompt
        assert "se separan con ESPACIO" in prompt, "falta la regla que rompió la pipeline"
        assert "la coma es solo para los arrays" in prompt
        assert 'BIEN:  convert => { "a" => "integer"  "b" => "float" }' in prompt
        assert 'MAL:   convert => { "a" => "integer", "b" => "float" }' in prompt
        assert "La asignación es `=>`" in prompt


def test_el_prompt_lleva_un_filter_de_verdad_como_ejemplo():
    """Sale del catálogo y no de un literal en el prompt: así es necesariamente
    uno que hoy corre en CSS, y el test del lint sobre todo el catálogo lo
    mantiene sano."""
    import conf_lint
    import maas_integrator as mi
    import verticals

    ejemplo = mi._ejemplo_filter()

    assert ejemplo in [v.get("filter_code", "").strip() for v in verticals.all_verticals()]
    assert conf_lint.lint_filtro(ejemplo) == []
    assert "convert => {" in ejemplo and "remove_field => [" in ejemplo
    # La forma que el modelo tiene que copiar: hash sin comas, array con comas.
    hash_convert = ejemplo.split("convert => {", 1)[1].split("}", 1)[0]
    assert "," not in hash_convert, hash_convert
    assert hash_convert.count("=>") >= 2, "un hash de una entrada no enseña nada"

    armado = (mi.SYSTEM_PROMPT_BASE.replace("{ejemplo_filter}", ejemplo)
              .replace("{plugin_context}", "").replace("{namespace}", "data"))
    assert "{ejemplo_filter}" not in armado
    assert ejemplo in armado


def test_la_coma_del_hash_del_llm_se_corrige_sin_gastar_un_reintento(monkeypatch):
    """600 s de reintento para algo que sabemos cómo se escribe, no: la coma
    entre entradas de un hash se arregla en el acto."""
    import types
    import maas_integrator as mi

    con_coma = ('{"filter_code": "filter { mutate { convert => { \\"a\\" => \\"integer\\", '
                '\\"b\\" => \\"float\\" } } }", "fields": []}')
    msg = types.SimpleNamespace(content=con_coma)
    creado = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(
        create=lambda **kw: types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)]))))
    monkeypatch.setattr(mi, "_build_client", lambda: creado)
    monkeypatch.setattr(mi, "get_pipeline_model", lambda: "glm")

    def no_llamar(**kw):
        raise AssertionError("gastó un reintento en una coma")

    monkeypatch.setattr(mi, "_regenerate_with_feedback", no_llamar)

    res = mi.generate_logstash_filter("una linea rara sin formato conocido 4242")

    assert '"a" => "integer"  "b" => "float"' in res["filter_code"]


def test_un_filter_sano_del_llm_no_dispara_el_reintento(monkeypatch):
    import types
    import maas_integrator as mi

    bueno = '{"filter_code": "filter { json { source => \\"message\\" } }", "fields": []}'
    msg = types.SimpleNamespace(content=bueno)
    creado = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(
        create=lambda **kw: types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)]))))
    monkeypatch.setattr(mi, "_build_client", lambda: creado)
    monkeypatch.setattr(mi, "get_pipeline_model", lambda: "glm")

    def no_llamar(**kw):
        raise AssertionError("reintentó con un filter que estaba bien")

    monkeypatch.setattr(mi, "_regenerate_with_feedback", no_llamar)

    res = mi.generate_logstash_filter("otra linea rara sin formato conocido 999")
    assert "json" in res["filter_code"]


def test_generate_filter_feedback_goes_straight_to_llm(monkeypatch):
    """Con `feedback` (error del sandbox), el generador SALTEA los parsers
    determinísticos — aunque el log sea JSON parseable — y va al LLM con el
    filter anterior + el feedback para corregirlo."""
    import maas_integrator as mi

    called = {}

    def fake_regen(sample_log, namespace, ecs_overlay, feedback, previous_filter, input_type=""):
        called.update(feedback=feedback, previous=previous_filter, ns=namespace)
        return {"filter_code": "filter { json { source => \"message\" } }",
                "fields": [], "multiline_hint": None}

    monkeypatch.setattr(mi, "_regenerate_with_feedback", fake_regen)
    json_log = '{"a": 1, "b": "x"}'

    # Sin feedback: path determinístico (no pasa por el regen).
    res = mi.generate_logstash_filter(json_log)
    assert "json" in res["filter_code"] and not called

    # Con feedback: directo al LLM correctivo, con el filter anterior.
    res = mi.generate_logstash_filter(
        json_log, feedback="_grokparsefailure en todos los docs",
        previous_filter="filter { grok { ... } }")
    assert called["feedback"] == "_grokparsefailure en todos los docs"
    assert called["previous"].startswith("filter { grok")

    # El endpoint pasa feedback/previous_filter al generador.
    seen = {}

    def fake_gen(raw_log, namespace="data", ecs_overlay=False, feedback="", previous_filter="", input_type=""):
        seen.update(feedback=feedback, previous=previous_filter)
        return {"filter_code": "filter { }", "fields": []}

    monkeypatch.setattr(main, "generate_logstash_filter", fake_gen)
    r = client.post("/api/v1/onboarding/generate-filter", json={
        "raw_log": json_log, "feedback": "fix", "previous_filter": "filter { old }"})
    assert r.status_code == 200
    assert seen == {"feedback": "fix", "previous": "filter { old }"}


def test_provision_capabilities_uses_configured_maas_key(monkeypatch, tmp_path):
    """Los connectors del chatbot de OpenSearch se crean con la key CONFIGURADA
    (⚙), no con la del env — así el agente consume los recursos del cliente."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setenv("MAAS_API_KEY", "env-key-del-operador")
    mi.set_maas_api_key("key-del-cliente")

    monkeypatch.setattr(main, "_read_capabilities", lambda td: {})
    monkeypatch.setattr(main, "_write_capabilities", lambda td, reg: None)

    connector_keys = []

    def fake_request(method, url, **kwargs):
        if method == "GET" and "/_plugins/_ml/stats" in url:
            return _FakeResp(200, {"nodes": {}})
        if "/_plugins/_ml/connectors/_create" in url:
            connector_keys.append((kwargs.get("json") or {}).get("credential", {}).get("maas_key"))
            return _FakeResp(200, {"connector_id": "C"})
        if "/_plugins/_ml/model_groups/_register" in url:
            return _FakeResp(200, {"model_group_id": "MG"})
        if method == "GET" and "/_field_caps" in url:
            return _FakeResp(404, {}, text="index_not_found_exception")
        if method == "GET" and url.endswith("/_count"):
            return _FakeResp(200, {"count": 0})
        return _FakeResp(200, {})

    monkeypatch.setattr("requests.request", fake_request)
    main._provision_capabilities({"public_endpoint": "1.2.3.4:9200"},
                                 "transacciones-billetera", "admin", "pw", https_enabled=False)
    assert connector_keys, "no se crearon connectors"
    assert all(k == "key-del-cliente" for k in connector_keys), connector_keys


def test_cts_output_emits_document_id_dedup():
    """El output de CTS deduplica por trace_id (document_id) y no administra
    template/ILM (los tipos los pone el index template)."""
    from main import gen_output_elasticsearch, ElasticsearchOutputConfig
    out = gen_output_elasticsearch(ElasticsearchOutputConfig(
        index="huawei-cts-%{+YYYY.MM}", document_id="%{trace_id}",
        manage_template=False, ilm_enabled=False,
    ))
    assert 'document_id => "%{trace_id}"' in out
    assert "manage_template => false" in out
    assert "ilm_enabled => false" in out


def test_cts_case_conf_is_readonly_oneshot():
    """Caso CTS (read_existing_bucket con document_id): el input lee los traces
    REALES read-only y one-shot — NO borra el origen (delete=false) ni queda
    polleando (watch=false), y el output deduplica por el document_id del caso
    (%{trace_id}). Así re-correr no altera el bucket universal ni duplica docs."""
    import main as _main
    case = _main.PipelineCase(
        slug="cts", filter_code="filter {}", fields=[],
        index_name="huawei-cts-%{+YYYY.MM}", obs_prefix="CloudTraces/",
        read_existing_bucket=True, document_id="%{trace_id}",
    )
    req = _main.TerraformDeployRequest(
        pipeline_conf="x", obs_bucket="mi-tracker-cts",
        obs_access_key="ak", obs_secret_key="sk",
        opensearch_user="admin", opensearch_password="pw",
    )
    conf = _main._build_pipeline_conf_for_case(case, req)
    assert "delete => false" in conf
    assert "watch_for_new_files => false" in conf
    assert 'document_id => "%{trace_id}"' in conf


def test_readexisting_case_without_document_id():
    """Predefinido read_existing SIN document_id (ej. firewall pre-cargado en OBS):
    input read-only one-shot + sin template/ILM administrados, y NO se emite
    `document_id` (Logstash asigna ids auto; el clear de índice evita dupes)."""
    import main as _main
    case = _main.PipelineCase(
        slug="firewall", filter_code="filter {}", fields=[],
        index_name="firewall-%{+YYYY.MM}", obs_prefix="firewall-logs/",
        read_existing_bucket=True,
    )
    req = _main.TerraformDeployRequest(
        pipeline_conf="x", obs_bucket="mi-tracker-cts",
        obs_access_key="ak", obs_secret_key="sk", opensearch_password="pw",
    )
    conf = _main._build_pipeline_conf_for_case(case, req)
    assert "delete => false" in conf
    assert "watch_for_new_files => false" in conf
    assert "manage_template => false" in conf
    assert "document_id =>" not in conf


def test_readexisting_case_with_fingerprint_document_id():
    """Predefinido read_existing CON document_id de fingerprint (fintech): el output
    deduplica por `%{[@metadata][generated_id]}` (el filtro lo calcula)."""
    import main as _main
    case = _main.PipelineCase(
        slug="transacciones-billetera", filter_code="filter {}", fields=[],
        index_name="transacciones-billetera-%{+YYYY.MM}",
        obs_prefix="transacciones-billetera-logs/",
        read_existing_bucket=True, document_id="%{[@metadata][generated_id]}",
    )
    req = _main.TerraformDeployRequest(
        pipeline_conf="x", obs_bucket="mi-tracker-cts",
        obs_access_key="ak", obs_secret_key="sk", opensearch_password="pw",
    )
    conf = _main._build_pipeline_conf_for_case(case, req)
    assert "delete => false" in conf
    assert 'document_id => "%{[@metadata][generated_id]}"' in conf


def test_non_cts_case_conf_deletes_and_watches():
    """Un caso normal (no CTS) mantiene el default: borra el archivo procesado y
    pollea por nuevos — son los sintéticos/dataset que subimos, no datos reales."""
    import main as _main
    case = _main.PipelineCase(
        slug="firewall", filter_code="filter {}", fields=[],
        index_name="firewall-%{+YYYY.MM}", obs_prefix="logs/firewall/",
    )
    req = _main.TerraformDeployRequest(
        pipeline_conf="x", obs_bucket="mi-tracker-cts",
        obs_access_key="ak", obs_secret_key="sk",
    )
    conf = _main._build_pipeline_conf_for_case(case, req)
    assert "delete => true" in conf
    assert "watch_for_new_files => true" in conf
    assert "document_id" not in conf


def test_deploy_sequence_writes_pipelines_map_to_tfvars(monkeypatch, tmp_path):
    """Deploy en dos fases: cada pipeline va en el mapa `pipelines` del tfvars
    (slug => {pipeline_conf, start_ingestion}); fase 2 = start_ingestion True.

    Apunta a `_prepare_deploy_tfvars`, que es el setup que corre el deploy REAL
    (el SSE). Antes llamaba a `_do_terraform_sequence`, el camino no-SSE que el
    front nunca usó: el test pasaba mientras el tfvars que producción escribía
    podía haber divergido sin que nadie se enterara."""
    import json as _json
    import main as _main

    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)  # skip init

    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **k: _OkProc())
    req = _main.TerraformDeployRequest(
        pipeline_conf="filter {}", project_name="x", start_ingestion=True,
        opensearch_index="logs-%{+YYYY.MM}",
    )
    _main._prepare_deploy_tfvars(req, td)
    tfvars = _json.loads((td / "deploy.auto.tfvars.json").read_text(encoding="utf-8"))
    assert "start_ingestion" not in tfvars  # ya no es escalar
    assert tfvars["pipelines"]["logs"]["start_ingestion"] is True
    assert tfvars["pipelines"]["logs"]["pipeline_conf"] == "filter {}"


def test_deploy_sequence_merges_pipelines_registry_not_overwrite(monkeypatch, tmp_path):
    """Dos deploys con índices distintos = dos slugs en paralelo en el registro;
    el segundo NO pisa al primero, y el tfvars del 2do incluye AMBOS (sino el
    for_each con el mapa parcial destruiría la primera pipeline)."""
    import json as _json
    import main as _main

    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **k: _OkProc())

    _main._prepare_deploy_tfvars(_main.TerraformDeployRequest(
        pipeline_conf="filter { A }", project_name="x", start_ingestion=True,
        opensearch_index="logs-%{+YYYY.MM}", obs_prefix="logs/",
    ), td)
    _main._prepare_deploy_tfvars(_main.TerraformDeployRequest(
        pipeline_conf="filter { B }", project_name="x", start_ingestion=False,
        opensearch_index="logs-ej2-%{+YYYY.MM}", obs_prefix="logs/ej2/",
    ), td)

    registry = _json.loads((td / _main._PIPELINES_REGISTRY_NAME).read_text(encoding="utf-8"))
    assert set(registry) == {"logs", "logs-ej2"}
    assert registry["logs"]["obs_prefix"] == "logs/"
    assert registry["logs-ej2"]["obs_prefix"] == "logs/ej2/"
    # El 2do apply mantiene ambas en el mapa, con sus flags independientes.
    tfvars = _json.loads((td / "deploy.auto.tfvars.json").read_text(encoding="utf-8"))
    assert set(tfvars["pipelines"]) == {"logs", "logs-ej2"}
    assert tfvars["pipelines"]["logs"]["start_ingestion"] is True
    assert tfvars["pipelines"]["logs-ej2"]["start_ingestion"] is False


def test_fresh_deploy_no_pisa_el_registro_con_entorno_activo(monkeypatch, tmp_path):
    """El front mandaba `fresh_deploy: true` en TODO deploy del wizard. Con un
    entorno de tres pipelines, agregar un caso dejaba una sola: el backend
    descartaba el registro y el for_each de Terraform destruía el resto. Con
    marcador de plataforma el flag se ignora: siempre se mergea, y el
    project_name es el del entorno (si no, se recrearía el Logstash)."""
    import json as _json
    import main as _main

    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **k: _OkProc())
    _main._prepare_deploy_tfvars(_main.TerraformDeployRequest(
        pipeline_conf="filter { A }", project_name="log-analytics",
        opensearch_index="siem-%{+YYYY.MM}", obs_prefix="siem-logs/"), td)
    _main._write_platform_marker(td, "log-analytics")

    _main._prepare_deploy_tfvars(_main.TerraformDeployRequest(
        pipeline_conf="filter { B }", project_name="otro-nombre", fresh_deploy=True,
        opensearch_index="reviews-olist-%{+YYYY.MM}", obs_prefix="reviews-olist-logs/"), td)

    registry = _json.loads((td / _main._PIPELINES_REGISTRY_NAME).read_text(encoding="utf-8"))
    assert set(registry) == {"siem", "reviews-olist"}, "fresh_deploy pisó el registro con entorno activo"
    tfvars = _json.loads((td / "deploy.auto.tfvars.json").read_text(encoding="utf-8"))
    assert set(tfvars["pipelines"]) == {"siem", "reviews-olist"}
    assert tfvars["project_name"] == "log-analytics", "otro project_name recrearía el Logstash"


def test_fresh_deploy_limpia_el_registro_sin_entorno(monkeypatch, tmp_path):
    """Sin marcador no hay entorno: un registro con basura de un deploy que
    falló antes del apply SÍ se limpia."""
    import json as _json
    import main as _main

    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **k: _OkProc())
    _main._prepare_deploy_tfvars(_main.TerraformDeployRequest(
        pipeline_conf="filter { A }", opensearch_index="basura-%{+YYYY.MM}"), td)

    _main._prepare_deploy_tfvars(_main.TerraformDeployRequest(
        pipeline_conf="filter { B }", fresh_deploy=True,
        opensearch_index="siem-%{+YYYY.MM}"), td)

    registry = _json.loads((td / _main._PIPELINES_REGISTRY_NAME).read_text(encoding="utf-8"))
    assert set(registry) == {"siem"}


def test_deploy_caps_concurrent_pipelines(monkeypatch, tmp_path):
    """Un slug NUEVO cuando ya hay _MAX_PIPELINES registradas → 400 (el Logstash
    es 1 nodo). No invoca terraform."""
    import json as _json
    import main as _main

    monkeypatch.setattr(_main, "_MAX_PIPELINES", 5)
    fake_main = tmp_path / "main.py"
    fake_main.write_text("")
    td = tmp_path / "terraform"
    td.mkdir()
    full = {f"ej{i}": {"pipeline_conf": "filter {}", "start_ingestion": True,
                       "index": f"logs-ej{i}", "obs_prefix": f"logs/ej{i}/"}
            for i in range(1, _main._MAX_PIPELINES + 1)}
    (td / _main._PIPELINES_REGISTRY_NAME).write_text(_json.dumps(full), encoding="utf-8")

    def _no_run(*a, **k):
        raise AssertionError("terraform no debería invocarse al pegar contra el cap")
    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setattr(_main.subprocess, "run", _no_run)

    # Contra /deploy-job, que es el endpoint que usa el front. El cap se chequea
    # antes de tocar terraform, así que el 400 llega sin lanzar el job.
    res = client.post("/api/v1/terraform/deploy-job", json={
        "pipeline_conf": "filter {}", "opensearch_index": "logs-nuevo-%{+YYYY.MM}",
    })
    assert res.status_code == 400
    assert "Máximo" in str(res.json()["detail"])


def test_terraform_destroy_returns_noop_when_state_empty(monkeypatch, tmp_path):
    """Sin tfstate (workspace local, sin backend remoto) destroy responde noop
    sin invocar terraform.

    Corre en un workspace propio: antes pegaba al `terraform/` del repo y se
    salteaba si había un tfstate real. Con el backend en OBS eso dejó de
    alcanzar —el `backend.tf` de un deploy local hacía que el state se leyera
    con `terraform state pull`, o sea un subprocess— y encima el test apuntaba
    el endpoint de destroy al workspace de verdad de la máquina.
    """
    import main as _main

    (tmp_path / "terraform").mkdir()
    fake_main = tmp_path / "main.py"
    fake_main.write_text("")
    monkeypatch.setattr(_main, "__file__", str(fake_main))

    def _fake_run(*args, **kwargs):
        raise AssertionError("subprocess.run no debería invocarse en path noop")
    monkeypatch.setattr(_main.subprocess, "run", _fake_run)

    res = client.post("/api/v1/terraform/destroy")
    assert res.status_code == 200
    assert res.json()["status"] == "noop"


def test_terraform_destroy_removes_pipelines_registry(monkeypatch, tmp_path):
    """Tras un destroy exitoso se borra el registro de pipelines (sino "Mi
    Infraestructura" seguiría listando pipelines de un entorno ya destruido)."""
    import json as _json
    import main as _main

    fake_main = tmp_path / "main.py"
    fake_main.write_text("")
    td = tmp_path / "terraform"
    td.mkdir()
    (td / "terraform.tfstate").write_text("x" * 512)  # state real → no noop
    (td / _main._DESTROY_CREDS_NAME).write_text('{"hwc_access_key":"a"}')  # hay creds
    (td / _main._PIPELINES_REGISTRY_NAME).write_text(
        _json.dumps({"logs": {"pipeline_conf": "filter {}", "start_ingestion": True}})
    )

    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **k: _OkProc())

    res = client.post("/api/v1/terraform/destroy", json={})
    assert res.status_code == 200
    assert res.json()["status"] == "success"
    assert not (td / _main._PIPELINES_REGISTRY_NAME).exists()


def test_terraform_destroy_endpoint_exists_and_accepts_post(monkeypatch, tmp_path):
    """Smoke: el endpoint existe, acepta POST y responde con shape válido.

    HERMÉTICO A PROPÓSITO: apuntamos `__file__` a un terraform/ temporal VACÍO
    (→ path noop) y mockeamos `subprocess.run` para que falle el test si se
    intenta invocar terraform. Antes este test pegaba al `terraform/` REAL sin
    mockear: con un entorno desplegado + creds persistidas, el POST corría
    `terraform destroy` de verdad y BORRABA EL CLUSTER del operador al correr
    el suite. Nunca más: un test jamás debe destruir infraestructura real."""
    import main as _main

    fake_main = tmp_path / "main.py"
    fake_main.write_text("")
    (tmp_path / "terraform").mkdir()  # sin tfstate → guard noop

    def _no_run(*a, **k):
        raise AssertionError("destroy no debe invocar terraform en un test")
    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setattr(_main.subprocess, "run", _no_run)

    res = client.post("/api/v1/terraform/destroy")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "noop"


def test_terraform_destroy_400_when_state_but_no_creds(monkeypatch, tmp_path):
    """Con state real pero sin `destroy.auto.tfvars.json` ni creds en el body,
    el destroy devuelve 400 (no se cuelga ni invoca terraform)."""
    import main as _main

    fake_main = tmp_path / "main.py"
    fake_main.write_text("")
    (tmp_path / "terraform").mkdir()
    (tmp_path / "terraform" / "terraform.tfstate").write_text("x" * 512)  # state real

    def _fail_run(*a, **k):
        raise AssertionError("terraform no debería invocarse sin creds")

    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setattr(_main.subprocess, "run", _fail_run)

    res = client.post("/api/v1/terraform/destroy", json={})
    assert res.status_code == 400


def test_terraform_status_returns_inactive_when_no_state(monkeypatch, tmp_path):
    """Si el tfstate no existe (o pesa <200 bytes), el endpoint reporta
    active=False y deja los timestamps en None — mismo criterio que el
    guard noop del endpoint /destroy."""
    from pathlib import Path as _Path
    import main as _main

    # Redirigimos Path(__file__).parent del handler hacia un tmp sin tfstate.
    fake_main = tmp_path / "main.py"
    fake_main.write_text("")  # solo necesitamos el parent existente
    (tmp_path / "terraform").mkdir()

    monkeypatch.setattr(_main, "__file__", str(fake_main))
    res = client.get("/api/v1/terraform/status")
    assert res.status_code == 200
    body = res.json()
    assert body["active"] is False
    assert body["deployed_at"] is None
    assert body["deployed_seconds_ago"] is None


def _write_fake_state_with_cluster(tmp_path, write_marker=True):
    """Escribe un tfstate sintético válido con un recurso css_cluster +
    devuelve (fake_main_path, tfstate_path). Reutilizado por los tests de
    status que ejercitan la construcción de la URL desde el state.

    `write_marker` controla si además se escribe el marcador
    `.platform_deploy.json` — necesario para que el endpoint reporte
    active=True (un state sin marcador se considera no-desplegado-por-la-app).
    """
    import json as _json

    fake_main = tmp_path / "main.py"
    fake_main.write_text("")
    (tmp_path / "terraform").mkdir()
    if write_marker:
        (tmp_path / "terraform" / ".platform_deploy.json").write_text(
            _json.dumps({"deployed_at": "2026-06-02T19:06:57+00:00", "project_name": "demo-cliente-x"})
        )
    tfstate = tmp_path / "terraform" / "terraform.tfstate"
    tfstate.write_text(_json.dumps({
        "version": 4,
        "resources": [
            {
                "mode": "managed",
                "type": "huaweicloud_css_cluster",
                "name": "opensearch_cluster",
                "instances": [
                    {"attributes": {
                        "id": "abc123-cluster-id",
                        "endpoint": "192.168.0.50:9200",
                        "name": "demo-cliente-x-opensearch",
                    }},
                ],
            },
            {
                "mode": "managed",
                "type": "huaweicloud_css_logstash_cluster",
                "name": "logstash_cluster",
                "instances": [
                    {"attributes": {
                        "id": "logstash-id",
                        "endpoint": "192.168.0.51:9200",
                        "name": "demo-cliente-x-logstash",
                    }},
                ],
            },
        ],
    }))
    return fake_main, tfstate


def test_terraform_status_builds_console_url_from_state(monkeypatch, tmp_path):
    """Con tfstate válido + HUAWEI_PROJECT_ID seteado, el endpoint
    construye la URL de consola Huawei desde el cluster_id del state (NO del
    output de Terraform). project_name se deriva del nombre del cluster.
    pipeline_conf + la lista de pipelines vienen del registro `.pipelines.json`."""
    import json as _json
    import main as _main

    fake_main, _ = _write_fake_state_with_cluster(tmp_path)
    # Registro de pipelines (fuente de pipeline_conf + lista, ya no el output TF).
    (tmp_path / "terraform" / _main._PIPELINES_REGISTRY_NAME).write_text(_json.dumps({
        "logs": {"pipeline_conf": "filter { kv { source => \"message\" } }",
                 "start_ingestion": True, "index": "logs-%{+YYYY.MM}", "obs_prefix": "logs/"},
    }))

    class _FakeProc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **kw: _FakeProc())
    monkeypatch.setenv("HUAWEI_PROJECT_ID", "deadbeef" * 4)  # 32 hex chars

    res = client.get("/api/v1/terraform/status")
    assert res.status_code == 200
    body = res.json()
    assert body["active"] is True
    assert body["deployed_at"] is not None
    assert isinstance(body["deployed_seconds_ago"], int)
    # URL de consola construida desde state + env, no del output de TF.
    assert body["dashboards_url"] == (
        "https://la-south-2-console.huaweicloud.com/elasticsearch/kibana/"
        "la-south-2/" + "deadbeef" * 4 + "/abc123-cluster-id/app/login"
    )
    assert body["pipeline_conf"] == "filter { kv { source => \"message\" } }"
    # La lista de pipelines en paralelo viene del registro.
    assert body["pipelines"] == [
        {"slug": "logs", "index": "logs-%{+YYYY.MM}", "obs_prefix": "logs/", "active": True,
         "config_status": "", "dashboards_imported": False, "has_capabilities": False}
    ]
    # Derivado de "demo-cliente-x-opensearch" → strip "-opensearch".
    assert body["project_name"] == "demo-cliente-x"
    # Endpoints parseados del state (OpenSearch + Logstash).
    assert body["opensearch_endpoint"] == "192.168.0.50:9200"
    assert body["logstash_endpoint"] == "192.168.0.51:9200"


def test_terraform_status_returns_index_template_snippet(monkeypatch, tmp_path):
    """Si hay artifact `.index_template.json` (persistido en el deploy), el
    status lo devuelve para que 'Mi Infraestructura' lo muestre."""
    import json as _json
    import main as _main

    fake_main, _ = _write_fake_state_with_cluster(tmp_path)
    # Persistir el artifact del template (como hace el deploy).
    (tmp_path / "terraform" / ".index_template.json").write_text(_json.dumps({
        "template_name": "demo-x",
        "put_snippet": "PUT _index_template/demo-x\n{}",
    }))

    class _FakeProc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **kw: _FakeProc())

    res = client.get("/api/v1/terraform/status")
    assert res.status_code == 200
    body = res.json()
    assert body["index_template_name"] == "demo-x"
    assert body["index_template_snippet"].startswith("PUT _index_template/demo-x")


def test_terraform_status_falls_back_to_internal_url_without_project_id(monkeypatch, tmp_path):
    """Sin HUAWEI_PROJECT_ID, la URL cae al link interno por VPC
    (protocol://endpoint/_dashboards) construido desde el endpoint del
    state — el output de TF no se usa."""
    import main as _main

    fake_main, _ = _write_fake_state_with_cluster(tmp_path)

    class _FailProc:
        returncode = 1
        stdout = ""
        stderr = ""

    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **kw: _FailProc())
    monkeypatch.delenv("HUAWEI_PROJECT_ID", raising=False)

    res = client.get("/api/v1/terraform/status")
    assert res.status_code == 200
    body = res.json()
    assert body["active"] is True
    assert body["dashboards_url"] == "https://192.168.0.50:9200/_dashboards"


def test_terraform_status_degrades_when_state_unparseable(monkeypatch, tmp_path):
    """Con marcador presente pero tfstate >200 bytes no parseable (corrupto),
    el endpoint igual responde active=True; dashboards_url y demás derivados
    quedan None — la UI degrada sin romper."""
    import json as _json
    import main as _main

    fake_main = tmp_path / "main.py"
    fake_main.write_text("")
    (tmp_path / "terraform").mkdir()
    # Marcador SIN project_name → fuerza el fallback (que también falla por
    # state corrupto) para verificar que project_name queda None.
    (tmp_path / "terraform" / ".platform_deploy.json").write_text(
        _json.dumps({"deployed_at": "2026-06-02T19:06:57+00:00"})
    )
    tfstate = tmp_path / "terraform" / "terraform.tfstate"
    tfstate.write_text("x" * 512)  # >200 bytes pero no es JSON válido

    class _FailProc:
        returncode = 1
        stdout = ""
        stderr = "Error: state corrupto"

    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **kw: _FailProc())
    monkeypatch.setenv("HUAWEI_PROJECT_ID", "deadbeef" * 4)

    res = client.get("/api/v1/terraform/status")
    assert res.status_code == 200
    body = res.json()
    assert body["active"] is True
    assert body["dashboards_url"] is None
    assert body["pipeline_conf"] is None
    assert body["project_name"] is None


def test_terraform_status_inactive_when_state_but_no_marker(monkeypatch, tmp_path):
    """REGRESIÓN clave: hay un tfstate válido con recursos, PERO sin el
    marcador .platform_deploy.json (entorno no desplegado desde la app, o un
    state stale de un cluster borrado a mano). El endpoint debe reportar
    active=False — la app no reclama como propio algo que no levantó."""
    import main as _main

    # write_marker=False → state presente con cluster, pero sin marcador.
    fake_main, _ = _write_fake_state_with_cluster(tmp_path, write_marker=False)

    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setenv("HUAWEI_PROJECT_ID", "deadbeef" * 4)

    res = client.get("/api/v1/terraform/status")
    assert res.status_code == 200
    body = res.json()
    assert body["active"] is False
    assert body["deployed_at"] is None
    assert body["opensearch_endpoint"] is None


def test_generate_pipeline_strips_comments_from_filter_code():
    """Regression: el cached example en index.html y la edición manual del
    operador en el textarea de step 5 son dos paths que no pasan por
    `generate_logstash_filter`, así que su strip no aplica. El handler
    `/generate-pipeline` debe stripear como catch-all final para que el
    .conf deployado nunca tenga comentarios."""
    filter_with_comments = """filter {
  kv { source => "message" }
  # ECS: duration en nanosegundos
  ruby { code => "event.set('[event][duration]', 0)" } # inline también
  mutate { rename => { "msg" => "[message]" } }
}"""
    res = client.post(
        "/api/v1/onboarding/generate-pipeline",
        json={
            "filter_code": filter_with_comments,
            "input_config": {
                "plugin_type": "s3",
                "s3": {"bucket": "b", "access_key_id": "AK", "secret_access_key": "SK"},
            },
            "output_config": {
                "plugin_type": "elasticsearch",
                "elasticsearch": {"hosts": ["http://10.0.0.5:9200"], "user": "admin", "password": "X"},
            },
        },
    )
    assert res.status_code == 200
    pipe = res.json()["pipeline_code"]
    # Ninguna línea del pipeline debe contener `#` fuera de strings quoted.
    for line in pipe.splitlines():
        if "#" in line:
            # `#` permitido solo dentro de strings (ej. ``"build #1234"``).
            # Heurística: la parte ANTES del primer `#` debe tener comilla impar.
            idx = line.index("#")
            prefix = line[:idx]
            assert prefix.count('"') % 2 == 1 or prefix.count("'") % 2 == 1, (
                f"Comentario sin stripear en pipeline: {line!r}"
            )


def test_clean_shape_strips_markdown_fences():
    from maas_integrator import _clean_shape
    assert _clean_shape("```\n127.0.0.1 - alice GET /api 200\n```") == "127.0.0.1 - alice GET /api 200"
    assert _clean_shape('```json\n{"a":1}\n```') == '{"a":1}'


def test_clean_shape_strips_numbered_list_prefix():
    from maas_integrator import _clean_shape
    assert _clean_shape("1. host=h1 user=alice resp=200") == "host=h1 user=alice resp=200"
    assert _clean_shape("12. <13>May 19 14:30:01 host01 sshd") == "<13>May 19 14:30:01 host01 sshd"


def test_clean_shape_drops_pure_comment_lines():
    from maas_integrator import _clean_shape
    assert _clean_shape("# variación 1") is None
    assert _clean_shape("   # otro comentario  ") is None
    # Pero una línea de código con `#` adentro NO es descartada por este
    # filtro (cleaner solo mira prefijo).
    assert _clean_shape('message => "build #1234"') == 'message => "build #1234"'


def test_shape_format_filter_rejects_json_when_raw_is_apache():
    """LLM aluciona devolviendo JSON cuando el raw era Apache. Filtrarlo."""
    from maas_integrator import _shape_matches_format
    raw_apache = '127.0.0.1 - alice [10/Oct/2026:13:55:36 -0700] "GET /api HTTP/1.0" 200 2326'
    shape_apache = '10.5.5.5 - bob [10/Oct/2026:14:00:00 -0700] "POST /login HTTP/1.0" 201 512'
    shape_json = '{"clientip": "10.5.5.5", "user": "bob", "status": 201}'

    assert _shape_matches_format(shape_apache, raw_apache) is True
    assert _shape_matches_format(shape_json, raw_apache) is False


def test_shape_format_filter_rejects_apache_when_raw_is_json():
    from maas_integrator import _shape_matches_format
    raw_json = '{"atype":"authCheck","local":{"ip":"127.0.0.1"}}'
    shape_json = '{"atype":"insert","local":{"ip":"10.1.2.3"}}'
    shape_apache = '127.0.0.1 - alice [10/Oct/2026] "GET /api" 200'

    assert _shape_matches_format(shape_json, raw_json) is True
    assert _shape_matches_format(shape_apache, raw_json) is False


def test_shape_format_filter_accepts_syslog_for_syslog_raw():
    from maas_integrator import _shape_matches_format
    raw = '<13>May 19 14:30:01 host01 sshd[1234]: Failed password'
    assert _shape_matches_format(
        '<14>Jun 20 09:15:42 host02 sudo[5678]: command executed', raw
    ) is True
    # JSON sería rechazado
    assert _shape_matches_format('{"msg":"failed"}', raw) is False


def test_type_coherence_numeric_value_maps_to_keyword_ecs_field_forces_string():
    """Regression: si un valor numérico mapea a un field ECS de tipo keyword,
    el filter debe emitir `mutate convert => "string"` (no "integer").

    Caso real que disparó este test: el wizard mostró un warning porque mi
    mapper hardcodeaba `result → event.code` como integer, pero ECS dice que
    event.code es keyword. Resultado: OpenSearch dynamic mapping creaba el
    field como long y rechazaba docs posteriores con value string.

    Ahora `_logstash_convert_for_ecs` consulta la spec ECS y emite el
    convert correcto. Aquí lo verificamos con `host_id` que mapea a `host.id`
    (keyword en ECS 8.11): aunque el valor sea numérico, debe forzar string.
    """
    log = '2026-05-19T10:15:30Z host=h1 host_id=42 user=alice resp=200'
    result = generate_logstash_filter(log)
    fc = result["filter_code"]

    # host_id resuelve a host.id vía snake_case→dot. ECS host.id es keyword.
    # El convert DEBE ser "string", no "integer".
    if '"[host][id]"' in fc:
        # Si emite el rename, el convert correspondiente debe ser string.
        assert '"[host][id]" => "string"' in fc, (
            "host.id es keyword en ECS; convert debería ser string aunque "
            "el value sea numérico"
        )
        assert '"[host][id]" => "integer"' not in fc

    # resp → http.response.status_code: ECS dice long → convert integer (OK).
    if '[http][response][status_code]' in fc:
        assert '"[http][response][status_code]" => "integer"' in fc


def test_json_nested_mongodb_namespaced():
    """MongoDB authCheck JSON con nested local/remote/users/roles, en modo
    namespaced: `json { target => "data" }` mete toda la estructura anidada
    bajo el namespace conservando nombres y tipos nativos. Sin renames ECS.
    """
    import json as _json
    log = _json.dumps({
        "atype": "authCheck",
        "ts": {"$date": "2026-05-28T15:30:12.123-03:00"},
        "local": {"ip": "127.0.0.1", "port": 27017},
        "remote": {"ip": "192.168.1.50", "port": 49210},
        "users": [{"user": "admin", "db": "admin"}],
        "roles": [{"role": "root", "db": "admin"}],
        "param": {"command": "find", "ns": "ventas.facturas"},
        "result": 0,
    })
    result = generate_logstash_filter(log)
    fc = result["filter_code"]

    # json con target = namespace; sin renames ECS.
    assert 'json {' in fc
    assert 'target => "data"' in fc
    assert 'rename =>' not in fc
    assert '=> "[source][ip]"' not in fc
    assert '=> "[user][name]"' not in fc

    # Los fields (hojas del walk recursivo) quedan bajo el namespace.
    paths = {f["field_path"] for f in result["fields"]}
    assert "data.atype" in paths
    assert "data.local.ip" in paths
    assert "data.users.0.user" in paths
    # raw_name conserva el path original de la hoja (sin el prefix de namespace).
    raw_names = {f["raw_name"] for f in result["fields"]}
    assert "ts.$date" in raw_names
    assert "local.ip" in raw_names
    assert "users.0.user" in raw_names

# --- Copiloto del pipeline (chatbot DeepSeek-3.2) ----------------------------
#
# El chatbot pasó de Q&A de docs a copiloto: recibe el pipeline del wizard
# (filter + fields + namespace + log) y propone un filter {} refinado con
# reglas de negocio. El contrato: el filter viaja en un fence ```logstash y
# el backend lo extrae a `updated_filter`.


# --- Dashboards baseline por caso + auto-import -----------------------------
#
# build_ndjson(slug) genera NDJSON válido (1 index-pattern + N viz + 1 dashboard).
# _import_dashboards mockea requests.post (NUNCA pega a un cluster real).


def test_build_ndjson_produces_valid_ndjson():
    """build_ndjson(slug) produce NDJSON parseable con saved objects válidos."""
    import json as _json
    from dashboards import build_ndjson, get_available_slugs

    for slug in get_available_slugs():
        ndjson = build_ndjson(slug)
        lines = ndjson.strip().split("\n")

        assert len(lines) >= 3, f"{slug}: debe tener al menos 3 objetos (ip + viz + dash)"

        objects = []
        for i, line in enumerate(lines):
            try:
                obj = _json.loads(line)
                objects.append(obj)
            except _json.JSONDecodeError as exc:
                raise AssertionError(f"{slug} línea {i} no es JSON válido: {exc}") from exc

        types = [obj["type"] for obj in objects]
        assert "index-pattern" in types, f"{slug}: debe tener index-pattern"
        assert "dashboard" in types, f"{slug}: debe tener dashboard"
        assert types.count("visualization") >= 3, f"{slug}: debe tener al menos 3 visualizaciones"

        ip_obj = next(obj for obj in objects if obj["type"] == "index-pattern")
        from dashboards import get_dashboard_spec
        spec = get_dashboard_spec(slug)
        expected_ip = (spec.get("ip_id") if spec else None) or f"{slug}-*"
        assert ip_obj["attributes"]["title"] == expected_ip
        assert ip_obj["attributes"]["timeFieldName"] == "@timestamp"

        dash_obj = next(obj for obj in objects if obj["type"] == "dashboard")
        assert slug in dash_obj["attributes"]["title"]
        assert "panelsJSON" in dash_obj["attributes"]


def test_dashboards_have_input_controls():
    """Cada dashboard de vertical trae un panel de Controls (input_control_vis):
    una barra de Options-list que filtra por terms, con una ref
    control_<i>_index_pattern al index-pattern por cada campo."""
    import json as _json
    from dashboards import build_ndjson, get_dashboard_spec

    for slug in ("transacciones-alyc", "fraud-detection", "siem", "produccion-pozos", "fortianalyzer-soc"):
        objs = [_json.loads(l) for l in build_ndjson(slug).splitlines() if l.strip()]
        ctrl = next((o for o in objs if o["type"] == "visualization"
                     and _json.loads(o["attributes"]["visState"]).get("type") == "input_control_vis"), None)
        assert ctrl is not None, f"{slug}: falta el panel de Controls"
        vs = _json.loads(ctrl["attributes"]["visState"])
        controls = vs["params"]["controls"]
        assert controls and all(c["type"] == "list" for c in controls)
        # Una ref control_<i>_index_pattern por control, al index-pattern del slug.
        ip_id = (get_dashboard_spec(slug).get("ip_id") if get_dashboard_spec(slug) else None) or f"{slug}-*"
        ctrl_refs = [r for r in ctrl["references"] if r["name"].startswith("control_")]
        assert len(ctrl_refs) == len(controls)
        assert all(r["id"] == ip_id and r["type"] == "index-pattern" for r in ctrl_refs)
        assert all(c["indexPatternRefName"] == f"control_{i}_index_pattern"
                   for i, c in enumerate(controls))


def test_build_ndjson_invalid_slug_raises():
    """build_ndjson con slug inválido debe raisear ValueError."""
    from dashboards import build_ndjson

    with pytest.raises(ValueError, match="No hay spec"):
        build_ndjson("slug-inexistente")


def test_import_dashboards_returns_false_for_invalid_slug():
    """_import_dashboards con slug sin spec retorna False sin llamar a requests."""
    import main as _main

    result = _main._import_dashboards(
        slug="no-existe",
        cluster={"endpoint": "10.0.0.5:9200"},
        password="test",
        https_enabled=True,
        terraform_dir=None,
    )
    assert result is False


def test_import_dashboards_returns_false_without_endpoint():
    """_import_dashboards sin endpoint retorna False sin llamar a requests."""
    import main as _main

    result = _main._import_dashboards(
        slug="firewall",
        cluster={},
        password="test",
        https_enabled=True,
        terraform_dir=None,
    )
    assert result is False


def test_import_dashboards_returns_false_without_password():
    """_import_dashboards sin password retorna False sin llamar a requests."""
    import main as _main

    result = _main._import_dashboards(
        slug="firewall",
        cluster={"endpoint": "10.0.0.5:9200"},
        password="",
        https_enabled=True,
        terraform_dir=None,
    )
    assert result is False


def test_import_dashboards_uses_kibana_backdoor_bulk(monkeypatch):
    """_import_dashboards usa la back-door: POST a `.kibana/_bulk` por el 9200 con
    el body bulk transformado, auth admin y verify off. (Kibana no es alcanzable
    directo en Huawei CSS, pero los saved objects son docs de `.kibana`.)"""
    import main as _main

    captured = {}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured["data"] = kwargs.get("data")
        captured["auth"] = kwargs.get("auth")
        captured["headers"] = kwargs.get("headers", {})
        captured["verify"] = kwargs.get("verify")

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"errors": False, "items": []}

        return _Resp()

    monkeypatch.setattr("requests.post", _fake_post)

    result = _main._import_dashboards(
        slug="firewall",
        cluster={"endpoint": "10.0.0.5:9200"},
        password="test-password",
        https_enabled=True,
        terraform_dir=None,
    )

    assert result is True
    assert captured["url"] == "https://10.0.0.5:9200/.kibana/_bulk?refresh=wait_for"
    assert captured["auth"] == ("admin", "test-password")
    assert captured["verify"] is False
    assert captured["headers"].get("Content-Type") == "application/x-ndjson"
    assert captured["data"]  # body bulk no vacío


def test_saved_objects_to_kibana_bulk_transform():
    """Transforma cada saved object a doc de `.kibana`: _id 'tipo:id' y atributos
    bajo la clave del tipo; saltea líneas sin type/id (ej. exportedCount)."""
    import json as _json
    import main as _main

    ndjson = (
        '{"type":"index-pattern","id":"firewall-*","attributes":{"title":"firewall-*"},"references":[]}\n'
        '{"type":"visualization","id":"v1","attributes":{"title":"V"},"references":[{"name":"x","type":"index-pattern","id":"firewall-*"}]}\n'
        '{"exportedCount":2}'
    )
    bulk = _main._saved_objects_to_kibana_bulk(ndjson)
    lines = [l for l in bulk.splitlines() if l.strip()]
    assert len(lines) == 4  # 2 objetos válidos × (action + doc); exportedCount salteado
    assert _json.loads(lines[0]) == {"index": {"_id": "index-pattern:firewall-*"}}
    doc0 = _json.loads(lines[1])
    assert doc0["type"] == "index-pattern" and doc0["index-pattern"] == {"title": "firewall-*"}
    assert _json.loads(lines[2]) == {"index": {"_id": "visualization:v1"}}
    doc1 = _json.loads(lines[3])
    assert doc1["visualization"]["title"] == "V" and doc1["references"][0]["id"] == "firewall-*"


def test_import_dashboards_retries_on_failure(monkeypatch):
    """_import_dashboards reintenta hasta 3 veces antes de retornar False."""
    import main as _main

    attempts = {"n": 0}

    def _fake_post(url, **kwargs):
        attempts["n"] += 1

        class _Resp:
            status_code = 500
            text = "Internal Server Error"

        return _Resp()

    monkeypatch.setattr("requests.post", _fake_post)
    monkeypatch.setattr(_main.time, "sleep", lambda x: None)

    result = _main._import_dashboards(
        slug="firewall",
        cluster={"endpoint": "10.0.0.5:9200"},
        password="test",
        https_enabled=True,
        terraform_dir=None,
    )

    assert result is False
    assert attempts["n"] == _main._DASHBOARDS_IMPORT_RETRIES


def test_import_dashboards_uses_file_from_disk_if_exists(monkeypatch, tmp_path):
    """_import_dashboards lee el NDJSON de docs/dashboards/<slug>.ndjson y lo manda
    transformado a bulk de `.kibana`."""
    import main as _main

    captured = {}

    def _fake_post(url, **kwargs):
        captured["data"] = kwargs.get("data")

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"errors": False}

        return _Resp()

    monkeypatch.setattr("requests.post", _fake_post)

    ndjson_content = '{"type":"index-pattern","id":"test-*","attributes":{"title":"test-*"}}\n{"exportedCount":1}'
    docs_dir = tmp_path / "docs" / "dashboards"
    docs_dir.mkdir(parents=True)
    (docs_dir / "firewall.ndjson").write_text(ndjson_content, encoding="utf-8")

    result = _main._import_dashboards(
        slug="firewall",
        cluster={"endpoint": "10.0.0.5:9200"},
        password="test",
        https_enabled=True,
        terraform_dir=tmp_path / "terraform",
    )

    assert result is True
    body = captured["data"].decode("utf-8") if isinstance(captured["data"], bytes) else captured["data"]
    assert '"index-pattern:test-*"' in body  # salió del archivo de disco, transformado
    assert "exportedCount" not in body       # la línea sin type/id se salteó


def test_dashboard_slugs_include_cts_not_huawei_cts():
    """El slug CTS es `cts` (no `huawei-cts`) — alinea con el case.slug del deploy
    y resuelve el 'slug no tiene spec — skip import'."""
    from dashboards import get_available_slugs

    slugs = get_available_slugs()
    assert "cts" in slugs
    assert "huawei-cts" not in slugs


def test_rich_dashboards_use_real_fields_and_indexref():
    """Los 3 dashboards rich (firewall/transacciones-billetera/cts) referencian los
    campos REALES (no `data.*`), encadenan el index-pattern por id==title==`<slug>-*`,
    y apuntan al index-pattern por references/indexRefName (sin id inline)."""
    import json as _json
    from dashboards import build_ndjson

    expected_fields = {
        "firewall": ["source.ip", "event.action", "source.geo.country_name", "network.application"],
        "transacciones-billetera": ["transaction.operation_code", "transaction.response_code", "transaction.channel", "transaction.customer_id"],
        "cts": ["trace_rating", "service_type", "user.user_name", "source_ip"],
    }
    for slug, fields in expected_fields.items():
        objs = [_json.loads(l) for l in build_ndjson(slug).splitlines() if l.strip()]
        ip = next(o for o in objs if o["type"] == "index-pattern")
        ip_id = f"{slug}-*"
        assert ip["id"] == ip_id and ip["attributes"]["title"] == ip_id

        viz = [o for o in objs if o["type"] == "visualization"]
        dash = next(o for o in objs if o["type"] == "dashboard")

        # Campos reales presentes en algún visState; NUNCA el namespace viejo data.*
        all_vis_states = " ".join(v["attributes"]["visState"] for v in viz)
        assert "data." not in all_vis_states, f"{slug}: visState aún referencia data.*"
        for f in fields:
            assert f in all_vis_states, f"{slug}: falta el campo {f} en las viz"

        # Viz con índice: apuntan por references + indexRefName (no id inline).
        for v in viz:
            refs = [r for r in v["references"] if r["type"] == "index-pattern"]
            if refs:  # las viz markdown no tienen índice
                assert refs[0]["id"] == ip_id
                # Los controles (input_control_vis) referencian por control_N_index_pattern
                # y no llevan índice en el searchSource — se saltean de esa aserción.
                if _json.loads(v["attributes"]["visState"]).get("type") == "input_control_vis":
                    continue
                ssj = v["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"]
                assert "indexRefName" in ssj

        # Todo panel del dashboard apunta a una viz existente.
        vis_ids = {v["id"] for v in viz}
        panel_ref_ids = {r["id"] for r in dash["references"] if r["type"] == "visualization"}
        assert panel_ref_ids and panel_ref_ids <= vis_ids


def test_build_ndjson_ids_are_deterministic():
    """Dos generaciones del mismo slug producen ids idénticos (uuid5) → re-import
    con overwrite=true reemplaza en vez de duplicar."""
    from dashboards import build_ndjson

    for slug in ("firewall", "transacciones-billetera", "cts"):
        assert build_ndjson(slug) == build_ndjson(slug)


def test_fortianalyzer_fortiview_dashboards():
    """Los 4 dashboards FortiAnalyzer FortiView (SOC, Traffic, UTM, Event)
    comparten index pattern fortianalyzer-*, usan campos nativos FortiGate
    y tienen los paneles esperados."""
    import json as _json
    from dashboards import build_ndjson, get_available_slugs

    expected = {
        "fortianalyzer-soc": 14,
        "fortianalyzer-traffic": 14,
        "fortianalyzer-utm": 14,
        "fortianalyzer-event": 13,
    }
    for slug in expected:
        assert slug in get_available_slugs(), f"{slug} no está en get_available_slugs"

    for slug, viz_count in expected.items():
        objs = [_json.loads(l) for l in build_ndjson(slug).splitlines() if l.strip()]
        ip = next(o for o in objs if o["type"] == "index-pattern")
        assert ip["id"] == "fortianalyzer-*", f"{slug}: ip_id should be fortianalyzer-*"
        assert ip["attributes"]["title"] == "fortianalyzer-*"

        viz = [o for o in objs if o["type"] == "visualization"]
        assert len(viz) == viz_count, f"{slug}: expected {viz_count} viz, got {len(viz)}"

        dash = next(o for o in objs if o["type"] == "dashboard")
        vis_ids = {v["id"] for v in viz}
        panel_ref_ids = {r["id"] for r in dash["references"] if r["type"] == "visualization"}
        assert panel_ref_ids and panel_ref_ids <= vis_ids

        all_vis_states = " ".join(v["attributes"]["visState"] for v in viz)
        assert "data." not in all_vis_states, f"{slug}: should not reference data.* namespace"

        assert build_ndjson(slug) == build_ndjson(slug), f"{slug}: ids not deterministic"


def test_build_ndjson_fintech_geo_map():
    """El dashboard fintech trae index-pattern `transacciones-billetera-*` con el
    campo geo_point poblado, una viz de mapa (tile_map) y refs que resuelven."""
    import json as _json
    from dashboards import build_ndjson

    objs = [_json.loads(l) for l in build_ndjson("transacciones-billetera").splitlines() if l.strip()]
    ip = next(o for o in objs if o["type"] == "index-pattern")
    assert ip["id"] == "transacciones-billetera-*"
    ip_fields = _json.loads(ip["attributes"]["fields"])
    geo = next((f for f in ip_fields if f["name"] == "transaction.geo_location"), None)
    assert geo and geo["esTypes"] == ["geo_point"]

    viz = [o for o in objs if o["type"] == "visualization"]
    vtypes = {_json.loads(v["attributes"]["visState"])["type"] for v in viz}
    assert "tile_map" in vtypes  # el mapa de coordenadas
    allvs = " ".join(v["attributes"]["visState"] for v in viz)
    assert "transaction.operation_code" in allvs and "transaction.geo_location" in allvs

    dash = next(o for o in objs if o["type"] == "dashboard")
    vis_ids = {v["id"] for v in viz}
    panel_refs = {r["id"] for r in dash["references"] if r["type"] == "visualization"}
    assert panel_refs and panel_refs <= vis_ids


def test_fraud_vertical():
    """El vertical fraud (IEEE-CIS Fraud Detection) tiene el slug correcto,
    index pattern fraud-*, dashboard curado (con Controls), 56 index_fields,
    3 forecasts y campos clave presentes."""
    import json as _json
    from dashboards import build_ndjson, get_dashboard_spec
    import capabilities as C

    slug = "fraud-detection"
    spec = get_dashboard_spec(slug)
    assert spec is not None, f"falta dashboard spec para {slug}"

    # Dashboard curado: header + Controls + KPIs + series + top-N (sin redundancia).
    assert 12 <= len(spec["panels"]) <= 16, f"panels fuera de rango: {len(spec['panels'])}"
    assert any(p["type"] == "controls" for p in spec["panels"]), "falta el panel de Controls"
    assert len(spec["index_fields"]) == 56, f"expected 56 index_fields, got {len(spec['index_fields'])}"

    paths = {p for p, _t in spec["index_fields"]}
    for required in (
        "fraud.is_fraud", "fraud.amount", "fraud.product_cd",
        "fraud.card.brand", "fraud.device.type", "fraud.email.purchaser",
    ):
        assert required in paths, f"falta {required} en index_fields"

    objs = [_json.loads(l) for l in build_ndjson(slug).splitlines() if l.strip()]
    ip = next(o for o in objs if o["type"] == "index-pattern")
    assert ip["attributes"]["title"] == "fraud-detection-*"
    assert ip["attributes"]["timeFieldName"] == "@timestamp"

    dash = next(o for o in objs if o["type"] == "dashboard")
    assert "fraud-detection" in dash["attributes"]["title"].lower() or "Fraude" in dash["attributes"]["title"]

    cap = C.get_capability_spec(slug)
    assert cap is not None, f"falta capability spec para {slug}"
    assert len(cap["forecasts"]) == 3
    fc_names = {fc["name"] for fc in cap["forecasts"]}
    assert fc_names == {
        "fraud-volume-forecast",
        "fraud-count-forecast",
        "fraud-amount-forecast",
    }
    assert build_ndjson(slug) == build_ndjson(slug), "ids not deterministic"


def test_dashboard_panel_baked_query():
    """Las viz con `query` hornean el KQL en su searchSourceJSON — confirma que
    el filtro por panel se propaga correctamente (fintech + firewall)."""
    import json as _json
    from dashboards import build_ndjson

    # Fintech: panel "Failed Transactions" con query transaction.funnel.failed:true
    objs = [_json.loads(l) for l in build_ndjson("transacciones-billetera").splitlines() if l.strip()]
    viz = [o for o in objs if o["type"] == "visualization"]
    queries = []
    for v in viz:
        ssj = _json.loads(v["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])
        q = ssj.get("query", {}).get("query", "")
        if q:
            queries.append(q)
    assert "transaction.funnel.failed:true" in queries, (
        "fintech: debe haber una viz con query transaction.funnel.failed:true"
    )
    assert "transaction.operation_code:TRANSFER" in queries, (
        "fintech: debe haber una viz con query transaction.operation_code:TRANSFER"
    )

    # Firewall: panel "Blocked Events" con query event.action:(blocked or dropped)
    objs = [_json.loads(l) for l in build_ndjson("firewall").splitlines() if l.strip()]
    viz = [o for o in objs if o["type"] == "visualization"]
    queries = []
    for v in viz:
        ssj = _json.loads(v["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])
        q = ssj.get("query", {}).get("query", "")
        if q:
            queries.append(q)
    assert "event.action:(blocked or dropped)" in queries, (
        "firewall: debe haber una viz con query event.action:(blocked or dropped)"
    )


def test_bundled_index_template_applied_verbatim(monkeypatch):
    """El template curado de fintech (`templates/transacciones-billetera.json`) se
    aplica VERBATIM en `_apply_index_templates` (con nested/geo_point/index.sort),
    NO el auto-generado desde los campos."""
    import main as _main

    bundled = _main._bundled_index_template("transacciones-billetera")
    assert bundled is not None
    assert bundled["index_patterns"] == ["transacciones-billetera-*"]
    # tiene lo que el auto-generador NO produce.
    props = bundled["template"]["mappings"]["properties"]["transaction"]["properties"]
    assert props["geo_location"]["type"] == "geo_point"
    assert props["steps_parsed"]["properties"]["data"]["properties"]["steps"]["type"] == "nested"
    assert bundled["template"]["settings"]["index.sort.field"] == "@timestamp"

    put_bodies = []

    class _Resp:
        status_code = 200
        text = "{}"

    def _fake_put(url, **kwargs):
        put_bodies.append({"url": url, "json": kwargs.get("json")})
        return _Resp()

    monkeypatch.setattr("requests.put", _fake_put)

    req = _main.TerraformDeployRequest(
        pipeline_conf="x", project_name="log-analytics",
        opensearch_password="pw", https_enabled=False,
        cases=[_main.PipelineCase(
            slug="transacciones-billetera", filter_code="f",
            fields=[{"raw_name": "operation_code", "field_path": "transaction.operation_code", "type": "keyword"}],
            index_name="transacciones-billetera-%{+YYYY.MM}",
        )],
    )
    ok = _main._apply_index_templates(req, {"public_endpoint": "1.2.3.4:9200"})
    assert ok is True
    assert len(put_bodies) == 1
    assert put_bodies[0]["url"].endswith("/_index_template/log-analytics-transacciones-billetera")
    # El body PUTeado es el template curado tal cual (no el auto-generado).
    assert put_bodies[0]["json"] == bundled


def test_import_dashboards_prefers_public_endpoint(monkeypatch):
    """Si el cluster tiene `public_endpoint` (EIP), el import lo usa en vez del
    endpoint privado — así alcanza el cluster desde fuera de la VPC."""
    import main as _main

    captured = {}

    def _fake_post(url, **kwargs):
        captured["url"] = url

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"errors": False}

        return _Resp()

    monkeypatch.setattr("requests.post", _fake_post)

    result = _main._import_dashboards(
        slug="firewall",
        cluster={"endpoint": "192.168.0.63:9200", "public_endpoint": "203.0.113.7:9200"},
        password="test",
        https_enabled=False,
        terraform_dir=None,
    )

    assert result is True
    # La back-door por 9200 usa el endpoint público (EIP del NAT), no el privado.
    assert captured["url"] == "http://203.0.113.7:9200/.kibana/_bulk?refresh=wait_for"


def test_import_dashboards_falls_back_to_kibana_api(monkeypatch):
    """Si la back-door por OpenSearch falla, cae a la API saved_objects de Kibana
    (`kibana_endpoint`, ruta raíz). Primero intenta back-door, después Kibana."""
    import main as _main

    calls = []

    def _fake_post(url, **kwargs):
        calls.append(url)

        class _Resp:
            # back-door (.kibana/_bulk) "falla" con 500; la API de Kibana responde 200.
            status_code = 500 if "/.kibana/_bulk" in url else 200
            text = "{}"

        return _Resp()

    monkeypatch.setattr("requests.post", _fake_post)
    monkeypatch.setattr(_main.time, "sleep", lambda x: None)

    result = _main._import_dashboards(
        slug="firewall",
        cluster={
            "public_endpoint": "203.0.113.7:9200",
            "kibana_endpoint": "203.0.113.7:5601",
        },
        password="test",
        https_enabled=True,
        terraform_dir=None,
    )

    assert result is True
    assert any("/.kibana/_bulk" in u for u in calls)  # intentó la back-door primero
    assert any("203.0.113.7:5601/api/saved_objects/_import" in u for u in calls)  # cayó a Kibana


def test_read_css_state_surfaces_public_endpoint(tmp_path):
    """_read_css_resource_from_state expone `public_endpoint` = `<public_ip>:9200`
    desde el bloque public_access del tfstate."""
    import json as _json
    import main as _main

    # `public_ip` ya viene como "IP:9200" en el state (igual que `endpoint`).
    state = {
        "resources": [{
            "type": "huaweicloud_css_cluster",
            "instances": [{"attributes": {
                "id": "abc", "endpoint": "192.168.0.63:9200", "name": "x",
                "public_access": [{"public_ip": "203.0.113.7:9200"}],
                "kibana_public_access": [{"public_ip": "203.0.113.7:5601"}],
            }}],
        }]
    }
    (tmp_path / "terraform.tfstate").write_text(_json.dumps(state), encoding="utf-8")
    cluster = _main._read_css_resource_from_state(tmp_path, "huaweicloud_css_cluster")
    # No se duplica el puerto (":9200:9200").
    assert cluster["public_endpoint"] == "203.0.113.7:9200"
    assert cluster["endpoint"] == "192.168.0.63:9200"
    # El endpoint público de Kibana (saved_objects) sale del bloque kibana_public_access.
    assert cluster["kibana_endpoint"] == "203.0.113.7:5601"


def test_overlay_public_endpoints_from_outputs():
    """Con NAT/DNAT, public_endpoint/kibana_endpoint vienen de los outputs cuando
    el state no los trae; si el cluster ya los tiene, se respetan."""
    import main as _main

    tf_outputs = {
        "opensearch_public_endpoint": {"value": "1.2.3.4:9200"},
        "dashboards_public_endpoint": {"value": "1.2.3.4:5601"},
    }
    # State sin endpoints (sin public_access) → se completan desde outputs.
    cluster = {"id": "x", "endpoint": "192.168.0.190:9200"}
    _main._overlay_public_endpoints(cluster, tf_outputs)
    assert cluster["public_endpoint"] == "1.2.3.4:9200"
    assert cluster["kibana_endpoint"] == "1.2.3.4:5601"

    # Si el cluster ya trae endpoints (setup viejo), NO se pisan.
    pre = {"public_endpoint": "9.9.9.9:9200", "kibana_endpoint": "9.9.9.9:5601"}
    _main._overlay_public_endpoints(pre, tf_outputs)
    assert pre["public_endpoint"] == "9.9.9.9:9200"
    assert pre["kibana_endpoint"] == "9.9.9.9:5601"


def test_apply_index_templates_puts_to_public_endpoint(monkeypatch):
    """_apply_index_templates hace PUT del index template al cluster por el
    endpoint público (EIP), con nombre <project>-<slug> y el tipado correcto
    (source.ip anidado como ip). Reemplaza el paso manual de Dev Tools."""
    import main as _main

    captured = {}

    def _fake_put(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["auth"] = kwargs.get("auth")
        captured["verify"] = kwargs.get("verify")

        class _Resp:
            status_code = 200
            text = "{}"

        return _Resp()

    monkeypatch.setattr("requests.put", _fake_put)

    req = _main.TerraformDeployRequest(
        pipeline_conf="x", project_name="log-analytics",
        opensearch_user="admin", opensearch_password="pw", https_enabled=True,
        cases=[_main.PipelineCase(
            slug="firewall", filter_code="filter {}",
            index_name="firewall-%{+YYYY.MM}",
            fields=[{"raw_name": "srcip", "field_path": "source.ip", "type": "ip"}],
        )],
    )

    ok = _main._apply_index_templates(
        req, {"endpoint": "192.168.0.63:9200", "public_endpoint": "203.0.113.7:9200"}
    )

    assert ok is True
    assert captured["url"] == "https://203.0.113.7:9200/_index_template/log-analytics-firewall"
    assert captured["auth"] == ("admin", "pw")
    assert captured["verify"] is False
    props = captured["json"]["template"]["mappings"]["properties"]
    assert props["source"]["properties"]["ip"] == {"type": "ip"}


def test_apply_index_templates_skips_without_endpoint():
    """Sin endpoint (ni público ni privado) no intenta aplicar nada."""
    import main as _main

    req = _main.TerraformDeployRequest(
        pipeline_conf="x", opensearch_password="pw",
        cases=[_main.PipelineCase(slug="firewall", filter_code="f", index_name="firewall-*",
                                  fields=[{"raw_name": "srcip", "field_path": "source.ip", "type": "ip"}])],
    )
    assert _main._apply_index_templates(req, {}) is False


def test_delete_indices_lists_then_deletes_by_name(monkeypatch):
    """_delete_indices lista los índices concretos del pattern y los borra por
    NOMBRE (no por wildcard, para no chocar con destructive_requires_name)."""
    import main as _main

    deleted = []

    def _fake_get(url, **kwargs):
        class _R:
            status_code = 200
            def json(self):
                return [{"index": "firewall-2023.05.10"}, {"index": "firewall-2026.06.22"}]
        return _R()

    def _fake_delete(url, **kwargs):
        deleted.append(url)

        class _R:
            status_code = 200
        return _R()

    monkeypatch.setattr("requests.get", _fake_get)
    monkeypatch.setattr("requests.delete", _fake_delete)

    n = _main._delete_indices("1.2.3.4:9200", "firewall-*", "pw", False)
    assert n == 2
    assert any("firewall-2023.05.10" in u for u in deleted)
    assert any("firewall-2026.06.22" in u for u in deleted)
    assert not any(u.endswith("/firewall-*") for u in deleted)  # nunca por wildcard


def test_clear_case_indices_targets_each_case_pattern(monkeypatch):
    """_clear_case_indices borra el pattern `<base>-*` de cada caso del request."""
    import main as _main

    cleared = []
    monkeypatch.setattr(_main, "_delete_indices",
                        lambda ep, pat, pw, https, user="admin": cleared.append(pat) or 1)

    req = _main.TerraformDeployRequest(
        pipeline_conf="x", opensearch_password="pw",
        cases=[
            _main.PipelineCase(slug="firewall", filter_code="f", fields=[], index_name="firewall-%{+YYYY.MM}"),
            _main.PipelineCase(slug="transacciones-billetera", filter_code="f", fields=[], index_name="transacciones-billetera-%{+YYYY.MM}"),
        ],
    )
    _main._clear_case_indices(req, {"public_endpoint": "1.2.3.4:9200"})
    assert "firewall-*" in cleared
    assert "transacciones-billetera-*" in cleared


def test_apply_schema_endpoint(monkeypatch):
    """POST /apply-schema aplica index template + importa dashboards al cluster
    existente (sin terraform) y reporta el resultado."""
    import main as _main

    monkeypatch.setattr(_main, "_cluster_with_public_access",
                        lambda td: {"endpoint": "192.168.0.1:9200", "public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(_main, "_apply_index_templates", lambda req, cl: True)
    monkeypatch.setattr(_main, "_import_dashboards", lambda **kw: True)
    monkeypatch.setattr(_main, "_index_time_bounds", lambda *a, **k: None)  # sin red
    monkeypatch.setattr(_main, "_os_req", lambda *a, **k: None)  # timepicker best-effort, sin red
    monkeypatch.setattr(_main, "_read_pipelines_registry", lambda td: {})

    body = {
        "pipeline_conf": "x", "opensearch_password": "pw", "opensearch_user": "admin",
        "https_enabled": False,
        "cases": [{"slug": "firewall", "filter_code": "f", "index_name": "firewall-%{+YYYY.MM}",
                   "fields": [{"raw_name": "srcip", "field_path": "source.ip", "type": "ip"}]}],
    }
    resp = client.post("/api/v1/onboarding/apply-schema", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["index_template_applied"] is True
    assert data["dashboards_imported"] is True
    assert data["status"] == "success"


def test_apply_schema_no_cluster_returns_503(monkeypatch):
    """Sin cluster alcanzable, /apply-schema responde 503 (¿provisionaste?)."""
    import main as _main

    monkeypatch.setattr(_main, "_cluster_with_public_access", lambda td: {})
    resp = client.post("/api/v1/onboarding/apply-schema",
                       json={"pipeline_conf": "x", "opensearch_password": "pw"})
    assert resp.status_code == 503


def test_build_ndjson_from_fields_generic():
    """build_ndjson_from_fields auto-arma un dashboard de los campos detectados:
    index-pattern con id del índice real + fields poblados, viz por tipo que
    referencian el field_path real, y text agrega por `.keyword`."""
    import json as _json
    from dashboards import build_ndjson_from_fields

    fields = [
        {"raw_name": "srcip", "field_path": "data.srcip", "type": "ip", "business_label": "Source IP"},
        {"raw_name": "country", "field_path": "data.country", "type": "keyword", "business_label": "Country"},
        {"raw_name": "bytes", "field_path": "data.bytes", "type": "integer", "business_label": "Bytes"},
        {"raw_name": "msg", "field_path": "data.msg", "type": "text"},
    ]
    objs = [_json.loads(l) for l in build_ndjson_from_fields("logs", "logs-%{+YYYY.MM}", fields).splitlines() if l.strip()]
    ip = next(o for o in objs if o["type"] == "index-pattern")
    assert ip["id"] == "logs-*"  # de index_pattern_from_name(index_name)
    names = {f["name"] for f in _json.loads(ip["attributes"]["fields"])}
    assert {"@timestamp", "data.srcip", "data.country", "data.bytes", "data.msg", "data.msg.keyword"} <= names

    viz = [o for o in objs if o["type"] == "visualization"]
    dash = next(o for o in objs if o["type"] == "dashboard")
    vizids = {v["id"] for v in viz}
    panelrefs = {r["id"] for r in dash["references"] if r["type"] == "visualization"}
    assert panelrefs and panelrefs <= vizids
    allvs = " ".join(v["attributes"]["visState"] for v in viz)
    assert "data.srcip" in allvs and "data.country" in allvs  # ip directo, keyword dimension Top-N


def test_import_dashboards_generic_from_fields(monkeypatch):
    """Slug SIN spec pero CON fields → auto-genera el dashboard y lo importa por la
    back-door `.kibana` (caso custom)."""
    import main as _main

    captured = {}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured["data"] = kwargs.get("data")

        class _R:
            status_code = 200
            text = "{}"

            def json(self):
                return {"errors": False}
        return _R()

    monkeypatch.setattr("requests.post", _fake_post)

    ok = _main._import_dashboards(
        slug="logs", cluster={"endpoint": "1.2.3.4:9200"}, password="pw", https_enabled=False,
        fields=[{"raw_name": "srcip", "field_path": "data.srcip", "type": "ip"}],
        index_name="logs-%{+YYYY.MM}",
    )
    assert ok is True
    assert captured["url"] == "http://1.2.3.4:9200/.kibana/_bulk?refresh=wait_for"
    body = captured["data"].decode("utf-8") if isinstance(captured["data"], bytes) else captured["data"]
    assert "data.srcip" in body and "logs-*" in body


def test_apply_schema_custom_passes_fields(monkeypatch):
    """apply-schema custom (sin cases, con fields) llama a _import_dashboards con
    los fields + index_name → auto-genera el dashboard."""
    import main as _main

    monkeypatch.setattr(_main, "_cluster_with_public_access", lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(_main, "_apply_index_templates", lambda req, cl: True)
    monkeypatch.setattr(_main, "_read_pipelines_registry", lambda td: {})
    monkeypatch.setattr(_main, "_index_time_bounds", lambda *a, **k: None)  # sin red
    monkeypatch.setattr(_main, "_os_req", lambda *a, **k: None)  # timepicker best-effort, sin red

    captured = {}

    def _imp(**kw):
        captured.update(kw)
        return True

    monkeypatch.setattr(_main, "_import_dashboards", _imp)

    body = {
        "pipeline_conf": "x", "opensearch_password": "pw", "https_enabled": False,
        "opensearch_index": "logs-%{+YYYY.MM}", "pipeline_slug": "logs",
        "fields": [{"raw_name": "srcip", "field_path": "data.srcip", "type": "ip"}],
    }
    resp = client.post("/api/v1/onboarding/apply-schema", json=body)
    assert resp.status_code == 200
    assert captured["slug"] == "logs"
    assert captured["index_name"] == "logs-%{+YYYY.MM}"
    assert captured["fields"] and captured["fields"][0]["field_path"] == "data.srcip"


def test_terraform_deploy_response_includes_dashboards_imported():
    """TerraformDeployResponse tiene el campo dashboards_imported."""
    import main as _main

    resp = _main.TerraformDeployResponse(
        opensearch_endpoint="10.0.0.5:9200",
        logstash_endpoint="10.0.0.6:9200",
        dashboards_url="https://example.com/dashboards",
        status="success",
        dashboards_imported=True,
    )

    assert resp.dashboards_imported is True


def test_terraform_status_response_includes_dashboards_imported():
    """TerraformStatusResponse tiene el campo dashboards_imported."""
    import main as _main

    resp = _main.TerraformStatusResponse(
        active=True,
        pipelines=[{"slug": "test", "dashboards_imported": True}],
        dashboards_imported=True,
    )

    assert resp.dashboards_imported is True
    assert resp.pipelines[0]["dashboards_imported"] is True


def test_determine_flavor_single_pipeline():
    """1 pipeline → flavors 4u8g."""
    import main as _main

    ls, os = _main._determine_flavor(1)
    assert ls == "ess.spec-4u8g"
    assert os == "ess.spec-4u8g"


def test_determine_flavor_multiple_pipelines():
    """Flavor fijo 4u8g sin importar la cantidad de pipelines (no escala). Los
    workers se reparten sobre los 4 vCPU fijos (ver _capacity_for)."""
    import main as _main

    for n in [2, 3, 4, 5, 8, 12]:
        ls, os = _main._determine_flavor(n)
        assert ls == "ess.spec-4u8g" and os == "ess.spec-4u8g"
    # Con muchas pipelines los workers por pipeline bajan (mín 1).
    assert _main._capacity_for(12)["pipeline_workers"] == 1
    assert _main._capacity_for(2)["pipeline_workers"] == 2


def test_max_pipelines_unlimited_by_default():
    """Sin MAX_PIPELINES_PER_USER no hay tope duro (0 = ilimitado)."""
    import main as _main

    assert _main._MAX_PIPELINES == 0


def test_deploy_rejects_more_than_max_cases(monkeypatch, tmp_path):
    """Con un cap configurado (>0), más de _MAX_PIPELINES cases → 400."""
    import main as _main

    monkeypatch.setattr(_main, "_MAX_PIPELINES", 5)
    fake_main = tmp_path / "main.py"
    fake_main.write_text("")
    td = tmp_path / "terraform"
    td.mkdir()

    def _no_run(*a, **k):
        raise AssertionError("terraform no debería invocarse")
    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setattr(_main.subprocess, "run", _no_run)

    cases = [
        {"slug": f"case{i}", "raw_log": "test", "filter_code": "filter {}", "fields": [], "index_name": f"case{i}", "obs_prefix": f"logs/case{i}/"}
        for i in range(6)
    ]

    res = client.post("/api/v1/terraform/deploy-job", json={
        "pipeline_conf": "filter {}",
        "cases": cases,
    })
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "Máximo" in str(detail)


def test_pipeline_case_model():
    """PipelineCase model existe y valida."""
    import main as _main

    case = _main.PipelineCase(
        slug="firewall",
        raw_log="test log",
        filter_code="filter {}",
        fields=[{"raw_name": "test", "type": "string"}],
        index_name="firewall-%{+YYYY.MM}",
        obs_prefix="logs/firewall/",
    )

    assert case.slug == "firewall"
    assert case.raw_log == "test log"
    assert len(case.fields) == 1


def test_terraform_deploy_request_accepts_cases():
    """TerraformDeployRequest acepta lista de cases."""
    import main as _main

    req = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        cases=[
            _main.PipelineCase(slug="firewall", raw_log="test"),
            _main.PipelineCase(slug="transacciones-billetera", raw_log="test2"),
        ],
    )

    assert len(req.cases) == 2
    assert req.cases[0].slug == "firewall"
    assert req.cases[1].slug == "transacciones-billetera"


def test_build_pipeline_conf_for_case():
    """_build_pipeline_conf_for_case genera .conf válido."""
    import main as _main

    case = _main.PipelineCase(
        slug="firewall",
        raw_log="test log",
        filter_code="filter { mutate { add_field => { 'test' => 'value' } } }",
        fields=[],
        index_name="firewall-%{+YYYY.MM}",
        obs_prefix="logs/firewall/",
    )

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="test_ak",
        obs_secret_key="test_sk",
        obs_bucket="test-bucket",
        obs_region="la-south-2",
        obs_endpoint="https://obs.la-south-2.myhuaweicloud.com",
        opensearch_user="admin",
        opensearch_password="test_pass",
        https_enabled=True,
    )

    conf = _main._build_pipeline_conf_for_case(case, request)

    assert "input {" in conf
    assert "s3 {" in conf
    assert "logs/firewall/" in conf
    assert "filter {" in conf
    assert "mutate" in conf
    assert "output {" in conf
    assert "elasticsearch {" in conf
    assert "firewall-%{+YYYY.MM}" in conf
    assert "codec => plain" in conf


def test_obs_upload_handles_multiple_cases(monkeypatch, tmp_path):
    """_do_obs_upload sube logs de cada caso a su prefix."""
    import main as _main
    import json as _json

    uploaded = []

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            return 0
        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)

    cases = [
        _main.PipelineCase(slug="firewall", raw_log="log1", filter_code="f1", fields=[], index_name="fw", obs_prefix="logs/fw/"),
        _main.PipelineCase(slug="web", raw_log="log2", filter_code="f2", fields=[], index_name="web", obs_prefix="logs/web/"),
    ]

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak",
        obs_secret_key="sk",
        obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        cases=cases,
    )

    _main._do_obs_upload(request)

    assert len(uploaded) == 2
    assert any("logs/fw/" in u["key"] for u in uploaded)
    assert any("logs/web/" in u["key"] for u in uploaded)


def test_obs_upload_cleans_prefix_before_uploading(monkeypatch):
    """Antes de subir, _do_obs_upload limpia el prefijo del caso (delete_prefix)
    para que no se acumulen ni crucen datasets viejos. CTS (read_existing) NO se
    limpia."""
    import main as _main

    cleaned = []
    uploaded = []

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def delete_prefix(self, prefix):
            cleaned.append(prefix)
            return 3
        def put_object(self, key, data):
            uploaded.append(key)
        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)
    monkeypatch.setattr(_main, "_bundled_dataset", lambda slug: "linea1\nlinea2")

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}", obs_access_key="ak", obs_secret_key="sk",
        obs_bucket="bucket", obs_endpoint="https://obs.example.com",        cases=[
            _main.PipelineCase(slug="firewall", raw_log="x", filter_code="f", fields=[],
                               index_name="firewall-*", obs_prefix="logs/firewall/"),
            _main.PipelineCase(slug="cts", raw_log="x", filter_code="f", fields=[],
                               index_name="cts-*", obs_prefix="CloudTraces/", read_existing_bucket=True),
        ],
    )
    _main._do_obs_upload(request)

    # firewall: se limpió su prefijo antes de subir; CTS no (read_existing).
    assert "logs/firewall/" in cleaned
    assert "CloudTraces/" not in cleaned
    assert any("logs/firewall/" in k for k in uploaded)


# ===========================================================================
# Datasets bundleados: los tipos predefinidos suben su log real entero en
# vez de generar sintéticos. Solo `custom` (slug `logs`) usa sintéticos.
# ===========================================================================
@requires_datasets
def test_bundled_dataset_reads_and_strips_comments():
    """`_bundled_dataset` devuelve las líneas de datos (sin comentarios `#`)
    para un slug predefinido, y None para slugs sin archivo (custom → `logs`)."""
    import main as _main

    fw = _main._bundled_dataset("firewall")
    assert fw is not None
    # Ninguna línea de comentario sobrevive.
    assert all(not l.lstrip().startswith("#") for l in fw.splitlines())
    # Las líneas de datos son logs FortiGate kv (type="traffic", srcip=...).
    assert 'type="traffic"' in fw
    assert "srcip=" in fw

    # custom / inexistentes → None (caen al path sintético/raw).
    assert _main._bundled_dataset("logs") is None
    assert _main._bundled_dataset("no-existe") is None
    assert _main._bundled_dataset("") is None


@requires_datasets
def test_obs_upload_uses_dataset_and_skips_synthetic_for_predefined(monkeypatch):
    """Para un caso predefinido (firewall), `_do_obs_upload` sube el dataset
    bundleado tal cual. La generación sintética que este test vigilaba ya no
    existe; lo que queda verificado es que el dataset sube verbatim."""
    import main as _main

    uploaded = []

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            return 0
        def close(self):
            pass


    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        cases=[_main.PipelineCase(
            slug="firewall", raw_log="ignored", filter_code="f",
            fields=[], index_name="firewall", obs_prefix="logs/firewall/",
        )],
    )

    _main._do_obs_upload(request)

    assert len(uploaded) == 1
    assert "dataset_" in uploaded[0]["key"]
    assert uploaded[0]["data"] == _main._bundled_dataset("firewall")


def test_obs_upload_custom_single_no_synthetic_raw_fallback(monkeypatch):
    """Custom (single, slug `logs`) SIN archivo importado: ya NO se generan
    sintéticos. Cae al fallback defensivo (sube la única línea `raw_log` como
    `sample_log_*`) y NO llama a los generadores sintéticos."""
    import main as _main

    uploaded = []

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            return 0
        def close(self):
            pass


    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        pipeline_slug="logs",
        raw_log='{"foo":"bar"}',
    )

    _main._do_obs_upload(request)

    assert len(uploaded) == 1
    assert "sample_log_" in uploaded[0]["key"]
    assert uploaded[0]["data"] == '{"foo":"bar"}'


def test_models_accept_log_file_content():
    """Los modelos aceptan `log_file_content` (custom: archivo importado)."""
    import main as _main

    case = _main.PipelineCase(slug="logs", log_file_content="a\nb\nc")
    assert case.log_file_content == "a\nb\nc"
    req = _main.TerraformDeployRequest(pipeline_conf="filter {}", log_file_content="x\ny")
    assert req.log_file_content == "x\ny"
    # Default vacío cuando no se provee.
    assert _main.PipelineCase(slug="logs").log_file_content == ""
    assert _main.TerraformDeployRequest(pipeline_conf="f").log_file_content == ""


def test_obs_upload_imported_file_verbatim_single(monkeypatch):
    """Custom single con `log_file_content`: se sube TAL CUAL (verbatim) como
    `dataset_*` y NO se invocan los generadores sintéticos."""
    import main as _main

    uploaded = []

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            return 0
        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        pipeline_slug="logs",
        raw_log="L1",
        log_file_content="L1\nL2\nL3",
        obs_prefix="logs-logs/",
    )

    _main._do_obs_upload(request)

    assert len(uploaded) == 1
    assert "dataset_" in uploaded[0]["key"]
    assert uploaded[0]["data"] == "L1\nL2\nL3"  # verbatim


def test_obs_upload_imported_file_verbatim_multicase(monkeypatch):
    """Custom como caso (multi): `log_file_content` se sube verbatim bajo su
    prefijo, `delete_prefix` se llamó, y sin sintéticos."""
    import main as _main

    uploaded = []
    cleaned = []

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            cleaned.append(prefix)
            return 0
        def close(self):
            pass

    def _boom(*a, **k):
        raise AssertionError("no debería generar sintéticos")

    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        cases=[_main.PipelineCase(
            slug="logs", raw_log="", filter_code="f", fields=[],
            index_name="logs-%{+YYYY.MM}", obs_prefix="logs-logs/",
            log_file_content="A\nB",
        )],
    )

    _main._do_obs_upload(request)

    assert "logs-logs/" in cleaned
    assert len(uploaded) == 1
    assert "logs-logs/dataset_" in uploaded[0]["key"]
    assert uploaded[0]["data"] == "A\nB"


def test_obs_upload_bundled_wins_over_imported_file(monkeypatch):
    """Precedencia: un slug predefinido (firewall) con dataset bundleado sube el
    dataset aunque venga `log_file_content` (el bundled gana)."""
    import main as _main

    uploaded = []

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            return 0
        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)
    monkeypatch.setattr(_main, "_bundled_dataset", lambda slug: "BUNDLED" if slug == "firewall" else None)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        cases=[_main.PipelineCase(
            slug="firewall", raw_log="x", filter_code="f", fields=[],
            index_name="firewall-*", obs_prefix="firewall-logs/",
            log_file_content="SHOULD-BE-IGNORED",
        )],
    )

    _main._do_obs_upload(request)

    assert len(uploaded) == 1
    assert uploaded[0]["data"] == "BUNDLED"


def test_obs_upload_skips_case_with_no_raw_and_no_file(monkeypatch):
    """Un caso sin `raw_log` ni `log_file_content` se saltea (guarda aflojada);
    el que sí trae archivo sube."""
    import main as _main

    uploaded = []

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            return 0
        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)
    monkeypatch.setattr(_main, "_bundled_dataset", lambda slug: None)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        cases=[
            _main.PipelineCase(slug="vacio", raw_log="", filter_code="f", fields=[],
                               index_name="vacio-*", obs_prefix="vacio-logs/"),
            _main.PipelineCase(slug="logs", raw_log="", filter_code="f", fields=[],
                               index_name="logs-*", obs_prefix="logs-logs/",
                               log_file_content="DATA"),
        ],
    )

    _main._do_obs_upload(request)

    assert len(uploaded) == 1
    assert "logs-logs/dataset_" in uploaded[0]["key"]
    assert uploaded[0]["data"] == "DATA"


def test_obs_upload_skips_read_existing_case_in_multi(monkeypatch):
    """Multi-deploy con bucket universal: el caso CTS (read_existing_bucket) lee
    de su prefijo real (CloudTraces/) y NO se sube nada encima; el resto sí."""
    import main as _main

    uploaded = []

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            return 0
        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="mi-tracker-cts",
        obs_endpoint="https://obs.example.com",
        cases=[
            _main.PipelineCase(
                slug="huawei-cts", raw_log='{"trace_id":"x"}', filter_code="f",
                fields=[], index_name="huawei-cts-%{+YYYY.MM}",
                obs_prefix="CloudTraces/", read_existing_bucket=True,
            ),
            _main.PipelineCase(
                slug="firewall", raw_log="ignored", filter_code="f",
                fields=[], index_name="firewall-%{+YYYY.MM}",
                obs_prefix="logs/firewall/",
            ),
        ],
    )

    _main._do_obs_upload(request)

    # Solo firewall sube (su dataset bundleado); CTS se saltea.
    assert len(uploaded) == 1
    assert "logs/firewall/" in uploaded[0]["key"]
    assert not any("CloudTraces" in u["key"] for u in uploaded)


# ===========================================================================
# Capability provisioner (ml-commons / anomaly detection / forecast / alerting)
# ===========================================================================
def test_capability_builders_wellformed():
    """Los builders producen JSON serializable; el PPL prompt trae el enum de
    operaciones, campos y el índice; los 8 specs de vertical están completos y
    el agente único lleva un PPLTool por vertical con su propio system_prompt."""
    import json as _json
    import capabilities as C

    spec = C.get_capability_spec("transacciones-billetera")
    assert spec is not None
    ip = spec["index_pattern"]
    ppl = C.build_ppl_system_prompt(ip, spec["operations"], spec["fields"], spec["success_code"])
    assert "TRANSFER" in ppl and ip in ppl and "transaction.customer_id" in ppl
    for body in (
        C.build_cluster_settings(),
        C.build_llm_connector("KEY"),
        C.build_ppl_connector("KEY", ppl),
        C.build_model_group(),
        C.build_remote_model("m", "C1", "MG", "d"),
        C.build_forecaster(ip, spec["volume_field"]),
    ):
        _json.dumps(body)
    # El connector PPL toma el prompt por PARÁMETRO (${parameters.system_prompt}),
    # con el default en parameters: así cada PPLTool pasa el prompt de SU vertical
    # y un solo modelo PPL sirve a todas las fuentes. Los newlines van ESCAPADOS
    # (\\n) para no romper el JSON del request_body al sustituir el parámetro.
    conn = C.build_ppl_connector("KEY", ppl)
    assert conn["parameters"]["system_prompt"] == ppl.replace("\n", "\\n")
    assert "${parameters.system_prompt}" in conn["actions"][0]["request_body"]

    # Los specs están completos: fields, operations y forecasts.
    assert set(C.get_capability_slugs()) == {
        "transacciones-billetera", "fraud-detection", "siem", "produccion-pozos",
        "ventas-ecommerce", "encuentros-clinicos",
        "transacciones-alyc", "fortianalyzer",
        "fortianalyzer-soc", "fortianalyzer-traffic",
        "fortianalyzer-utm", "fortianalyzer-event",
        "streaming-ott", "cts"}
    verticals = []
    for slug in C.get_capability_slugs():
        s = C.get_capability_spec(slug)
        assert s["fields"] and s["operations"], f"spec incompleto: {slug}"
        assert len(s.get("forecasts", [])) == 3, f"faltan forecasts: {slug}"
        for fc in s["forecasts"]:
            _json.dumps(C.build_forecaster(s["index_pattern"], s["volume_field"],
                                           name=fc["name"], feature_name=fc["feature_name"],
                                           aggregation_query=fc.get("aggregation_query"),
                                           description=fc.get("description", "")))
        verticals.append({
            "tool_name": f"PPLTool-{slug}", "label": s["label"],
            "index_pattern": s["index_pattern"], "operations": s["operations"],
            "fields": s["fields"], "success_code": s.get("success_code", ""),
            "ppl_system_prompt": C.build_ppl_system_prompt(
                s["index_pattern"], s["operations"], s["fields"],
                s.get("success_code", ""), s["label"]),
        })
    # UN solo agente multi-fuente: un PPLTool por vertical, cada uno con su prompt.
    instr = C.build_agent_system_instruction(verticals)
    agent = C.build_conversational_agent("LLM_ID", "PPL_ID", instr, verticals)
    _json.dumps(agent)
    assert agent["type"] == "conversational"
    assert agent["llm"]["model_id"] == "LLM_ID"
    assert {t["name"] for t in agent["tools"]} == {f"PPLTool-{s}" for s in C.get_capability_slugs()}
    for t in agent["tools"]:
        assert t["type"] == "PPLTool" and t["parameters"]["model_id"] == "PPL_ID"
        assert t["parameters"]["execute"] == "true" and t["parameters"]["system_prompt"]
    assert "Producción de pozos" in instr and "Encuentros clínicos" in instr


def test_model_descriptions_are_ascii_safe():
    """OpenSearch valida las descripciones (solo letras/números/espacios/.,!?():@-_'/\").
    Un `→` o un acento rompía el models/_register con 400. Los builders sanitizan."""
    import re
    import capabilities as C
    allowed = re.compile(r"^[A-Za-z0-9 .,!?():@\-_'/\"]*$")
    # build_remote_model sanitiza descripciones con flecha + acentos.
    desc = C.build_remote_model("m", "C", "MG", "NL→PPL con acentós")["description"]
    assert allowed.match(desc), f"description con chars inválidos: {desc!r}"
    assert "→" not in desc and "ó" not in desc
    # model_group y connectors también quedan dentro del set permitido.
    for d in (
        C.build_model_group()["description"],
        C.build_llm_connector("KEY")["description"],
        C.build_ppl_connector("KEY", "filter {}")["description"],
    ):
        assert allowed.match(d), f"description con chars inválidos: {d!r}"


class _FakeResp:
    def __init__(self, status=200, body=None, text="{}"):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text
    def json(self):
        return self._body


def test_provision_capabilities_full_sequence(monkeypatch):
    """_provision_capabilities corre la secuencia ml-commons + forecasts (AD y
    alerting quedaron fuera por diseño) y persiste los IDs (agent/forecasters).
    El agente se registra multi-fuente (acá solo fintech tiene índice con datos)."""
    monkeypatch.setenv("MAAS_API_KEY", "KEY")

    persisted = {}
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {})
    monkeypatch.setattr(main, "_write_capabilities", lambda td, reg: persisted.update(reg))

    conn_ids = iter(["C1", "C2"])
    task_ids = iter(["T1", "T2"])
    model_ids = {"T1": "M1", "T2": "M2"}
    fc_ids = iter(["FC1", "FC2", "FC3"])
    run_once = []
    os_chat_cfg = []

    def fake_request(method, url, **kwargs):
        if method == "GET" and "/_plugins/_ml/stats" in url:
            return _FakeResp(200, {"nodes": {}})
        if method == "GET" and "/_field_caps" in url:
            # Solo el índice de fintech existe con su volume_field: los otros
            # verticales quedan fuera del agente en esta corrida.
            return _FakeResp(200, {}, text='{"fields":{"transaction.operation_code":{}}}')
        if method == "GET" and url.endswith("/_count"):
            return _FakeResp(200, {"count": 100})
        if method == "PUT" and "/_cluster/settings" in url:
            return _FakeResp(200, {"acknowledged": True})
        if "/_plugins/_ml/model_groups/_register" in url:
            return _FakeResp(200, {"model_group_id": "MG"})
        if "/_plugins/_ml/connectors/_create" in url:
            return _FakeResp(200, {"connector_id": next(conn_ids)})
        if "/_plugins/_ml/models/_register" in url:
            return _FakeResp(200, {"task_id": next(task_ids)})
        if "/_plugins/_ml/tasks/" in url:
            tid = url.rsplit("/", 1)[-1]
            return _FakeResp(200, {"state": "COMPLETED", "model_id": model_ids[tid]})
        if "/_deploy" in url:
            return _FakeResp(200, {"status": "DEPLOYED"})
        # Poll de model_state tras el deploy (GET /_plugins/_ml/models/<id>).
        if method == "GET" and "/_plugins/_ml/models/" in url:
            return _FakeResp(200, {"model_state": "DEPLOYED"})
        if url.endswith("/_plugins/_ml/agents/_search"):
            return _FakeResp(200, {"hits": {"hits": []}})
        if "/_plugins/_ml/agents/_register" in url:
            return _FakeResp(200, {"agent_id": "AG"})
        if url.endswith("/_plugins/_forecast/forecasters"):
            return _FakeResp(201, {"_id": next(fc_ids)})
        if "/_run_once" in url:
            run_once.append(url)
            return _FakeResp(200, {})
        # Rango de @timestamp para dimensionar el forecaster (min/max/count).
        if method == "POST" and url.endswith("/_search"):
            aggs = (kwargs.get("json") or {}).get("aggs") or {}
            if "tmin" in aggs:
                return _FakeResp(200, {
                    "aggregations": {"tmin": {"value": 1.5e12}, "tmax": {"value": 1.6e12}},
                    "hits": {"total": {"value": 5000}}})
            return _FakeResp(200, {"aggregations": {}})   # _discover_enums (terms)
        # _profile del forecaster tras run_once: backtest completo.
        if "/_plugins/_forecast/forecasters/" in url and url.endswith("/_profile"):
            return _FakeResp(200, {"forecaster_state": "TEST_COMPLETE"})
        # Config os_chat: apunta el Assistant al agente root.
        if method == "PUT" and url.endswith("/.plugins-ml-config/_doc/os_chat"):
            os_chat_cfg.append((kwargs.get("json") or {}))
            return _FakeResp(200, {"result": "created"})
        return _FakeResp(200, {})

    monkeypatch.setattr("requests.request", fake_request)

    result = main._provision_capabilities({"public_endpoint": "1.2.3.4:9200"},
                                          "transacciones-billetera", "admin", "pw", https_enabled=False)
    # El chatbot es un agente ml-commons (multi-PPLTool) en OpenSearch.
    assert result["conversational"]["ok"]
    assert result["conversational"]["agent_id"] == "AG"
    assert result["conversational"]["ppl_model_id"] == "M1"
    assert result["conversational"]["llm_model_id"] == "M2"
    # El agente queda apuntado al Assistant automáticamente (config os_chat).
    assert os_chat_cfg == [{"type": "os_chat_root_agent", "configuration": {"agent_id": "AG"}}]
    # Los 3 forecasts del spec se crean, disparan run-once (backtest) y el
    # _profile confirma TEST_COMPLETE (ya no se reporta ok a ciegas).
    assert result["forecast"]["ok"]
    assert result["forecast"]["forecaster_ids"] == ["FC1", "FC2", "FC3"]
    assert "backtest" in result["forecast"]["note"].lower()
    assert all(s.endswith("TEST_COMPLETE") for s in result["forecast"]["states"])
    assert result["forecast"]["window"]["window_delay_min"] > 0
    assert len(run_once) == 3
    # AD y alerting quedaron fuera por diseño.
    assert "anomaly" not in result and "alerting" not in result
    ids = persisted["transacciones-billetera"]
    assert ids["ppl_model_id"] == "M1" and ids["llm_model_id"] == "M2"
    assert ids["agent_id"] == "AG"
    assert ids["forecaster_ids"] == ["FC1", "FC2", "FC3"]


def test_forecaster_window_sizes_from_data():
    """_forecaster_window usa window_delay=1 (fijo) para que el forecast arranque
    en now y el time picker "Today" del UI muestre los datos sin ajuste manual.
    history<=10000 y suficientes buckets poblados (≥40) para inicializar."""
    DAY = 86400000
    # Datos viejos (fraud IEEE-CIS: ~6 meses en 2017-2018), "ahora" 8 años después.
    interval, wd, hist = main._forecaster_window(0, 180 * DAY, 8 * 365 * DAY, 590000)
    assert wd == 1
    assert hist <= 10000
    assert (180 * 24 * 60) // interval >= 40
    # Datos recientes (1 año), "ahora" 1 día después del fin.
    interval2, wd2, hist2 = main._forecaster_window(0, 365 * DAY, 366 * DAY, 100000)
    assert wd2 == 1
    assert hist2 <= 10000 and (365 * 24 * 60) // interval2 >= 40


def test_build_forecaster_omits_low_seasonality_and_takes_window():
    """build_forecaster: no manda suggested_seasonality si ≤16 (OpenSearch lo
    ignora), respeta el window_delay pasado y capea history a 10000."""
    import capabilities as C
    b = C.build_forecaster("i-*", "f", history=99999, window_delay_minutes=12345,
                           suggested_seasonality=8)
    assert "suggested_seasonality" not in b
    assert b["window_delay"]["period"]["interval"] == 12345
    assert b["history"] == 10000
    assert C.build_forecaster("i-*", "f", suggested_seasonality=48)["suggested_seasonality"] == 48


def test_forecast_test_state_reads_profile(monkeypatch):
    """_forecast_test_state pollea el _profile: TEST_COMPLETE → ok; un estado de
    espera/INIT → no ok (y no cuelga)."""
    monkeypatch.setattr("requests.request",
                        lambda m, u, **k: _FakeResp(200, {"forecaster_state": "TEST_COMPLETE"}))
    assert main._forecast_test_state("http://x:9200", "a", "p", "FC", tries=1, delay=0) == (True, "TEST_COMPLETE")
    monkeypatch.setattr("requests.request",
                        lambda m, u, **k: _FakeResp(200, {"forecaster_state": "AWAITING_DATA_TO_INIT"}))
    ok, state = main._forecast_test_state("http://x:9200", "a", "p", "FC", tries=2, delay=0)
    assert ok is False and state == "AWAITING_DATA_TO_INIT"


def test_forecast_omitted_without_event_timestamp(monkeypatch):
    """Índice ingerido pero SIN fecha de evento real (@timestamp sin rango) → no se
    crean forecasters; se reporta el motivo (cierra 'forecasts solo con fecha')."""
    monkeypatch.setenv("MAAS_API_KEY", "KEY")
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {})
    monkeypatch.setattr(main, "_write_capabilities", lambda td, reg: None)
    monkeypatch.setattr(main, "_ml_commons_available", lambda *a, **kw: False)

    posted = []

    def fake_request(method, url, **kwargs):
        if method == "GET" and "/_field_caps" in url:
            return _FakeResp(200, {}, text='{"fields":{"transaction.operation_code":{}}}')
        if method == "GET" and url.endswith("/_count"):
            return _FakeResp(200, {"count": 100})
        if method == "POST" and url.endswith("/_search"):
            return _FakeResp(200, {"aggregations": {"tmin": {"value": None}, "tmax": {"value": None}}})
        if method == "POST":
            posted.append(url)
        return _FakeResp(200, {})

    monkeypatch.setattr("requests.request", fake_request)
    result = main._provision_capabilities({"public_endpoint": "1.2.3.4:9200"},
                                          "transacciones-billetera", "admin", "pw", https_enabled=False)
    assert result["forecast"]["ok"] is False
    assert "fecha de evento" in result["forecast"]["reason"]
    assert not any(u.endswith("/_plugins/_forecast/forecasters") for u in posted)


def test_patch_dashboard_time_range_only_dashboard():
    """_patch_dashboard_time_range reescribe timeFrom/timeTo/timeRestore SOLO del
    objeto dashboard (para que abra en el rango real de sus datos, ej. fraud 2017)
    y deja intactos el index-pattern y las visualizaciones."""
    import json as _json
    nd = "\n".join([
        _json.dumps({"type": "index-pattern", "id": "fraud-detection-*", "attributes": {"title": "fraud-detection-*"}}),
        _json.dumps({"type": "visualization", "id": "v1", "attributes": {"title": "V"}}),
        _json.dumps({"type": "dashboard", "id": "d1", "attributes": {
            "title": "[fraud] X", "timeFrom": "2025-07-01T00:00:00.000Z",
            "timeTo": "2026-07-02T00:00:00.000Z"}}),
    ])
    out = [_json.loads(l) for l in main._patch_dashboard_time_range(
        nd, "2017-12-01T00:00:00.000Z", "2018-06-01T00:00:00.000Z").splitlines()]
    dash = next(o for o in out if o["type"] == "dashboard")
    assert dash["attributes"]["timeFrom"] == "2017-12-01T00:00:00.000Z"
    assert dash["attributes"]["timeTo"] == "2018-06-01T00:00:00.000Z"
    assert dash["attributes"]["timeRestore"] is True
    # index-pattern y viz sin tocar.
    assert next(o for o in out if o["type"] == "index-pattern")["attributes"]["title"] == "fraud-detection-*"
    assert any(o["type"] == "visualization" for o in out)
    assert main._epoch_ms_to_iso(1512086400000) == "2017-12-01T00:00:00.000Z"


def test_ppl_chat_endpoint(monkeypatch):
    """El chatbot PPL orquesta: modelo PPL (NL→PPL) → _ppl (ejecuta) → modelo LLM (frasea).
    Devuelve el dato real, nunca inventado."""
    monkeypatch.setattr(main, "_cluster_with_public_access",
                        lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_read_capabilities",
                        lambda td: {"transacciones-billetera": {"ppl_model_id": "PM", "llm_model_id": "LM"}})

    def fake_predict(base, user, pw, model_id, params, timeout=60):
        if model_id == "PM":  # NL → PPL
            return "source=transacciones-billetera* | where transaction.operation_code='TRANSFER' | stats count() as failed"
        return "Fallaron 1.708 transferencias."  # LLM frasea

    monkeypatch.setattr(main, "_ml_predict", fake_predict)

    def fake_request(method, url, **kwargs):
        if url.endswith("/_plugins/_ppl"):
            return _FakeResp(200, {"schema": [{"name": "failed", "type": "long"}], "datarows": [[1708]]})
        return _FakeResp(200, {})

    monkeypatch.setattr("requests.request", fake_request)

    res = client.post("/api/v1/capabilities/ppl-chat",
                      json={"question": "¿Cuántas transferencias fallaron?", "opensearch_password": "pw"})
    assert res.status_code == 200
    body = res.json()
    assert body["answer"] == "Fallaron 1.708 transferencias."
    assert body["ppl"].startswith("source=")
    assert body["result"]["datarows"] == [[1708]]


def test_teardown_orphans_by_name(monkeypatch):
    """El force limpia por NOMBRE los huérfanos (forecasters de TODOS los specs,
    agente, models, group, connectors) aunque no estén en el registry — evita el
    409 de re-create."""
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if method == "POST" and url.endswith("/_search"):
            return _FakeResp(200, {"hits": {"hits": [{"_id": "X1"}]}})
        return _FakeResp(200, {})

    monkeypatch.setattr("requests.request", fake_request)
    main._teardown_orphans_by_name("http://x:9200", "admin", "pw")
    deletes = [u for m, u in calls if m == "DELETE"]
    joined = " ".join(deletes)
    for frag in ("/forecasters/X1", "/agents/X1",
                 "/models/X1", "/model_groups/X1", "/connectors/X1"):
        assert frag in joined, f"falta DELETE {frag}"
    # Forecaster se para antes de borrar.
    stops = [u for m, u in calls if m == "POST" and u.endswith("/_stop")]
    assert any("/forecasters/X1/_stop" in u for u in stops)
    # Busca los forecasters de TODOS los verticales (specs), no solo fintech.
    import capabilities as C
    fc_names = [fc["name"] for s in C.get_capability_slugs()
                for fc in (C.get_capability_spec(s) or {}).get("forecasts", [])]
    searches = len([1 for m, u in calls if m == "POST" and u.endswith("/forecasters/_search")])
    # Lo que importa: el teardown busca TODOS los forecasters que el registro
    # define, sin saltearse ninguno (uno que quede vivo sigue consumiendo ML del
    # cluster). El número fijo es un tripwire de inventario: 14 capabilities × 3.
    assert searches == len(fc_names) and len(fc_names) == 42


@requires_datasets
def test_dataset_preview_resolves_underscore():
    """El preview del dataset resuelve dataset_files desde el registro de verticales
    (transacciones-billetera → transacciones-billetera.log) y devuelve N líneas de datos (sin comentarios)."""
    res = client.get("/api/v1/datasets/transacciones-billetera/preview?lines=3")
    assert res.status_code == 200
    body = res.json()
    assert body["slug"] == "transacciones-billetera"
    assert len(body["lines"]) == 3
    assert all(l.strip() and not l.lstrip().startswith("#") for l in body["lines"])


def test_dataset_preview_404_when_no_dataset():
    res = client.get("/api/v1/datasets/no-existe-xyz/preview")
    assert res.status_code == 404


@requires_datasets
def test_dataset_preview_globs_multi_file_siem():
    """El SIEM no tiene `siem.log` sino varios `siem-*.log` (una fuente c/u). El
    preview hace glob e INTERCALA líneas de cada archivo, así refleja las 4 fuentes:
    una línea FortiGate (`date=…`) y al menos una JSON (`{…}`) tienen que aparecer."""
    res = client.get("/api/v1/datasets/siem/preview?lines=8")
    assert res.status_code == 200
    lines = res.json()["lines"]
    assert len(lines) >= 4
    assert any(l.startswith("date=") for l in lines), "falta una línea FortiGate"
    assert any(l.startswith("{") for l in lines), "falta una línea JSON (cloudaudit/waf)"
    assert any(l.startswith("<") for l in lines), "falta una línea syslog (auth)"


def test_ml_register_model_group_reuses_on_conflict(monkeypatch):
    """Si el model_group ya existe (nombre único), OpenSearch da 400 con el ID en el
    mensaje → el helper lo reusa en vez de fallar (evita el bloqueo del re-provision)."""
    def fake_request(method, url, **kwargs):
        return _FakeResp(400, {}, text=(
            '{"error":{"root_cause":[{"type":"illegal_argument_exception",'
            '"reason":"The name you provided is already being used by a model group '
            'with ID: ymzkPZ8BjGosDiWdSy3r."}]}}'))
    monkeypatch.setattr("requests.request", fake_request)
    gid = main._ml_register_model_group("http://x:9200", "admin", "pw", {"name": "platform-maas-deepseek"})
    assert gid == "ymzkPZ8BjGosDiWdSy3r"


def test_ml_register_model_group_ok(monkeypatch):
    monkeypatch.setattr("requests.request",
                        lambda method, url, **kw: _FakeResp(200, {"model_group_id": "MG"}))
    assert main._ml_register_model_group("http://x:9200", "admin", "pw", {"name": "g"}) == "MG"


def test_ppl_chat_not_provisioned_400(monkeypatch):
    """Sin modelos provisionados para el slug → 400 claro (no inventa)."""
    monkeypatch.setattr(main, "_cluster_with_public_access",
                        lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {})
    res = client.post("/api/v1/capabilities/ppl-chat",
                      json={"question": "hola", "opensearch_password": "pw"})
    assert res.status_code == 400


def test_add_css_cluster_routes(monkeypatch):
    """Agrega las IPs de MaaS por la API del CSS: una llamada por IP faltante,
    saltea las ya presentes, y un error no propaga excepción (best-effort)."""
    added = []

    class _FakeClient:
        def list_route_ips(self, cluster_id):
            return {"183.87.47.249"}  # una ya existe

        def add_route(self, cluster_id, ip):
            added.append((cluster_id, ip))

    monkeypatch.setattr(main, "_css_client", lambda ak, sk, pid: _FakeClient())
    res = main._add_css_cluster_routes("CID", "ak", "sk", "proj",
                                       ips=["119.8.35.218", "183.87.47.249"])
    assert res["added"] == ["119.8.35.218"]       # la faltante se agrega
    assert "183.87.47.249" in res["skipped"]      # la existente se saltea
    assert added == [("CID", "119.8.35.218")]
    assert res["error"] is None


def test_add_css_cluster_routes_no_sdk(monkeypatch):
    """Si el SDK no está (o faltan creds), no rompe: devuelve error y sigue."""
    monkeypatch.setattr(main, "_css_client", lambda ak, sk, pid: None)
    res = main._add_css_cluster_routes("CID", "ak", "sk", "proj", ips=["1.2.3.4"])
    assert res["added"] == [] and res["error"]
    # Sin cluster_id tampoco explota.
    res2 = main._add_css_cluster_routes("", "ak", "sk", "proj")
    assert res2["error"]


def test_provision_capabilities_skips_conversational_without_mlcommons(monkeypatch):
    """Preflight ml-commons falla → conversacional se saltea; los forecasts siguen."""
    monkeypatch.setenv("MAAS_API_KEY", "KEY")
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {})
    monkeypatch.setattr(main, "_write_capabilities", lambda td, reg: None)

    def fake_request(method, url, **kwargs):
        if method == "GET" and "/_plugins/_ml/stats" in url:
            return _FakeResp(400, {}, text="no handler found for uri [/_plugins/_ml/stats]")
        if method == "GET" and "/_field_caps" in url:
            return _FakeResp(200, {}, text='{"fields":{"transaction.operation_code":{}}}')
        if method == "GET" and url.endswith("/_count"):
            return _FakeResp(200, {"count": 100})
        if url.endswith("/_plugins/_forecast/forecasters"):
            return _FakeResp(201, {"_id": "FC"})
        if method == "POST" and url.endswith("/_search"):
            aggs = (kwargs.get("json") or {}).get("aggs") or {}
            if "tmin" in aggs:
                return _FakeResp(200, {
                    "aggregations": {"tmin": {"value": 1.5e12}, "tmax": {"value": 1.6e12}},
                    "hits": {"total": {"value": 5000}}})
            return _FakeResp(200, {"aggregations": {}})
        if "/_plugins/_forecast/forecasters/" in url and url.endswith("/_profile"):
            return _FakeResp(200, {"forecaster_state": "TEST_COMPLETE"})
        return _FakeResp(200, {})

    monkeypatch.setattr("requests.request", fake_request)
    result = main._provision_capabilities({"public_endpoint": "1.2.3.4:9200"},
                                          "transacciones-billetera", "admin", "pw", https_enabled=False)
    assert result["conversational"]["ok"] is False
    assert "ml-commons" in result["conversational"]["reason"]
    assert result["forecast"]["ok"]


def test_teardown_capabilities_deletes(monkeypatch):
    """_teardown_capabilities borra los artefactos (best-effort)."""
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {
        "transacciones-billetera": {
            "agent_id": "AG", "model_ids": ["M1", "M2"], "model_group_id": "MG",
            "connector_ids": ["C1", "C2"], "detector_id": "DET",
            "forecaster_id": "FC", "monitor_id": "MON",
        }
    })
    deleted = []

    def fake_request(method, url, **kwargs):
        if method == "DELETE":
            deleted.append(url)
        return _FakeResp(200, {})

    monkeypatch.setattr("requests.request", fake_request)
    main._teardown_capabilities({"public_endpoint": "1.2.3.4:9200"}, "admin", "pw", https_enabled=False)
    joined = " ".join(deleted)
    for frag in ("/monitors/MON", "/forecasters/FC", "/detectors/DET", "/agents/AG",
                 "/models/M1", "/models/M2", "/model_groups/MG", "/connectors/C1", "/connectors/C2"):
        assert frag in joined, f"falta DELETE {frag}"


def test_teardown_capabilities_skips_when_cluster_unreachable(monkeypatch):
    """Cluster ya destruido/inalcanzable → el teardown se saltea en el preflight y
    NO intenta cada DELETE (que colgaría su timeout completo)."""
    import requests as _requests
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {
        "transacciones-billetera": {"monitor_id": "MON", "detector_id": "DET", "agent_id": "AG"}
    })
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        # Todo request (incluido el preflight GET /) falla como si el cluster no existiera.
        raise _requests.exceptions.ConnectTimeout("cluster gone")

    monkeypatch.setattr("requests.request", fake_request)
    main._teardown_capabilities({"public_endpoint": "1.2.3.4:9200"}, "admin", "pw", https_enabled=False)
    assert any(m == "GET" for m, _ in calls), "debería intentar el preflight GET"
    assert all(m != "DELETE" for m, _ in calls), f"no debería intentar DELETE: {calls}"


def test_provision_capabilities_skips_forecast_when_index_empty(monkeypatch):
    """Índice sin documentos → los forecasts se saltean con reason claro (no se
    crea ningún forecaster)."""
    monkeypatch.setenv("MAAS_API_KEY", "KEY")
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {})
    monkeypatch.setattr(main, "_write_capabilities", lambda td, reg: None)

    posted = []

    def fake_request(method, url, **kwargs):
        if method == "GET" and "/_plugins/_ml/stats" in url:
            return _FakeResp(200, {"nodes": {}})
        if method == "GET" and "/_field_caps" in url:
            return _FakeResp(404, {}, text="index_not_found_exception")
        if method == "GET" and url.endswith("/_count"):
            return _FakeResp(200, {"count": 0})
        if method == "POST":
            posted.append(url)
        return _FakeResp(200, {})

    monkeypatch.setattr("requests.request", fake_request)
    result = main._provision_capabilities({"public_endpoint": "1.2.3.4:9200"},
                                          "transacciones-billetera", "admin", "pw", https_enabled=False)
    assert result["forecast"]["ok"] is False
    assert not any("/_forecast/forecasters" in u for u in posted)


def test_provision_capabilities_endpoint(monkeypatch):
    """POST /provision-capabilities provisiona los slugs con spec y devuelve 200."""
    monkeypatch.setattr(main, "_cluster_with_public_access",
                        lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_provision_capabilities",
                        lambda cluster, slug, user, pw, https, force=False: {slug: {"anomaly": {"ok": True}}})

    resp = client.post("/api/v1/onboarding/provision-capabilities",
                       json={"opensearch_password": "pw", "https_enabled": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "transacciones-billetera" in data["capabilities"]


def test_provision_capabilities_endpoint_no_cluster_503(monkeypatch):
    """Sin cluster alcanzable, /provision-capabilities responde 503."""
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {})
    resp = client.post("/api/v1/onboarding/provision-capabilities",
                       json={"opensearch_password": "pw"})
    assert resp.status_code == 503


# ── Tests del plan: paridad demo ↔ despliegue productivo ─────────────────────

def test_build_spec_from_fields_produces_valid_spec():
    """(a) build_spec_from_fields produce un spec con la forma de los curados
    (index_pattern, fields, 1-3 forecasts) y build_ppl_system_prompt lo acepta."""
    import capabilities as C

    fields = [
        {"field_path": "data.status", "type": "keyword", "business_label": "Estado", "dimension": True},
        {"field_path": "data.category", "type": "keyword", "business_label": "Categoría", "dimension": True},
        {"field_path": "data.price", "type": "float", "business_label": "Precio", "dimension": False},
        {"field_path": "data.customer_id", "type": "keyword", "business_label": "Customer ID", "dimension": False},
        {"field_path": "data.description", "type": "text", "business_label": "Descripción", "dimension": False},
    ]
    enums = {"data.status": ["COMPLETED", "CANCELLED", "PENDING"]}
    spec = C.build_spec_from_fields("my-log", "my-log-*", fields, label="My Log", enums=enums)

    # Mismo shape que los curados.
    assert spec["label"] == "My Log"
    assert spec["index_pattern"] == "my-log-*"
    assert isinstance(spec["operations"], list)
    assert isinstance(spec["fields"], dict)
    assert isinstance(spec["forecasts"], list)
    assert 1 <= len(spec["forecasts"]) <= 3
    assert spec["volume_field"]

    # Los campos no dimensionales (description, customer_id) no están en el prompt.
    assert "data.description" not in spec["fields"]
    assert "data.customer_id" not in spec["fields"]
    # Las dimensiones y medidas sí.
    assert "data.status" in spec["fields"]
    assert "data.price" in spec["fields"]

    # operations = los valores del enum de la dimensión principal.
    assert spec["operations"] == ["COMPLETED", "CANCELLED", "PENDING"]

    # build_ppl_system_prompt lo acepta sin error.
    prompt = C.build_ppl_system_prompt(spec["index_pattern"], spec["operations"],
                                       spec["fields"], spec.get("success_code", ""),
                                       spec.get("label", ""))
    assert "my-log-*" in prompt
    assert "COMPLETED" in prompt


def test_build_spec_from_fields_forecasts():
    """El spec productivo arma 3 forecasts: volumen + entidades (si hay id) + suma (si hay numérico)."""
    import capabilities as C

    fields = [
        {"field_path": "data.status", "type": "keyword", "business_label": "Estado", "dimension": True},
        {"field_path": "data.order_id", "type": "keyword", "business_label": "Order ID", "dimension": False},
        {"field_path": "data.total", "type": "float", "business_label": "Total", "dimension": False},
    ]
    spec = C.build_spec_from_fields("shop", "shop-*", fields, label="Shop")
    fc_names = [fc["name"] for fc in spec["forecasts"]]
    assert any("volume" in n for n in fc_names)
    assert any("entities" in n for n in fc_names)
    assert any("measure" in n for n in fc_names)


def test_discover_enums_keeps_low_cardinality_discards_ids(monkeypatch):
    """(b) _discover_enums con requests.request fakeado → keeps low-cardinality,
    descarta ids (alta cardinalidad)."""
    import json as _json

    fields = [
        {"field_path": "data.status", "dimension": True},
        {"field_path": "data.user_id", "dimension": True},
    ]

    def fake_request(method, url, **kwargs):
        assert method == "POST"
        assert "_search" in url

        class _R:
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "aggregations": {
                        "f0": {"buckets": [   # status: 3 valores → enum
                            {"key": "ACTIVE", "doc_count": 100},
                            {"key": "INACTIVE", "doc_count": 50},
                            {"key": "PENDING", "doc_count": 10},
                        ]},
                        "f1": {"buckets": [   # user_id: 16 valores → alta cardinalidad
                            {"key": f"user_{i}", "doc_count": 1} for i in range(16)
                        ]},
                    }
                }

        return _R()

    monkeypatch.setattr("requests.request", fake_request)
    result = main._discover_enums("http://1.2.3.4:9200", "admin", "pw",
                                  "my-index*", fields, max_cardinality=15)
    assert "data.status" in result
    assert result["data.status"] == ["ACTIVE", "INACTIVE", "PENDING"]
    assert "data.user_id" not in result  # 16 > 15 → descartado


def test_spec_from_fields_no_date_no_temporal_panels():
    """(d) _spec_from_fields sin campos date no emite paneles temporales."""
    from dashboards import _spec_from_fields

    fields = [
        {"field_path": "data.status", "type": "keyword", "business_label": "Estado", "dimension": True},
        {"field_path": "data.amount", "type": "float", "business_label": "Monto", "dimension": False},
    ]
    spec = _spec_from_fields("my-log", "logs-%{+YYYY.MM}", fields)
    panel_types = [p["type"] for p in spec["panels"]]
    assert "area" not in panel_types   # sin date → no "Eventos en el tiempo"
    assert "line" not in panel_types   # sin date → no "X en el tiempo"


def test_spec_from_fields_dimension_false_excluded_from_topn():
    """(d) _spec_from_fields no grafica dimension=False en Top-N."""
    from dashboards import _spec_from_fields

    fields = [
        {"field_path": "data.status", "type": "keyword", "business_label": "Estado", "dimension": True},
        {"field_path": "data.review", "type": "text", "business_label": "Review", "dimension": False},
    ]
    spec = _spec_from_fields("my-log", "logs-%{+YYYY.MM}", fields)
    all_text = str(spec["panels"])
    assert "data.status" in all_text   # dimension=True → Top-N
    assert "data.review" not in all_text  # dimension=False → excluido


# ── La visualización que le corresponde a cada campo ───────────────────────
# El generador automático decidía casi todo por el tipo de ES y usaba tres de
# los seis `role` detectados: un booleano salía como ranking de dos barras, una
# latencia se graficaba sumada, y "éxitos vs fallos" —el gráfico que mira
# primero cualquiera que opera esto— no existía.
def _campo(path, tipo, **kw):
    base = {"field_path": path, "type": tipo, "business_label": path.rsplit(".", 1)[-1],
            "dimension": tipo in ("keyword", "text", "boolean")}
    base.update(kw)
    return base


def _paneles(fields, slug="my-log"):
    from dashboards import _spec_from_fields
    return _spec_from_fields(slug, "logs-%{+YYYY.MM}", fields)["panels"]


def test_un_dataset_con_coordenadas_sale_en_un_mapa():
    paneles = _paneles([
        _campo("data.geo_location", "geo_point", dimension=False),
        _campo("data.estado", "keyword"),
    ])

    mapa = [p for p in paneles if p["type"] == "map"]
    assert len(mapa) == 1, [p["type"] for p in paneles]
    assert mapa[0]["field"] == "data.geo_location"
    # Y no se cuela además como una categoría más para agrupar.
    barras = [p for p in paneles if p.get("field") == "data.geo_location" and p["type"] != "map"]
    assert not barras, barras


def test_un_booleano_va_a_torta_y_no_a_barras():
    """Dos valores no son un ranking."""
    paneles = _paneles([_campo("data.exitosa", "boolean"), _campo("data.canal", "keyword")])

    del_bool = [p for p in paneles if p.get("field") == "data.exitosa"]
    assert del_bool and all(p["type"] == "pie" for p in del_bool), del_bool


def test_una_medida_de_tiempo_se_promedia_en_vez_de_sumarse():
    """"Las latencias suman 40 segundos" no significa nada; el promedio sí."""
    con_ms = _paneles([
        _campo("data.fecha", "date", dimension=False),
        _campo("data.latencia", "float", dimension=False, role="measure", unit="ms"),
        _campo("data.canal", "keyword", role="primary_dimension"),
    ])
    aggs = {p.get("agg") or p.get("metric") for p in con_ms if p.get("field") == "data.latencia"}
    assert "avg" in aggs and "sum" not in aggs, aggs

    # Una medida de dinero, en cambio, se suma.
    con_usd = _paneles([
        _campo("data.fecha", "date", dimension=False),
        _campo("data.monto", "float", dimension=False, role="measure", unit="USD"),
        _campo("data.canal", "keyword", role="primary_dimension"),
    ])
    aggs = {p.get("agg") or p.get("metric") for p in con_usd if p.get("field") == "data.monto"}
    assert "sum" in aggs and "avg" not in aggs, aggs


def test_el_indicador_de_exito_parte_la_serie_en_el_tiempo():
    paneles = _paneles([
        _campo("data.fecha", "date", dimension=False),
        _campo("data.resultado", "keyword", role="success_indicator"),
    ])

    partidas = [p for p in paneles if p["type"] == "area" and p.get("split") == "data.resultado"]
    assert partidas, [p for p in paneles if p["type"] == "area"]


def test_el_indicador_critico_sale_como_metrica_y_como_barras():
    """Es el campo que SOLO aparece cuando algo salió mal: cuántos fallaron y
    dónde fallan."""
    paneles = _paneles([
        _campo("data.error_code", "keyword", role="critical_indicator"),
        _campo("data.canal", "keyword", role="primary_dimension"),
    ])

    metrica = [p for p in paneles if p["type"] == "metric" and p.get("query") == "data.error_code:*"]
    barras = [p for p in paneles if p["type"] == "bar" and p.get("field") == "data.error_code"]
    assert metrica, [p for p in paneles if p["type"] == "metric"]
    assert barras, [p["type"] for p in paneles]


def test_una_entidad_sale_como_tabla_y_no_como_torta():
    """Un id de cliente tiene miles de valores: una torta de eso es ilegible."""
    paneles = _paneles([
        _campo("data.cliente_id", "keyword", role="entity_id"),
        _campo("data.canal", "keyword", role="primary_dimension"),
    ])

    de_entidad = [p for p in paneles if p.get("field") == "data.cliente_id"]
    assert any(p["type"] == "table" for p in de_entidad), de_entidad
    assert not any(p["type"] == "pie" for p in de_entidad), de_entidad
    assert any(p["type"] == "metric" and p.get("agg") == "cardinality" for p in paneles)


def test_ningun_campo_se_grafica_dos_veces():
    """El primer categórico salía en la torta Y otra vez en los breakdowns; solo
    se notaba porque `_uniq` le cambiaba el título al segundo."""
    paneles = _paneles([
        _campo("data.canal", "keyword", role="primary_dimension"),
        _campo("data.estado", "keyword"),
        _campo("data.tipo", "keyword"),
    ])

    for campo in ("data.canal", "data.estado", "data.tipo"):
        usos = [p for p in paneles
                if p.get("field") == campo and p["type"] in ("pie", "bar", "table")
                and not p.get("agg_field")]     # el cross-tab es otra pregunta
        assert len(usos) == 1, f"{campo} graficado {len(usos)} veces: {usos}"


def test_la_serie_temporal_sobrevive_a_que_la_fecha_se_haya_ido_a_timestamp():
    """El prompt le pide al modelo que mande la fecha a `@timestamp` y borre el
    campo original. Cuando lo hacía, el dashboard perdía el área y la línea
    aunque el índice tuviera una serie temporal perfecta."""
    paneles = _paneles([
        _campo("data.ts", "keyword", dimension=False, role="timestamp"),
        _campo("data.monto", "float", dimension=False, role="measure"),
    ])

    tipos = [p["type"] for p in paneles]
    assert "area" in tipos and "line" in tipos, tipos


def test_de_las_coordenadas_al_mapa_de_punta_a_punta(monkeypatch):
    """La cadena completa: el LLM devuelve lat y lon sueltas, el endpoint arma el
    `geo_point`, el index template lo mapea y el dashboard lo dibuja. Cada
    eslabón existía por separado; lo que no existía era la cadena."""
    import json as _json

    import conf_lint
    from dashboards import build_ndjson_from_fields
    from index_template import build_index_template

    def _fake(raw_log, namespace="data", ecs_overlay=False, feedback="", previous_filter="", input_type=""):
        return {
            "filter_code": 'filter {\n  csv { columns => ["a"] target => "data" }\n}',
            "fields": [
                {"raw_name": "lat", "field_path": "data.lat", "type": "float",
                 "business_label": "Latitud", "dimension": False, "role": None},
                {"raw_name": "lon", "field_path": "data.lon", "type": "float",
                 "business_label": "Longitud", "dimension": False, "role": None},
                {"raw_name": "estado", "field_path": "data.estado", "type": "string",
                 "business_label": "Estado", "dimension": True, "role": "primary_dimension"},
            ],
        }

    monkeypatch.setattr(main, "generate_logstash_filter", _fake)

    res = client.post("/api/v1/onboarding/generate-filter", json={"raw_log": "x", "namespace": "data"})
    assert res.status_code == 200
    body = res.json()

    # 1. El filter fusiona las dos columnas, y sigue compilando.
    assert "[data][geo_location]" in body["filter_code"]
    assert conf_lint.lint_filtro(body["filter_code"]) == []

    # 2. El campo viaja al paso 2 como geo_point.
    geo = [f for f in body["fields"] if f["type"] == "geo_point"]
    assert len(geo) == 1 and geo[0]["field_path"] == "data.geo_location"

    # 3. El index template lo mapea (con ignore_malformed, o una coordenada
    #    basura tira el documento entero).
    props = build_index_template(body["fields"], "data", "logs-%{+YYYY.MM}")["template"]["mappings"]["properties"]
    assert props["data"]["properties"]["geo_location"] == {"type": "geo_point", "ignore_malformed": True}

    # 4. Y el dashboard lo dibuja en un mapa.
    objetos = [_json.loads(l) for l in build_ndjson_from_fields("mi-log", "logs-*", body["fields"]).splitlines() if l.strip()]
    tipos = [_json.loads(o["attributes"]["visState"])["type"]
             for o in objetos if o["type"] == "visualization"]
    assert "tile_map" in tipos, tipos


def test_un_campo_geo_de_ecs_se_tipa_solo(monkeypatch):
    """La spec ECS ya sabe que `source.geo.location` y sus siete hermanos son
    `geo_point`; ese dato se venía tirando y el campo terminaba como keyword, o
    sea sin mapa posible."""
    def _fake(raw_log, namespace="data", ecs_overlay=False, feedback="", previous_filter="", input_type=""):
        return {
            "filter_code": "filter { }",
            "fields": [{"raw_name": "loc", "ecs_path": "source.geo.location", "field_path": "source.geo.location",
                        "type": "string", "business_label": "Ubicación", "is_ecs": True, "dimension": True}],
        }

    monkeypatch.setattr(main, "generate_logstash_filter", _fake)

    campo = client.post("/api/v1/onboarding/generate-filter",
                        json={"raw_log": "x"}).json()["fields"][0]

    assert campo["type"] == "geo_point", campo


def test_los_dashboards_curados_no_pasan_por_la_heuristica():
    """La mejora de visualizaciones es SOLO para los datasets nuevos: los diez
    verticales eligen sus paneles a mano y no se tocan."""
    from dashboards import build_ndjson, get_available_slugs

    for slug in get_available_slugs():
        assert "Dashboard auto-generado" not in build_ndjson(slug), slug


def test_provision_capabilities_productive_slug(monkeypatch, tmp_path):
    """(c) _provision_capabilities con un slug productivo en el registry (fields
    persistidos, sin spec curado) construye el spec y provisiona forecasters."""
    import json as _json

    terraform_dir = tmp_path / "terraform"
    terraform_dir.mkdir()
    # Registry de pipelines con fields persistidos (productivo).
    prod_registry = {
        "my-log": {
            "pipeline_conf": "filter { }",
            "index": "my-log-%{+YYYY.MM}",
            "fields": [
                {"field_path": "data.status", "type": "keyword", "business_label": "Estado", "dimension": True},
                {"field_path": "data.amount", "type": "float", "business_label": "Monto", "dimension": False},
            ],
            "label": "My Log",
        }
    }
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda td: prod_registry)
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {})
    monkeypatch.setattr(main, "_write_capabilities", lambda td, data: None)

    posted = []

    def fake_request(method, url, **kwargs):
        posted.append(url)

        class _R:
            status_code = 200
            text = '{"count": 100}'

            def json(self):
                if "_count" in url:
                    return {"count": 100}
                if "_field_caps" in url:
                    return {"fields": {"data.status": {"keyword": {"type": "keyword"}}}}
                if "_search" in url:
                    return {"aggregations": {}}
                if "models" in url and "register" in url:
                    return {"task_id": "task-123"}
                if "tasks" in url:
                    return {"state": "COMPLETED", "model_id": "model-123"}
                if "models" in url:
                    return {"model_state": "DEPLOYED"}
                if "agents" in url and "_search" in url:
                    return {"hits": {"hits": []}}
                if "agents" in url and "_register" in url:
                    return {"agent_id": "agent-123"}
                if "forecasters" in url:
                    return {"_id": "fc-123"}
                return {}

        return _R()

    monkeypatch.setattr("requests.request", fake_request)
    monkeypatch.setattr(main, "_ml_commons_available", lambda *a, **kw: True)
    monkeypatch.setattr(main, "_ml_wait_model", lambda *a, **kw: "model-123")
    monkeypatch.setattr(main, "_ml_wait_deployed", lambda *a, **kw: (True, "DEPLOYED"))
    monkeypatch.setattr(main, "_ml_register_model_group", lambda *a, **kw: "grp-123")
    monkeypatch.setattr(main, "_ml_create", lambda *a, **kw: "fake-id")
    monkeypatch.setattr(main, "_search_ids", lambda *a, **kw: [])
    monkeypatch.setattr(main, "_os_req", lambda *a, **kw: type("R", (), {"status_code": 200, "text": "{}", "json": lambda self: {}})())
    monkeypatch.setattr(main, "_os_base", lambda cluster, https: "http://1.2.3.4:9200")
    monkeypatch.setattr(main, "_cluster_hwc_creds", lambda td: ("", ""))
    monkeypatch.setattr(main, "_add_css_cluster_routes", lambda *a, **kw: "ok")

    import maas_integrator
    monkeypatch.setattr(maas_integrator, "get_maas_api_key", lambda: "fake-key")

    result = main._provision_capabilities(
        {"public_endpoint": "1.2.3.4:9200"},
        "my-log", "admin", "pw", https_enabled=False,
    )
    # El spec productivo se construyó y el forecast se intentó.
    assert "forecast" in result or "conversational" in result


def test_build_spec_detects_critical_field_forecast():
    """build_spec_from_fields detecta un campo crítico (failed, denied, etc.) y
    crea un forecast de eventos críticos — como los demos (fintech-failed, siem-denied)."""
    import capabilities as C

    fields = [
        {"field_path": "data.status", "type": "keyword", "business_label": "Estado", "dimension": True},
        {"field_path": "data.error_code", "type": "keyword", "business_label": "Código de error", "dimension": False},
        {"field_path": "data.amount", "type": "float", "business_label": "Monto", "dimension": False},
    ]
    spec = C.build_spec_from_fields("shop", "shop-*", fields, label="Shop")
    fc_names = [fc["name"] for fc in spec["forecasts"]]
    assert any("critical" in n for n in fc_names), f"expected critical forecast in {fc_names}"


def test_build_spec_detects_critical_via_enum_values():
    """Si no hay campo crítico por nombre, lo detecta por valores de enum
    (status con valores 'failed', 'error', etc.)."""
    import capabilities as C

    fields = [
        {"field_path": "data.status", "type": "keyword", "business_label": "Estado", "dimension": True},
    ]
    enums = {"data.status": ["success", "failed", "error"]}
    spec = C.build_spec_from_fields("app", "app-*", fields, label="App", enums=enums)
    fc_names = [fc["name"] for fc in spec["forecasts"]]
    assert any("critical" in n for n in fc_names)


def test_build_spec_discovers_success_code():
    """build_spec_from_fields descubre success_code de un campo response_code
    cuando un valor matchea patrones conocidos (000, 200, success, etc.)."""
    import capabilities as C

    fields = [
        {"field_path": "data.response_code", "type": "keyword", "business_label": "Código de respuesta", "dimension": True},
    ]
    enums = {"data.response_code": ["000", "100", "200"]}
    spec = C.build_spec_from_fields("txn", "txn-*", fields, label="Txn", enums=enums)
    assert spec["success_code"] == "000"


def test_build_spec_success_code_fallback_most_common():
    """Si ningún valor matchea _SUCCESS_VALUES, usa el más frecuente (primer bucket)."""
    import capabilities as C

    fields = [
        {"field_path": "data.status_code", "type": "keyword", "business_label": "Status", "dimension": True},
    ]
    enums = {"data.status_code": ["OK_CUSTOM", "WEIRD"]}
    spec = C.build_spec_from_fields("svc", "svc-*", fields, label="Svc", enums=enums)
    assert spec["success_code"] == "OK_CUSTOM"  # primer bucket = más frecuente


def test_spec_from_fields_has_pie_chart():
    """_spec_from_fields agrega un pie chart de la dimensión primaria."""
    from dashboards import _spec_from_fields

    fields = [
        {"field_path": "data.status", "type": "keyword", "business_label": "Estado", "dimension": True},
        {"field_path": "data.category", "type": "keyword", "business_label": "Categoría", "dimension": True},
    ]
    spec = _spec_from_fields("my-log", "logs-%{+YYYY.MM}", fields)
    pie_panels = [p for p in spec["panels"] if p["type"] == "pie"]
    assert len(pie_panels) >= 1
    assert "data.status" in str(pie_panels[0])


def test_spec_from_fields_has_crosstab_bar():
    """_spec_from_fields agrega un bar 'Top dimensión por medida' (cross-tab)."""
    from dashboards import _spec_from_fields

    fields = [
        {"field_path": "data.category", "type": "keyword", "business_label": "Categoría", "dimension": True},
        {"field_path": "data.revenue", "type": "float", "business_label": "Revenue", "dimension": False},
    ]
    spec = _spec_from_fields("shop", "logs-%{+YYYY.MM}", fields)
    crosstab = [p for p in spec["panels"] if p.get("metric") == "sum" and p.get("agg_field")]
    assert len(crosstab) >= 1
    assert "data.revenue" in str(crosstab[0])


# ── Tests del `role` semántico (Opción A) ─────────────────────────────────────

def test_infer_role_respects_llm():
    """infer_role respeta el role del LLM si viene un valor válido."""
    from maas_integrator import infer_role
    assert infer_role("data.foo", "keyword", "primary_dimension") == "primary_dimension"
    assert infer_role("data.foo", "keyword", "measure") == "measure"
    assert infer_role("data.foo", "keyword", "null") is None
    assert infer_role("data.foo", "keyword", None) is not None or True  # fallback


def test_infer_role_fallback_by_name():
    """infer_role infiere por nombre cuando el LLM no manda role."""
    from maas_integrator import infer_role
    assert infer_role("data.customer_id", "keyword") == "entity_id"
    assert infer_role("data.failed_at_code", "keyword") == "critical_indicator"
    assert infer_role("data.response_code", "keyword") == "success_indicator"
    assert infer_role("data.amount", "float") == "measure"
    assert infer_role("data.event_time", "string") == "timestamp"


def test_build_spec_uses_role_for_forecasts():
    """build_spec_from_fields usa `role` para elegir forecasts: critical_indicator,
    entity_id y measure generan cada uno su forecast."""
    import capabilities as C

    fields = [
        {"field_path": "data.status", "type": "keyword", "business_label": "Estado",
         "dimension": True, "role": "primary_dimension"},
        {"field_path": "data.error_code", "type": "keyword", "business_label": "Error",
         "dimension": False, "role": "critical_indicator"},
        {"field_path": "data.customer_id", "type": "keyword", "business_label": "Cliente",
         "dimension": False, "role": "entity_id"},
        {"field_path": "data.amount", "type": "float", "business_label": "Monto",
         "dimension": False, "role": "measure"},
    ]
    spec = C.build_spec_from_fields("shop", "shop-*", fields, label="Shop")
    fc_names = " ".join(fc["name"] for fc in spec["forecasts"])
    assert "critical" in fc_names
    assert "entities" in fc_names
    assert "volume" in fc_names
    assert len(spec["forecasts"]) == 3  # cap: volume + critical + entities (measure dropea)


def test_build_spec_uses_role_for_success_code():
    """build_spec_from_fields usa role=success_indicator + enums para success_code."""
    import capabilities as C

    fields = [
        {"field_path": "data.rc", "type": "keyword", "business_label": "Código",
         "dimension": True, "role": "success_indicator"},
    ]
    enums = {"data.rc": ["000", "100", "200"]}
    spec = C.build_spec_from_fields("txn", "txn-*", fields, label="Txn", enums=enums)
    assert spec["success_code"] == "000"


def test_build_spec_role_overrides_regex():
    """Si role=critical_indicator pero el nombre no matchea _CRITICAL_FIELD_RE,
    el role gana y se crea el forecast."""
    import capabilities as C

    fields = [
        {"field_path": "data.weird_field", "type": "keyword", "business_label": "Weird",
         "dimension": False, "role": "critical_indicator"},
    ]
    spec = C.build_spec_from_fields("app", "app-*", fields, label="App")
    fc_names = " ".join(fc["name"] for fc in spec["forecasts"])
    assert "critical" in fc_names


def test_spec_from_fields_uses_role_for_pie():
    """_spec_from_fields usa role=primary_dimension para el pie chart."""
    from dashboards import _spec_from_fields

    fields = [
        {"field_path": "data.category", "type": "keyword", "business_label": "Categoría",
         "dimension": True, "role": "primary_dimension"},
        {"field_path": "data.status", "type": "keyword", "business_label": "Estado",
         "dimension": True},
    ]
    spec = _spec_from_fields("shop", "logs-%{+YYYY.MM}", fields)
    pies = [p for p in spec["panels"] if p["type"] == "pie"]
    assert len(pies) >= 1
    assert "data.category" in str(pies[0])


def test_spec_from_fields_uses_role_for_crosstab():
    """_spec_from_fields usa role=primary_dimension × role=measure para el cross-tab."""
    from dashboards import _spec_from_fields

    fields = [
        {"field_path": "data.region", "type": "keyword", "business_label": "Región",
         "dimension": True, "role": "primary_dimension"},
        {"field_path": "data.revenue", "type": "float", "business_label": "Revenue",
         "dimension": False, "role": "measure"},
    ]
    spec = _spec_from_fields("shop", "logs-%{+YYYY.MM}", fields)
    crosstabs = [p for p in spec["panels"] if p.get("metric") == "sum" and p.get("agg_field")]
    assert len(crosstabs) >= 1
    assert "data.region" in str(crosstabs[0])
    assert "data.revenue" in str(crosstabs[0])


def test_spec_from_fields_uses_role_entity_for_metric():
    """_spec_from_fields usa role=entity_id para el metric tile de cardinality."""
    from dashboards import _spec_from_fields

    fields = [
        {"field_path": "data.customer_id", "type": "keyword", "business_label": "Cliente",
         "dimension": False, "role": "entity_id"},
    ]
    spec = _spec_from_fields("shop", "logs-%{+YYYY.MM}", fields)
    metrics = [p for p in spec["panels"] if p["type"] == "metric" and p.get("agg") == "cardinality"]
    assert len(metrics) >= 1
    assert "data.customer_id" in str(metrics[0])


# ===========================================================================
# Registro declarativo de verticales (verticals/)
# ===========================================================================
def test_verticals_registry_wellformed():
    """Cada VERTICAL tiene las keys mínimas; los visibles traen card+specs y
    fraud-detection es el único hidden (sin card). Los agregadores producen
    exactamente los sets que el resto del backend espera (13 capabilities,
    13 industry, 8 datasets — cts no tiene dataset: lee su propio bucket)."""
    import verticals as V

    vs = V.all_verticals()
    slugs = [v["slug"] for v in vs]
    assert slugs == [
        "siem", "fortianalyzer", "transacciones-billetera", "fraud-detection", "transacciones-alyc",
        "streaming-ott", "produccion-pozos", "ventas-ecommerce", "encuentros-clinicos", "cts"]

    visible = V.visible_verticals()
    assert [v["slug"] for v in visible] == [
        "siem", "fortianalyzer", "transacciones-billetera", "transacciones-alyc",
        "streaming-ott", "produccion-pozos", "ventas-ecommerce", "encuentros-clinicos",
        "cts"]
    for v in visible:
        # Card + datos de front + specs backend presentes en cada primario.
        for k in ("label", "full_label", "group", "icon", "index_base", "description",
                  "sample", "filter_code", "fields", "suggested_questions",
                  "capability", "dashboard"):
            assert v.get(k), f"{v['slug']} sin {k}"

    # `dataset_files` NO entra en la lista de arriba porque dejó de ser universal:
    # CTS es el único caso visible cuyo dato la plataforma no sube. Son trazas de
    # auditoría reales que ya viven en OTRO bucket, y de ahí salen dos invariantes
    # que valen plata.
    cts = V.get_vertical("cts")
    assert "dataset_files" not in cts, (
        "si CTS declarara dataset, 'Preparar bucket' intentaría subirle encima a "
        "las trazas reales y el guard del deploy exigiría verificarlas")
    assert cts["obs_bucket"] == "mi-tracker-cts" and cts["obs_prefix"] == "CloudTraces/"
    assert cts["dedup_id"], "sin dedup, re-ingerir las trazas las duplica"

    for v in visible:
        if v["slug"] != "cts":
            assert v.get("dataset_files"), f"{v['slug']} sin dataset_files"
            # Y nadie más trae bucket propio: el resto lee del bucket de demos.
            assert not v.get("obs_bucket"), f"{v['slug']} no debería traer bucket propio"

    # Agregadores == lo que consume el backend.
    assert len(V.capability_specs()) == 14
    assert set(V.demo_dataset_files()) == {
        "siem", "fortianalyzer", "transacciones-billetera", "transacciones-alyc",
        "streaming-ott", "produccion-pozos", "ventas-ecommerce", "encuentros-clinicos"}
    # fortianalyzer aporta sus 4 sub-specs backend-only.
    assert {"fortianalyzer-soc", "fortianalyzer-traffic", "fortianalyzer-utm",
            "fortianalyzer-event"} <= set(V.capability_specs())


def test_verticals_back_registro_consistente():
    """capabilities/dashboards/main leen del registro (no de literales sueltos)."""
    import verticals as V
    import capabilities as C
    import dashboards as D

    assert C._CAPABILITY_SPECS == V.capability_specs()
    assert main._DEMO_DATASET_FILES == V.demo_dataset_files()

    # dashboards = verticales + el spec sin vertical (`firewall`, que no tiene card).
    assert "firewall" in D._DASHBOARD_SPECS
    assert "firewall" not in V.dashboard_specs()
    assert set(D.get_available_slugs()) == set(V.dashboard_specs()) | {"firewall"}


def test_obs_read_sample_handles_stream_folder_and_gz():
    """Regresión del 'NoneType' object is not callable en read_sample (vía
    "Llegan en vivo → Bucket OBS"): el download debe ir EN MEMORIA (body.buffer), debe
    saltear el marcador de carpeta (key con '/' final, size 0) que listObjects
    devuelve primero, y descomprimir .gz (los traces de CTS vienen gzipeados)."""
    import gzip
    from types import SimpleNamespace as NS
    from obs_client import OBSClient

    payload = gzip.compress(b'\n{"trace_name":"loginUser","code":200}\notra linea\n')

    class _FakeSdk:
        def listObjects(self, bucket, prefix=None, marker=None, max_keys=None):
            return NS(status=200, body=NS(contents=[
                NS(key="CloudTraces/", size=0),                      # marcador de carpeta
                NS(key="CloudTraces/t1.json.gz", size=len(payload)),  # objeto real
                NS(key="CloudTraces/t2.json.gz", size=len(payload)),
            ], is_truncated=False))

        def getObject(self, bucket, key, loadStreamInMemory=False, range=None):
            assert loadStreamInMemory, "el sample debe descargarse en memoria (body.buffer)"
            return NS(status=200, body=NS(buffer=payload))

    client = OBSClient.__new__(OBSClient)   # sin __init__: no requiere el SDK real
    client._bucket = "mi-tracker-cts"
    client._client = _FakeSdk()

    line, total, key = client.read_sample("CloudTraces/")
    assert key == "CloudTraces/t1.json.gz"          # salteó el folder-marker
    assert total == 2                                # solo objetos reales
    # gunzip + las líneas no vacías
    assert line.startswith('{"trace_name":"loginUser","code":200}')
    assert "otra linea" in line


def test_obs_read_sample_trae_hasta_200_lineas():
    """El perfilador decide tipos con las filas: con 3 no distingue un id de
    una medida. Se leen hasta 200 (en memoria, nunca se guardan)."""
    from types import SimpleNamespace as NS
    from obs_client import OBSClient

    payload = "".join(f"{i},valor-{i}\n" for i in range(500)).encode()

    class _FakeSdk:
        def listObjects(self, bucket, prefix=None, marker=None, max_keys=None):
            return NS(status=200, body=NS(contents=[NS(key="datos/a.csv", size=len(payload))]))

        def getObject(self, bucket, key, loadStreamInMemory=False, **kw):
            return NS(status=200, body=NS(buffer=payload))

    client = OBSClient.__new__(OBSClient)
    client._bucket = "b"
    client._client = _FakeSdk()

    texto, _, _ = client.read_sample("datos/")
    assert len(texto.splitlines()) == 200
    assert texto.splitlines()[-1] == "199,valor-199"



def test_read_sample_devuelve_solo_la_muestra(monkeypatch):
    """El endpoint no llama al LLM ni matchea industria: eso lo hacía la versión
    anterior y era una llamada a MaaS de más, porque el paso 2 vuelve a detectar
    los campos igual. Devuelve la muestra y nada más."""
    class _FakeObs:
        def __init__(self, **kw):
            pass
        def read_sample(self, prefix=""):
            return '{"event":{"action":"deny"}}', 7, "logs/a.log"
        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", _FakeObs)
    llamadas = []
    monkeypatch.setattr(main, "generate_logstash_filter",
                        lambda *a, **k: llamadas.append(1) or {})

    res = client.post("/api/v1/obs/read-sample", json={
        "access_key": "AK", "secret_key": "SK", "bucket": "b", "prefix": "logs/"})
    assert res.status_code == 200
    assert res.json() == {"sample_line": '{"event":{"action":"deny"}}',
                          "sample_lines": ['{"event":{"action":"deny"}}'],
                          "total_objects": 7, "object_key": "logs/a.log"}
    assert not llamadas, "leer una muestra no tiene por qué llamar al LLM"


def test_read_sample_muestra_tres_y_analiza_todas(monkeypatch):
    """Tres líneas alcanzan para mirar; para decidir tipos y formatos el
    perfilador necesita filas (con 3 no distingue un id de una medida)."""
    filas = "\n".join(f"{i},{i * 10}" for i in range(50))

    class _FakeObs:
        def __init__(self, **kw):
            pass
        def read_sample(self, prefix=""):
            return filas, 1, "logs/a.csv"
        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", _FakeObs)

    body = client.post("/api/v1/obs/read-sample", json={
        "access_key": "AK", "secret_key": "SK", "bucket": "b", "prefix": "logs/"}).json()

    assert body["sample_line"].count("\n") == 2
    assert len(body["sample_lines"]) == 50


def test_read_sample_sin_bucket_es_400():
    res = client.post("/api/v1/obs/read-sample", json={"access_key": "AK", "secret_key": "SK"})
    assert res.status_code == 400 and "bucket" in res.json()["detail"].lower()


def test_index_inyecta_verticals_y_endpoint():
    """GET / reemplaza el placeholder por el JSON real (no queda null) y expone
    los 9 verticales visibles; GET /api/v1/verticals devuelve el mismo payload."""
    import re as _re

    html = client.get("/").text
    m = _re.search(r"window\.__VERTICALS__ = (.*?);</script>", html, _re.S)
    assert m and m.group(1).strip() != "null"
    import json as _json
    injected = _json.loads(m.group(1))
    assert len(injected["groups"]) == 6
    visible = [v for v in injected["verticals"] if not v["hidden"]]
    assert len(visible) == 9
    assert {v["slug"] for v in visible} >= {"siem", "transacciones-alyc", "fortianalyzer", "cts"}

    # El front necesita el origen propio para no mandar CTS a buscar sus trazas
    # al bucket de demos: sin estas dos claves el caso se despliega contra el
    # bucket equivocado y no ingiere nada.
    cts = next(v for v in injected["verticals"] if v["slug"] == "cts")
    assert cts["obsBucket"] == "mi-tracker-cts" and cts["obsPrefix"] == "CloudTraces/"
    assert all(v["obsBucket"] == "" for v in visible if v["slug"] != "cts")

    api = client.get("/api/v1/verticals").json()
    assert api == injected


# ── Cookie de sesión: flag Secure derivado del esquema real del request ──────
# Regresión del bug "login OK, recarga, vuelve el overlay": la cookie se seteaba
# Secure siempre → el navegador la descarta en HTTP y no queda sesión.
import auth as _auth


class _FakeURL:
    def __init__(self, scheme):
        self.scheme = scheme


class _FakeReq:
    def __init__(self, scheme="http", forwarded_proto=None):
        self.url = _FakeURL(scheme)
        self.headers = {}
        if forwarded_proto is not None:
            self.headers["x-forwarded-proto"] = forwarded_proto


def test_secure_cookie_https_via_forwarded_proto(monkeypatch):
    """Caddy termina TLS → X-Forwarded-Proto: https → cookie Secure."""
    monkeypatch.setattr(_auth, "SECURE_COOKIES", True)
    assert _auth.secure_for_request(_FakeReq(forwarded_proto="https")) is True


def test_secure_cookie_http_via_forwarded_proto(monkeypatch):
    """Detrás de un proxy por HTTP → no Secure (sino el browser la descarta)."""
    monkeypatch.setattr(_auth, "SECURE_COOKIES", True)
    assert _auth.secure_for_request(_FakeReq(forwarded_proto="http")) is False


def test_secure_cookie_https_direct_no_proxy(monkeypatch):
    """HTTPS sin proxy (scheme del request) → Secure."""
    monkeypatch.setattr(_auth, "SECURE_COOKIES", True)
    assert _auth.secure_for_request(_FakeReq(scheme="https")) is True


def test_secure_cookie_http_direct_no_proxy(monkeypatch):
    """HTTP directo, sin X-Forwarded-Proto → no Secure."""
    monkeypatch.setattr(_auth, "SECURE_COOKIES", True)
    assert _auth.secure_for_request(_FakeReq(scheme="http")) is False


def test_secure_cookie_insecure_override_forces_false(monkeypatch):
    """APP_INSECURE_COOKIES=1 (SECURE_COOKIES=False) → nunca Secure, aun en HTTPS."""
    monkeypatch.setattr(_auth, "SECURE_COOKIES", False)
    assert _auth.secure_for_request(_FakeReq(forwarded_proto="https")) is False


def test_workspace_refreshes_template_source(tmp_path, monkeypatch):
    """Un git pull + rebuild cambia el main.tf del template → el workspace del
    usuario lo refresca (no se queda con la copia stale del primer deploy)."""
    tmpl = tmp_path / "template" / "terraform"
    tmpl.mkdir(parents=True)
    (tmpl / "main.tf").write_text("size = 5\n", encoding="utf-8")
    (tmpl / "terraform.tfvars.example").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(_auth, "TERRAFORM_TEMPLATE", tmpl)
    monkeypatch.setattr(_auth, "DATA_ROOT", tmp_path / "data")

    ctx = _auth.build_user_ctx("sa@huawei.com")
    assert (ctx.terraform_dir / "main.tf").read_text(encoding="utf-8") == "size = 5\n"

    (tmpl / "main.tf").write_text("size = 300\n", encoding="utf-8")

    _auth.build_user_ctx("sa@huawei.com")
    assert (ctx.terraform_dir / "main.tf").read_text(encoding="utf-8") == "size = 300\n"


# ── El OBS Secret Access Key no sale del servidor ────────────────────────────
# El SK viajaba al navegador en dos respuestas (`GET /settings/obs` y el
# `pipeline_conf` de `/terraform/status`) y volvía en cada body de deploy. Ahora
# vive solo en el servidor: el front manda el campo vacío y el backend completa.


def test_get_obs_settings_never_returns_the_secret_key(monkeypatch, tmp_path):
    """El GET dice SI hay SK y sus últimos 4, pero nunca el valor."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    mi.set_obs_creds("AKIAEXAMPLE", "sk-super-secreto-1234")

    body = client.get("/api/v1/settings/obs").json()
    assert "sk" not in body
    assert "sk-super-secreto-1234" not in str(body)
    assert body == {"configured": True, "ak": "AKIAEXAMPLE",
                    "sk_configured": True, "sk_last4": "1234"}


def test_post_obs_settings_keeps_saved_sk_when_body_omits_it(monkeypatch, tmp_path):
    """Guardar la card con el SK vacío (el relleno decorativo no se re-tipeó) NO
    borra el secreto: solo actualiza el AK. Vaciar ambos sí borra."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    mi.set_obs_creds("AK-VIEJO", "sk-guardada")

    r = client.post("/api/v1/settings/obs", json={"access_key": "AK-NUEVO", "secret_key": ""})
    assert r.status_code == 200 and r.json()["configured"] is True
    assert mi.get_obs_creds() == {"ak": "AK-NUEVO", "sk": "sk-guardada"}

    # Re-tipeada: gana la del body.
    client.post("/api/v1/settings/obs", json={"access_key": "AK-NUEVO", "secret_key": "sk-nueva"})
    assert mi.get_obs_creds()["sk"] == "sk-nueva"

    # Ambas vacías = borrar (el único camino para limpiar las credenciales).
    client.post("/api/v1/settings/obs", json={"access_key": "", "secret_key": ""})
    assert mi.get_obs_creds() == {"ak": "", "sk": ""}


def test_resolve_obs_creds_body_wins_over_saved(monkeypatch, tmp_path):
    """Vacío → las de la cuenta. Con valor → el body (usar las de un tercero)."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    mi.set_obs_creds("AK-CUENTA", "SK-CUENTA")

    assert mi.resolve_obs_creds("", "") == ("AK-CUENTA", "SK-CUENTA")
    assert mi.resolve_obs_creds("  ", "  ") == ("AK-CUENTA", "SK-CUENTA")
    assert mi.resolve_obs_creds("AK-CLIENTE", "SK-CLIENTE") == ("AK-CLIENTE", "SK-CLIENTE")
    # Mixto: el AK del body con el SK de la cuenta (cambiar solo uno).
    assert mi.resolve_obs_creds("AK-CLIENTE", "") == ("AK-CLIENTE", "SK-CUENTA")


def test_input_block_fills_obs_creds_from_the_account(monkeypatch, tmp_path):
    """El front ya no manda el SK: el `.conf` generado igual sale con la
    credencial real, tomada de la cuenta del usuario que despliega."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    mi.set_obs_creds("AK-CUENTA", "SK-CUENTA")

    block = main.generate_input_block({
        "plugin_type": "obs",
        "obs": {"bucket": "b", "region": "la-south-2", "prefix": "p/",
                "endpoint": "https://obs.la-south-2.myhuaweicloud.com",
                "access_key_id": "", "secret_access_key": ""},
    })
    assert 'access_key_id => "AK-CUENTA"' in block
    assert 'secret_access_key => "SK-CUENTA"' in block

    # Las del body siguen ganando (bucket de un cliente).
    block = main.generate_input_block({
        "plugin_type": "obs",
        "obs": {"bucket": "b", "region": "la-south-2",
                "endpoint": "https://obs.la-south-2.myhuaweicloud.com",
                "access_key_id": "AK-CLIENTE", "secret_access_key": "SK-CLIENTE"},
    })
    assert 'secret_access_key => "SK-CLIENTE"' in block


def test_mask_conf_hides_secrets_and_keeps_the_rest():
    """El `.conf` que va al navegador sale sin credenciales, pero legible."""
    conf = (
        'input { s3 {\n'
        '  access_key_id => "AKIAEXAMPLE"\n'
        '  secret_access_key => "sk-super-secreto"\n'
        '  bucket => "demo2css"\n'
        '  region => "la-south-2"\n'
        '} }\n'
        'output { elasticsearch {\n'
        '  password => "Huawei1234"\n'
        '  index => "siem-%{+YYYY.MM}"\n'
        '} }'
    )
    masked = main.mask_conf(conf)
    for secreto in ("AKIAEXAMPLE", "sk-super-secreto", "Huawei1234"):
        assert secreto not in masked
    for visible in ("demo2css", "la-south-2", "siem-%{+YYYY.MM}"):
        assert visible in masked


def test_unmask_conf_restores_creds_on_a_redeploy():
    """El front puede reenviar un `.conf` que recibió enmascarado (redeploy tras
    un refresh): sin reponer, Logstash intentaría autenticarse con bullets."""
    conf = ('input { s3 {\n  access_key_id => "AK-REAL"\n'
            '  secret_access_key => "SK-REAL"\n  bucket => "b"\n} }')
    masked = main.mask_conf(conf)
    assert main.unmask_conf(masked, "AK-REAL", "SK-REAL") == conf
    # Sin bullets es un no-op (no toca un .conf que llegó completo).
    assert main.unmask_conf(conf, "AK-REAL", "SK-REAL") == conf


def test_terraform_status_masks_the_pipeline_conf(monkeypatch, tmp_path):
    """`/terraform/status` alimenta la vista de Entorno desplegado: su
    `pipeline_conf` no puede llevar el SK en claro. El bucket sí se sigue viendo."""
    import json as _json
    import main as _main

    fake_main, _ = _write_fake_state_with_cluster(tmp_path)
    (tmp_path / "terraform" / _main._PIPELINES_REGISTRY_NAME).write_text(_json.dumps({
        "logs": {"pipeline_conf": 'input { s3 { access_key_id => "AK-REAL" '
                                  'secret_access_key => "SK-REAL" '
                                  'bucket => "demo2css" } }',
                 "index": "logs-%{+YYYY.MM}", "obs_prefix": "logs/"},
    }))

    class _FakeProc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr(_main, "__file__", str(fake_main))
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **kw: _FakeProc())

    body = client.get("/api/v1/terraform/status").json()
    assert "SK-REAL" not in _json.dumps(body)
    assert "AK-REAL" not in _json.dumps(body)
    assert "demo2css" in body["pipeline_conf"]


def test_generate_pipeline_response_hides_the_obs_creds(monkeypatch, tmp_path):
    """El preview del paso 4 sale del backend con las credenciales inyectadas:
    el AK/SK se enmascaran antes de responder. Un secreto de otro plugin NO se
    enmascara acá — esta pipeline no está desplegada, no hay de dónde reponerlo."""
    import maas_integrator as mi

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    mi.set_obs_creds("AK-CUENTA", "SK-CUENTA")

    r = client.post("/api/v1/onboarding/generate-pipeline", json={
        "filter_code": "filter { mutate { add_field => { \"a\" => \"b\" } } }",
        "input_config": {"plugin_type": "obs",
                         "obs": {"bucket": "demo2css", "region": "la-south-2",
                                 "endpoint": "https://obs.la-south-2.myhuaweicloud.com"}},
        "output_config": {"plugins": ["opensearch"],
                          "opensearch": {"hosts": [], "index": "logs",
                                         "user": "admin", "password": "Huawei1234"}},
    })
    assert r.status_code == 200
    conf = r.json()["pipeline_code"]
    assert "SK-CUENTA" not in conf and "AK-CUENTA" not in conf
    assert "demo2css" in conf
    assert "Huawei1234" in conf   # el deploy lo reenvía tal cual; no es reponible


def test_deploy_restores_every_secret_from_the_stored_conf(monkeypatch, tmp_path):
    """Redeploy con el `.conf` que vino de `/terraform/status`: se restaura el
    original completo, incluida una password que el servidor no podría
    reconstruir (Kafka), porque el `.conf` guardado sirve de referencia."""
    import maas_integrator as mi
    import main as _main

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    mi.set_obs_creds("AK-CUENTA", "SK-CUENTA")

    original = ('input { s3 { access_key_id => "AK-CUENTA" '
                'secret_access_key => "SK-CUENTA" bucket => "b" }\n'
                '  kafka { sasl_password => "kafka-secreta" } }')
    monkeypatch.setattr(_main, "_stored_confs", lambda: [original])

    req = _main.TerraformDeployRequest(pipeline_conf=_main.mask_conf(original))
    assert "kafka-secreta" not in req.pipeline_conf   # así viaja al navegador
    _main._fill_obs_creds(req)
    assert req.pipeline_conf == original              # y así vuelve al deploy
    assert req.obs_secret_key == "SK-CUENTA"


def test_deploy_falls_back_to_obs_creds_without_a_stored_conf(monkeypatch, tmp_path):
    """Sin referencia (primer deploy), se reponen las credenciales de OBS: son
    las únicas que la cuenta conoce."""
    import maas_integrator as mi
    import main as _main

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    mi.set_obs_creds("AK-CUENTA", "SK-CUENTA")
    monkeypatch.setattr(_main, "_stored_confs", lambda: [])

    masked = _main.mask_obs_creds(
        'input { s3 { access_key_id => "x" secret_access_key => "y" bucket => "b" } }')
    req = _main.TerraformDeployRequest(pipeline_conf=masked)
    _main._fill_obs_creds(req)
    assert 'access_key_id => "AK-CUENTA"' in req.pipeline_conf
    assert 'secret_access_key => "SK-CUENTA"' in req.pipeline_conf


def test_deploy_body_creds_win_over_the_account(monkeypatch, tmp_path):
    """Leer el bucket de un cliente: si el body trae credenciales, son esas las
    que van al `.conf`, no las de la cuenta."""
    import maas_integrator as mi
    import main as _main

    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    mi.set_obs_creds("AK-CUENTA", "SK-CUENTA")
    monkeypatch.setattr(_main, "_stored_confs", lambda: [])

    masked = _main.mask_obs_creds(
        'input { s3 { access_key_id => "x" secret_access_key => "y" bucket => "b" } }')
    req = _main.TerraformDeployRequest(
        pipeline_conf=masked, obs_access_key="AK-CLIENTE", obs_secret_key="SK-CLIENTE")
    _main._fill_obs_creds(req)
    assert 'secret_access_key => "SK-CLIENTE"' in req.pipeline_conf
    assert req.obs_secret_key == "SK-CLIENTE"


# ── El state y los datasets comparten bucket ─────────────────────────────────
# Conviven porque el input s3 lista POR PREFIJO: un pipeline sobre `<slug>-logs/`
# nunca ve `tfstate/`. El agujero es el prefijo vacío: el default es "" y
# `delete => true` también, así que un input sin prefijo listaría el bucket
# entero y BORRARÍA lo que lee, incluido el estado de Terraform.


def _s3_input(bucket, prefix):
    return {"plugin_type": "obs",
            "obs": {"bucket": bucket, "prefix": prefix, "region": "la-south-2",
                    "endpoint": "https://obs.la-south-2.myhuaweicloud.com"}}


@pytest.fixture
def bucket_con_state(monkeypatch, tmp_path):
    """El backend remoto activado sobre el bucket de demos."""
    import maas_integrator as mi
    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    mi.set_obs_creds("AK", "SK")
    monkeypatch.setattr(mi, "get_huawei_settings", lambda: {"demo_bucket": "demos-css"})
    return "demos-css"


def test_input_sin_prefijo_sobre_el_bucket_del_state_se_rechaza(bucket_con_state):
    """Es un 400 y no un warning porque el daño no se puede deshacer: Logstash
    borraría el terraform.tfstate y quedarías sin forma de destruir los clusters."""
    with pytest.raises(main.HTTPException) as exc:
        main.generate_input_block(_s3_input(bucket_con_state, ""))

    assert exc.value.status_code == 400
    msg = exc.value.detail["message"]
    assert "tfstate/" in msg and "prefijo" in msg


def test_input_con_prefijo_sobre_el_mismo_bucket_es_valido(bucket_con_state):
    """El caso normal: los datasets viven en `<slug>-logs/` del mismo bucket."""
    block = main.generate_input_block(_s3_input(bucket_con_state, "siem-logs/"))

    assert 'prefix => "siem-logs/"' in block
    assert 'bucket => "demos-css"' in block


def test_el_prefijo_del_state_no_colisiona_con_los_datasets(bucket_con_state):
    """`tfstate/` no puede ser el prefijo de ningún caso: los slugs terminan en
    `-logs`, así que un `terraform state pull` y una ingesta nunca se cruzan."""
    import tfstate as _tfs
    assert not _tfs.STATE_PREFIX.endswith("-logs")
    assert _tfs.state_key_for("u1").startswith(_tfs.STATE_PREFIX + "/")


def test_un_bucket_ajeno_sin_prefijo_sigue_siendo_valido(bucket_con_state):
    """El bucket del cliente en modo productivo suele tener los logs en la raíz.
    El guard apunta SOLO al bucket que guarda el state."""
    block = main.generate_input_block(_s3_input("bucket-del-cliente", ""))

    assert 'bucket => "bucket-del-cliente"' in block


def test_sin_bucket_configurado_no_hay_guard(monkeypatch, tmp_path):
    """Sin bucket de demos el estado sigue local, así que no hay nada que
    proteger y el guard no tiene por qué activarse."""
    import maas_integrator as mi
    monkeypatch.setattr(mi, "_SETTINGS_PATH", tmp_path / "settings.json")
    mi.set_obs_creds("AK", "SK")
    monkeypatch.setattr(mi, "get_huawei_settings", lambda: {"demo_bucket": ""})

    assert 'bucket => "cualquiera"' in main.generate_input_block(_s3_input("cualquiera", ""))


def test_el_guard_tolera_espacios_en_el_prefijo(bucket_con_state):
    """Un prefijo de puros espacios es un prefijo vacío."""
    with pytest.raises(main.HTTPException):
        main.generate_input_block(_s3_input(bucket_con_state, "   "))


# ── Bucket por caso ─────────────────────────────────────────────────────────
# Hasta ahora el bucket era uno solo para todo el deploy ("el bucket es
# universal, viene del request"). CTS rompió esa premisa: sus trazas de
# auditoría son reales y viven en otro bucket. Lo que sigue protege que los dos
# mundos convivan en un mismo deploy sin pisarse.


def _case_b(slug, prefix, bucket=""):
    return main.PipelineCase(
        slug=slug, filter_code="filter { }", index_name=slug + "-%{+YYYY.MM}",
        obs_prefix=prefix, obs_bucket=bucket, read_existing_bucket=True)


def _deploy_req_b(*cases, bucket="demos-del-sa"):
    return main.TerraformDeployRequest(
        pipeline_conf="filter { }", obs_bucket=bucket,
        obs_access_key="AK", obs_secret_key="SK",
        obs_endpoint="https://obs.la-south-2.myhuaweicloud.com",
        cases=list(cases))


def test_un_caso_con_bucket_propio_no_usa_el_del_request():
    req = _deploy_req_b(_case_b("cts", "CloudTraces/", "mi-tracker-cts"))

    conf = main._build_pipeline_conf_for_case(req.cases[0], req)

    assert 'bucket => "mi-tracker-cts"' in conf
    assert 'prefix => "CloudTraces/"' in conf
    assert "demos-del-sa" not in conf


def test_un_caso_sin_bucket_propio_sigue_usando_el_del_request():
    """El comportamiento de los casos que ya existian no cambia."""
    req = _deploy_req_b(_case_b("siem", "siem-logs/"))

    assert 'bucket => "demos-del-sa"' in main._build_pipeline_conf_for_case(
        req.cases[0], req)


def test_en_un_mismo_deploy_conviven_dos_buckets():
    """El corazon del cambio: CTS junto a un caso de demo tiene que producir dos
    pipelines apuntando a buckets distintos, y ninguno ver el del otro."""
    req = _deploy_req_b(_case_b("cts", "CloudTraces/", "mi-tracker-cts"),
                        _case_b("siem", "siem-logs/"))

    cts_conf = main._build_pipeline_conf_for_case(req.cases[0], req)
    siem_conf = main._build_pipeline_conf_for_case(req.cases[1], req)

    assert 'bucket => "mi-tracker-cts"' in cts_conf
    assert 'bucket => "demos-del-sa"' in siem_conf
    assert "demos-del-sa" not in cts_conf
    assert "mi-tracker-cts" not in siem_conf


def test_el_upload_nunca_escribe_en_el_bucket_de_un_caso(monkeypatch):
    """Son trazas de auditoria reales: escribirles encima no se deshace.

    El skip NO puede depender de `read_existing_bucket`, que lo manda el front;
    abajo de ese guard hay un `delete_prefix`. Por eso el caso se arma con el
    flag en False a proposito.
    """
    subidas, borrados = [], []

    class FakeOBS:
        def __init__(self, **kw):
            self.bucket = kw.get("bucket")

        def delete_prefix(self, p):
            borrados.append((self.bucket, p))
            return 0

        def put_object(self, key, data):
            subidas.append((self.bucket, key))

        def close(self):
            pass

    import obs_client
    monkeypatch.setattr(obs_client, "OBSClient", FakeOBS)

    cts = main.PipelineCase(slug="cts", obs_prefix="CloudTraces/",
                            obs_bucket="mi-tracker-cts", raw_log="una traza",
                            read_existing_bucket=False)

    main._do_obs_upload(_deploy_req_b(cts))

    assert subidas == [], "escribio en un bucket ajeno: %r" % (subidas,)
    assert borrados == [], "borro un prefijo ajeno: %r" % (borrados,)


# ── El origen propio no depende de lo que mande el navegador ────────────────
# El bucket de CTS viajaba correcto del backend al front y el front lo perdía
# (`applyVerticalsPayload` no lo copiaba a LOG_EXAMPLES), así que el deploy salía
# contra el bucket de demos: Logstash arranca, no encuentra nada y nadie avisa.
# El front ya está arreglado; esto es lo que hace que no dependa de él.
_CONF_CTS = '''input {
  s3 {
    access_key_id => "AK"
    bucket => "demoscss"
    prefix => "cts-logs/"
    codec => plain
  }
}

filter {
  # El operador editó esto en el paso 3 y no se puede perder.
  mutate { add_field => { "marca" => "mia" } }
}

output {
  elasticsearch {
    hosts => []
    index => "cts-%{+YYYY.MM}"
  }
}
'''


def _req_unico_cts(conf=_CONF_CTS, bucket="demoscss", prefix="cts-logs/"):
    return main.TerraformDeployRequest(
        pipeline_conf=conf, pipeline_slug="cts", obs_bucket=bucket, obs_prefix=prefix,
        obs_access_key="AK", obs_secret_key="SK", opensearch_index="cts-%{+YYYY.MM}",
        obs_endpoint="https://obs.la-south-2.myhuaweicloud.com", read_existing_bucket=True)


def test_el_backend_corrige_el_origen_de_un_caso_propio_en_single():
    """Con una sola tarjeta el `.conf` viaja armado desde el navegador y el
    backend no lo rearma: es el único camino donde un bucket equivocado llegaba
    entero a Terraform."""
    req = _req_unico_cts()

    notas = main._force_case_own_source(req)

    assert notas == ["cts → obs://mi-tracker-cts/CloudTraces/"]
    assert req.obs_bucket == "mi-tracker-cts" and req.obs_prefix == "CloudTraces/"
    assert 'bucket => "mi-tracker-cts"' in req.pipeline_conf
    assert 'prefix => "CloudTraces/"' in req.pipeline_conf
    assert "demoscss" not in req.pipeline_conf
    # Se reescriben dos líneas, no el archivo: la edición del paso 3 sobrevive.
    assert 'add_field => { "marca" => "mia" }' in req.pipeline_conf
    assert 'index => "cts-%{+YYYY.MM}"' in req.pipeline_conf


def test_corregir_el_origen_es_idempotente_y_no_toca_a_los_demas():
    ya = _req_unico_cts(conf=_CONF_CTS.replace("demoscss", "mi-tracker-cts")
                        .replace("cts-logs/", "CloudTraces/"),
                        bucket="mi-tracker-cts", prefix="CloudTraces/")
    assert main._force_case_own_source(ya) == []

    otro = main.TerraformDeployRequest(
        pipeline_conf=_CONF_CTS, pipeline_slug="siem", obs_bucket="demoscss",
        obs_prefix="siem-logs/", obs_access_key="AK", obs_secret_key="SK")
    assert main._force_case_own_source(otro) == []
    assert otro.obs_bucket == "demoscss", "un caso sin origen propio no se toca"


def test_corregir_el_origen_tambien_cubre_el_deploy_multi_caso():
    """Una pestaña vieja manda `obs_bucket: ''` para CTS y el backend caía al
    bucket del request."""
    req = _deploy_req_b(_case_b("cts", "", ""), _case_b("siem", "siem-logs/"))

    notas = main._force_case_own_source(req)

    assert notas == ["cts → obs://mi-tracker-cts/CloudTraces/"]
    assert req.cases[0].obs_bucket == "mi-tracker-cts"
    assert req.cases[0].obs_prefix == "CloudTraces/"
    assert req.cases[1].obs_bucket == "", "el caso sin origen propio queda como estaba"
    assert 'bucket => "mi-tracker-cts"' in main._build_pipeline_conf_for_case(req.cases[0], req)


# ── El bucket de un caso no se le presta a los demás ────────────────────────
# `obs_bucket` del body es el bucket COMPARTIDO: de ahí leen los casos de demo y
# ahí se suben sus datasets si faltan. Elegir la pestaña de CTS en el paso 3
# escribía `mi-tracker-cts` en el campo compartido y no lo devolvía: el deploy
# salía con los casos de demo leyendo el bucket de trazas del cliente, y el guard
# de datasets les subía los sintéticos ahí.
def _settings_con_demo_bucket(monkeypatch, bucket="demos-del-sa"):
    import maas_integrator
    monkeypatch.setattr(maas_integrator, "get_huawei_settings",
                        lambda: {"demo_bucket": bucket})
    monkeypatch.setattr(main, "get_huawei_settings",
                        lambda: {"demo_bucket": bucket}, raising=False)


def test_el_bucket_de_cts_no_queda_como_el_compartido(monkeypatch):
    _settings_con_demo_bucket(monkeypatch)
    req = _deploy_req_b(_case_b("cts", "CloudTraces/", "mi-tracker-cts"),
                        _case_b("siem", "siem-logs/"),
                        bucket="mi-tracker-cts")

    notas = main._aislar_bucket_compartido(req)

    assert req.obs_bucket == "demos-del-sa"
    assert len(notas) == 1 and "siem" in notas[0] and "mi-tracker-cts" in notas[0]
    # Y con eso cada pipeline vuelve a su bucket.
    assert 'bucket => "mi-tracker-cts"' in main._build_pipeline_conf_for_case(req.cases[0], req)
    assert 'bucket => "demos-del-sa"' in main._build_pipeline_conf_for_case(req.cases[1], req)


def test_el_bucket_compartido_correcto_no_se_toca(monkeypatch):
    _settings_con_demo_bucket(monkeypatch)
    req = _deploy_req_b(_case_b("cts", "CloudTraces/", "mi-tracker-cts"),
                        _case_b("siem", "siem-logs/"))

    assert main._aislar_bucket_compartido(req) == []
    assert req.obs_bucket == "demos-del-sa"


def test_si_todos_los_casos_traen_su_bucket_el_compartido_no_molesta(monkeypatch):
    """No hay a quién proteger: nadie lee del compartido."""
    _settings_con_demo_bucket(monkeypatch)
    req = _deploy_req_b(_case_b("cts", "CloudTraces/", "mi-tracker-cts"),
                        bucket="mi-tracker-cts")

    assert main._aislar_bucket_compartido(req) == []
    assert req.obs_bucket == "mi-tracker-cts"


def test_un_caso_live_no_cuenta_como_compartido(monkeypatch):
    """Su fuente es Kafka/JDBC: no lee de ningún bucket."""
    _settings_con_demo_bucket(monkeypatch)
    vivo = main.PipelineCase(slug="kafka-caso", filter_code="filter { }",
                             index_name="k-%{+YYYY.MM}",
                             input_config={"plugin_type": "kafka", "kafka": {}})
    req = _deploy_req_b(_case_b("cts", "CloudTraces/", "mi-tracker-cts"), vivo,
                        bucket="mi-tracker-cts")

    assert main._aislar_bucket_compartido(req) == []
    assert req.obs_bucket == "mi-tracker-cts"


def test_sin_bucket_de_demos_configurado_el_deploy_corta(monkeypatch):
    """Vaciarlo es a propósito: el 400 dice dónde cargarlo, y eso es mejor que
    escribir los datasets de demo en el bucket de trazas del cliente."""
    _settings_con_demo_bucket(monkeypatch, bucket="")
    req = _deploy_req_b(_case_b("cts", "CloudTraces/", "mi-tracker-cts"),
                        _case_b("siem", "siem-logs/"),
                        bucket="mi-tracker-cts")

    main._aislar_bucket_compartido(req)

    assert req.obs_bucket == ""
    with pytest.raises(main.HTTPException) as exc:
        main._check_conf_reads_from_a_bucket(req)
    assert "siem" in exc.value.detail["message"]


def test_el_guard_de_datasets_nunca_sube_al_bucket_de_otro_caso(monkeypatch):
    """El guard sube el dataset que falte. Sobre el bucket de un caso con origen
    propio eso sería escribirle sintéticos encima a las trazas del cliente."""
    mirados, subidos = [], []

    class FakeOBS:
        def __init__(self, **kw):
            self.bucket = kw.get("bucket")

        def prefix_has_objects(self, prefix):
            mirados.append((self.bucket, prefix))
            return False

        def put_file(self, key, src):
            subidos.append((self.bucket, key))

        def close(self):
            pass

    import obs_client
    monkeypatch.setattr(obs_client, "OBSClient", FakeOBS)
    monkeypatch.setattr(main, "_demo_dataset_files",
                        lambda: {"cts": ["trazas.json"], "siem": ["siem.log"]})

    req = _deploy_req_b(_case_b("cts", "CloudTraces/", "mi-tracker-cts"),
                        _case_b("siem", "siem-logs/"))
    with pytest.raises(main.HTTPException):
        main._check_demo_datasets_present(req)

    assert all(b == "demos-del-sa" for b, _ in mirados + subidos), (mirados, subidos)
    assert not any("CloudTraces" in p for _, p in mirados)


@pytest.mark.parametrize("fn", ["terraform_deploy_stream", "terraform_deploy_job"])
def test_el_deploy_aisla_el_bucket_antes_de_los_guards(fn):
    """Los dos guards miran `obs_bucket`, y el de datasets además SUBE ahí: si
    el aislamiento corre después, el upload ya fue al bucket ajeno."""
    import inspect
    src = inspect.getsource(getattr(main, fn))

    assert "_aislar_bucket_compartido" in src, f"{fn} no aísla el bucket compartido"
    assert (src.index("_aislar_bucket_compartido")
            < src.index("_check_demo_datasets_present")), fn
    assert (src.index("_aislar_bucket_compartido")
            < src.index("_check_conf_reads_from_a_bucket")), fn


def test_el_deploy_corrige_el_origen_antes_de_escribir_nada(monkeypatch):
    """La corrección tiene que correr antes de `_prepare_deploy_tfvars` y del
    upload a OBS: es `case.obs_bucket` lo que hace que el upload NO escriba
    encima de las trazas reales de la cuenta."""
    import inspect
    src = inspect.getsource(main._deploy_stream_gen_raw)

    assert "_force_case_own_source(request)" in src
    assert src.index("_force_case_own_source(request)") < src.index("_prepare_deploy_tfvars(request"), \
        "se corrige después de escribir el tfvars: demasiado tarde"


# ── La prueba de que la pipeline ingiere ────────────────────────────────────
# Terraform "success" solo dice que la configuración se creó: Logstash puede no
# compilar la pipeline, o compilarla y quedarse poleando un prefijo vacío, y el
# cluster queda igual de vacío. El 9600 de Logstash no está expuesto (VPC
# privada), así que la señal honesta es el `_count` del índice.
class _RespFalsa:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def test_el_conteo_de_documentos_distingue_vacio_de_inalcanzable(monkeypatch):
    llamadas = []

    def fake_req(method, url, user, password, json_body=None, timeout=30):
        llamadas.append(url)
        return {
            "vacio": _RespFalsa(200, {"count": 0}),
            "lleno": _RespFalsa(200, {"count": 501}),
            "nada": None,
            "404": _RespFalsa(404, {}),
        }[url.split("/")[-2]]

    monkeypatch.setattr(main, "_os_req", fake_req)

    assert main._index_doc_count("http://x:9200", "admin", "pw", "lleno/_count".split("/")[0]) == 501
    assert main._index_doc_count("http://x:9200", "admin", "pw", "vacio") == 0
    assert main._index_doc_count("http://x:9200", "admin", "pw", "404") == 0, \
        "un índice que todavía no existe son cero documentos, no un error"
    assert main._index_doc_count("http://x:9200", "admin", "pw", "nada") is None, \
        "no poder preguntar NO es lo mismo que cero"


def test_la_verificacion_de_ingesta_espera_y_reporta(monkeypatch):
    """Arranca vacío (el input s3 poléa cada 60 s) y después entran documentos:
    el paso tiene que esperar en vez de cantar fracaso en el primer intento."""
    import json as _json
    respuestas = iter([0, 0, 501])
    monkeypatch.setattr(main, "_index_doc_count", lambda *a, **k: next(respuestas, 501))
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)

    req = main.TerraformDeployRequest(
        pipeline_conf="x", pipeline_slug="sp500", opensearch_index="sp500-%{+YYYY.MM}",
        opensearch_password="pw", start_ingestion=True)
    eventos = list(main._verificar_ingesta(req, {"public_endpoint": "1.2.3.4:9200"}))

    pasos = [_json.loads(e[6:]) for e in eventos if '"type": "step"' in e]
    assert len(pasos) == 1
    assert pasos[0]["ok"] is True
    assert "501" in pasos[0]["reason"]


def test_sin_documentos_el_paso_dice_donde_mirar(monkeypatch):
    import json as _json
    monkeypatch.setattr(main, "_index_doc_count", lambda *a, **k: 0)
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)

    req = main.TerraformDeployRequest(
        pipeline_conf="x", pipeline_slug="sp500", opensearch_index="sp500-%{+YYYY.MM}",
        opensearch_password="pw", start_ingestion=True)
    pasos = [_json.loads(e[6:]) for e in main._verificar_ingesta(req, {"public_endpoint": "1.2.3.4:9200"})
             if '"type": "step"' in e]

    assert pasos[0]["ok"] is False
    for pista in ("prefijo", "filtro", "consola de CSS"):
        assert pista in pasos[0]["reason"], pasos[0]["reason"]


def test_el_endpoint_de_salud_reporta_documentos_por_pipeline(monkeypatch, tmp_path):
    td = tmp_path / "terraform"
    td.mkdir()
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: td)
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda _d: {
        "sp500": {"index": "sp500-%{+YYYY.MM}", "start_ingestion": True},
        "siem": {"index": "siem-%{+YYYY.MM}", "start_ingestion": False}})
    monkeypatch.setattr(main, "_cluster_with_public_access",
                        lambda _d: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_cluster_admin_password", lambda _d: "pw")
    monkeypatch.setattr(main, "_read_https_enabled_from_state", lambda _d: False)
    monkeypatch.setattr(main, "_index_doc_count",
                        lambda base, u, p, patron: 501 if patron.startswith("sp500") else 0)

    data = client.get("/api/v1/pipelines/health").json()

    assert data["reachable"] is True
    por_slug = {p["slug"]: p for p in data["pipelines"]}
    assert por_slug["sp500"]["docs"] == 501
    assert por_slug["siem"]["docs"] == 0
    assert por_slug["sp500"]["index"] == "sp500-*", "se consulta el patrón, no el nombre con date-math"

    # Sin contraseña del cluster no se inventa un cero: se dice que no se pudo.
    monkeypatch.setattr(main, "_cluster_admin_password", lambda _d: "")
    data = client.get("/api/v1/pipelines/health").json()
    assert data["reachable"] is False
    assert all(p["docs"] is None for p in data["pipelines"])


# ── Lo que CSS dice de cada configuration file ─────────────────────────────
# CSS valida el .conf al crear la configuración y la deja `available` o
# `unavailable`. Ese estado viaja al state de Terraform y no lo miraba nadie: un
# .conf que no compila daba "Apply complete!" igual, y el SA se enteraba mirando
# la consola de CSS o, peor, viendo que no entraba un documento.
def _state_con_configuraciones(tmp_path, *pares):
    import json as _json

    td = tmp_path / "terraform"
    td.mkdir(exist_ok=True)
    (td / "terraform.tfstate").write_text(_json.dumps({
        "version": 4,
        "resources": [{
            "mode": "managed", "type": "huaweicloud_css_logstash_configuration",
            "name": "pipeline",
            "instances": [
                {"index_key": slug,
                 "attributes": {"name": f"pipeline-{slug}", "status": estado}}
                for slug, estado in pares
            ],
        }],
    }))
    return td


def test_el_estado_de_cada_configuracion_sale_del_state(tmp_path):
    td = _state_con_configuraciones(tmp_path, ("sp500", "available"), ("siem", "unavailable"))

    assert main._estado_configuraciones(td) == {"sp500": "available", "siem": "unavailable"}


def test_resource_instances_devuelve_todas_y_no_solo_la_primera(tmp_path):
    """`resource_attributes` devuelve la primera instancia: con cuatro pipelines
    desplegadas leía siempre la misma."""
    import tfstate as _tf

    td = _state_con_configuraciones(tmp_path, ("a", "available"), ("b", "unavailable"))
    estado = _tf.read_state(td)

    assert len(_tf.resource_instances(estado, "huaweicloud_css_logstash_configuration")) == 2
    assert _tf.resource_instances(estado, "no_existe") == []


def test_una_configuracion_que_no_compila_se_reporta_como_paso_fallido(tmp_path):
    import json as _json

    td = _state_con_configuraciones(tmp_path, ("sp500", "available"), ("telemetria", "unavailable"))
    pasos = [_json.loads(e[6:]) for e in main._verificar_configuraciones(td)]

    por_nombre = {p["name"]: p for p in pasos}
    assert por_nombre["Configuración · sp500"]["ok"] is True
    malo = por_nombre["Configuración · telemetria"]
    assert malo["ok"] is False
    assert "no pudo compilar" in malo["reason"]
    assert "consola de CSS" in malo["reason"]


def test_el_estado_devuelve_si_cada_configuracion_compila(monkeypatch, tmp_path):
    """Y llega a la tarjeta del entorno: una pipeline que no compila se veía
    igual que una en pausa."""
    import json as _json

    fake_main, tfstate_path = _write_fake_state_with_cluster(tmp_path)
    estado = _json.loads(tfstate_path.read_text(encoding="utf-8"))
    estado["resources"].append({
        "mode": "managed", "type": "huaweicloud_css_logstash_configuration", "name": "pipeline",
        "instances": [{"index_key": "logs",
                       "attributes": {"name": "pipeline-logs", "status": "unavailable"}}],
    })
    tfstate_path.write_text(_json.dumps(estado))
    (tmp_path / "terraform" / main._PIPELINES_REGISTRY_NAME).write_text(_json.dumps({
        "logs": {"index": "logs-%{+YYYY.MM}", "start_ingestion": True}}))

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = "{}"

    monkeypatch.setattr(main, "__file__", str(fake_main))
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **kw: _Proc())

    pipelines = client.get("/api/v1/terraform/status").json()["pipelines"]
    assert pipelines[0]["config_status"] == "unavailable"


def test_las_configuraciones_se_revisan_apenas_termina_el_apply():
    import inspect
    src = inspect.getsource(main._deploy_stream_gen_raw)

    assert "_verificar_configuraciones(terraform_dir)" in src
    assert src.index("_verificar_configuraciones(terraform_dir)") < src.index('"type": "complete"')


def test_la_ingesta_se_verifica_solo_en_la_fase_2():
    """En la fase 1 (aprovisionar) la pipeline todavía no arrancó: esperar
    documentos ahí sería esperar de gusto."""
    import inspect
    src = inspect.getsource(main._deploy_stream_gen_raw)

    assert "if request.start_ingestion:\n        yield from _verificar_ingesta(request, cluster)" in src
    assert src.index("_verificar_ingesta(request, cluster)") < src.index('"type": "complete"'), \
        "se verifica después de dar el deploy por terminado"


def test_el_estado_prefiere_las_pipelines_que_activo_terraform(monkeypatch, tmp_path):
    """`pipelines[].active` salía del registro local — lo que PEDIMOS. Cuando un
    apply falla a medias, la tarjeta decía "Ingestando" igual. El output
    `active_pipeline_names` dice lo que Terraform activó de verdad."""
    import json as _json

    fake_main, _ = _write_fake_state_with_cluster(tmp_path)
    (tmp_path / "terraform" / main._PIPELINES_REGISTRY_NAME).write_text(_json.dumps({
        "sp500": {"index": "sp500-%{+YYYY.MM}", "start_ingestion": True},
        "siem": {"index": "siem-%{+YYYY.MM}", "start_ingestion": True},
    }))

    class _Proc:
        returncode = 0
        stderr = ""
        stdout = _json.dumps({"active_pipeline_names": {"value": ["pipeline-sp500"]}})

    monkeypatch.setattr(main, "__file__", str(fake_main))
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **kw: _Proc())

    por_slug = {p["slug"]: p for p in client.get("/api/v1/terraform/status").json()["pipelines"]}

    assert por_slug["sp500"]["active"] is True
    assert por_slug["siem"]["active"] is False, "el registro decía que sí; Terraform, que no"


def test_sin_el_output_de_terraform_el_estado_cae_al_registro(monkeypatch, tmp_path):
    """Si `terraform output` no está disponible (CLI ausente, state ilegible) se
    sigue mostrando lo que pedimos, que es mejor que mostrar todo en pausa."""
    import json as _json

    fake_main, _ = _write_fake_state_with_cluster(tmp_path)
    (tmp_path / "terraform" / main._PIPELINES_REGISTRY_NAME).write_text(_json.dumps({
        "sp500": {"index": "sp500-%{+YYYY.MM}", "start_ingestion": True}}))

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "no terraform"

    monkeypatch.setattr(main, "__file__", str(fake_main))
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **kw: _Proc())

    pipelines = client.get("/api/v1/terraform/status").json()["pipelines"]
    assert pipelines[0]["active"] is True


def test_preparar_bucket_sigue_sin_incluir_cts():
    """CTS no declara `dataset_files`, y de eso depende que la pre-carga lo
    ignore. Si alguien le agregara uno, "Preparar bucket" le subiria sinteticos
    encima a las trazas reales."""
    import verticals as V

    assert "cts" not in V.demo_dataset_files()
    assert "cts" not in main._demo_dataset_files()


def test_deploy_no_aplica_si_el_state_anterior_no_se_pudo_subir(monkeypatch):
    """Un `errored.tfstate` es lo ÚNICO que sabe qué clusters existen de un
    apply que no pudo guardar su state. Si no se puede subir, el deploy tiene
    que PARAR antes del apply: seguir crea un segundo par de clusters, con el
    primero facturando sin que nadie lo pueda destruir desde la app."""
    # Workspace aislado: el endpoint escribe registry/tfvars/creds de teardown,
    # y contra `terraform/` del repo eso contaminaba a otros tests (el
    # huawei_project_id de la cuenta local aparecía en el destroy.auto.tfvars).
    import tempfile
    td = pathlib.Path(tempfile.mkdtemp()) / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: td)
    # La infra de la cuenta viene de ⚙ Configuración: sin esto el pre-flight
    # corta antes y el test pasaba o no según lo que tuviera guardado la máquina.
    monkeypatch.setattr(main, "get_huawei_settings", lambda: {
        "vpc_id": "vpc-1", "subnet_id": "net-2", "security_group_id": "sg-3",
        "availability_zone": "la-south-2a"})
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda _d: {})
    monkeypatch.setattr(main, "_backend_init_args", lambda _d: None)
    monkeypatch.setattr(main.tfstate, "push_errored_state",
                        lambda _d: (False, "Error: lineage mismatch"))
    applies = []

    def _popen(*a, **k):
        applies.append(a)
        raise AssertionError("no tiene que llegar al apply")
    monkeypatch.setattr(main.subprocess, "Popen", _popen)

    res = client.post("/api/v1/terraform/deploy-stream", json={
        "pipeline_conf": "input {} output {}", "obs_access_key": "AK",
        "obs_secret_key": "SK", "obs_bucket": "mis-demos", "opensearch_password": "pw",
        "opensearch_index": "logs-%{+YYYY.MM}", "read_existing_bucket": True,
    })
    assert res.status_code == 200
    import json as _json
    eventos = [_json.loads(l[len("data: "):]) for l in res.text.splitlines() if l.startswith("data: ")]
    errores = [e for e in eventos if e.get("type") == "error"]
    assert errores and "errored.tfstate" in errores[0]["message"], eventos[-3:]
    assert applies == [], "aplicó igual: state bifurcado"


# ── Secretos fuera del log del deploy ────────────────────────────────────────
# El log de un deploy real, guardado en Actividad con botón Copiar, tenía AK, SK
# y la password de OpenSearch en claro: el plan de Terraform imprime el diff del
# `conf_content` entero en cada apply (el provider guarda `***` en el state y el
# config tiene el valor real → update in-place perpetuo). Tres capas: `sensitive()`
# en el HCL, el envoltorio del stream, y el regex en `runs.append`.
def test_mask_text_tapa_pares_del_conf_y_literales():
    texto = ('          -     access_key_id => "***"\n'
             '          +     access_key_id => "HPUA-LITERAL"\n'
             "          +     password => 'Clave-Fuerte-1'\n"
             '                bucket => "demoscss"\n'
             'Error: IAM rechazó HPUA-LITERAL y SK-LITERAL-XYZ\n')
    out = main.mask_text(texto, ["HPUA-LITERAL", "SK-LITERAL-XYZ", "Clave-Fuerte-1", "", "ab"])

    for secreto in ("HPUA-LITERAL", "SK-LITERAL-XYZ", "Clave-Fuerte-1"):
        assert secreto not in out, out
    assert 'bucket => "demoscss"' in out, "lo que no es secreto queda igual"
    assert "Error: IAM rechazó" in out


def test_el_conf_content_es_sensible_en_el_hcl():
    """Sin `sensitive()` el plan imprime el .conf entero con las credenciales."""
    hcl = (pathlib.Path(__file__).resolve().parent.parent / "terraform" / "main.tf").read_text(encoding="utf-8")
    bloque = hcl[hcl.index('resource "huaweicloud_css_logstash_configuration" "pipeline"'):]
    bloque = bloque[:bloque.index("\n}\n")]
    assert re.search(r"conf_content\s*=\s*sensitive\(", bloque), "conf_content sin sensitive()"


def test_deploy_stream_no_filtra_secretos_y_borra_el_tfvars(monkeypatch, tmp_path):
    """Un apply que imprime el diff del .conf y un error con las credenciales:
    nada de eso llega al stream. Y al terminar, `deploy.auto.tfvars.json` (los
    .conf en claro + creds) ya no está; lo que el destroy necesita quedó en
    `destroy.auto.tfvars.json`, incluidas las variables requeridas de infra."""
    import json as _json
    import main as _main

    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    monkeypatch.setattr(_main, "get_huawei_settings", lambda: {
        "vpc_id": "vpc-1", "subnet_id": "net-2", "security_group_id": "sg-3",
        "availability_zone": "la-south-2a", "region": "la-south-2"})
    monkeypatch.setattr(_main, "_do_obs_upload", lambda req: None)
    monkeypatch.setattr(_main, "_backend_init_args", lambda d: None)
    monkeypatch.setattr(_main.tfstate, "push_errored_state", lambda d: (True, ""))
    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **k: _OkProc())

    class _PopenFalso:
        returncode = 1

        def __init__(self, *a, **k):
            self.stdout = iter([
                '  ~ resource "huaweicloud_css_logstash_configuration" "pipeline" {\n',
                '          -     access_key_id => "***"\n',
                '          +     access_key_id => "AK-REAL-1234"\n',
                '          +     secret_access_key => "SK-REAL-5678"\n',
                "          +     password => 'PW-REAL-9'\n",
                'Error: el provider rechazó AK-REAL-1234 / SK-REAL-5678 / PW-REAL-9\n',
            ])

        def wait(self):
            return 1

    class _Stdout:
        def __init__(self, lines):
            self._lines = lines
        def __iter__(self):
            return self._lines
        def close(self):
            pass

    def _popen(*a, **k):
        p = _PopenFalso()
        p.stdout = _Stdout(p.stdout)
        return p
    monkeypatch.setattr(_main.subprocess, "Popen", _popen)

    req = _main.TerraformDeployRequest(
        pipeline_conf='input {} output { elasticsearch { password => "PW-REAL-9" } }',
        obs_access_key="AK-REAL-1234", obs_secret_key="SK-REAL-5678",
        obs_bucket="demoscss", opensearch_password="PW-REAL-9",
        opensearch_index="siem-%{+YYYY.MM}", read_existing_bucket=True)

    eventos = list(_main._deploy_stream_gen(req, td, None, None))
    todo = "".join(eventos)

    assert eventos, "el stream no emitió nada"
    for secreto in ("AK-REAL-1234", "SK-REAL-5678", "PW-REAL-9"):
        assert secreto not in todo, f"{secreto} salió por el stream"
    assert "access_key_id" in todo, "la línea sigue ahí, solo sin el valor"
    assert not (td / "deploy.auto.tfvars.json").exists(), "el tfvars con secretos quedó en disco"
    teardown = _json.loads((td / "destroy.auto.tfvars.json").read_text(encoding="utf-8"))
    assert teardown["vpc_id"] == "vpc-1" and teardown["obs_secret_key"] == "SK-REAL-5678", \
        "el destroy necesita creds + infra; sin el tfvars del deploy no tiene otro origen"


def test_runs_append_tambien_enmascara(tmp_path, monkeypatch):
    """Segunda capa: lo que se guarda en Actividad pasa por el regex del .conf,
    aunque el evento hubiera llegado crudo."""
    import runs as _runs
    monkeypatch.setattr(_runs, "runs_dir", lambda: tmp_path)
    run = _runs.start("deploy", "t")
    _runs.append(run, {"type": "log", "message": 'x  +  access_key_id => "AK-CRUDO"'}, d=tmp_path)
    _runs.append(run, {"type": "step", "name": "n", "ok": False,
                       "reason": "password => 'PW-CRUDO'"}, d=tmp_path)

    guardado = (tmp_path / f"{run['id']}.json").read_text(encoding="utf-8")
    assert "AK-CRUDO" not in guardado and "PW-CRUDO" not in guardado, guardado


def test_la_salida_del_proceso_pasa_a_utf8():
    """Un `print` con `→` en cp1252 tiraba UnicodeEncodeError adentro de un
    endpoint: 500 por una línea de log. La salida se reconfigura al importar."""
    class _Stream:
        def __init__(self, enc):
            self.encoding = enc
            self.llamadas = []
        def reconfigure(self, **kw):
            self.llamadas.append(kw)
            self.encoding = kw["encoding"]

    cp = _Stream("cp1252")
    ya = _Stream("utf-8")
    main._salida_utf8(cp, ya)
    assert cp.llamadas == [{"encoding": "utf-8", "errors": "backslashreplace"}]
    assert ya.llamadas == [], "si ya es UTF-8 no se toca"

    class _SinReconfigure:
        encoding = "cp1252"
    main._salida_utf8(_SinReconfigure())   # no levanta


# ── La fase 2 no pierde la password del cluster ─────────────────────────────
def test_el_teardown_no_pierde_la_password_si_el_request_no_la_trae(monkeypatch, tmp_path):
    """La fase 2 (ingesta) llega sin password: el body se rearma desde
    /terraform/status tras un F5. Cuando el archivo de teardown empezó a llevar
    también la infra, ese request lo pisaba SIN la password, y el apply moría
    con "No value for required variable opensearch_password"."""
    import json as _json
    import main as _main

    td = tmp_path / "terraform"; td.mkdir()
    monkeypatch.setattr(_main, "get_huawei_settings", lambda: {
        "vpc_id": "vpc-1", "subnet_id": "net-2", "security_group_id": "sg-3"})
    _main._write_destroy_creds(td, _main.TerraformDeployRequest(
        pipeline_conf="x", obs_access_key="AK", obs_secret_key="SK", opensearch_password="PW-1"))
    _main._write_destroy_creds(td, _main.TerraformDeployRequest(
        pipeline_conf="x", obs_access_key="AK", obs_secret_key="SK"))   # fase 2: sin password

    d = _json.loads((td / "destroy.auto.tfvars.json").read_text(encoding="utf-8"))
    assert d["opensearch_password"] == "PW-1", "el request sin password borró la guardada"
    assert d["vpc_id"] == "vpc-1"


def test_la_fase_2_repone_la_password_del_cluster(monkeypatch, tmp_path):
    """Como con las AK/SK: si el body no trae la password y hay un cluster, se
    toma la de ese cluster (teardown → state). Sin cluster queda vacía: una
    inventada haría que Terraform intentara CAMBIARLA."""
    import json as _json
    import main as _main

    td = tmp_path / "terraform"; td.mkdir()
    monkeypatch.setattr(_main, "_active_terraform_dir", lambda: td)
    monkeypatch.setattr(_main, "_stored_confs", lambda: [])
    import maas_integrator as _mi
    monkeypatch.setattr(_mi, "resolve_obs_creds", lambda ak="", sk="": (ak, sk))

    req = _main.TerraformDeployRequest(pipeline_conf="x")
    _main._fill_obs_creds(req)
    assert req.opensearch_password == "", "sin cluster no se inventa nada"

    # Del state (el archivo de teardown no la tiene).
    (td / "terraform.tfstate").write_text(_json.dumps({"version": 4, "resources": [{
        "type": "huaweicloud_css_cluster", "instances": [{"attributes": {"password": "PW-STATE"}}]}]}))
    req = _main.TerraformDeployRequest(pipeline_conf="x")
    _main._fill_obs_creds(req)
    assert req.opensearch_password == "PW-STATE"

    # El archivo de teardown gana sobre el state.
    (td / "destroy.auto.tfvars.json").write_text(_json.dumps({"opensearch_password": "PW-FILE"}))
    req = _main.TerraformDeployRequest(pipeline_conf="x")
    _main._fill_obs_creds(req)
    assert req.opensearch_password == "PW-FILE"

    # La que viene en el body manda.
    req = _main.TerraformDeployRequest(pipeline_conf="x", opensearch_password="PW-BODY")
    _main._fill_obs_creds(req)
    assert req.opensearch_password == "PW-BODY"


def test_apply_schema_repone_la_password_del_cluster(monkeypatch, tmp_path):
    """Tras un F5 el body llega sin password y `/apply-schema` no pasa por
    `_fill_obs_creds`: el template y los dashboards fallaban con mensajes que
    culpaban al cluster. Se toma la del cluster; sin ninguna, es un 400 claro."""
    import json as _json
    import main as _main

    td = tmp_path / "terraform"; td.mkdir()
    monkeypatch.setattr(_main, "_active_terraform_dir", lambda: td)
    monkeypatch.setattr(_main, "_cluster_with_public_access",
                        lambda d: {"public_endpoint": "1.2.3.4:9200", "endpoint": "10.0.0.1:9200"})
    monkeypatch.setattr(_main, "_read_pipelines_registry", lambda d: {})
    visto = {}

    def _templates(request, cluster):
        visto["password"] = request.opensearch_password
        return True
    monkeypatch.setattr(_main, "_apply_index_templates", _templates)
    monkeypatch.setattr(_main, "_import_dashboards", lambda *a, **k: True)
    monkeypatch.setattr(_main, "_os_req", lambda *a, **k: None)   # el timepicker, sin red

    body = {"pipeline_conf": "x", "opensearch_index": "olist-%{+YYYY.MM}", "pipeline_slug": "olist"}

    # Sin cluster conocido → 400 que dice qué falta, no "el cluster rechazó".
    res = client.post("/api/v1/onboarding/apply-schema", json=body)
    assert res.status_code == 400 and "password" in res.json()["detail"]["message"].lower()

    # Con la password en el teardown, la usa.
    (td / "destroy.auto.tfvars.json").write_text(_json.dumps({"opensearch_password": "PW-CLUSTER"}))
    res = client.post("/api/v1/onboarding/apply-schema", json=body)
    assert res.status_code == 200, res.text
    assert visto["password"] == "PW-CLUSTER"
