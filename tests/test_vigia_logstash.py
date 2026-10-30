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
from types import SimpleNamespace as _NS

import pytest

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


def test_no_arranca_las_que_todavia_no_se_arrancaron():
    """Las que se suman a una activación se arrancan de a una: las que todavía
    no les tocó no figuran (o están `stopped`). Si el vigía las arrancaba, al
    llegar su turno CSS lo rechazaba ("the conf[pipeline-x] is being executed")."""
    for falta in ({}, {"pipeline-cts": "stopped"}):
        v, arranques, _ = _vigia([{"pipeline-siem": "failed", **falta}])
        (paso,) = v.mirar()
        assert arranques == [["pipeline-siem"]] and paso["ok"]


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


# ── Con más de 10 pipelines ─────────────────────────────────────────────────
# Caso real (11 casos): CSS lista las pipelines de a 10. El provider leía una
# sola página, "perdía" una que corría, la volvía a arrancar y CSS rechazaba
# cada "Iniciar ingesta" ("CSS.5090 ... the conf[pipeline-x] is being executed").
ONCE = [f"pipeline-{i:02d}" for i in range(11)]


class _CssPaginado:
    """Como CSS: de a 10 si no se pide otra cosa; las pipelines cuentan el
    `offset` desde 0 y las configuraciones desde 1 (con 0, CSS.0001)."""

    def __init__(self, nombres, estados=None):
        self.nombres = list(nombres)
        self.estados = dict(estados if estados is not None else {n: "working" for n in nombres})
        self.hot = []

    def list_pipelines(self, req):
        off, lim = int(req.offset or 0), int(req.limit or 10)
        hay = [n for n in self.nombres if n in self.estados]
        return _NS(pipelines=[_NS(name=n, status=self.estados[n]) for n in hay[off:off + lim]])

    def list_confs(self, req):
        off, lim = int(req.offset or 1), int(req.limit or 10)
        if off < 1:
            raise RuntimeError("CSS.0001 offset")
        return _NS(confs=[_NS(name=n) for n in self.nombres[off - 1:off - 1 + lim]], total_size=len(self.nombres))

    def start_hot_pipeline(self, req):
        nombre = req.body.name
        if self.estados.get(nombre) not in (None, "stopped"):
            raise RuntimeError(f"CSS.5090 the conf[{nombre}] is being executed")
        self.hot.append((req.cluster_id, nombre, req.body.keep_alive))
        self.estados[nombre] = "working"


@pytest.mark.parametrize("pagina", [4, 100])
def test_las_listas_de_css_se_leen_enteras(monkeypatch, pagina):
    monkeypatch.setattr(main, "_PAGINA_CSS", pagina)
    css = _CssPaginado(ONCE)
    assert sorted(main._estados_de_pipelines(css, "ls")) == ONCE
    assert main._confs_de_logstash(css, "ls") == ONCE


class _Cluster:
    """Un Logstash en el tiempo: lo arrancado pasa por `starting` y a la mirada
    siguiente queda `working` (o `failed`, una vez, si está en `caen`)."""

    def __init__(self, estados, caen=(), siempre_arrancando=()):
        self.estados, self.caen, self.lentas = dict(estados), set(caen), set(siempre_arrancando)
        self.hot, self.start = [], []

    def listar(self):
        out = dict(self.estados)
        for n, s in list(self.estados.items()):
            if s == "starting" and n not in self.lentas:
                self.estados[n] = "failed" if n in self.caen else "working"
                self.caen.discard(n)
        return out

    def hot_start(self, nombre):
        if self.estados.get(nombre) not in (None, "stopped"):
            raise RuntimeError(f"CSS.5090 the conf[{nombre}] is being executed")
        self.hot.append(nombre)
        self.estados[nombre] = "starting"

    def arrancar(self, nombres):
        self.start.append(list(nombres))
        for n in nombres:
            self.estados[n] = "starting"


def _devuelve(gen, pasos=None):
    """Consume el generador (juntando lo que da en `pasos`) y devuelve su `return`."""
    try:
        while True:
            paso = next(gen)
            if pasos is not None:
                pasos.append(paso)
    except StopIteration as fin:
        return fin.value


def _faltan(cluster, nombres, vigia=None, reloj=None):
    reloj = reloj or _Reloj()

    def dormir(s):
        reloj.t += s

    pasos = []
    faltaron = _devuelve(vigia_logstash.arrancar_las_que_faltan(
        cluster.listar, cluster.hot_start, nombres, vigia=vigia, espera_s=10, tope_s=600,
        dormir=dormir, reloj=reloj), pasos)
    return faltaron, pasos


def test_si_ya_corren_todas_no_arranca_nada():
    c = _Cluster({n: "working" for n in NOMBRES})
    assert _faltan(c, NOMBRES) == ([], []) and c.hot == []


def test_arranca_de_a_una_las_que_faltan():
    nombres = ["pipeline-a", "pipeline-b", "pipeline-c"]
    c = _Cluster({"pipeline-a": "working", "pipeline-c": "stopped"})
    faltaron, pasos = _faltan(c, nombres)
    assert faltaron == [] and c.hot == ["pipeline-b", "pipeline-c"]
    assert pasos == [{"name": "Logstash · arranque de las pipelines", "ok": True,
                      "reason": "se sumaron 2 a las que ya corrían: pipeline-b, pipeline-c"}]


def test_si_una_se_cae_el_vigia_no_arranca_las_que_esperan_su_turno():
    nombres = ["pipeline-a", "pipeline-b", "pipeline-c"]
    reloj = _Reloj()
    c = _Cluster({"pipeline-a": "working"}, caen={"pipeline-b"})
    v = vigia_logstash.Vigia(c.listar, c.arrancar, nombres, reloj=reloj)
    faltaron, pasos = _faltan(c, nombres, vigia=v, reloj=reloj)
    assert faltaron == [] and c.hot == ["pipeline-b", "pipeline-c"]
    assert c.start == [["pipeline-a", "pipeline-b"]], "pipeline-c todavía no se había arrancado"
    assert any("reintento 1 de 3" in p["reason"] for p in pasos) and pasos[-1]["ok"]


def test_si_css_rechaza_el_arranque_avisa_y_deja_las_demas():
    def rechaza(nombre):
        raise RuntimeError("CSS.5090 more than 5 configs in the checking state")

    pasos = []
    faltaron = _devuelve(vigia_logstash.arrancar_las_que_faltan(
        _Cluster({}).listar, rechaza, NOMBRES, dormir=lambda s: None), pasos)
    assert faltaron == NOMBRES
    (paso,) = pasos
    assert not paso["ok"] and "no se pudo arrancar pipeline-siem" in paso["reason"] and "CSS.5090" in paso["reason"]


def test_si_no_llega_a_correr_se_rinde_con_su_estado():
    c = _Cluster({}, siempre_arrancando={"pipeline-siem"})
    faltaron, pasos = _faltan(c, NOMBRES)
    assert faltaron == NOMBRES and c.hot == ["pipeline-siem"]
    assert pasos == [{"name": "Logstash · arranque de las pipelines", "ok": False,
                      "reason": "pipeline-siem no quedó corriendo en 10 minutos (estado: starting)"}]


def test_sin_estado_no_arranca_a_ciegas():
    def listar():
        raise RuntimeError("sin red")

    hot, pasos = [], []
    assert _devuelve(vigia_logstash.arrancar_las_que_faltan(listar, hot.append, NOMBRES,
                                                            dormir=lambda s: None), pasos) == []
    assert hot == [] and not pasos[0]["ok"] and "sin red" in pasos[0]["reason"]


def test_la_plataforma_arranca_las_que_faltan_en_el_logstash_del_state(monkeypatch, tmp_path):
    (tmp_path / "deploy.auto.tfvars.json").write_text(json.dumps({"pipelines": {
        f"{i:02d}": {"start_ingestion": True} for i in range(11)}}), encoding="utf-8")
    monkeypatch.setattr(main.tfstate, "read_state", lambda td: {"resources": [
        {"mode": "managed", "type": "huaweicloud_css_logstash_cluster", "name": "logstash_cluster",
         "instances": [{"attributes": {"id": "ls-9"}}]}]})
    css = _CssPaginado(ONCE, {n: "working" for n in ONCE[:9]})
    monkeypatch.setattr(main, "_cliente_css", lambda ak, sk: css)
    # Sin esperas de verdad: si no la ve correr, se rinde enseguida.
    monkeypatch.setattr(vigia_logstash, "ESPERA_S", 0)
    monkeypatch.setattr(vigia_logstash, "TOPE_POR_PIPELINE_S", 0)

    class _Req:
        obs_access_key, obs_secret_key = "AK", "SK"

    eventos = []
    assert _devuelve(main._arrancar_las_que_faltan(_Req(), tmp_path), eventos) == []
    assert css.hot == [("ls-9", "pipeline-09", False), ("ls-9", "pipeline-10", False)]
    (paso,) = [json.loads(e[len("data: "):]) for e in eventos]
    assert paso["type"] == "step" and paso["ok"] and "pipeline-09, pipeline-10" in paso["reason"]

    css.hot.clear()
    assert _devuelve(main._arrancar_las_que_faltan(_Req(), tmp_path)) == [] and css.hot == [], \
        "con las 11 corriendo (la 11ª en la segunda página) no arranca nada"

    class _SinCred:
        obs_access_key, obs_secret_key = "", ""

    assert _devuelve(main._arrancar_las_que_faltan(_SinCred(), tmp_path)) == []


def test_el_paso_2_arranca_las_que_faltan_despues_de_la_activacion():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    activacion = src.index("returncode = yield from _aplicar_pasos(terraform_dir, pasos, tf_lines,")
    faltan = src.index("faltaron = yield from _arrancar_las_que_faltan(request, terraform_dir)")
    assert activacion < faltan < src.index('yield _sse({"type": "progress", "percent": _PCT_OUTPUTS')
