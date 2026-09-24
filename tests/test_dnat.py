"""Las reglas DNAT se crean sí o sí, o el deploy dice que faltan.

El cluster CSS es privado: la plataforma llega a OpenSearch (index template) y
a Dashboards (import) solo por las DNAT del NAT. Un deploy terminó "operativo"
sin ellas —el cluster existía, así que el apply fallido se tomó por un detalle
de security group— y los imports fallaban después sin decir por qué.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest
from fastapi import HTTPException

import main

_TF = pathlib.Path(__file__).resolve().parent.parent / "terraform" / "main.tf"
_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"

OS = "huaweicloud_css_cluster.opensearch_cluster[0]"
LS = "huaweicloud_css_logstash_cluster.logstash_cluster"
D_OS = "huaweicloud_nat_dnat_rule.opensearch[0]"
D_KB = "huaweicloud_nat_dnat_rule.kibana[0]"
D_BE = "huaweicloud_nat_dnat_rule.logstash_beats[0]"


# ── Qué DNAT tendría que haber ──────────────────────────────────────────────
@pytest.mark.parametrize("en_state, beats, faltan", [
    ({OS, LS}, False, ["opensearch", "kibana"]),
    ({OS, LS, D_OS}, False, ["kibana"]),
    ({OS, LS, D_OS, D_KB}, False, []),
    # Cluster existente (no lo crea este state): sus DNAT no son nuestras.
    ({LS}, False, []),
    ({OS, LS, D_OS, D_KB}, True, ["logstash_beats"]),
    ({OS, LS, D_OS, D_KB, D_BE}, True, []),
    # Sin su cluster, un apply dirigido crearía el cluster: no se pide.
    (set(), True, []),
])
def test_las_dnat_que_faltan(en_state, beats, faltan):
    assert main._dnat_faltantes(en_state, beats) == faltan


def test_las_direcciones_del_state_como_las_lista_terraform():
    state = {"resources": [
        {"mode": "managed", "type": "huaweicloud_css_cluster", "name": "opensearch_cluster",
         "instances": [{"index_key": 0}]},
        {"mode": "managed", "type": "huaweicloud_css_logstash_cluster", "name": "logstash_cluster",
         "instances": [{}]},
        {"mode": "managed", "type": "huaweicloud_css_logstash_configuration", "name": "pipeline",
         "instances": [{"index_key": "fintech"}]},
        {"mode": "data", "type": "huaweicloud_networking_port", "name": "os_node",
         "instances": [{"index_key": 0}]},
    ]}
    assert main._direcciones_del_state(state) == {
        OS, LS, 'huaweicloud_css_logstash_configuration.pipeline["fintech"]'}


# ── Los errores de un apply, con nombre ─────────────────────────────────────
_SALIDA = """\
huaweicloud_css_cluster.opensearch_cluster[0]: Creation complete after 14m2s [id=abc]

Error: Your query returned more than one result. Please try a more specific search criteria

  with data.huaweicloud_networking_port.os_node[0],
  on main.tf line 265, in data "huaweicloud_networking_port" "os_node":
 265: data "huaweicloud_networking_port" "os_node" {

╷
│ Error: Error creating security group rule: Security group rule already exists
│
│   with huaweicloud_networking_secgroup_rule.opensearch_public_9200[0],
│   on main.tf line 310, in resource "huaweicloud_networking_secgroup_rule" "opensearch_public_9200":
╵
"""


def test_los_errores_de_terraform_nombran_el_recurso():
    errores = main._errores_de_terraform(_SALIDA.splitlines(keepends=True))
    assert errores == {
        "data.huaweicloud_networking_port.os_node[0]":
            "Your query returned more than one result. Please try a more specific search criteria",
        "huaweicloud_networking_secgroup_rule.opensearch_public_9200[0]":
            "Error creating security group rule: Security group rule already exists",
    }


def test_el_aviso_ya_no_culpa_siempre_a_una_regla_de_security_group():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert "una regla de security group falló" not in src
    assert "El entorno se creó, pero fallaron: " in src


# ── La reparación ───────────────────────────────────────────────────────────
def _correr(gen):
    eventos = []
    try:
        while True:
            eventos.append(json.loads(next(gen)[len("data: "):]))
    except StopIteration as fin:
        return fin.value, eventos


def _fake_apply(vistos, rc=0, salida=()):
    def _fake(terraform_dir, args, desde, hasta, tf_lines, plan=True):
        vistos.append((args, plan))
        tf_lines.extend(salida)
        return rc
        yield  # noqa: unreachable — lo vuelve generador
    return _fake


def test_si_faltan_se_crean_con_un_apply_dirigido(monkeypatch, tmp_path):
    estados = iter([{OS, LS}, {OS, LS, D_OS, D_KB}])
    monkeypatch.setattr(main, "_direcciones_en_state", lambda d: next(estados))
    vistos = []
    monkeypatch.setattr(main, "_correr_apply", _fake_apply(vistos))
    tf_lines = []

    siguen, eventos = _correr(main._asegurar_dnat(tmp_path, False, tf_lines))

    assert siguen == []
    # Solo las que faltan, y sin reemplazar la lista de componentes en pantalla.
    assert vistos == [(["-target=huaweicloud_nat_dnat_rule.opensearch",
                        "-target=huaweicloud_nat_dnat_rule.kibana"], False)]
    paso = [e for e in eventos if e["type"] == "step"][-1]
    assert paso["ok"] and "se crearon" in paso["reason"]


def test_si_estan_todas_no_se_corre_nada(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_direcciones_en_state", lambda d: {OS, LS, D_OS, D_KB})
    vistos = []
    monkeypatch.setattr(main, "_correr_apply", _fake_apply(vistos))
    siguen, eventos = _correr(main._asegurar_dnat(tmp_path, False, []))
    assert siguen == [] and vistos == [] and eventos == []


def test_sin_state_legible_no_se_inventa_un_faltante(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_direcciones_en_state", lambda d: None)
    vistos = []
    monkeypatch.setattr(main, "_correr_apply", _fake_apply(vistos))
    assert _correr(main._asegurar_dnat(tmp_path, True, []))[0] == [] and vistos == []


def test_si_no_se_pueden_crear_el_deploy_lo_dice_con_el_error(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_direcciones_en_state", lambda d: {OS, LS})
    salida = _SALIDA.splitlines(keepends=True)
    monkeypatch.setattr(main, "_correr_apply", _fake_apply([], rc=1, salida=salida))
    tf_lines = []

    siguen, eventos = _correr(main._asegurar_dnat(tmp_path, False, tf_lines))

    assert siguen == ["opensearch", "kibana"]
    paso = [e for e in eventos if e["type"] == "step"][-1]
    assert not paso["ok"] and "more than one result" in paso["reason"]
    assert tf_lines == salida, "la salida del apply dirigido queda en el log del deploy"


def test_si_se_corta_la_lectura_siguen_faltando(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_direcciones_en_state", lambda d: {OS, LS})
    monkeypatch.setattr(main, "_correr_apply", _fake_apply([], rc=None))
    assert _correr(main._asegurar_dnat(tmp_path, False, []))[0] == ["opensearch", "kibana"]


def test_el_deploy_verifica_las_dnat_despues_de_todo_apply():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def _deploy_stream_gen_raw(")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    aplica = cuerpo.index("yield from _aplicar_pasos(")
    asegura = cuerpo.index("yield from _asegurar_dnat(")
    salida = cuerpo.index('["terraform", "output", "-json"]')
    assert aplica < asegura < salida
    # No depende de start_ingestion: "Iniciar ingesta" también repara.
    assert "if not request.start_ingestion" not in cuerpo[aplica:asegura]


# ── /apply-schema ───────────────────────────────────────────────────────────
def test_apply_schema_sin_dnat_dice_por_que(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_cluster_with_public_access",
                        lambda d: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main.tfstate, "read_state", lambda d: {"resources": [
        {"mode": "managed", "type": "huaweicloud_css_cluster", "name": "opensearch_cluster",
         "instances": [{"index_key": 0}]}]})
    req = main.TerraformDeployRequest(pipeline_conf="input {} output {}")

    with pytest.raises(HTTPException) as exc:
        main.apply_schema(req)

    assert exc.value.detail["stage"] == "dnat_faltante"
    assert "OpenSearch (9200), Dashboards" in exc.value.detail["message"]


def test_el_front_ofrece_crear_las_dnat_y_reintenta():
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("async function infraApplySchema(btn)")
    fn = html[i:html.index("async function _seguirDeploy(", i)]
    assert "throw detailError(data)" in fn
    assert "err.stage === 'dnat_faltante'" in fn and "infraRepararDnat(fix)" in fn

    j = html.index("async function infraRepararDnat(btn)")
    rep = html[j:html.index("async function infraStartIngestion(", j)]
    # Fase 1: no arranca la ingesta de nada; y no es "fresh" (borraría el registro).
    assert "start_ingestion: false, fresh_deploy: false" in rep
    assert "infraApplySchema(aplicar)" in rep


# ── Terraform ───────────────────────────────────────────────────────────────
def test_el_puerto_del_nodo_se_busca_en_la_subred_del_deploy():
    """Buscando solo por IP, otra VPC con el mismo rango da dos puertos: el data
    source falla por ambiguo y las DNAT no se crean."""
    tf = _TF.read_text(encoding="utf-8")
    for nombre in ("os_node", "logstash_node"):
        bloque = tf[tf.index(f'data "huaweicloud_networking_port" "{nombre}"'):]
        bloque = bloque[:bloque.index("\n}")]
        assert re.search(r"network_id\s*=\s*var\.subnet_id", bloque), nombre
        assert "fixed_ip" in bloque


_MINI = """
resource "terraform_data" "sg" {
  provisioner "local-exec" { command = "exit 1" }
}
resource "terraform_data" "cluster" {}
resource "terraform_data" "dnat" { input = terraform_data.cluster.id }
"""


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform no está instalado")
def test_un_error_independiente_no_frena_las_dnat(tmp_path):
    """Por qué la causa no era la regla de SG: Terraform sigue creando lo que no
    depende del recurso que falló. Si faltaban las DNAT, falló la DNAT misma
    (o su data source), no otra cosa antes."""
    (tmp_path / "main.tf").write_text(_MINI, encoding="utf-8")
    run = lambda *a: subprocess.run(["terraform", *a, "-no-color"], cwd=tmp_path,  # noqa: E731
                                    capture_output=True, text=True, timeout=180)
    assert run("init", "-input=false").returncode == 0
    assert run("apply", "-auto-approve", "-input=false").returncode != 0
    assert "terraform_data.dnat" in run("state", "list").stdout


def test_el_apply_dirigido_no_reemplaza_la_lista_de_la_pantalla(monkeypatch, tmp_path):
    """El `plan` del apply de reparación traería solo las DNAT: la pantalla
    perdería los clusters y la red que ya mostraba como listos."""
    salida = ("  # huaweicloud_nat_dnat_rule.kibana[0] will be created\n"
              "Plan: 1 to add, 0 to change, 0 to destroy.\n"
              "huaweicloud_nat_dnat_rule.kibana[0]: Creating...\n"
              "huaweicloud_nat_dnat_rule.kibana[0]: Creation complete after 3s [id=x]\n")

    class _Proc:
        def __init__(self, *a, **k):
            import io
            self.stdout = io.StringIO(salida)
            self.returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(main.subprocess, "Popen", _Proc)

    def tipos(plan):
        rc, eventos = _correr(main._correr_apply(tmp_path, [], 92, 92, [], plan=plan))
        assert rc == 0
        return {e["type"] for e in eventos}

    assert "plan" in tipos(True)
    sin = tipos(False)
    assert "plan" not in sin and "item" in sin
