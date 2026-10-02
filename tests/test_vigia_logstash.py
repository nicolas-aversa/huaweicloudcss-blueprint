"""Las pipelines que se caen al arrancar se rearrancan solas.

Caso real (deploy de 9 casos): al activarlas juntas, Logstash 7.10 de CSS se
cayó entero (`NameError: uninitialized constant Gem::Specification`), las 9
quedaron en `failed` y Terraform esperó hasta su timeout de 10 minutos. Volver a
arrancarlas desde la consola funcionó: ahora lo hace la plataforma. Y la
activación que quedó marcada (`tainted`) con las pipelines ya corriendo se
desmarca, en vez de pararlas y volver a arrancarlas."""
import io
import json
import pathlib

import main
import recuperacion
import vigia_logstash

NOMBRES = ["pipeline-siem", "pipeline-cts"]


class _Reloj:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _vigia(estados, reintentos=3, arranque=None):
    """Un vigía sobre una secuencia de estados; devuelve (vigía, arranques, reloj)."""
    arranques, reloj, it = [], _Reloj(), iter(estados)

    def arrancar(nombres):
        arranques.append(list(nombres))
        if arranque:
            raise arranque

    return vigia_logstash.Vigia(lambda: next(it), arrancar, NOMBRES, reintentos=reintentos,
                                enfriamiento_s=90, reloj=reloj), arranques, reloj


def test_los_nombres_activos_salen_del_tfvars():
    tfvars = {"pipelines": {"siem": {"start_ingestion": True}, "cts": {"start_ingestion": False},
                            "x" * 40: {"start_ingestion": True}}}
    assert vigia_logstash.nombres_activos(tfvars) == ["pipeline-siem", ("pipeline-" + "x" * 40)[:32]]
    assert vigia_logstash.nombres_activos({}) == []


def test_si_arrancan_bien_no_hace_nada():
    v, arranques, _ = _vigia([{"pipeline-siem": "starting"}, {n: "working" for n in NOMBRES}])
    assert v.mirar() == [] and v.mirar() == []
    assert arranques == [] and v.terminado


def test_si_fallan_las_rearranca_todas_juntas_y_avisa_cuando_corren():
    falladas = {n: "failed" for n in NOMBRES}
    v, arranques, reloj = _vigia([falladas, falladas, falladas, {n: "working" for n in NOMBRES}])
    (paso,) = v.mirar()
    assert arranques == [NOMBRES], "todas, como la consola: el proceso de Logstash se cayó entero"
    assert paso["ok"] and "reintento 1 de 3" in paso["reason"] and "2 de 2 fallaron" in paso["reason"]
    reloj.t = 30
    assert v.mirar() == [] and len(arranques) == 1, "el failed todavía es el del intento anterior"
    reloj.t = 100
    v.mirar()
    assert len(arranques) == 2
    (fin,) = v.mirar()
    assert fin == {"name": "Logstash · arranque de las pipelines", "ok": True,
                   "reason": "corriendo después de 2 reintento(s)"}
    assert v.terminado and v.mirar() == []


def test_si_una_sola_fallo_tambien():
    v, arranques, _ = _vigia([{"pipeline-siem": "working", "pipeline-cts": "failed"}])
    v.mirar()
    assert arranques == [NOMBRES]


def test_despues_de_los_reintentos_avisa_y_no_insiste():
    falladas = {n: "failed" for n in NOMBRES}
    v, arranques, reloj = _vigia([falladas] * 5, reintentos=2)
    for t in (0, 100, 200):
        reloj.t = t
        pasos = v.mirar()
    assert len(arranques) == 2
    (paso,) = pasos
    assert not paso["ok"] and "después de 2 reintentos" in paso["reason"] and "Iniciar ingesta" in paso["reason"]
    reloj.t = 300
    assert v.mirar() == [] and len(arranques) == 2


def test_si_no_se_puede_leer_o_arrancar_no_rompe():
    def listar():
        raise RuntimeError("sin red")

    v = vigia_logstash.Vigia(listar, lambda n: None, NOMBRES)
    assert v.mirar() == [] and not v.terminado
    v, arranques, _ = _vigia([{n: "failed" for n in NOMBRES}], arranque=RuntimeError("CSS.0001"))
    (paso,) = v.mirar()
    assert not paso["ok"] and "CSS.0001" in paso["reason"]


def test_en_segundo_plano_los_pasos_quedan_en_la_cola():
    falladas = {n: "failed" for n in NOMBRES}
    v, arranques, _ = _vigia([falladas, {n: "working" for n in NOMBRES}])
    with vigia_logstash.VigiaEnSegundoPlano(v, intervalo_s=0.01) as bg:
        bg._hilo.join(timeout=2)
        assert not bg._hilo.is_alive(), "con las pipelines corriendo el vigía termina solo"
    pasos = bg.pendientes()
    assert [p["ok"] for p in pasos] == [True, True] and arranques == [NOMBRES]
    assert bg.pendientes() == []


# ── En el deploy ────────────────────────────────────────────────────────────
class _VigiaFalso:
    def __init__(self, pasos):
        self.pasos, self.entradas = list(pasos), []

    def __enter__(self):
        self.entradas.append("in")
        return self

    def __exit__(self, *a):
        self.entradas.append("out")

    def pendientes(self):
        out, self.pasos = self.pasos, []
        return out


def test_los_pasos_del_vigia_salen_en_el_stream_del_apply(monkeypatch, tmp_path):
    class _Popen:
        returncode = 0

        def __init__(self, *a, **k):
            self.stdout = io.StringIO("Still creating... [10s]\nStill creating... [20s]\n")

        def wait(self):
            return 0

    monkeypatch.setattr(main.subprocess, "Popen", _Popen)
    v = _VigiaFalso([{"name": "Logstash · arranque de las pipelines", "ok": True, "reason": "reintento 1 de 3"}])
    eventos = [json.loads(e[len("data: "):]) for e in main._correr_apply(tmp_path, [], 0, 1, [], vigia=v)]
    paso = {"type": "step", "name": "Logstash · arranque de las pipelines", "ok": True, "reason": "reintento 1 de 3"}
    assert paso in eventos
    segunda = next(i for i, e in enumerate(eventos) if e.get("message") == "Still creating... [20s]")
    assert eventos.index(paso) < segunda, "sale mientras Terraform espera, no al final"


def test_el_vigia_solo_corre_durante_la_activacion(monkeypatch, tmp_path):
    (tmp_path / "deploy.auto.tfvars.json").write_text(json.dumps({"pipelines": {
        "fintech": {"pipeline_conf": "x", "start_ingestion": True}}}), encoding="utf-8")
    vigias, con_vigia = [], []

    def _fake(terraform_dir, args, desde, hasta, tf_lines, vigia=None, **_):
        con_vigia.append((args, vigia))
        return 0
        yield  # noqa

    def crear():
        vigias.append(_VigiaFalso([]))
        return vigias[-1]

    monkeypatch.setattr(main, "_correr_apply", _fake)
    pasos = main._pasos_del_apply(True, {}, {"fintech": {"pipeline_conf": "x"}}, ["fintech"], set())
    list(main._aplicar_pasos(tmp_path, pasos, [], vigia=crear))
    assert [a for a, _ in con_vigia] == [[], [main._TARGET_ACTIVACION]]
    assert con_vigia[0][1] is None and con_vigia[1][1] is vigias[0]
    assert len(vigias) == 1 and vigias[0].entradas == ["in", "out"]


def test_el_deploy_le_pasa_el_vigia():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert "vigia=lambda: _vigia_de_activacion(request, terraform_dir))" in src


def test_terraform_espera_lo_que_tardan_los_reintentos():
    tf = (pathlib.Path(main.__file__).parent / "terraform" / "main.tf").read_text(encoding="utf-8")
    bloque = tf[tf.index('resource "huaweicloud_css_logstash_pipeline" "pipeline"'):]
    bloque = bloque[:bloque.index("\n}\n")]
    assert 'create = "30m"' in bloque and 'update = "30m"' in bloque


# ── La activación marcada con las pipelines corriendo ──────────────────────
def _activacion(status="tainted"):
    return {"resources": [{"mode": "managed", "type": "huaweicloud_css_logstash_pipeline", "name": "pipeline",
                           "instances": [{"index_key": 0, "status": status,
                                          "attributes": {"id": "ls-1", "names": NOMBRES}}]}]}


def test_se_desmarca_la_activacion_si_ya_corren():
    corriendo = {n: "working" for n in NOMBRES}
    assert recuperacion.a_desmarcar(_activacion(), {}, corriendo) == [
        {"direccion": "huaweicloud_css_logstash_pipeline.pipeline[0]", "que": "activación de 2 pipelines (ya corren)"}]
    assert recuperacion.a_desmarcar(_activacion(), {}, {**corriendo, "pipeline-cts": "failed"}) == []
    assert recuperacion.a_desmarcar(_activacion(), {}, None) == []
    assert recuperacion.a_desmarcar(_activacion(""), {}, corriendo) == []
    assert recuperacion.a_desmarcar(_activacion(), {"ls-1": "200"}, {}) == [], "no es un cluster"


def test_la_recuperacion_lee_el_estado_de_las_pipelines(monkeypatch, tmp_path):
    monkeypatch.setattr(main.tfstate, "read_state", lambda td: _activacion())
    monkeypatch.setattr(main, "_inventario_huawei", lambda *a: {
        "clusters": [], "reglas_dnat": [], "confs_logstash": [], "pipelines": {n: "working" for n in NOMBRES}})
    monkeypatch.setattr(main, "_huawei_infra_tfvars", lambda: {"subnet_id": "s"})
    corridas = []

    class _P:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **k: corridas.append(cmd) or _P())

    class _Req:
        obs_access_key, obs_secret_key, project_name, existing_opensearch_endpoint, cases = "AK", "SK", "p", "", []

    eventos = [json.loads(e[len("data: "):]) for e in main._recuperar_deploy_cortado(_Req(), tmp_path)]
    assert corridas == [["terraform", "untaint", "-no-color", "huaweicloud_css_logstash_pipeline.pipeline[0]"]]
    assert eventos[0]["ok"] and "activación de 2 pipelines" in eventos[0]["name"]


def test_sin_credenciales_no_hay_vigia(tmp_path):
    class _Req:
        obs_access_key, obs_secret_key = "", ""

    assert main._vigia_de_activacion(_Req(), tmp_path) is None


def test_el_vigia_mira_el_logstash_del_state(monkeypatch, tmp_path):
    (tmp_path / "deploy.auto.tfvars.json").write_text(json.dumps({"pipelines": {
        "siem": {"start_ingestion": True}, "cts": {"start_ingestion": True}}}), encoding="utf-8")
    monkeypatch.setattr(main.tfstate, "read_state", lambda td: {"resources": [
        {"mode": "managed", "type": "huaweicloud_css_logstash_cluster", "name": "logstash_cluster",
         "instances": [{"attributes": {"id": "ls-9"}}]}]})
    llamados = []

    class _Css:
        def list_pipelines(self, req):
            llamados.append(("list", req.cluster_id))
            return type("R", (), {"pipelines": [type("P", (), {"name": n, "status": "failed"})() for n in NOMBRES]})()

        def start_pipeline(self, req):
            llamados.append(("start", req.cluster_id, sorted(req.body.names)))

    monkeypatch.setattr(main, "_cliente_css", lambda ak, sk: _Css())

    class _Req:
        obs_access_key, obs_secret_key = "AK", "SK"

    bg = main._vigia_de_activacion(_Req(), tmp_path)
    assert sorted(bg.vigia.nombres) == sorted(NOMBRES)
    bg.vigia.mirar()
    assert llamados == [("list", "ls-9"), ("start", "ls-9", sorted(NOMBRES))]
    monkeypatch.setattr(main.tfstate, "read_state", lambda td: {"resources": []})
    assert main._vigia_de_activacion(_Req(), tmp_path) is None, "sin Logstash en el state no hay qué mirar"


def test_el_inventario_trae_el_estado_de_las_pipelines(monkeypatch):
    class _Css:
        def list_clusters_details(self, req):
            return type("R", (), {"clusters": []})()

        def list_confs(self, req):
            return type("R", (), {"confs": []})()

        def list_pipelines(self, req):
            assert req.cluster_id == "ls-9"
            return type("R", (), {"pipelines": [type("P", (), {"name": "pipeline-siem", "status": "working"})()]})()

    monkeypatch.setattr(main, "_cliente_css", lambda ak, sk: _Css())
    monkeypatch.setattr(main, "get_huawei_project_id", lambda: "pid")
    monkeypatch.setattr(main, "get_region", lambda: "la-south-2")
    state = {"resources": [{"mode": "managed", "type": "huaweicloud_css_logstash_cluster", "name": "logstash_cluster",
                            "instances": [{"attributes": {"id": "ls-9"}}]}]}
    assert main._inventario_huawei("AK", "SK", state)["pipelines"] == {"pipeline-siem": "working"}
