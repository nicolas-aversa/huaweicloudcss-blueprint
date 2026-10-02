"""Un deploy cortado a la mitad se recupera solo: reintentar alcanza.

Caso real: el proceso del apply murió y Huawei terminó de crear el Logstash y
las dos reglas DNAT sin que quedaran en el state; el apply siguiente falló con
VPC.2024 ("ya existe") y "conflict in the request"."""
import json
import pathlib

import main
import recuperacion

SUBNET = "94f39a19"


def _inst(tipo, nombre, attrs, index=None, status=""):
    i = {"attributes": attrs}
    if index is not None:
        i["index_key"] = index
    if status:
        i["status"] = status
    return {"mode": "managed", "type": tipo, "name": nombre, "instances": [i]}


# El state como quedó en el corte: OpenSearch, NAT y EIP, sin Logstash ni DNAT.
STATE = {"resources": [
    _inst("huaweicloud_css_cluster", "opensearch_cluster", {"id": "os-1", "name": "log-analytics-opensearch"}, 0),
    _inst("huaweicloud_nat_gateway", "nat", {"id": "gw-1"}),
    _inst("huaweicloud_vpc_eip", "nat_eip", {"id": "eip-1"}),
    {"mode": "data", "type": "huaweicloud_networking_port", "name": "os_node", "instances": [{"attributes": {"id": "p"}}]},
]}
CLUSTERS = [{"id": "os-1", "name": "log-analytics-opensearch", "status": "200", "subnet_id": SUBNET},
            {"id": "ls-1", "name": "log-analytics-logstash", "status": "200", "subnet_id": SUBNET},
            {"id": "otro", "name": "otro-logstash", "status": "200", "subnet_id": SUBNET}]
REGLAS = [{"id": "dnat-9200", "external_service_port": 9200}, {"id": "dnat-5601", "external_service_port": 5601}]


def _importar(**k):
    base = dict(proyecto="log-analytics", subnet_id=SUBNET, reglas_dnat=REGLAS, clusters=CLUSTERS,
                confs_logstash=[], pipelines=[])
    base.update(k)
    return recuperacion.a_importar(k.pop("state", STATE) if "state" in k else STATE,
                                   **{x: y for x, y in base.items() if x != "state"})


def test_se_adopta_lo_que_el_corte_dejo_fuera_del_state():
    assert _importar() == [
        {"direccion": "huaweicloud_nat_dnat_rule.opensearch[0]", "id": "dnat-9200", "que": "regla DNAT 9200 (OpenSearch)"},
        {"direccion": "huaweicloud_nat_dnat_rule.kibana[0]", "id": "dnat-5601", "que": "regla DNAT 5601 (Dashboards)"},
        {"direccion": "huaweicloud_css_logstash_cluster.logstash_cluster", "id": "ls-1", "que": "cluster log-analytics-logstash"},
    ]


def test_no_se_adopta_nada_de_otro_entorno():
    # Otra subnet: no es de este entorno aunque se llame igual.
    ajenos = [dict(c, subnet_id="otra") for c in CLUSTERS]
    assert [i["direccion"] for i in _importar(clusters=ajenos)] == [
        "huaweicloud_nat_dnat_rule.opensearch[0]", "huaweicloud_nat_dnat_rule.kibana[0]"]
    # Otro proyecto: otro nombre.
    assert "huaweicloud_css_logstash_cluster.logstash_cluster" not in [
        i["direccion"] for i in _importar(proyecto="demo-x")]
    # Sin nada de este entorno en el state: no se adopta nada.
    assert _importar(state={"resources": []}) == []


def test_reusando_un_opensearch_no_se_tocan_sus_reglas():
    assert [i["direccion"] for i in _importar(reusa_opensearch=True)] == ["huaweicloud_css_logstash_cluster.logstash_cluster"]


def test_las_configuraciones_de_logstash():
    state = {"resources": STATE["resources"] + [
        _inst("huaweicloud_css_logstash_cluster", "logstash_cluster", {"id": "ls-1", "name": "log-analytics-logstash"})]}
    r = _importar(state=state, confs_logstash=["pipeline-siem", "pipeline-otra"], pipelines=["siem", "cts"])
    assert {"direccion": 'huaweicloud_css_logstash_configuration.pipeline["siem"]', "id": "ls-1/pipeline-siem",
            "que": "configuración pipeline-siem"} in r
    assert not [i for i in r if "cts" in i["direccion"] or "otra" in i["direccion"]]
    # Si el Logstash también es huérfano, sus configuraciones se adoptan con su id.
    r = _importar(confs_logstash=["pipeline-siem"], pipelines=["siem"])
    assert {"direccion": 'huaweicloud_css_logstash_configuration.pipeline["siem"]', "id": "ls-1/pipeline-siem",
            "que": "configuración pipeline-siem"} in r


def test_lo_ya_registrado_no_se_vuelve_a_importar():
    state = {"resources": STATE["resources"] + [
        _inst("huaweicloud_nat_dnat_rule", "opensearch", {"id": "dnat-9200"}, 0),
        _inst("huaweicloud_nat_dnat_rule", "kibana", {"id": "dnat-5601"}, 0),
        _inst("huaweicloud_css_logstash_cluster", "logstash_cluster", {"id": "ls-1"})]}
    assert _importar(state=state) == []


def test_se_desmarca_un_cluster_sano_marcado_como_mal_creado():
    state = {"resources": [
        _inst("huaweicloud_css_logstash_cluster", "logstash_cluster", {"id": "ls-1", "name": "log-analytics-logstash"}, status="tainted"),
        _inst("huaweicloud_css_cluster", "opensearch_cluster", {"id": "os-1"}, 0, status="tainted"),
        _inst("huaweicloud_nat_dnat_rule", "kibana", {"id": "d"}, 0, status="tainted"),
    ]}
    r = recuperacion.a_desmarcar(state, {"ls-1": "200", "os-1": "100"})
    assert r == [{"direccion": "huaweicloud_css_logstash_cluster.logstash_cluster", "que": "cluster log-analytics-logstash"}], \
        "el que todavía se crea (100) no; una regla tampoco"


def test_las_direcciones_del_state():
    d = [x[0] for x in recuperacion.instancias({"resources": [
        _inst("a", "b", {}, 0), _inst("a", "c", {}, "siem"), _inst("a", "d", {})]})]
    assert d == ["a.b[0]", 'a.c["siem"]', "a.d"]


# ── El paso en la plataforma ────────────────────────────────────────────────
class _Req:
    obs_access_key, obs_secret_key, project_name, existing_opensearch_endpoint, cases = "AK", "SK", "log-analytics", "", []


def test_el_paso_importa_y_desmarca(monkeypatch, tmp_path):
    state = {"resources": STATE["resources"] + [
        _inst("huaweicloud_css_cluster", "otro", {"id": "os-1"}, status="tainted")]}
    monkeypatch.setattr(main.tfstate, "read_state", lambda td: state)
    monkeypatch.setattr(main, "_inventario_huawei", lambda ak, sk, st: {"clusters": CLUSTERS, "reglas_dnat": REGLAS, "confs_logstash": []})
    monkeypatch.setattr(main, "_huawei_infra_tfvars", lambda: {"subnet_id": SUBNET})
    corridas = []

    class _P:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, "", "boom" if rc else ""

    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **k: corridas.append(cmd) or _P(1 if any("kibana[0]" in x for x in cmd) else 0))
    eventos = [json.loads(e[len("data: "):]) for e in main._recuperar_deploy_cortado(_Req(), tmp_path)]
    assert ["terraform", "import", "-input=false", "-no-color", "huaweicloud_css_logstash_cluster.logstash_cluster", "ls-1"] in corridas
    assert ["terraform", "untaint", "-no-color", "huaweicloud_css_cluster.otro"] in corridas
    nombres = {e["name"]: (e["ok"], e["reason"]) for e in eventos}
    assert nombres["Recuperar deploy cortado · cluster log-analytics-logstash"] == (True, "adoptado")
    assert nombres["Recuperar deploy cortado · regla DNAT 5601 (Dashboards)"] == (False, "boom")
    assert nombres["Recuperar deploy cortado · cluster os-1"] == (True, "desmarcado (estaba sano)")


def test_sin_credenciales_o_si_huawei_no_responde_se_sigue(monkeypatch, tmp_path):
    class _SinCred(_Req):
        obs_access_key = ""
    assert list(main._recuperar_deploy_cortado(_SinCred(), tmp_path)) == []
    monkeypatch.setattr(main.tfstate, "read_state", lambda td: STATE)
    monkeypatch.setattr(main, "_inventario_huawei", lambda *a: (_ for _ in ()).throw(RuntimeError("sin red")))
    assert list(main._recuperar_deploy_cortado(_Req(), tmp_path)) == []


def test_va_antes_del_apply_y_antes_del_destroy():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def _deploy_stream_gen_raw(")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    assert cuerpo.index("tfstate.push_errored_state(terraform_dir)") < \
        cuerpo.index("yield from _recuperar_deploy_cortado(request, terraform_dir)") < \
        cuerpo.index('"phase": "Terraform apply"')
    j = src.index("def terraform_destroy(")
    destroy = src[j:]
    assert destroy.index("tfstate.push_errored_state(terraform_dir)") < \
        destroy.index("for ev in _recuperar_deploy_cortado(request, terraform_dir):") < \
        destroy.index('["terraform", "destroy"')
