"""Tests del alta de casos de demo desde la plataforma (`custom_cases.py`).

Los 8 casos built-in viven en `verticals/` (registro del repo, inventario fijo con
sus propios tests). Los casos creados desde el Builder viven en el volumen de datos
y se **mergean** al catálogo en `main._front_payload()`. Estos tests cubren el store,
la validación y ese merge — y que el registro built-in quede intacto.
"""

import json
import pytest
from fastapi.testclient import TestClient

import auth
import custom_cases
import maas_integrator
import main
import verticals


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Aísla el store en un tmp_path: `custom_cases.cases_dir()` lo resuelve en
    cada llamada desde `auth.DATA_ROOT`, así que basta con moverlo."""
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    return tmp_path / "cases"


@pytest.fixture
def client(store):
    return TestClient(main.app)


def _meta(label="Firewall de ACME", **kw):
    base = {
        "label": label,
        "description": "Tráfico perimetral del cliente",
        "icon": "shield",
        "group": "seguridad",
        "filter_code": "filter { grok { match => { \"message\" => \"%{GREEDYDATA:raw}\" } } }",
        "fields": [
            {"raw_name": "src", "field_path": "data.src", "type": "ip", "business_label": "Origen"},
            {"raw_name": "bytes", "field_path": "data.bytes", "type": "long", "business_label": "Bytes"},
        ],
    }
    base.update(kw)
    return base


LOG = "evento 1\n# un comentario\nevento 2\nevento 3\n"


# ── Store ───────────────────────────────────────────────────────────────────
def test_save_case_persists_json_and_dataset(store):
    case = custom_cases.save_case(_meta(), LOG, created_by="sa@huawei.com")

    assert case["slug"] == "firewall-de-acme"
    assert case["index_base"] == "firewall-de-acme"
    assert case["full_label"] == "Firewall de ACME"
    assert case["lines"] == 3, "los comentarios `#` no cuentan como datos"
    assert case["created_by"] == "sa@huawei.com"

    on_disk = json.loads((store / "firewall-de-acme.json").read_text(encoding="utf-8"))
    assert on_disk == case
    # El dataset se guarda ya sin comentarios: es lo que se sube a OBS tal cual.
    assert (store / "firewall-de-acme.log").read_text(encoding="utf-8") == "evento 1\nevento 2\nevento 3\n"


def test_fields_are_cleaned_of_browser_junk(store):
    case = custom_cases.save_case(
        _meta(fields=[{"field_path": "data.a", "type": "text", "_ui_open": True, "vacio": ""}]),
        LOG)
    assert case["fields"] == [{"field_path": "data.a", "type": "text"}]


def test_list_and_get_and_delete_roundtrip(store):
    custom_cases.save_case(_meta("Caso Uno"), LOG)
    custom_cases.save_case(_meta("Caso Dos"), LOG)

    # Orden por (created_at, slug): en el mismo segundo desempata el slug.
    assert [c["slug"] for c in custom_cases.list_cases()] == ["caso-dos", "caso-uno"]
    assert custom_cases.get_case("caso-uno")["label"] == "Caso Uno"
    assert custom_cases.label_for("caso-dos") == "Caso Dos"
    assert custom_cases.dataset_path("caso-uno").is_file()

    assert custom_cases.delete_case("caso-uno") is True
    assert custom_cases.get_case("caso-uno") is None
    assert custom_cases.dataset_path("caso-uno") is None
    assert custom_cases.delete_case("caso-uno") is False, "borrar dos veces no explota"
    assert [c["slug"] for c in custom_cases.list_cases()] == ["caso-dos"]


@pytest.mark.parametrize("meta, log, expected", [
    ({"label": ""}, LOG, "Falta el nombre"),
    ({"label": "ab"}, LOG, "no es válido"),
    ({"label": "custom"}, LOG, "reservado"),
    ({"label": "siem"}, LOG, "built-in"),
    ({"label": "Sin campos", "fields": []}, LOG, "campos detectados"),
    ({"label": "Sin filter", "filter_code": ""}, LOG, "filter de Logstash"),
    # Sin archivo el caso pasa a ser `live` → hace falta decir de dónde salen los datos.
    ({"label": "Sin datos"}, "# solo comentarios\n\n", "elegí la fuente"),
])
def test_save_case_validations(store, meta, log, expected):
    with pytest.raises(custom_cases.CaseError, match=expected):
        custom_cases.save_case(_meta(**meta), log)


def test_duplicate_slug_is_rejected(store):
    custom_cases.save_case(_meta("Repetido"), LOG)
    with pytest.raises(custom_cases.CaseError, match="Ya existe un caso guardado"):
        custom_cases.save_case(_meta("Repetido"), LOG)


def test_unknown_group_and_icon_fall_back(store):
    case = custom_cases.save_case(_meta(group="inventado", icon="no-existe"), LOG)
    assert case["group"] == custom_cases.GROUP["id"]
    assert case["icon"] == "box"


def test_can_delete_only_creator_or_admin(store, monkeypatch):
    custom_cases.save_case(_meta(), LOG, created_by="dueño@huawei.com")
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "is_admin", lambda email: email == "jefe@huawei.com")

    assert custom_cases.can_delete("firewall-de-acme", "dueño@huawei.com") is True
    assert custom_cases.can_delete("firewall-de-acme", "jefe@huawei.com") is True
    assert custom_cases.can_delete("firewall-de-acme", "otro@huawei.com") is False
    assert custom_cases.can_delete("no-existe", "dueño@huawei.com") is False


def test_can_delete_when_auth_disabled(store, monkeypatch):
    """Single-user (sin login): el operador es el único usuario y puede borrar."""
    custom_cases.save_case(_meta(), LOG, created_by="")
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    assert custom_cases.can_delete("firewall-de-acme", None) is True


# ── Casos `live`: la fuente la pone el cliente ──────────────────────────────
KAFKA = {"plugin_type": "kafka", "kafka": {
    "bootstrap_servers": "10.0.0.9:9092", "topics": ["app-logs"],
    "security_protocol": "SASL_SSL", "sasl_username": "svc",
    "sasl_password": "s3cr3t"}}


def test_live_case_needs_no_dataset(store):
    case = custom_cases.save_case(
        _meta("Kafka de ACME", sample="ts=1 msg=hola", input_config=KAFKA), "")

    assert case["case_type"] == "live"
    assert case["dataset_files"] == [], "un caso live no se pre-carga en OBS"
    assert custom_cases.dataset_path("kafka-de-acme") is None
    assert custom_cases.case_type_for("kafka-de-acme") == "live"
    assert "kafka-de-acme" not in main._demo_dataset_files()


def test_live_case_without_source_is_rejected(store):
    with pytest.raises(custom_cases.CaseError, match="elegí la fuente"):
        custom_cases.save_case(_meta("Sin fuente", sample="x"), "")


def test_dataset_case_keeps_its_type(store):
    case = custom_cases.save_case(_meta(), LOG)
    assert case["case_type"] == "dataset"
    assert case["dataset_files"] == ["firewall-de-acme.log"]


def test_input_config_secrets_are_encrypted_at_rest(store, monkeypatch):
    """La password del Kafka del cliente no puede quedar en claro en el JSON."""
    from cryptography.fernet import Fernet
    import maas_integrator as _mi
    monkeypatch.setattr(_mi, "_fernet_cache", None)
    monkeypatch.setenv("APP_SECRET_KEY", "una-clave-de-prueba")

    custom_cases.save_case(_meta("Kafka de ACME", sample="x", input_config=KAFKA), "")

    raw = (store / "kafka-de-acme.json").read_text(encoding="utf-8")
    assert "s3cr3t" not in raw, "el secreto quedó en claro en disco"
    assert "10.0.0.9:9092" in raw, "lo no-sensible sigue legible"

    # Y se recupera descifrado para armar el pipeline.
    cfg = custom_cases.input_config_for("kafka-de-acme")
    assert cfg["kafka"]["sasl_password"] == "s3cr3t"
    assert cfg["kafka"]["bootstrap_servers"] == "10.0.0.9:9092"


def test_input_config_drops_other_plugins(store):
    """El front manda la config de todos los plugins; solo se guarda la elegida."""
    case = custom_cases.save_case(
        _meta("Multi", sample="x", input_config={
            "plugin_type": "beats", "beats": {"port": 5044},
            "kafka": {"bootstrap_servers": "no-va"}}), "")
    assert set(case["input_config"]) == {"plugin_type", "beats"}


def test_front_entries_never_leak_credentials(store, monkeypatch):
    """El payload se inyecta en el HTML: solo puede viajar el nombre del plugin."""
    import maas_integrator as _mi
    monkeypatch.setattr(_mi, "_fernet_cache", None)
    custom_cases.save_case(_meta("Kafka de ACME", sample="x", input_config=KAFKA), "")

    entry = custom_cases.front_entries()[0]
    assert entry["inputPlugin"] == "kafka"
    assert entry["caseType"] == "live"
    assert "s3cr3t" not in json.dumps(entry)
    assert "bootstrap_servers" not in json.dumps(entry)


# ── Merge con el catálogo (main._front_payload) ─────────────────────────────
def test_front_payload_merges_case_into_its_group(store):
    before = main._front_payload()
    custom_cases.save_case(_meta(), LOG)
    after = main._front_payload()

    assert len(after["verticals"]) == len(before["verticals"]) + 1
    entry = after["verticals"][-1]
    assert entry["slug"] == "firewall-de-acme"
    assert entry["custom"] is True
    assert entry["hasCapability"] is False, "el spec del chatbot se auto-deriva de fields"

    seguridad = next(g for g in after["groups"] if g["id"] == "seguridad")
    assert seguridad["members"][-1] == "firewall-de-acme"
    assert "mis-casos" not in [g["id"] for g in after["groups"]]


def test_front_payload_adds_mis_casos_group_only_when_needed(store):
    assert "mis-casos" not in [g["id"] for g in main._front_payload()["groups"]]

    custom_cases.save_case(_meta("Suelto", group="mis-casos"), LOG)
    grupos = {g["id"]: g for g in main._front_payload()["groups"]}
    assert grupos["mis-casos"]["members"] == ["suelto"]


def test_builtin_registry_is_untouched_by_custom_cases(store):
    """El merge vive en main, no en `verticals/`: el registro del repo (y sus
    tests de inventario) no se ven afectados por lo que se cree en runtime."""
    custom_cases.save_case(_meta(), LOG)
    assert verticals.get_vertical("firewall-de-acme") is None
    assert len(verticals.front_payload()["verticals"]) == len(verticals.all_verticals())


def test_demo_dataset_files_and_source_include_custom_case(store):
    custom_cases.save_case(_meta(), LOG)
    mapping = main._demo_dataset_files()

    assert mapping["firewall-de-acme"] == ["firewall-de-acme.log"]
    assert mapping["siem"], "los built-in siguen presentes"

    src = main._dataset_source("firewall-de-acme", "firewall-de-acme.log")
    assert src is not None and src.is_file()
    assert main._bundled_dataset("firewall-de-acme") == "evento 1\nevento 2\nevento 3"


# ── Deploy: el caso se despliega con SU input ───────────────────────────────
def _deploy_req(case_kw):
    return main.TerraformDeployRequest(
        pipeline_conf="input {}", obs_access_key="AK", obs_secret_key="SK",
        obs_bucket="mi-bucket", obs_endpoint="https://obs.x.com",
        opensearch_password="pw",
        cases=[main.PipelineCase(**case_kw)])


def test_deploy_falls_back_to_s3_for_normal_cases(store):
    """Sin fuente propia (todos los casos de demo) se arma el input s3 de siempre."""
    req = _deploy_req({"slug": "siem", "filter_code": "filter {}",
                       "obs_prefix": "siem-logs/", "read_existing_bucket": True})
    conf = main._build_pipeline_conf_for_case(req.cases[0], req)

    assert "input {" in conf and "s3 {" in conf
    assert 'bucket => "mi-bucket"' in conf
    assert 'prefix => "siem-logs/"' in conf
    assert "delete => false" in conf, "read_existing no debe borrar los objetos"


def test_deploy_uses_case_own_kafka_input(store):
    """El deploy multi-caso hardcodeaba s3: un caso Kafka salía con el input
    equivocado y leía del bucket de demos en vez del broker del cliente."""
    req = _deploy_req({"slug": "kafka-acme", "filter_code": "filter {}",
                       "read_existing_bucket": True, "input_config": KAFKA})
    conf = main._build_pipeline_conf_for_case(req.cases[0], req)

    assert "kafka {" in conf
    assert 'bootstrap_servers => "10.0.0.9:9092"' in conf
    assert "s3 {" not in conf
    assert "mi-bucket" not in conf


def test_deploy_reads_input_from_store_after_refresh(store, monkeypatch):
    """Si el front no reenvía el input_config (deploy reconstruido tras un F5),
    se toma del caso guardado."""
    import maas_integrator as _mi
    monkeypatch.setattr(_mi, "_fernet_cache", None)
    custom_cases.save_case(_meta("Kafka de ACME", sample="x", input_config=KAFKA), "")

    req = _deploy_req({"slug": "kafka-de-acme", "filter_code": "filter {}",
                       "read_existing_bucket": True})
    conf = main._build_pipeline_conf_for_case(req.cases[0], req)
    assert 'bootstrap_servers => "10.0.0.9:9092"' in conf


def test_live_case_skips_the_dataset_guard(store, monkeypatch):
    """El guard exige el dataset en OBS a los casos de demo. Un caso live no
    tiene dataset: sin esta excepción bloqueaba un deploy válido."""
    import maas_integrator as _mi
    monkeypatch.setattr(_mi, "_fernet_cache", None)
    custom_cases.save_case(_meta("Kafka de ACME", sample="x", input_config=KAFKA), "")

    req = _deploy_req({"slug": "kafka-de-acme", "filter_code": "filter {}",
                       "read_existing_bucket": True})
    main._check_demo_datasets_present(req)  # no debe levantar HTTPException


def test_dataset_case_never_stores_the_creators_bucket(store):
    """AISLAMIENTO: un caso con dataset NO guarda la fuente.

    El modal manda el `input_config` del paso 3, que para OBS lleva el bucket y
    las AK/SK del creador. Si eso se persistiera, cualquier otro usuario de la
    instancia desplegaría leyendo de la cuenta ajena.
    """
    case = custom_cases.save_case(
        _meta(input_config={"plugin_type": "obs", "obs": {
            "bucket": "bucket-del-creador", "access_key_id": "AK-CREADOR",
            "secret_access_key": "SK-CREADOR"}}), LOG)

    assert case["case_type"] == "dataset"
    assert case["input_config"] == {}
    raw = (store / "firewall-de-acme.json").read_text(encoding="utf-8")
    assert "bucket-del-creador" not in raw and "AK-CREADOR" not in raw


def test_dataset_case_deploys_with_the_deployers_bucket(store):
    """Y en el deploy, el input se arma con las credenciales del request."""
    custom_cases.save_case(_meta(), LOG)
    req = _deploy_req({"slug": "firewall-de-acme", "filter_code": "filter {}",
                       "obs_prefix": "firewall-de-acme-logs/", "read_existing_bucket": True})
    conf = main._build_pipeline_conf_for_case(req.cases[0], req)

    assert 'bucket => "mi-bucket"' in conf, "usa el bucket del que despliega"
    assert 'access_key_id => "AK"' in conf


def test_beats_port_only_exposed_when_used(store):
    """Terraform abre el puerto (SG + DNAT) solo si algún caso usa el input beats:
    abrirlo siempre sería exponer superficie sin motivo."""
    sin_beats = _deploy_req({"slug": "siem", "filter_code": "filter {}"})
    assert main._beats_port(sin_beats) == 0

    con_beats = _deploy_req({"slug": "beats-acme", "filter_code": "filter {}",
                             "input_config": {"plugin_type": "beats",
                                              "beats": {"port": 5555, "host": "0.0.0.0"}}})
    assert main._beats_port(con_beats) == 5555


def test_source_ips_extracted_for_cluster_routes(store):
    """El Logstash necesita Cluster Routes hacia el broker/DB del cliente; se
    sacan de la config de la fuente (las IPs pasan derecho)."""
    req = _deploy_req({"slug": "kafka-acme", "filter_code": "filter {}",
                       "input_config": {"plugin_type": "kafka", "kafka": {
                           "bootstrap_servers": "10.0.0.9:9092,10.0.0.10:9092"}}})
    assert main._case_source_ips(req) == ["10.0.0.10", "10.0.0.9"]

    jdbc = _deploy_req({"slug": "db-acme", "filter_code": "filter {}",
                        "input_config": {"plugin_type": "jdbc", "jdbc": {
                            "jdbc_connection_string": "jdbc:postgresql://10.1.2.3:5432/prod"}}})
    assert main._case_source_ips(jdbc) == ["10.1.2.3"]

    assert main._case_source_ips(_deploy_req({"slug": "siem", "filter_code": "filter {}"})) == []


def test_jdbc_conf_omits_empty_optionals(store):
    """`jdbc_driver_library => \"\"` hace que el plugin no arranque: los opcionales
    vacíos no se emiten."""
    req = _deploy_req({"slug": "db", "filter_code": "filter {}",
                       "input_config": {"plugin_type": "jdbc", "jdbc": {
                           "jdbc_connection_string": "jdbc:postgresql://h:5432/d",
                           "statement": "SELECT 1"}}})
    conf = main._build_pipeline_conf_for_case(req.cases[0], req)
    assert 'jdbc_connection_string => "jdbc:postgresql://h:5432/d"' in conf
    assert 'jdbc_driver_library' not in conf
    assert 'jdbc_user' not in conf

    completo = _deploy_req({"slug": "db", "filter_code": "filter {}",
                            "input_config": {"plugin_type": "jdbc", "jdbc": {
                                "jdbc_connection_string": "jdbc:postgresql://h:5432/d",
                                "jdbc_driver_library": "/opt/pg.jar",
                                "jdbc_user": "u", "jdbc_password": "p",
                                "statement": "SELECT 1"}}})
    conf2 = main._build_pipeline_conf_for_case(completo.cases[0], completo)
    assert 'jdbc_driver_library => "/opt/pg.jar"' in conf2
    assert 'jdbc_user => "u"' in conf2


def test_pipeline_secrets_collected_for_masking(store):
    """Las credenciales de la fuente se hornean en el .conf → tienen que ir a
    `sensitive_words` de Terraform o quedan legibles en la consola de CSS."""
    req = _deploy_req({"slug": "kafka-acme", "filter_code": "filter {}",
                       "input_config": KAFKA})
    assert main._pipeline_secrets(req) == ["s3cr3t"]

    plano = _deploy_req({"slug": "siem", "filter_code": "filter {}"})
    assert main._pipeline_secrets(plano) == []


# ── Endpoints ───────────────────────────────────────────────────────────────
def test_create_case_endpoint(client, store, monkeypatch):
    # Sin AK/SK cargadas el upload a OBS se saltea (best-effort) y el caso igual queda.
    monkeypatch.setattr(maas_integrator, "get_obs_creds", lambda: {"ak": "", "sk": ""})
    res = client.post("/api/v1/cases", json={**_meta(), "log_content": LOG})

    assert res.status_code == 200
    body = res.json()
    assert body["case"]["slug"] == "firewall-de-acme"
    assert body["uploaded"] is False
    assert "bucket de demos" in body["upload_error"]

    listed = client.get("/api/v1/cases").json()["cases"]
    assert [c["slug"] for c in listed] == ["firewall-de-acme"]
    assert listed[0]["lines"] == 3

    # Y ya aparece en el catálogo que consume el front.
    slugs = [v["slug"] for v in client.get("/api/v1/verticals").json()["verticals"]]
    assert "firewall-de-acme" in slugs


def test_create_live_case_endpoint(client, store, monkeypatch):
    """El endpoint tiene que reenviar el `input_config` al store: sin eso, todo
    caso `live` era imposible de crear (el store lo veía vacío y lo rechazaba)."""
    monkeypatch.setattr(maas_integrator, "get_obs_creds", lambda: {"ak": "", "sk": ""})
    res = client.post("/api/v1/cases", json={
        **_meta("Kafka de ACME"), "sample": "ts=1 msg=hola", "log_content": "",
        "input_config": KAFKA})

    assert res.status_code == 200, res.json()
    case = res.json()["case"]
    assert case["case_type"] == "live"
    assert case["input_config"]["plugin_type"] == "kafka"

    # Y el catálogo lo publica sin filtrar credenciales.
    payload = client.get("/api/v1/verticals").json()
    assert "s3cr3t" not in json.dumps(payload)
    entry = next(v for v in payload["verticals"] if v["slug"] == "kafka-de-acme")
    assert entry["caseType"] == "live" and entry["inputPlugin"] == "kafka"


def test_create_case_endpoint_rejects_bad_payload(client, store):
    res = client.post("/api/v1/cases", json={**_meta(label="siem"), "log_content": LOG})
    assert res.status_code == 400
    assert "built-in" in res.json()["detail"]["message"]


def test_delete_case_endpoint(client, store, monkeypatch):
    monkeypatch.setattr(maas_integrator, "get_obs_creds", lambda: {"ak": "", "sk": ""})
    client.post("/api/v1/cases", json={**_meta(), "log_content": LOG})

    assert client.delete("/api/v1/cases/no-existe").status_code == 404

    res = client.delete("/api/v1/cases/firewall-de-acme")
    assert res.status_code == 200 and res.json()["status"] == "deleted"
    assert custom_cases.list_cases() == []


def test_delete_case_endpoint_forbidden_for_non_owner(store, monkeypatch):
    """El guard del endpoint, con un usuario logueado que NO es el creador.

    Se llama al handler directo en vez de por HTTP: con `AUTH_ENABLED` el
    middleware cortaría antes con 401 (otra cosa, ya cubierta por su propio test)
    y no se llegaría a evaluar la pertenencia."""
    from fastapi import HTTPException

    custom_cases.save_case(_meta(), LOG, created_by="otro@huawei.com")
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "is_admin", lambda email: False)
    # UserCtx armado a mano: `build_user_ctx` además siembra el workspace de
    # terraform (copia los providers), que no hace falta para probar el guard.
    ctx = auth.UserCtx(email="intruso@huawei.com", user_id="intruso",
                       data_dir=store.parent, settings_path=store.parent / "s.json",
                       terraform_dir=store.parent / "tf")
    token = auth.current_user_var.set(ctx)
    try:
        with pytest.raises(HTTPException) as exc:
            main.delete_custom_case("firewall-de-acme")
    finally:
        auth.current_user_var.reset(token)

    assert exc.value.status_code == 403
    assert custom_cases.get_case("firewall-de-acme") is not None
