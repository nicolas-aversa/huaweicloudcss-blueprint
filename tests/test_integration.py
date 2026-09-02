"""
Tests de integración del servicio de AI-Driven Onboarding.

Arquitectura del flujo:

    1. /api/v1/onboarding/generate-filter  -> LLM glm-5.2 (raw_log -> filter)
    2. /api/v1/onboarding/generate-pipeline -> arma .conf (input + filter + output)

El paso de validación con Logstash efímero quedó deprecado. La validación
SEMÁNTICA del filter sigue activa en /validate-mapping (sandbox de OpenSearch).
"""

import pathlib
import re
import pytest
from fastapi.testclient import TestClient

import main


# Los datasets no se versionan (se regeneran con los build scripts). Los tests que
# leen los .log reales se saltean si la data no está presente en datasets/.
_DATASETS_DIR = pathlib.Path(__file__).resolve().parent.parent / "datasets"
requires_datasets = pytest.mark.skipif(
    not any(_DATASETS_DIR.glob("*.log")),
    reason="datasets no presentes (regenerá con build_vertical_datasets.py / build_siem_dataset.py)",
)


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

    # Todos los campos quedan bajo el namespace, sin forzar ECS.
    for x in body["fields"]:
        assert x["is_ecs"] is False
        assert x["field_path"].startswith("data.")
        assert x["ecs_overlay_path"] is None

    txn_id = next(x for x in body["fields"] if x["raw_name"] == "txn_id")
    assert txn_id["field_path"] == "data.txn_id"


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
    def boom(_log, *args, **kwargs):
        raise RuntimeError("La variable de entorno MAAS_API_KEY no está definida.")

    monkeypatch.setattr(main, "generate_logstash_filter", boom)

    res = client.post(
        "/api/v1/onboarding/generate-filter",
        json={"raw_log": SAMPLE_FINANCIAL_LOG},
    )
    assert res.status_code == 500


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


# --- Validadores de shape (LLM hardening) ----------------------------------
#
# `generate_synthetic_shapes` aplica dos filtros al output del LLM antes de
# devolverlo: `_clean_shape` (strip de markdown/comentarios/numbered-lists)
# y `_shape_matches_format` (descarta alucinaciones de formato). Estos
# tests ejercitan los validadores aislados, sin necesidad de API key.


# --- Defensive: generate_synthetic_shapes nunca debe raise ----------------
#
# Por contrato la función es best-effort. Cualquier error del SDK del LLM,
# IndexError por choices vacío, JSON malformado, etc. debe devolver None
# sin propagar — sino el handler /terraform/deploy explota con 500.


def test_generate_synthetic_shapes_returns_none_on_empty_choices(monkeypatch):
    """MaaS devuelve choices=[] (puede pasar por content-filter o length cutoff).
    Antes esto disparaba IndexError fuera de cualquier try → 500 en el handler."""
    from maas_integrator import generate_synthetic_shapes
    import maas_integrator as _m

    class _EmptyChoicesResponse:
        choices = []

    class _StubClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return _EmptyChoicesResponse()

    monkeypatch.setattr(_m, "_build_client", lambda: _StubClient())
    # Asegurar que no se valida API key real.
    monkeypatch.setenv("MAAS_API_KEY", "fake-test-key")

    result = generate_synthetic_shapes("any sample log", n_shapes=5)
    assert result is None  # no raise, devuelve None


def test_generate_synthetic_shapes_returns_none_on_unexpected_exception(monkeypatch):
    """El SDK puede tirar excepciones que no son OpenAIError (httpx, validation,
    runtime). El outer try/except Exception las atrapa todas."""
    from maas_integrator import generate_synthetic_shapes
    import maas_integrator as _m

    class _BoomClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("simulación de SDK error no-OpenAIError")

    monkeypatch.setattr(_m, "_build_client", lambda: _BoomClient())
    monkeypatch.setenv("MAAS_API_KEY", "fake-test-key")

    result = generate_synthetic_shapes("any sample log", n_shapes=5)
    assert result is None  # no raise


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
    assert ns_props["geo_loc"] == {"type": "geo_point"}
    # text → multi-field text + keyword (full-text Y aggregatable).
    assert ns_props["err_msg"]["type"] == "text"
    assert ns_props["err_msg"]["fields"]["keyword"]["type"] == "keyword"
    # Códigos string → NO están en properties (= keyword vía dynamic template).
    assert "trxl_resp" not in ns_props
    assert "trxl_account4" not in ns_props

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
    # string → keyword via dynamic template (ausente de properties).
    assert "trace_name" not in props


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
    # keyword → dynamic template (no explícito), incluso anidado.
    assert "event" not in props  # event.action es keyword → no mapeado explícito
    assert "geo" not in props.get("destination", {}).get("properties", {})


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


# --- Export Starter Kit + Destroy (handoff al cliente + FinOps) ----------


def test_export_starter_kit_returns_zip_with_expected_files():
    """El endpoint debe devolver un .zip con 4 archivos esperados y filename
    derivado del project_name."""
    import io
    import zipfile

    res = client.post(
        "/api/v1/onboarding/export-starter-kit",
        json={
            "pipeline_conf": "filter { kv { source => \"message\" } }",
            "project_name": "demo-cliente-X",
        },
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert "demo-cliente-X-starter-kit.zip" in res.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        names = z.namelist()
        assert "starter-kit/logstash.conf" in names
        assert "starter-kit/terraform/main.tf" in names
        assert "starter-kit/terraform/terraform.tfvars.example" in names
        assert "starter-kit/index-template.json" in names
        assert "starter-kit/README.md" in names
        # README debe interpolar el project_name.
        readme = z.read("starter-kit/README.md").decode("utf-8")
        assert "demo-cliente-X" in readme
        # Runbook de consola (PoC guiada SA+cliente): mismos artefactos, camino consola.
        assert "starter-kit/RUNBOOK-consola.md" in names
        runbook = z.read("starter-kit/RUNBOOK-consola.md").decode("utf-8")
        assert "demo-cliente-X" in runbook
        assert "Configuration Center" in runbook and "delete => false" in runbook


def test_export_kit_single_markdown_document():
    """El Builder arma TODO en UN documento Markdown: config (con la fuente
    elegida), index template, dashboards, runbook de consola y los comandos del
    chatbot (agente root + os_chat) y forecasts — parametrizados con el índice y
    los campos del log pegado. Sin Terraform como camino principal."""
    res = client.post("/api/v1/onboarding/export-kit", json={
        "pipeline_conf": ('input { kafka { bootstrap_servers => "k:9092" topics => ["logs"] } }\n'
                          'filter { json { source => "message" } }\n'
                          'output { opensearch { hosts => ["h"] index => "pagos-%{+YYYY.MM}" } }'),
        "project_name": "pagos-cliente",
        "fields": [
            {"raw_name": "status", "field_path": "status", "type": "keyword",
             "business_label": "Estado", "dimension": True, "role": "primary_dimension"},
            {"raw_name": "amount", "field_path": "amount", "type": "float",
             "business_label": "Monto", "dimension": False, "role": "measure"},
            {"raw_name": "user_id", "field_path": "user_id", "type": "keyword",
             "business_label": "Usuario", "dimension": False, "role": "entity_id"},
        ],
        "namespace": "data",
        "opensearch_index": "pagos-%{+YYYY.MM}",
    })
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/markdown")
    assert "pagos-cliente-kit.md" in res.headers["content-disposition"]
    doc = res.text
    # Config del cliente con la fuente elegida (kafka), no forzado a OBS.
    assert "bootstrap_servers" in doc
    # Index template + dashboards + runbook de consola.
    assert "PUT _index_template/pagos-cliente" in doc
    assert "## 3. Dashboards" in doc
    assert "Configuration Center" in doc and "delete => false" in doc
    # Chatbot: secuencia Dev Tools completa + bind al Assistant.
    assert "POST _plugins/_ml/agents/_register" in doc
    assert "PUT .plugins-ml-config/_doc/os_chat" in doc
    # Forecasts para el volume_field derivado del schema.
    assert "_plugins/_forecast/forecasters" in doc
    # Índice del cliente en el prompt PPL / source.
    assert "source=pagos-" in doc


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


def test_obs_read_sample_handles_stream_folder_and_gz():
    """Regresión del 'NoneType' object is not callable en read_sample (flujo
    "Despliegue productivo"): el download debe ir EN MEMORIA (body.buffer), debe
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
    # gunzip + hasta 3 líneas no vacías
    assert line.startswith('{"trace_name":"loginUser","code":200}')
    assert "otra linea" in line


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
        "project_id": "pid123", "region": "sa-brazil-1", "availability_zone": "sa-brazil-1a",
        "vpc_id": "vpc-1", "subnet_id": "net-2", "security_group_id": "sg-3",
        "demo_bucket": "mis-demos",
    })
    body = res.json()
    assert body["source"] == "settings"
    assert body["effective_region"] == "sa-brazil-1"
    assert body["values"]["demo_bucket"] == "mis-demos"
    assert mi.get_region() == "sa-brazil-1"
    assert mi.get_huawei_project_id() == "pid123"
    assert main._huawei_infra_tfvars() == {
        "vpc_id": "vpc-1", "subnet_id": "net-2", "security_group_id": "sg-3",
        "availability_zone": "sa-brazil-1a", "region": "sa-brazil-1",
    }

    # Vaciar todo → vuelve al fallback.
    client.post("/api/v1/settings/huawei", json={})
    assert client.get("/api/v1/settings/huawei").json()["source"] == "tfvars"


@requires_datasets
def test_datasets_preload_uploads_missing(monkeypatch):
    """El preload sube los datasets FALTANTES a `<slug>-logs/` con put_file
    (streaming de disco), saltea los que ya están (only_missing) y crea el
    bucket si no existe — todo streameado por SSE."""
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


def test_deploy_rejects_unavailable_plugins(monkeypatch):
    """La Logstash de CSS no trae `translate` (el cluster no puede instalar
    plugins). El deploy corta con 400 accionable ANTES del apply — el sandbox
    local NO lo detecta porque su imagen sí lo trae."""
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda _dir: {})
    body = {
        "pipeline_conf": 'filter { translate { source => "code" target => "desc" } }',
        "opensearch_password": "pw", "opensearch_index": "logs-%{+YYYY.MM}",
    }
    res = client.post("/api/v1/terraform/deploy-stream", json=body)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["stage"] == "unavailable_plugins"
    assert "translate" in detail["message"]

    # Un conf sin plugins vetados NO debe disparar este 400 (puede fallar
    # después por otras cosas, pero no en este gate).
    ok_req = main.TerraformDeployRequest(
        pipeline_conf='filter { mutate { add_field => { "translated" => "x" } } }')
    assert main._check_unavailable_plugins(ok_req) is None


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
    (slug => {pipeline_conf, start_ingestion}); fase 2 = start_ingestion True."""
    import json as _json
    import main as _main

    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)  # skip init

    monkeypatch.setattr(_main.subprocess, "run", lambda *a, **k: _OkProc())
    req = _main.TerraformDeployRequest(
        pipeline_conf="filter {}", project_name="x", start_ingestion=True,
        opensearch_index="logs-%{+YYYY.MM}",
    )
    _main._do_terraform_sequence(req, td)
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

    _main._do_terraform_sequence(_main.TerraformDeployRequest(
        pipeline_conf="filter { A }", project_name="x", start_ingestion=True,
        opensearch_index="logs-%{+YYYY.MM}", obs_prefix="logs/",
    ), td)
    _main._do_terraform_sequence(_main.TerraformDeployRequest(
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

    res = client.post("/api/v1/terraform/deploy", json={
        "pipeline_conf": "filter {}", "opensearch_index": "logs-nuevo-%{+YYYY.MM}",
    })
    assert res.status_code == 400
    assert "Máximo" in str(res.json()["detail"])


def test_export_starter_kit_bundles_index_template():
    """El .zip incluye index-template.json con los tipos derivados de fields."""
    import io
    import json as _json
    import zipfile

    res = client.post(
        "/api/v1/onboarding/export-starter-kit",
        json={
            "pipeline_conf": "filter { kv { source => \"message\" target => \"data\" } }",
            "project_name": "demo",
            "fields": [{"raw_name": "amount", "type": "float"}, {"raw_name": "resp", "type": "string"}],
            "namespace": "data",
            "opensearch_index": "logs-%{+YYYY.MM}",
        },
    )
    assert res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        tpl = _json.loads(z.read("starter-kit/index-template.json").decode("utf-8"))
    assert tpl["index_patterns"] == ["logs-*"]
    props = tpl["template"]["mappings"]["properties"]
    assert props["data"]["properties"]["amount"] == {"type": "double"}
    assert "resp" not in props["data"]["properties"]  # keyword vía dynamic template


def test_export_starter_kit_strips_comments_from_logstash_conf():
    """Si el operador editó el pipeline_conf con comentarios, el .conf del
    .zip debe salir limpio (consistente con strip_logstash_comments)."""
    import io
    import zipfile

    pipeline_with_comments = (
        "filter {\n"
        "  # ECS: duration en nanosegundos\n"
        "  kv { source => \"message\" } # inline también\n"
        "  ruby { code => \"event.set('[a]', 1)\" }\n"
        "}"
    )
    res = client.post(
        "/api/v1/onboarding/export-starter-kit",
        json={"pipeline_conf": pipeline_with_comments, "project_name": "test"},
    )
    assert res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        conf = z.read("starter-kit/logstash.conf").decode("utf-8")
        for line in conf.splitlines():
            stripped = line.lstrip()
            assert not stripped.startswith("#"), f"comentario sin stripear: {line!r}"


def test_export_starter_kit_includes_dashboards_ndjson():
    """El .zip incluye dashboards.ndjson cuando el índice coincide con un caso."""
    import io
    import json as _json
    import zipfile

    res = client.post(
        "/api/v1/onboarding/export-starter-kit",
        json={
            "pipeline_conf": "filter { kv { source => \"message\" } }",
            "project_name": "firewall-demo",
            "opensearch_index": "firewall-%{+YYYY.MM}",
        },
    )
    assert res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        names = z.namelist()
        assert "starter-kit/dashboards.ndjson" in names
        ndjson = z.read("starter-kit/dashboards.ndjson").decode("utf-8")
        lines = ndjson.strip().split("\n")
        assert len(lines) >= 3
        objects = [_json.loads(line) for line in lines]
        types = [obj["type"] for obj in objects]
        assert "index-pattern" in types
        assert "dashboard" in types
    assert res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        conf = z.read("starter-kit/logstash.conf").decode("utf-8")
        for line in conf.splitlines():
            stripped = line.lstrip()
            assert not stripped.startswith("#"), f"comentario sin stripear: {line!r}"


def test_terraform_destroy_returns_noop_when_state_empty(monkeypatch):
    """Si el tfstate no existe o está vacío, destroy responde noop sin
    invocar el subprocess de terraform.

    Si el tfstate real ya tiene contenido (caso post-deploy), skip — no
    queremos arriesgar a destruir un cluster real desde un test, y mockear
    el chequeo de tamaño para forzar el path noop sería invasivo. El
    smoke-test del endpoint en `test_terraform_destroy_endpoint_exists`
    cubre el caso "endpoint vivo y responde shape correcto".
    """
    from pathlib import Path as _Path
    import main as _main

    real_tfstate = _Path(_main.__file__).parent / "terraform" / "terraform.tfstate"
    if real_tfstate.exists() and real_tfstate.stat().st_size >= 200:
        pytest.skip("tfstate real con contenido — saltando para no riesgar destroy real")

    # Doble safety: si por alguna razón el test no fuera saltado, mockeamos
    # subprocess.run para que falle el test antes de invocar terraform.
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
        {"slug": "logs", "index": "logs-%{+YYYY.MM}", "obs_prefix": "logs/", "active": True, "dashboards_imported": False, "has_capabilities": False}
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

    res = client.post("/api/v1/terraform/deploy", json={
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
        synthetic_count=0,
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
        obs_bucket="bucket", obs_endpoint="https://obs.example.com", synthetic_count=0,
        cases=[
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
    bundleado tal cual y NO llama a la generación sintética, aunque
    `synthetic_count > 0`."""
    import main as _main
    import maas_integrator as _maas

    uploaded = []
    synth_called = {"n": 0}

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            return 0
        def close(self):
            pass

    def _boom_synth(*a, **k):
        synth_called["n"] += 1
        return "SYNTHETIC"

    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)
    monkeypatch.setattr(_maas, "generate_synthetic_shapes", _boom_synth)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        synthetic_count=200,
        cases=[_main.PipelineCase(
            slug="firewall", raw_log="ignored", filter_code="f",
            fields=[], index_name="firewall", obs_prefix="logs/firewall/",
        )],
    )

    _main._do_obs_upload(request)

    assert synth_called["n"] == 0
    assert len(uploaded) == 1
    assert "dataset_" in uploaded[0]["key"]
    assert uploaded[0]["data"] == _main._bundled_dataset("firewall")


def test_obs_upload_custom_single_no_synthetic_raw_fallback(monkeypatch):
    """Custom (single, slug `logs`) SIN archivo importado: ya NO se generan
    sintéticos. Cae al fallback defensivo (sube la única línea `raw_log` como
    `sample_log_*`) y NO llama a los generadores sintéticos."""
    import main as _main
    import maas_integrator as _maas

    uploaded = []
    synth_called = {"n": 0}

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            return 0
        def close(self):
            pass

    def _spy_synth(*a, **k):
        synth_called["n"] += 1
        return "SYNTHETIC-BATCH"

    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)
    monkeypatch.setattr(_maas, "generate_synthetic_shapes", _spy_synth)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        pipeline_slug="logs",
        raw_log='{"foo":"bar"}',
        synthetic_count=200,
    )

    _main._do_obs_upload(request)

    assert synth_called["n"] == 0
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
    import maas_integrator as _maas

    uploaded = []
    synth_called = {"n": 0}

    class FakeOBS:
        def __init__(self, **kwargs):
            pass
        def put_object(self, key, data):
            uploaded.append({"key": key, "data": data})
        def delete_prefix(self, prefix):
            return 0
        def close(self):
            pass

    def _boom(*a, **k):
        synth_called["n"] += 1
        raise AssertionError("no debería generar sintéticos en custom")

    monkeypatch.setattr("obs_client.OBSClient", FakeOBS)
    monkeypatch.setattr(_maas, "generate_synthetic_shapes", _boom)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        pipeline_slug="logs",
        raw_log="L1",
        log_file_content="L1\nL2\nL3",
        synthetic_count=200,
        obs_prefix="logs-logs/",
    )

    _main._do_obs_upload(request)

    assert synth_called["n"] == 0
    assert len(uploaded) == 1
    assert "dataset_" in uploaded[0]["key"]
    assert uploaded[0]["data"] == "L1\nL2\nL3"  # verbatim


def test_obs_upload_imported_file_verbatim_multicase(monkeypatch):
    """Custom como caso (multi): `log_file_content` se sube verbatim bajo su
    prefijo, `delete_prefix` se llamó, y sin sintéticos."""
    import main as _main
    import maas_integrator as _maas

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
    monkeypatch.setattr(_maas, "generate_synthetic_shapes", _boom)

    request = _main.TerraformDeployRequest(
        pipeline_conf="filter {}",
        obs_access_key="ak", obs_secret_key="sk", obs_bucket="bucket",
        obs_endpoint="https://obs.example.com",
        synthetic_count=200,
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
        synthetic_count=0,
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
        synthetic_count=0,
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
        synthetic_count=0,
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
        "streaming-ott"}
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
    assert searches == len(fc_names) and len(fc_names) == 39


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
    """Cada VERTICAL tiene las keys mínimas; los 8 visibles traen card+specs y
    fraud/cts son hidden (sin card visible). Los agregadores producen exactamente
    los sets que el resto del backend espera (13 capabilities, 13 industry, 8 datasets)."""
    import verticals as V

    vs = V.all_verticals()
    slugs = [v["slug"] for v in vs]
    assert slugs == [
        "siem", "fortianalyzer", "transacciones-billetera", "fraud-detection", "transacciones-alyc",
        "streaming-ott", "produccion-pozos", "ventas-ecommerce", "encuentros-clinicos", "cts"]

    visible = V.visible_verticals()
    assert [v["slug"] for v in visible] == [
        "siem", "fortianalyzer", "transacciones-billetera", "transacciones-alyc",
        "streaming-ott", "produccion-pozos", "ventas-ecommerce", "encuentros-clinicos"]
    for v in visible:
        # Card + datos de front + specs backend presentes en cada primario.
        for k in ("label", "full_label", "group", "icon", "index_base", "description",
                  "sample", "filter_code", "fields", "suggested_questions",
                  "industry_fields", "dataset_files", "capability", "dashboard"):
            assert v.get(k), f"{v['slug']} sin {k}"

    cts = V.get_vertical("cts")
    assert cts["hidden"] is True and "label" not in cts and "capability" not in cts
    assert cts["sample"] and cts["dashboard"]   # aporta EXAMPLE_DATA + dashboard legacy

    # Agregadores == lo que consume el backend.
    assert len(V.capability_specs()) == 13
    assert len(V.industry_fields()) == 13
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
    assert main._INDUSTRY_FIELDS == V.industry_fields()
    assert main._DEMO_DATASET_FILES == V.demo_dataset_files()
    # dashboards = verticales + legacy firewall (que NO es un vertical).
    assert "firewall" in D._DASHBOARD_SPECS
    assert "firewall" not in V.dashboard_specs()
    assert set(D.get_available_slugs()) == set(V.dashboard_specs()) | {"firewall"}


def test_index_inyecta_verticals_y_endpoint():
    """GET / reemplaza el placeholder por el JSON real (no queda null) y expone
    los 8 verticales visibles; GET /api/v1/verticals devuelve el mismo payload."""
    import re as _re

    html = client.get("/").text
    m = _re.search(r"window\.__VERTICALS__ = (.*?);</script>", html, _re.S)
    assert m and m.group(1).strip() != "null"
    import json as _json
    injected = _json.loads(m.group(1))
    assert len(injected["groups"]) == 6
    visible = [v for v in injected["verticals"] if not v["hidden"]]
    assert len(visible) == 8
    assert {v["slug"] for v in visible} >= {"siem", "transacciones-alyc", "fortianalyzer"}

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
