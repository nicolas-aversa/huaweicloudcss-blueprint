"""Security Analytics para un dataset nuevo: el LLM propone reglas Sigma sobre
los campos descubiertos y todo se valida contra los datos antes de usarlo.
Después entra por el mismo camino que las verticales de seguridad."""
import json

import pytest

import main
import plan_de_cluster
import seguridad
import seguridad_derivada as sd
from test_seguridad import _Cluster

CAMPOS = [
    {"field_path": "fecha", "type": "date", "role": "timestamp"},
    {"field_path": "srcip", "type": "ip"},
    {"field_path": "subtype", "type": "string", "frecuentes": ["forward", "ips", "virus", "webfilter"]},
    {"field_path": "severity", "type": "string", "frecuentes": ["critical", "high"]},
    {"field_path": "action", "type": "string", "frecuentes": ["accept", "blocked"]},
    {"field_path": "dstport", "type": "integer"},
]
LINEAS = ([f"fecha=2026-04-13 srcip=10.0.0.{i} subtype=forward action=accept dstport=443" for i in range(40)]
          + ["fecha=2026-04-13 srcip=10.0.0.9 subtype=ips severity=critical action=blocked dstport=22",
             "fecha=2026-05-02 srcip=10.0.0.8 subtype=virus action=blocked dstport=80"])


def _regla(**kw):
    base = {"titulo": "IPS crítico", "descripcion": "d", "nivel": "critical", "tags": ["attack.t1190"],
            "seleccion": {"subtype": "ips", "severity": "critical"}}
    return {**base, **kw}


def _validar(*reglas, es=True):
    return sd.validar({"es_seguridad": es, "descripcion": "FortiGate", "reglas": list(reglas)}, CAMPOS, LINEAS)


def test_una_regla_sobre_campos_y_valores_reales_pasa():
    spec = _validar(_regla(), _regla(titulo="Puerto de admin", nivel="high", tags=["Attack.T1021", "no-mitre"],
                                     seleccion={"action": "blocked", "dstport": [22, 80]}))
    assert spec["descripcion"] == "FortiGate" and [r["titulo"] for r in spec["reglas"]] == ["IPS crítico", "Puerto de admin"]
    assert spec["reglas"][1]["tags"] == ["attack.t1021"], "solo tags de ATT&CK"


@pytest.mark.parametrize("regla", [
    _regla(seleccion={"no_existe": "x"}),                       # campo inventado
    _regla(seleccion={"puerto": 22}),                          # el valor se ve, el campo no existe
    _regla(seleccion={"subtype": "ransomware"}),                # valor que no se vio
    _regla(seleccion={"action": "accept"}),                     # casi todo el tráfico: satura
    _regla(nivel="urgente"),
    _regla(titulo=""),
    _regla(seleccion={}),
    _regla(seleccion={"subtype": ["ips", True]}),
    _regla(seleccion={"dstport": [22, 3389]}),                  # 3389 no está en la muestra
])
def test_una_regla_invalida_se_descarta(regla):
    assert _validar(regla) is None


def test_sin_seguridad_o_sin_reglas_validas_no_hay_spec():
    assert _validar(_regla(), es=False) is None
    assert sd.validar("nada", CAMPOS, LINEAS) is None
    assert _validar(_regla(), _regla()) == _validar(_regla()), "un título repetido no suma"


def test_un_dataset_de_negocio_no_llama_al_llm():
    llamado = []
    ventas = [{"field_path": "monto", "type": "float"}, {"field_path": "categoria", "type": "string"}]
    assert sd.proponer(ventas, ["monto=1"], llamar=lambda p: llamado.append(p) or "{}") is None
    assert not llamado and sd.candidato(CAMPOS)


def test_proponer_con_el_llm():
    respuesta = "pensando… " + json.dumps({"es_seguridad": True, "reglas": [_regla()]})
    vistos = []
    spec = sd.proponer(CAMPOS, LINEAS, llamar=lambda p: vistos.append(p) or respuesta)
    assert spec["reglas"][0]["seleccion"] == {"subtype": "ips", "severity": "critical"}
    assert '"campo": "subtype"' in vistos[0] and "valores_frecuentes" in vistos[0]
    assert sd.proponer(CAMPOS, LINEAS, llamar=lambda p: "no es json") is None
    assert sd.proponer(CAMPOS, LINEAS, llamar=lambda p: (_ for _ in ()).throw(TimeoutError())) is None


def test_los_meses_del_dataset():
    texto = "\n".join(LINEAS)
    assert sd.meses_del_dataset(texto, CAMPOS) == ("2026-04", "2026-06"), "un mes de margen al final"
    syslog = "<34>1 2025-12-30T10:00:00Z host sshd - - fallo\n<34>1 2026-01-02T10:00:00Z host sshd - - fallo"
    assert sd.meses_del_dataset(syslog, []) == ("2025-12", "2026-02")
    assert sd.meses_del_dataset("", []) is None and sd.meses_del_dataset("sin fechas", []) is None


def test_la_spec_del_caso_tiene_la_forma_de_la_de_una_vertical():
    spec = sd.spec_del_caso("mi-firewall", _validar(_regla()), ("2026-04", "2026-06"))
    assert spec["log_types"][0]["nombre"] == "mi_firewall" and spec["meses"] == ("2026-04", "2026-06")
    assert spec["log_types"][0]["descripcion"] == "FortiGate" and spec["correlaciones"] == []
    yaml = seguridad.sigma_yaml(spec["log_types"][0]["reglas"][0], "mi_firewall")
    assert "    subtype: \"ips\"" in yaml and "product: mi_firewall" in yaml


def test_correlaciones_entre_fuentes():
    a = sd.spec_del_caso("siem-fw", _validar(_regla()))
    b = sd.spec_del_caso("siem-waf", _validar(_regla(titulo="Virus", nivel="high",
                                                       seleccion={"subtype": "virus", "action": ["blocked", "x"]})) or
                         {"reglas": [_regla(titulo="Virus", seleccion={"subtype": "virus"})]})
    cor = sd.correlaciones("siem", [("siem-fw", a, "siem-fw-*"), ("siem-waf", b, "siem-waf-*")])
    assert len(cor) == 1 and cor[0]["ventana_min"] == 60
    lados = cor[0]["correlate"]
    assert [l["log_type"] for l in lados] == ["siem_fw", "siem_waf"] and lados[1]["index"] == "siem-waf-*"
    assert lados[0]["query"] == '(subtype:"ips" AND severity:"critical")'
    cuerpo = seguridad.build_correlacion(cor[0], "no-se-usa-*")
    assert [c["index"] for c in cuerpo["correlate"]] == ["siem-fw-*", "siem-waf-*"]
    assert sd.consulta_de_reglas([{"seleccion": {"x": ["a", 'b"c']}}]) == '(x:("a" OR "b\\"c"))'
    assert sd.consulta_de_reglas([{"seleccion": {"x": "a"}}, {"seleccion": {"y": 1}}]) == '(x:"a") OR (y:"1")'
    cuatro = [(f"f{i}", a, f"f{i}-*") for i in range(4)]
    assert len(sd.correlaciones("x", cuatro)) == 3, "hasta tres"


def test_el_plan_muestra_las_reglas_y_se_puede_apagar():
    reglas = _validar(_regla())
    item = {i["plugin"]: i for i in plan_de_cluster.plan("fw", CAMPOS, seguridad=reglas)}["security_analytics"]
    assert item["aplica"] and item["opcional"] and "IPS crítico" in item["motivo"]
    sin = {i["plugin"]: i for i in plan_de_cluster.plan("ventas", CAMPOS)}["security_analytics"]
    assert not sin["aplica"] and "seguridad" in sin["motivo"]


# ── El cableado: del caso al registro y al provisioning ─────────────────────
def _deploy(tmp_path, monkeypatch, casos, guardados=None):
    import custom_cases

    guardados = guardados or {}
    monkeypatch.setattr(custom_cases, "get_case", lambda slug: guardados.get(slug))
    monkeypatch.setattr(custom_cases, "dataset_path", lambda slug: None)
    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True, exist_ok=True)
    req = main.TerraformDeployRequest(pipeline_conf="filter { }", cases=casos)
    main._casos_de_seguridad_al_indice_mensual(req)
    main._prepare_deploy_tfvars(req, td)
    return req, td, json.loads((td / main._PIPELINES_REGISTRY_NAME).read_text(encoding="utf-8"))


def _caso(slug, **kw):
    return main.PipelineCase(slug=slug, filter_code="filter { }", fields=CAMPOS,
                             index_name=f"{slug}-%{{+YYYY.MM}}", log_file_content="\n".join(LINEAS), **kw)


def test_un_caso_con_reglas_va_al_indice_mensual_y_al_registro(tmp_path, monkeypatch):
    req, td, reg = _deploy(tmp_path, monkeypatch, [_caso("fw", seguridad=_validar(_regla())),
                                                   _caso("ventas")])
    assert req.cases[0].index_name == "fw-%{+YYYY_MM}" and req.cases[1].index_name == "ventas-%{+YYYY.MM}"
    spec = reg["fw"]["seguridad"]
    assert spec["log_types"][0]["nombre"] == "fw" and spec["meses"] == ["2026-04", "2026-06"]
    assert reg["ventas"]["seguridad"] is None
    specs = main._specs_de_seguridad(td)
    assert specs["fw"] == spec and "ventas" not in specs


def test_apagado_en_el_paso_2_no_hay_security_analytics(tmp_path, monkeypatch):
    req, td, reg = _deploy(tmp_path, monkeypatch, [_caso("fw", seguridad=_validar(_regla()),
                                                         excluir=["security_analytics"])])
    assert req.cases[0].index_name == "fw-%{+YYYY.MM}" and reg["fw"]["seguridad"] is None
    assert "fw" not in main._specs_de_seguridad(td)


def test_un_body_rearmado_toma_lo_del_caso_guardado(tmp_path, monkeypatch):
    """Tras un F5 el body se rearma sin las reglas: salen del caso guardado."""
    guardado = {"seguridad": _validar(_regla()), "excluir": ["perfil"], "familia": ""}
    req, td, reg = _deploy(tmp_path, monkeypatch, [_caso("fw")], {"fw": guardado})
    assert reg["fw"]["seguridad"]["log_types"][0]["reglas"] and reg["fw"]["excluir"] == ["perfil"]
    assert req.cases[0].index_name == "fw-%{+YYYY_MM}"


def test_las_fuentes_de_una_familia_se_correlacionan(tmp_path, monkeypatch):
    reglas = _validar(_regla())
    guardados = {s: {"seguridad": reglas, "familia": "siem"} for s in ("siem-fw", "siem-waf")}
    guardados["ventas"] = {"familia": "siem"}
    _, td, reg = _deploy(tmp_path, monkeypatch, [_caso("siem-fw"), _caso("siem-waf"), _caso("ventas")], guardados)
    assert reg["siem-fw"]["seguridad"]["correlaciones"] == []
    cor = reg["siem-waf"]["seguridad"]["correlaciones"]
    assert len(cor) == 1 and [c["index"] for c in cor[0]["correlate"]] == ["siem-fw-*", "siem-waf-*"]


def test_se_provisiona_por_el_mismo_camino(monkeypatch, tmp_path):
    """La spec derivada entra a `_provision_security_analytics` como la de una
    vertical: índices de cada mes, tipo de log propio, reglas y detector."""
    c = _Cluster()
    monkeypatch.setattr(main, "_os_req", c.req)
    monkeypatch.setattr(main, "_sa_post_texto", c.post_texto)
    monkeypatch.setattr(main.runs, "step", lambda *a, **k: None)
    spec = sd.spec_del_caso("fw", _validar(_regla()), ("2026-04", "2026-06"))
    res = main._provision_security_analytics({"public_endpoint": "x:9200"}, "admin", "pw", False,
                                             "fw", "fw-*", spec, tmp_path, run={"id": "r"})
    assert res["available"]
    assert [x[1] for x in c.creados if x[0] == "indice"] == ["fw-2026_04", "fw-2026_05", "fw-2026_06"]
    assert [x[1]["name"] for x in c.creados if x[0] == "log_type"] == ["fw"]
    assert [x[1]["name"] for x in c.creados if x[0] == "detector"] == ["fw-fw"]



def test_un_registro_con_security_analytics_apagado_no_lo_provisiona(tmp_path):
    spec = sd.spec_del_caso("fw", _validar(_regla()))
    (tmp_path / main._PIPELINES_REGISTRY_NAME).write_text(json.dumps({
        "fw": {"seguridad": spec, "excluir": ["security_analytics"]},
        "fw2": {"seguridad": spec, "excluir": []}}), encoding="utf-8")
    specs = main._specs_de_seguridad(tmp_path)
    assert "fw" not in specs and specs["fw2"]["log_types"][0]["nombre"] == "fw"


# ── Syslog con fecha ISO y reglas sobre texto libre ─────────────────────────
AUTH = ["<134>2025-07-01T10:21:33Z web-prod-02 sshd[36336]: Accepted password for postgres from 83.30.177.140 port 52077 ssh2",
        "<38>2025-07-01T10:22:40Z web-prod-02 sshd[36340]: Failed password for root from 45.9.1.2 port 51000 ssh2",
        "<85>2025-07-01T10:25:00Z web-prod-02 sudo[1201]: svc_ci : TTY=pts/0 ; PWD=/home ; USER=root ; COMMAND=/bin/bash"]


def test_el_syslog_con_fecha_iso_lo_lee_el_catalogo():
    """Antes iba al LLM del .conf: 8 minutos con 3000 líneas (medido)."""
    import conf_lint
    import log_format_catalog as cat

    assert all(cat.detect_syslog_iso(l) for l in AUTH)
    assert not cat.detect_syslog_iso("2025-07-01T10:21:33Z hola mundo sin proceso")
    r = cat.try_match(AUTH)
    assert "TIMESTAMP_ISO8601:event_timestamp" in r["filter_code"]
    assert {f["ecs_path"] for f in r["fields"]} >= {"host.hostname", "process.name", "message"}
    conf_lint.parse(r["filter_code"])


TEXTO = [{"field_path": "process.name", "type": "string", "frecuentes": ["sshd", "sudo"]},
         {"field_path": "message", "type": "text"}]


def test_una_regla_contains_sobre_texto_libre():
    def regla(sel):
        relleno = [f"<134>2025-07-01T11:{i:02d}:00Z web-prod-02 sshd[{i}]: Accepted password for u{i} from 10.0.0.{i} port 5{i} ssh2"
                   for i in range(20)]
        return sd.validar({"es_seguridad": True, "reglas": [_regla(seleccion=sel)]}, TEXTO, AUTH + relleno)
    ok = regla({"process.name": "sshd", "message|contains": "Failed password for root"})
    assert ok["reglas"][0]["seleccion"] == {"process.name": "sshd", "message|contains": "Failed password for root"}
    assert regla({"message|contains": "frase que no está"}) is None
    assert regla({"process.name|contains": "ssh"}) is None, "contains solo sobre texto libre"
    assert regla({"message|startswith": "Failed"}) is None, "solo el modificador contains"
    assert regla({"message|contains": "password"}) is None, "una frase en todas las líneas es demasiado amplia"
    yaml = seguridad.sigma_yaml(ok["reglas"][0], "auth")
    assert '    message|contains: "Failed password for root"' in yaml
    q = seguridad.consulta_de_reglas(ok["reglas"])
    filtros = q["bool"]["should"][0]["bool"]["filter"]
    assert {"terms": {"process.name": ["sshd"]}} in filtros
    assert {"bool": {"minimum_should_match": 1, "should": [{"match_phrase": {"message": "Failed password for root"}}]}} in filtros
    assert sd.consulta_de_reglas(ok["reglas"]) == '(process.name:"sshd" AND message:"Failed password for root")'
