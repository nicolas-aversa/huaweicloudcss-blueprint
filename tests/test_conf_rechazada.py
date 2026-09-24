"""Cuando CSS rechaza un configuration file, el deploy lo dice y no sigue a ciegas.

Pasó con un dataset nuevo: CSS respondió "config is forbidden" al crear la
configuración. El deploy dio los clusters por buenos con un aviso genérico;
"Iniciar ingesta" falló igual, pero siguió y esperó documentos dos minutos,
para terminar mandando a revisar el bucket y el filtro. Y la tarjeta mostraba
la pipeline "En pausa", como si solo faltara arrancarla.
"""
import json

import main

# El error tal cual lo escribe Terraform (del deploy que lo destapó).
_ERROR = [
    'huaweicloud_css_logstash_configuration.pipeline["reviews-ordenes"]: Creating...\n',
    'Error: error creating CSS logstash cluster configuration: Bad request with: [POST '
    'https://css.la-south-2.myhuaweicloud.com/v1.0/p/clusters/c/lgsconf/submit], request_id: '
    'e14d, error message: {"errCode":"CSS.0001","externalMessage":"CSS.0001 : Incorrect '
    'parameters. (config is forbidden, change it.)"}\n',
    '  with huaweicloud_css_logstash_configuration.pipeline["reviews-ordenes"],\n',
    '  on main.tf line 452, in resource "huaweicloud_css_logstash_configuration" "pipeline":\n',
]


def test_se_lee_que_configuracion_rechazo_css_y_por_que():
    rechazadas = main._configuraciones_rechazadas(main._errores_de_terraform(_ERROR))
    assert list(rechazadas) == ["reviews-ordenes"]
    assert "config is forbidden" in rechazadas["reviews-ordenes"]
    assert "No es un error de sintaxis" in rechazadas["reviews-ordenes"]


def test_otro_rechazo_trae_el_mensaje_de_css():
    msg = main._motivo_de_rechazo('... {"errCode":"CSS.0001","externalMessage":"CSS.0001 : '
                                  'The configuration file name already exists."}')
    assert msg == "CSS rechazó el configuration file: The configuration file name already exists."


def test_un_error_que_no_es_de_una_configuracion_no_cuenta():
    errores = {"huaweicloud_nat_dnat_rule.kibana[0]": "algo",
               "huaweicloud_css_logstash_pipeline.pipeline[0]": "otra cosa"}
    assert main._configuraciones_rechazadas(errores) == {}


def test_el_rechazo_queda_anotado_y_se_borra_cuando_pasa(tmp_path):
    main._write_pipelines_registry(tmp_path, {"a": {"index": "a"}, "b": {"index": "b"}})
    main._anotar_rechazos(tmp_path, {"a": "CSS rechazó…"}, ["a", "b"])
    reg = main._read_pipelines_registry(tmp_path)
    assert reg["a"]["config_rechazo"] == "CSS rechazó…" and "config_rechazo" not in reg["b"]

    main._anotar_rechazos(tmp_path, {}, ["a", "b"])
    assert "config_rechazo" not in main._read_pipelines_registry(tmp_path)["a"]


# ── "Iniciar ingesta" con la configuración rechazada ────────────────────────
class _Ok:
    returncode = 0
    stdout = ""
    stderr = ""


class _Stdout:
    def __init__(self, lineas):
        self._it = iter(lineas)

    def __iter__(self):
        return self._it

    def close(self):
        pass


def test_iniciar_ingesta_corta_con_el_motivo_y_no_espera_documentos(monkeypatch, tmp_path):
    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    monkeypatch.setattr(main, "get_huawei_settings", lambda: {
        "vpc_id": "v", "subnet_id": "s", "security_group_id": "g",
        "availability_zone": "la-south-2a", "region": "la-south-2"})
    monkeypatch.setattr(main, "_backend_init_args", lambda d: None)
    monkeypatch.setattr(main.tfstate, "push_errored_state", lambda d: (True, ""))
    monkeypatch.setattr(main, "_clear_case_indices", lambda *a, **k: None)
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda d: {})
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: _Ok())
    esperas = []
    monkeypatch.setattr(main, "_verificar_ingesta",
                        lambda *a, **k: esperas.append(1) or iter(()))

    class _Popen:
        returncode = 1

        def __init__(self, *a, **k):
            self.stdout = _Stdout(_ERROR)

        def wait(self):
            return 1

    monkeypatch.setattr(main.subprocess, "Popen", _Popen)
    req = main.TerraformDeployRequest(
        pipeline_conf="input {} filter {} output {}", pipeline_slug="reviews-ordenes",
        opensearch_index="reviews-ordenes-%{+YYYY.MM}", obs_bucket="b",
        read_existing_bucket=True, start_ingestion=True)

    eventos = [json.loads(e[len("data: "):]) for e in main._deploy_stream_gen(req, td, None, None)
               if e.startswith("data: ")]

    pasos = {e["name"]: e for e in eventos if e["type"] == "step"}
    assert pasos["Configuración · reviews-ordenes"]["ok"] is False
    error = [e for e in eventos if e["type"] == "error"]
    assert error and "No se pudo iniciar la ingesta" in error[-1]["message"]
    assert "config is forbidden" in error[-1]["message"]
    assert not esperas, "esperó documentos de una pipeline que ni se creó"
    assert not [e for e in eventos if e["type"] == "complete"]
    assert main._read_pipelines_registry(td)["reviews-ordenes"]["config_rechazo"]


# ── La tarjeta del entorno ──────────────────────────────────────────────────
def test_el_status_la_muestra_rechazada_y_no_en_pausa(monkeypatch, tmp_path):
    from test_integration import _write_fake_state_with_cluster, client

    fake_main, _ = _write_fake_state_with_cluster(tmp_path)
    main._write_pipelines_registry(tmp_path / "terraform", {
        "reviews-ordenes": {"index": "r", "obs_prefix": "r/", "start_ingestion": False,
                            "config_rechazo": "CSS rechazó el configuration file (config is forbidden)"},
        "otra": {"index": "o", "obs_prefix": "o/", "start_ingestion": False}})
    monkeypatch.setattr(main, "__file__", str(fake_main))
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": "{}", "stderr": ""})())

    por_slug = {p["slug"]: p for p in client.get("/api/v1/terraform/status").json()["pipelines"]}

    assert por_slug["reviews-ordenes"]["config_status"] == "rechazada"
    assert "config is forbidden" in por_slug["reviews-ordenes"]["config_error"]
    assert por_slug["otra"]["config_status"] == "" and por_slug["otra"]["config_error"] == ""


def test_el_front_dice_rechazada_por_css():
    import pathlib
    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    i = html.index("const pipeRows = pipelines.length")
    filas = html[i:html.index("Sin pipelines registradas.", i)]
    assert "p.config_status === 'rechazada'" in filas and "Rechazada por CSS" in filas
    assert "escapeHtml(p.config_error" in filas
    assert "const rechazadas = pipelines.filter(p => p.config_status === 'rechazada');" in html
    assert "CSS rechazó el configuration file de ${rechazadas.map(" in html


def test_en_el_deploy_el_aviso_nombra_la_configuracion_rechazada(monkeypatch, tmp_path):
    """Los clusters quedan (facturan): el deploy los registra, pero el aviso
    dice qué configuración rechazó CSS, no una dirección de Terraform suelta."""
    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    monkeypatch.setattr(main, "get_huawei_settings", lambda: {
        "vpc_id": "v", "subnet_id": "s", "security_group_id": "g",
        "availability_zone": "la-south-2a", "region": "la-south-2"})
    monkeypatch.setattr(main, "_backend_init_args", lambda d: None)
    monkeypatch.setattr(main.tfstate, "push_errored_state", lambda d: (True, ""))
    monkeypatch.setattr(main, "_do_obs_upload", lambda req: None)
    monkeypatch.setattr(main, "_add_css_cluster_routes", lambda *a, **k: {})
    monkeypatch.setattr(main, "_verificar_configuraciones", lambda d: iter(()))

    class _Run:
        returncode = 0
        stderr = ""

        def __init__(self, args):
            self.stdout = (json.dumps({"opensearch_cluster_id": {"value": "os-1"}})
                           if args[:2] == ["terraform", "output"] else "")

    monkeypatch.setattr(main.subprocess, "run", lambda args, **k: _Run(args))

    class _Popen:
        returncode = 1

        def __init__(self, *a, **k):
            self.stdout = _Stdout(_ERROR)

        def wait(self):
            return 1

    monkeypatch.setattr(main.subprocess, "Popen", _Popen)
    req = main.TerraformDeployRequest(
        pipeline_conf="input {} filter {} output {}", pipeline_slug="reviews-ordenes",
        opensearch_index="reviews-ordenes-%{+YYYY.MM}", obs_bucket="b", read_existing_bucket=True)

    eventos = [json.loads(e[len("data: "):]) for e in main._deploy_stream_gen(req, td, None, None)
               if e.startswith("data: ")]

    avisos = [e["message"] for e in eventos if e.get("phase") == "Aviso"]
    assert any("CSS rechazó el configuration file de reviews-ordenes" in a for a in avisos), avisos
    assert not any("huaweicloud_css_logstash_configuration" in a for a in avisos), avisos
    assert [e for e in eventos if e["type"] == "complete"], "los clusters existen: se registra el entorno"
