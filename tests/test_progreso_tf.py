"""El progreso del deploy solo avanza, y cada componente tiene el suyo.

El log de abajo es como lo escribe `terraform apply -no-color`: primero el plan,
después los recursos EN PARALELO —la red termina mientras el cluster de
OpenSearch sigue creándose—, con duraciones de más de un minuto (`1m10s`) y un
recurso que se modifica (`[id=…, 10s elapsed]`). Eran justo las tres cosas que
hacían bajar la barra.
"""
import json

import pytest

import main
import progreso_tf

LOG = """
Terraform will perform the following actions:

  # huaweicloud_css_cluster.opensearch_cluster[0] will be created
  + resource "huaweicloud_css_cluster" "opensearch_cluster" {
      + name = "log-analytics-os"
    }

  # huaweicloud_css_logstash_cluster.logstash_cluster will be created
  # huaweicloud_css_logstash_configuration.pipeline["fintech"] will be created
  # huaweicloud_css_logstash_configuration.pipeline["siem"] will be created
  # huaweicloud_css_logstash_pipeline.pipeline[0] will be updated in-place
  # huaweicloud_nat_gateway.nat will be created
  # huaweicloud_vpc_eip.nat_eip will be created
  # huaweicloud_networking_secgroup_rule.opensearch_public_9200 will be created
  # huaweicloud_nat_dnat_rule.opensearch[0] will be created
  # huaweicloud_nat_dnat_rule.kibana[0] will be created
  # data.huaweicloud_networking_port.os_node will be read during apply

Plan: 7 to add, 1 to change, 0 to destroy.
huaweicloud_nat_gateway.nat: Creating...
huaweicloud_css_cluster.opensearch_cluster[0]: Creating...
huaweicloud_vpc_eip.nat_eip: Creating...
huaweicloud_vpc_eip.nat_eip: Creation complete after 6s [id=eip-1]
huaweicloud_nat_gateway.nat: Still creating... [10s elapsed]
huaweicloud_css_cluster.opensearch_cluster[0]: Still creating... [10s elapsed]
huaweicloud_nat_gateway.nat: Creation complete after 21s [id=nat-1]
huaweicloud_networking_secgroup_rule.opensearch_public_9200: Creating...
huaweicloud_networking_secgroup_rule.opensearch_public_9200: Creation complete after 1s [id=sg-1]
huaweicloud_css_cluster.opensearch_cluster[0]: Still creating... [1m10s elapsed]
huaweicloud_css_cluster.opensearch_cluster[0]: Still creating... [9m0s elapsed]
huaweicloud_css_cluster.opensearch_cluster[0]: Still creating... [18m30s elapsed]
huaweicloud_css_cluster.opensearch_cluster[0]: Creation complete after 18m41s [id=os-1]
data.huaweicloud_networking_port.os_node: Reading...
huaweicloud_nat_dnat_rule.opensearch[0]: Creating...
huaweicloud_nat_dnat_rule.kibana[0]: Creating...
huaweicloud_nat_dnat_rule.opensearch[0]: Creation complete after 4s [id=d1]
huaweicloud_nat_dnat_rule.kibana[0]: Creation complete after 5s [id=d2]
huaweicloud_css_logstash_cluster.logstash_cluster: Creating...
huaweicloud_css_logstash_cluster.logstash_cluster: Still creating... [5m0s elapsed]
huaweicloud_css_logstash_cluster.logstash_cluster: Creation complete after 9m12s [id=ls-1]
huaweicloud_css_logstash_configuration.pipeline["fintech"]: Creating...
huaweicloud_css_logstash_configuration.pipeline["siem"]: Creating...
huaweicloud_css_logstash_configuration.pipeline["siem"]: Creation complete after 40s [id=c2]
huaweicloud_css_logstash_configuration.pipeline["fintech"]: Creation complete after 45s [id=c1]
huaweicloud_css_logstash_pipeline.pipeline[0]: Modifying... [id=p0]
huaweicloud_css_logstash_pipeline.pipeline[0]: Still modifying... [id=p0, 10s elapsed]
huaweicloud_css_logstash_pipeline.pipeline[0]: Modifications complete after 22s [id=p0]

Apply complete! Resources: 7 added, 1 changed, 0 destroyed.
""".strip("\n")


def _correr(log=LOG):
    p = progreso_tf.ProgresoApply()
    eventos = []
    for linea in log.splitlines():
        eventos += p.linea(linea + "\n")
    return p, eventos


# ── Duraciones ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("linea, segundos", [
    ("x: Still creating... [40s elapsed]", 40),
    ("x: Still creating... [1m10s elapsed]", 70),
    ("x: Still creating... [18m30s elapsed]", 1110),
    ("x: Still creating... [1h2m3s elapsed]", 3723),
    ("x: Still modifying... [id=abc-123, 10s elapsed]", 10),
    ("x: Creating...", 0),
])
def test_se_leen_todas_las_duraciones(linea, segundos):
    assert progreso_tf.transcurrido(linea) == segundos


# ── El global ───────────────────────────────────────────────────────────────
def test_el_global_nunca_baja_aunque_los_recursos_vayan_en_paralelo():
    _, eventos = _correr()
    globales = [e["fraccion"] for e in eventos if e["type"] == "apply"]

    assert globales == sorted(globales), globales
    assert globales[-1] == 1.0
    # Y avanza de verdad durante los 18 minutos del cluster, no a saltos al final.
    assert 0.2 < max(g for g in globales if g < 1.0) < 1.0


def test_el_cluster_pesa_lo_que_tarda():
    """Que terminen la red y la EIP (segundos) no puede mover la barra como
    los quince minutos del cluster."""
    lineas = LOG.splitlines()
    fin_red = lineas.index("huaweicloud_networking_secgroup_rule.opensearch_public_9200: "
                           "Creation complete after 1s [id=sg-1]")
    p, eventos = _correr("\n".join(lineas[:fin_red + 1]))   # hasta que termina la red
    listos = {e["key"] for e in eventos if e["type"] == "item" and e["done"]}
    assert {"nat", "eip", "sg"} <= listos
    assert p.fraccion() < 0.1


def test_un_recurso_que_el_plan_no_anuncio_no_hace_bajar_el_global():
    p, _ = _correr("\n".join(LOG.splitlines()[:40]))
    antes = p.fraccion()
    p.linea("huaweicloud_otro_recurso.x: Creating...\n")
    assert p.fraccion() >= antes


def test_sin_cambios_el_apply_termina_en_cien():
    p, eventos = _correr("No changes. Your infrastructure matches the configuration.\n"
                         "Apply complete! Resources: 0 added, 0 changed, 0 destroyed.")
    assert eventos[0] == {"type": "plan", "items": []}
    assert p.fraccion() == 1.0


# ── Los componentes ─────────────────────────────────────────────────────────
def test_el_plan_anuncia_los_componentes_antes_de_empezar():
    _, eventos = _correr()
    plan = eventos[0]
    assert plan["type"] == "plan"
    # Cada servicio por separado (no "la red" como una caja cerrada), y cada
    # regla DNAT con su fila: son las que dan acceso al cluster privado.
    assert [c["label"] for c in plan["items"]] == [
        "CSS OpenSearch cluster", "CSS Logstash cluster", "NAT gateway", "EIP pública",
        "DNAT :5601 · Dashboards", "DNAT :9200 · OpenSearch",
        "Reglas de SG",
        "Pipeline · fintech", "Pipeline · siem", "Activar pipelines"]
    assert all(c["percent"] == 0 and not c["done"] and c["estado"] == "En espera"
               for c in plan["items"])
    # El data source no es un recurso que se cree.
    assert "Otros recursos" not in [c["label"] for c in plan["items"]]


def test_cada_componente_solo_avanza_y_termina_listo():
    _, eventos = _correr()
    por_componente = {}
    for e in eventos:
        if e["type"] == "item":
            por_componente.setdefault(e["key"], []).append(e)
    for key, evs in por_componente.items():
        pcts = [e["percent"] for e in evs]
        assert pcts == sorted(pcts), (key, pcts)
        assert evs[-1]["done"] and evs[-1]["percent"] == 100 and evs[-1]["estado"] == "listo", key


def test_el_estado_dice_que_se_esta_haciendo():
    _, eventos = _correr()
    estados = {(e["key"], e["estado"]) for e in eventos if e["type"] == "item"}
    assert ("opensearch", "Creando") in estados
    assert ("activar", "Actualizando") in estados


def test_la_fase_es_lo_que_se_esta_esperando():
    """Con la red ya lista y solo el cluster en curso, la fase es el cluster."""
    lineas = LOG.splitlines()
    # Hasta el "9m0s" del cluster: la red ya terminó, el cluster sigue.
    _, eventos = _correr("\n".join(lineas[:lineas.index(
        "huaweicloud_css_cluster.opensearch_cluster[0]: Still creating... [9m0s elapsed]") + 1]))
    ultima = [e for e in eventos if e["type"] == "apply"][-1]
    assert ultima["phase"] == "CSS OpenSearch cluster"
    assert ultima["message"] == "Creando CSS OpenSearch cluster…"


def test_un_recurso_que_recien_arranca_ya_esta_en_curso():
    """"Creating..." es 0%, pero no "En espera": ya se está creando."""
    lineas = LOG.splitlines()
    _, eventos = _correr("\n".join(lineas[:lineas.index(
        "huaweicloud_css_logstash_cluster.logstash_cluster: Creating...") + 1]))
    ls = [e for e in eventos if e["type"] == "item" and e["key"] == "logstash"][-1]
    assert ls["estado"] == "Creando" and ls["percent"] == 0
    assert [e for e in eventos if e["type"] == "apply"][-1]["phase"] == "CSS Logstash cluster"


def test_un_reemplazo_primero_elimina_y_despues_crea():
    log = ("  # huaweicloud_css_logstash_cluster.logstash_cluster must be replaced\n"
           "Plan: 1 to add, 0 to change, 1 to destroy.\n"
           "huaweicloud_css_logstash_cluster.logstash_cluster: Destroying... [id=ls-1]\n"
           "huaweicloud_css_logstash_cluster.logstash_cluster: Destruction complete after 4m0s\n"
           "huaweicloud_css_logstash_cluster.logstash_cluster: Creating...\n"
           "huaweicloud_css_logstash_cluster.logstash_cluster: Creation complete after 9m0s [id=ls-2]\n")
    _, eventos = _correr(log)
    items = [e for e in eventos if e["type"] == "item"]
    assert items[0]["estado"] == "Reemplazando"
    # Terminar de eliminar no es terminar el recurso.
    tras_eliminar = next(e for e in items if 0 < e["percent"] < 100)
    assert not tras_eliminar["done"]
    assert items[-1]["done"]


def test_crear_antes_de_eliminar_no_hace_retroceder_al_recurso():
    """Con `create_before_destroy` Terraform crea primero y elimina después:
    el "Destroying..." no puede devolver el componente a cero."""
    log = ("  # huaweicloud_css_logstash_cluster.logstash_cluster must be replaced\n"
           "Plan: 1 to add, 0 to change, 1 to destroy.\n"
           "huaweicloud_css_logstash_cluster.logstash_cluster: Creating...\n"
           "huaweicloud_css_logstash_cluster.logstash_cluster: Still creating... [8m0s elapsed]\n"
           "huaweicloud_css_logstash_cluster.logstash_cluster: Destroying... [id=ls-1]\n"
           "huaweicloud_css_logstash_cluster.logstash_cluster: Destruction complete after 3m0s\n")
    _, eventos = _correr(log)
    pcts = [e["percent"] for e in eventos if e["type"] == "item"]
    assert pcts == sorted(pcts) and pcts[-1] > 0, pcts


def test_un_recurso_que_tarda_mas_de_lo_previsto_se_sigue_moviendo():
    """El cluster de OpenSearch se estima en 15 minutos: a los 16 y a los 20
    tiene que seguir avanzando, sin llegar a 100% hasta que Terraform lo diga."""
    log = ("  # huaweicloud_css_cluster.opensearch_cluster[0] will be created\n"
           "Plan: 1 to add, 0 to change, 0 to destroy.\n"
           "huaweicloud_css_cluster.opensearch_cluster[0]: Still creating... [16m0s elapsed]\n"
           "huaweicloud_css_cluster.opensearch_cluster[0]: Still creating... [20m0s elapsed]\n")
    _, eventos = _correr(log)
    a, b = [e["fraccion"] for e in eventos if e["type"] == "apply"]
    assert 0.9 < a < b < 1.0, (a, b)


def test_con_varios_en_curso_la_fase_dice_cuantos():
    """Nombrar solo al que más falta ("Creando OpenSearch cluster…") escondía
    lo que se creaba al mismo tiempo."""
    log = ("  # huaweicloud_css_cluster.opensearch_cluster[0] will be created\n"
           "  # huaweicloud_css_logstash_pipeline.pipeline[0] will be updated in-place\n"
           "Plan: 1 to add, 1 to change, 0 to destroy.\n"
           "huaweicloud_css_cluster.opensearch_cluster[0]: Creating...\n"
           "huaweicloud_css_logstash_pipeline.pipeline[0]: Modifying... [id=p0]\n")
    _, eventos = _correr(log)
    ultima = [e for e in eventos if e["type"] == "apply"][-1]
    assert ultima["phase"] == "En paralelo"
    assert ultima["message"] == "2 servicios en paralelo · 0 de 2 listos"

    _, eventos = _correr(log + "huaweicloud_css_logstash_pipeline.pipeline[0]: "
                               "Modifications complete after 20s [id=p0]\n")
    ultima = [e for e in eventos if e["type"] == "apply"][-1]
    assert ultima["message"] == "Creando CSS OpenSearch cluster…", "queda uno: se nombra"

    # En el deploy entero: la EIP ya terminó; el NAT y el cluster siguen.
    lineas = LOG.splitlines()
    _, eventos = _correr("\n".join(lineas[:lineas.index(
        "huaweicloud_css_cluster.opensearch_cluster[0]: Still creating... [10s elapsed]") + 1]))
    ultima = [e for e in eventos if e["type"] == "apply"][-1]
    assert ultima["message"] == "2 servicios en paralelo · 1 de 10 listos"


def test_un_data_source_no_es_un_recurso_que_se_crea():
    p = progreso_tf.ProgresoApply()
    assert p._registrar("data.huaweicloud_networking_port.os_node", "crear") is None
    assert p._registrar("huaweicloud_vpc_eip.nat_eip", "crear") is not None


# ── El stream ───────────────────────────────────────────────────────────────
def test_el_stream_no_deja_bajar_el_porcentaje(monkeypatch, tmp_path):
    """La red de seguridad del envoltorio: un paso con un número a mano más
    bajo que el anterior sale con el anterior."""
    def _crudo(*a, **k):
        for pct in (5, 40, 99, 96, 97):
            yield main._sse({"type": "progress", "percent": pct, "phase": "x"})
        yield main._sse({"type": "log", "message": "sigue"})

    monkeypatch.setattr(main, "_deploy_stream_gen_raw", _crudo)
    req = main.TerraformDeployRequest(pipeline_conf="filter { }")
    salida = [json.loads(e[len("data: "):]) for e in
              main._deploy_stream_gen(req, tmp_path, None, None)]

    assert [e["percent"] for e in salida if e["type"] == "progress"] == [5, 40, 99, 99, 99]
    assert salida[-1] == {"type": "log", "message": "sigue"}


def test_los_tramos_del_global_estan_en_orden():
    assert (main._PCT_APPLY_DESDE < main._PCT_APPLY_HASTA < main._PCT_OUTPUTS
            <= main._PCT_FINALIZANDO < main._PCT_SECURITY < main._PCT_INGESTA_DESDE
            < main._PCT_INGESTA_HASTA < 100)
    assert main._pct_apply(0) == main._PCT_APPLY_DESDE
    assert main._pct_apply(1) == main._PCT_APPLY_HASTA


def test_cada_regla_dnat_es_su_propia_fila():
    p = progreso_tf.ProgresoApply()
    for d in ("huaweicloud_nat_dnat_rule.opensearch[0]", "huaweicloud_nat_dnat_rule.kibana[0]",
              "huaweicloud_nat_dnat_rule.logstash_beats[0]", "huaweicloud_nat_dnat_rule.otra[0]"):
        p._registrar(d, "crear")
    assert [(c["key"], c["label"]) for c in p.componentes()] == [
        ("dnat:kibana", "DNAT :5601 · Dashboards"),
        ("dnat:logstash_beats", "DNAT · Logstash Beats"),
        ("dnat:opensearch", "DNAT :9200 · OpenSearch"),
        ("dnat:otra", "DNAT · otra"),
    ]


def test_las_etiquetas_entran_en_una_columna_y_las_dnat_dicen_su_puerto():
    """La lista va en dos columnas: "DNAT → OpenSearch Dashboards" se cortaba
    ("DNAT → OpenSearch Das…"), y era la única DNAT sin puerto."""
    fijas = list(progreso_tf._ETIQUETAS.values()) + list(progreso_tf._DNAT.values())
    largas = [e for e in fijas if len(e) > 23]
    assert not largas, largas
    assert ":9200" in progreso_tf._DNAT["opensearch"]
    assert ":5601" in progreso_tf._DNAT["kibana"]
    # 5601 es el default de `kibana_port`, y el backend no lo cambia.
    tf = (progreso_tf.__file__.rsplit("progreso_tf.py", 1)[0] + "terraform/main.tf")
    bloque = open(tf, encoding="utf-8").read()
    bloque = bloque[bloque.index('variable "kibana_port"'):]
    assert "default     = 5601" in bloque[:bloque.index("}")]
    assert "kibana_port" not in open(main.__file__, encoding="utf-8").read()
