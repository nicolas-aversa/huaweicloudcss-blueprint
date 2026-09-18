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
    # Sin nombre de archivo cae a `<slug>.log`, pero el contenido es el subido
    # ENTERO: el comentario `#` no cuenta como dato pero tampoco se pierde.
    assert case["dataset_files"] == ["firewall-de-acme.log"]
    assert (store / "firewall-de-acme" / "firewall-de-acme.log").read_bytes() == LOG.encode("utf-8")


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


# ── "Preparar bucket" con casos custom ──────────────────────────────────────
# Un caso creado por un SA queda disponible para todos (el store es por
# instancia), pero el dataset vive en el bucket de CADA UNO. Sin esto, el
# segundo que quiere demostrarlo se encuentra el bucket vacío. Lo de arriba
# cubre el mapeo; esto cubre el endpoint, que es lo que el botón llama.
def _fake_obs(calls, ya_estan=()):
    class _FakeObs:
        def __init__(self, **kwargs):
            calls["kwargs"] = kwargs
        def ensure_bucket(self, region=""):
            calls.setdefault("ensure", []).append(region)
            return False
        def object_exists(self, key):
            return key in self._keys
        def prefix_has_objects(self, prefix):
            return any(k.startswith(prefix) for k in self._keys)
        def put_file(self, key, path):
            calls.setdefault("put", []).append(key)
            self._keys.add(key)          # lo subido pasa a estar
        def close(self):
            pass
    _FakeObs._keys = set(ya_estan)
    return _FakeObs


def _eventos(res):
    return [json.loads(l[len("data: "):]) for l in res.text.splitlines()
            if l.startswith("data: ")]


def test_preparar_bucket_sube_el_dataset_de_un_caso_custom(client, store, monkeypatch):
    """El SSE de /datasets/preload tiene que incluir el `.log` del caso custom
    bajo `<slug>-logs/<slug>.log`, igual que un built-in."""
    custom_cases.save_case(_meta(), LOG)
    calls = {}
    monkeypatch.setattr("obs_client.OBSClient", _fake_obs(calls))

    res = client.post("/api/v1/datasets/preload", json={
        "access_key": "AK", "secret_key": "SK", "bucket": "mis-demos",
        "region": "la-south-2",
    })
    assert res.status_code == 200

    clave = "firewall-de-acme-logs/firewall-de-acme.log"
    assert clave in calls.get("put", []), (
        f"el caso custom no se subió. Subidos: {calls.get('put', [])}")
    hechos = [e for e in _eventos(res)
              if e.get("type") == "file" and e.get("slug") == "firewall-de-acme"]
    assert hechos and hechos[-1]["state"] == "done", (
        "el progreso del caso custom no llega al front por el SSE")


def test_un_dataset_custom_se_resube_aunque_ya_este_en_el_bucket(client, store, monkeypatch):
    """`only_missing` es un atajo válido para los `.log` del repo, que no cambian
    bajo el mismo nombre. Un caso custom SÍ cambia: se borra y se recrea con el
    mismo slug y otro contenido. Si se lo saltea, el bucket se queda con la
    versión vieja y la demo muestra datos que ya no son los del caso."""
    custom_cases.save_case(_meta(), LOG)
    clave = "firewall-de-acme-logs/firewall-de-acme.log"
    calls = {}
    # Todo ya está en el bucket, incluido el dataset del caso custom.
    monkeypatch.setattr("obs_client.OBSClient", _fake_obs(calls, ya_estan={clave}))

    res = client.post("/api/v1/datasets/preload", json={
        "access_key": "AK", "secret_key": "SK", "bucket": "mis-demos",
        "only_missing": True,
    })
    assert res.status_code == 200
    assert clave in calls.get("put", []), (
        "el dataset custom se salteó por only_missing: el bucket se queda con la "
        "versión vieja del caso")

    saltados = [e for e in _eventos(res)
                if e.get("type") == "file" and e.get("state") == "skipped"]
    assert all(e["slug"] != "firewall-de-acme" for e in saltados)


# ── El chatbot de un caso creado desde el Builder ───────────────────────────
# El síntoma reportado fue "no se generó el chatbot". El agente SÍ se creaba; lo
# que faltaba era (a) que el front renderizara el chat —cubierto en
# tests/test_front_chat_slugs.py— y (b) que el backend supiera a QUÉ índice
# apuntar. Esto último es lo de acá: sin spec, el chat no manda `system_prompt` y
# el modelo cae al default del connector, que quedó armado con otro vertical.
def test_el_spec_de_un_caso_custom_sale_de_los_fields_persistidos(store, monkeypatch, tmp_path):
    custom_cases.save_case(_meta(), LOG)
    registro = {
        "firewall-de-acme": {
            "index": "firewall-de-acme-%{+YYYY.MM}",
            "fields": [
                {"field_path": "data.src", "type": "ip", "business_label": "Origen", "dimension": True},
                {"field_path": "data.bytes", "type": "long", "business_label": "Bytes", "dimension": False},
            ],
            "label": "",          # registro viejo, sin label
        }
    }
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda td: registro)

    spec = main._resolve_capability_spec("firewall-de-acme", terraform_dir=tmp_path)

    assert spec, "un caso custom con fields tiene que resolver un spec"
    # Igualdad exacta, no `startswith`: el patrón sale del índice REAL que el
    # deploy persistió, y un `startswith` deja pasar cualquier sufijo inventado.
    assert spec["index_pattern"] == "firewall-de-acme-*", (
        f"el chat apuntaría al índice equivocado: {spec['index_pattern']}")
    assert spec["label"] == "Firewall de ACME", (
        "con el label vacío en el registry hay que caer al nombre del caso, no a "
        "'Tus logs'")


def test_sin_fields_no_hay_spec(store, monkeypatch, tmp_path):
    """Un caso `live` o un slug desconocido no tiene de dónde derivarlo: {} y que
    el llamador decida, en vez de inventar un índice."""
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda td: {"x": {"fields": []}})
    assert main._resolve_capability_spec("x", terraform_dir=tmp_path) == {}
    assert main._resolve_capability_spec("no-existe", terraform_dir=tmp_path) == {}


def test_un_vertical_del_repo_usa_su_spec_curado(store, tmp_path):
    """El spec curado gana siempre: derivar de fields perdería los forecasts y las
    operaciones que el vertical declara a mano."""
    spec = main._resolve_capability_spec("siem", terraform_dir=tmp_path)
    assert spec and spec["index_pattern"] == "siem*"


def test_el_label_del_registry_cae_al_nombre_del_caso(store):
    """`_registry_label` centraliza el fallback que antes estaba en 1 de los 4
    lugares que escriben el registro. Lo lee el chatbot para nombrar la fuente."""
    custom_cases.save_case(_meta(), LOG)

    assert main._registry_label("firewall-de-acme") == "Firewall de ACME"
    # Un slug sin caso: vacío, no un placeholder inventado.
    assert main._registry_label("no-existe") == ""


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


# ── El guard de datasets con el body de UN caso ─────────────────────────────
# Con una sola tarjeta el front manda `pipeline_slug` + `obs_prefix`, sin
# `cases`. El guard iteraba solo `cases`, así que el deploy más común pasaba sin
# chequeo: si el upload del dataset al guardar el caso había fallado en silencio,
# Logstash arrancaba contra un prefijo vacío y nadie avisaba.
def _deploy_req_unico(slug, **kw):
    base = dict(
        pipeline_conf="input {}", obs_access_key="AK", obs_secret_key="SK",
        obs_bucket="mi-bucket", obs_endpoint="https://obs.x.com", opensearch_password="pw",
        pipeline_slug=slug, obs_prefix=f"{slug}-logs/", read_existing_bucket=True)
    base.update(kw)
    return main.TerraformDeployRequest(**base)


def test_el_guard_cubre_el_deploy_de_un_solo_caso(store, monkeypatch):
    """Bucket vacío y el upload de rescate también falla → 400 accionable, en
    vez de 20 minutos de deploy contra un prefijo sin datos."""
    from obs_client import OBSUploadError
    custom_cases.save_case(_meta(), LOG)
    Fake = _fake_obs({})                                        # bucket vacío
    def _put_falla(self, key, path):
        raise OBSUploadError("OBS rechazó el upload")
    Fake.put_file = _put_falla
    monkeypatch.setattr("obs_client.OBSClient", Fake)

    with pytest.raises(main.HTTPException) as exc:
        main._check_demo_datasets_present(_deploy_req_unico("firewall-de-acme"))
    assert exc.value.status_code == 400
    assert exc.value.detail["stage"] == "datasets_missing"
    assert "firewall-de-acme" in exc.value.detail["message"]


def test_el_guard_sube_el_dataset_custom_si_falta(store, monkeypatch):
    """El `.log` del caso vive en el store: si falta en el bucket, se sube ahí
    mismo en vez de mandar al SA a "Preparar"."""
    custom_cases.save_case(_meta(), LOG)
    calls = {}
    monkeypatch.setattr("obs_client.OBSClient", _fake_obs(calls))

    main._check_demo_datasets_present(_deploy_req_unico("firewall-de-acme"))   # no levanta

    assert calls.get("put") == ["firewall-de-acme-logs/firewall-de-acme.log"]


def test_el_guard_no_toca_el_flujo_productivo(monkeypatch):
    """Un slug que no es de demo (prefijo del cliente) ni se chequea: el guard no
    tiene por qué saber qué hay ahí."""
    class _Explota:
        def __init__(self, **kw):
            raise AssertionError("no tenía que abrir OBS")
    monkeypatch.setattr("obs_client.OBSClient", _Explota)

    main._check_demo_datasets_present(_deploy_req_unico("acme-prod"))


# ── Un input s3 sin bucket no llega a Terraform ─────────────────────────────
# "Guardar y desplegar" de un dataset nuevo mandó un .conf con `bucket => ""`
# (armado en el paso 3, antes de conocer el slug). Logstash lo validó OK, arrancó
# y poleó la nada durante todo el deploy: "No files found in bucket" cada 60 s.
def _conf_s3(bucket_line):
    return ("input {\n  s3 {\n    access_key_id => \"AK\"\n    secret_access_key => \"SK\"\n"
            f"    {bucket_line}\n    region => \"la-south-2\"\n    codec => plain\n  }}\n}}\n\n"
            "filter {\n  csv { separator => \",\" }\n}\n\n"
            "output {\n  elasticsearch {\n    hosts => [\"http://x:9200\"]\n    index => \"sp500-%{+YYYY.MM}\"\n  }\n}\n")


def test_un_conf_con_bucket_vacio_corta_antes_de_terraform():
    with pytest.raises(main.HTTPException) as exc:
        main._check_conf_reads_from_a_bucket(_deploy_req_unico("sp500", pipeline_conf=_conf_s3('bucket => ""')))
    assert exc.value.status_code == 400
    assert exc.value.detail["stage"] == "pipeline_conf"
    assert "sp500" in exc.value.detail["message"]

    # Sin la línea `bucket` directamente, mismo resultado.
    with pytest.raises(main.HTTPException):
        main._check_conf_reads_from_a_bucket(_deploy_req_unico("sp500", pipeline_conf=_conf_s3("interval => 60")))


def test_un_conf_con_bucket_pasa_y_uno_sin_s3_tambien():
    main._check_conf_reads_from_a_bucket(_deploy_req_unico("sp500", pipeline_conf=_conf_s3('bucket => "demos"')))
    kafka = 'input {\n  kafka {\n    bootstrap_servers => "b:9092"\n    topics => ["t"]\n  }\n}\n\nfilter {}\n\noutput { stdout {} }\n'
    main._check_conf_reads_from_a_bucket(_deploy_req_unico("acme", pipeline_conf=kafka))


def test_con_cases_el_bucket_sale_del_request_y_los_live_no_cuentan(store, monkeypatch):
    """Multi-caso: el .conf lo arma el backend con `case.obs_bucket or
    request.obs_bucket`. Sin bucket de demos configurado, los casos de demo
    saldrían leyendo de `""`; un caso live (Kafka) no lee de OBS y no cuenta."""
    import maas_integrator as _mi
    monkeypatch.setattr(_mi, "_fernet_cache", None)
    custom_cases.save_case(_meta("Kafka de ACME", sample="x", input_config=KAFKA), "")

    req = _deploy_req({"slug": "siem", "filter_code": "filter {}", "read_existing_bucket": True})
    req.cases.append(main.PipelineCase(slug="kafka-de-acme", filter_code="filter {}", read_existing_bucket=True))
    main._check_conf_reads_from_a_bucket(req)                 # bucket "mi-bucket": pasa

    req.obs_bucket = ""
    with pytest.raises(main.HTTPException) as exc:
        main._check_conf_reads_from_a_bucket(req)
    assert exc.value.detail["stage"] == "pipeline_conf"
    assert "siem" in exc.value.detail["message"]
    assert "kafka-de-acme" not in exc.value.detail["message"], "un caso live no lee de OBS"

    # Con bucket propio (CTS) no depende del de demos.
    req.cases[0].obs_bucket = "mi-tracker-cts"
    req.cases.pop()
    main._check_conf_reads_from_a_bucket(req)


def test_los_dos_endpoints_de_deploy_pasan_por_el_guard_del_bucket():
    """Hay dos entradas al deploy (stream y job) y las dos tienen que cortar
    antes de Terraform, o el error vuelve por la que quedó afuera."""
    import inspect
    for fn in (main.terraform_deploy_stream, main.terraform_deploy_job):
        src = inspect.getsource(fn)
        assert "_check_conf_reads_from_a_bucket(request)" in src, fn.__name__
        assert src.index("_check_conf_reads_from_a_bucket(request)") < src.index("_deploy_lock_for_current()"), \
            f"{fn.__name__}: el guard tiene que correr antes de tomar el lock"


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


# ── Un caso `live` en "Preparar" ────────────────────────────────────────────
def test_preparar_bucket_lista_los_casos_live_y_dice_por_que_no_los_sube(client, store, monkeypatch):
    """Un caso `live` (Kafka, Beats, JDBC) no tiene dataset: Logstash lee de la
    fuente en cada deploy. "Preparar" no lo subía —correcto— pero tampoco lo
    mencionaba, y el SA veía que "su caso no se subió"."""
    custom_cases.save_case(dict(_meta(), label="Lee de Kafka", sample="src=1.2.3.4 bytes=10", input_config={
        "plugin_type": "kafka",
        "kafka": {"bootstrap_servers": "k:9092", "topics": ["logs", "audit"],
                  "sasl_username": "u", "sasl_password": "SECRETO"},
    }), log_text="")
    calls = {}
    monkeypatch.setattr("obs_client.OBSClient", _fake_obs(calls))

    res = client.post("/api/v1/datasets/preload", json={
        "access_key": "AK", "secret_key": "SK", "bucket": "mis-demos"})
    assert res.status_code == 200

    assert not [k for k in calls.get("put", []) if k.startswith("lee-de-kafka")],         "un caso live no tiene .log que subir"
    [ev] = [e for e in _eventos(res) if e.get("slug") == "lee-de-kafka"]
    assert ev["state"] == "live"
    assert "kafka (logs, audit)" in ev["detail"] and "no hay dataset" in ev["detail"]
    assert "SECRETO" not in ev["detail"], "sin secretos en el SSE"


def test_source_label_de_un_caso_inexistente():
    assert custom_cases.source_label("no-existe") == "su fuente"


def test_un_caso_no_puede_leer_directo_de_un_bucket(store):
    """El flujo "Ya está en un bucket" se fue: un caso así solo lo podía desplegar
    quien tuviera acceso a ESE bucket, y la alternativa —descargarle los datos al
    cliente para guardarlos como dataset— es justo lo que no queremos hacer. Sin
    archivo y con fuente OBS, el error manda al paso 1 a subir el archivo."""
    with pytest.raises(custom_cases.CaseError, match="subilo en el paso 1"):
        custom_cases.save_case(dict(_meta(), sample="x=1", input_config={
            "plugin_type": "obs", "obs": {"bucket": "ajeno", "prefix": "logs/"},
        }), log_text="")


# ── El dataset se guarda con su nombre y su contenido exactos ───────────────
# Antes se renombraba a `<slug>.log` y se le sacaban las líneas `#`: el archivo
# que aparecía en el bucket no era el que el SA había subido.
def test_el_dataset_conserva_nombre_y_bytes(store, monkeypatch):
    crudo = "# cabecera\r\nid,monto\r\n1,10\r\n2,20\r\n"
    case = custom_cases.save_case(_meta(label="Reviews Olist"), crudo, filename="reviews-olist.csv")

    assert case["dataset_files"] == ["reviews-olist.csv"]
    path = custom_cases.dataset_path("reviews-olist")
    assert path == store / "reviews-olist" / "reviews-olist.csv"
    assert path.read_bytes() == crudo.encode("utf-8"), "ni el # ni los \\r\\n se tocan"
    assert case["lines"] == 3, "la cabecera # no cuenta como dato, pero se guarda"
    assert custom_cases.dataset_files()["reviews-olist"] == ["reviews-olist.csv"]


def test_el_dataset_sube_a_obs_con_su_nombre(client, store, monkeypatch):
    """`<slug>-logs/<nombre original>`: al guardar el caso, al desplegar si
    falta, y en "Preparar" — los tres con la misma key."""
    custom_cases.save_case(_meta(label="Reviews Olist"), "a,b\n1,2\n", filename="reviews-olist.csv")
    calls = {}
    monkeypatch.setattr("obs_client.OBSClient", _fake_obs(calls))
    monkeypatch.setattr(main, "get_huawei_settings", lambda: {"demo_bucket": "mis-demos"})
    import maas_integrator as _mi
    monkeypatch.setattr(_mi, "get_obs_creds", lambda: {"ak": "AK", "sk": "SK"})

    ok, err = main._upload_case_dataset("reviews-olist")
    assert ok, err
    assert calls["put"] == ["reviews-olist-logs/reviews-olist.csv"]

    calls.clear()
    monkeypatch.setattr("obs_client.OBSClient", _fake_obs(calls))
    main._check_demo_datasets_present(_deploy_req_unico("reviews-olist"))
    assert calls["put"] == ["reviews-olist-logs/reviews-olist.csv"]

    calls.clear()
    monkeypatch.setattr("obs_client.OBSClient", _fake_obs(calls))
    res = client.post("/api/v1/datasets/preload", json={
        "access_key": "AK", "secret_key": "SK", "bucket": "mis-demos"})
    assert res.status_code == 200
    assert "reviews-olist-logs/reviews-olist.csv" in calls["put"]


def test_borrar_el_caso_borra_su_carpeta(store):
    custom_cases.save_case(_meta(), "x=1\n", filename="datos.txt")
    assert (store / "firewall-de-acme" / "datos.txt").is_file()

    assert custom_cases.delete_case("firewall-de-acme")
    assert not (store / "firewall-de-acme").exists()
    assert custom_cases.dataset_path("firewall-de-acme") is None


def test_un_caso_viejo_con_slug_log_plano_sigue_andando(store):
    """Los casos creados antes tienen `<slug>.log` suelto y `dataset_files`
    con ese nombre: se siguen encontrando."""
    case = custom_cases.save_case(_meta(), LOG)
    # Simular el layout anterior: archivo plano, sin carpeta.
    (store / "firewall-de-acme.log").write_text(LOG, encoding="utf-8")
    import shutil
    shutil.rmtree(store / "firewall-de-acme")

    assert custom_cases.dataset_path("firewall-de-acme") == store / "firewall-de-acme.log"
    assert custom_cases.dataset_files()["firewall-de-acme"] == ["firewall-de-acme.log"]


@pytest.mark.parametrize("feo,esperado", [
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\yo\\mi log (final).txt", "mi_log_final_.txt"),
    (".oculto", "oculto"),
    ("", "firewall-de-acme.log"),
    ("///", "firewall-de-acme.log"),
])
def test_safe_filename(feo, esperado):
    assert custom_cases.safe_filename(feo, "firewall-de-acme") == esperado
