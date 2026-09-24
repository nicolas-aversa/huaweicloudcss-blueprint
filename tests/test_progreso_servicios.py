"""El progreso del deploy, agrupado por servicio de Huawei Cloud y con logos.

Cada componente dice bajo qué servicio va (CSS, NAT Gateway, EIP, VPC), y las
Cluster Routes —que no son de Terraform: el backend las agrega por la API del
CSS— también son filas, bajo CSS, desde el principio.
"""
import io
import json
import pathlib
import re
import xml.etree.ElementTree as ET

import main
import progreso_tf

_STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


def test_cada_componente_de_terraform_tiene_su_servicio():
    for comp, *_ in progreso_tf._TIPOS.values():
        assert progreso_tf.ProgresoApply.grupo(comp) in {"css", "nat", "eip", "vpc"}, comp
    assert progreso_tf.ProgresoApply.grupo("pipeline:fintech") == "css"
    assert progreso_tf.ProgresoApply.grupo("dnat:kibana") == "nat"
    assert progreso_tf.ProgresoApply.grupo("algo_nuevo") == "otros"


def test_el_nombre_suelto_dice_el_servicio():
    """Bajo el grupo alcanza "OpenSearch cluster"; en el mensaje de fase, que
    va solo, tiene que decir CSS."""
    larga = progreso_tf.ProgresoApply.larga
    assert larga("opensearch") == "CSS OpenSearch cluster"
    assert larga("logstash") == "CSS Logstash cluster"
    assert larga("nat") == "NAT gateway"
    assert larga("dnat:opensearch") == "DNAT :9200 · OpenSearch"


# ── Las Cluster Routes ──────────────────────────────────────────────────────
def test_las_rutas_se_anuncian_en_la_fase_1(monkeypatch):
    req = main.TerraformDeployRequest(pipeline_conf="input {} output {}")
    monkeypatch.setattr(main, "_case_source_ips", lambda r: [])
    assert [i["key"] for i in main._items_de_rutas(req)] == ["rutas:maas"]
    assert all(i["grupo"] == "css" and i["estado"] == "En espera" and not i["done"]
               for i in main._items_de_rutas(req))

    monkeypatch.setattr(main, "_case_source_ips", lambda r: ["10.0.0.5"])
    assert [i["key"] for i in main._items_de_rutas(req)] == ["rutas:maas", "rutas:fuentes"]

    # "Iniciar ingesta" no agrega rutas.
    req.start_ingestion = True
    assert main._items_de_rutas(req) == []


def test_una_ruta_que_falla_queda_marcada():
    ok = main._item_ruta("rutas:maas", "Rutas → MaaS", "listo", done=True)
    assert ok["done"] and ok["percent"] == 100 and "error" not in ok
    mal = main._item_ruta("rutas:maas", "Rutas → MaaS", "Falló", error=True)
    assert mal["error"] and not mal["done"] and mal["estado"] == "Falló"


def test_el_deploy_actualiza_las_filas_de_las_rutas():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def _deploy_stream_gen_raw(")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    assert "extras=_items_de_rutas(request)" in cuerpo
    for key in ("rutas:maas", "rutas:fuentes"):
        assert re.search(rf'_item_ruta\("{key}", "[^"]+",\s*"Configurando"\)', cuerpo), key
    assert "done=ok_maas, error=not ok_maas" in cuerpo
    assert "done=ok_f, error=not ok_f" in cuerpo


def _correr(gen):
    eventos = []
    try:
        while True:
            eventos.append(json.loads(next(gen)[len("data: "):]))
    except StopIteration as fin:
        return fin.value, eventos


def test_el_plan_trae_las_filas_que_no_son_de_terraform(monkeypatch, tmp_path):
    salida = ("  # huaweicloud_vpc_eip.nat_eip will be created\n"
              "Plan: 1 to add, 0 to change, 0 to destroy.\n"
              "huaweicloud_vpc_eip.nat_eip: Creating...\n")

    class _Proc:
        def __init__(self, *a, **k):
            self.stdout = io.StringIO(salida)
            self.returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(main.subprocess, "Popen", _Proc)
    extra = main._item_ruta("rutas:maas", "Rutas → MaaS")
    _, eventos = _correr(main._correr_apply(tmp_path, [], 5, 92, [], extras=[extra]))
    plan = next(e for e in eventos if e["type"] == "plan")
    assert [(c["grupo"], c["key"]) for c in plan["items"]] == [("eip", "eip"), ("css", "rutas:maas")]


# ── Los logos ───────────────────────────────────────────────────────────────
def test_cada_servicio_tiene_su_logo_y_existe():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    bloque = html[html.index("const PROGRESO_GRUPOS = {"):]
    bloque = bloque[:bloque.index("};")]
    logos = dict(re.findall(r"(\w+): \{ nombre: '[^']+', logo: '([^']*)' \}", bloque))
    assert set(logos) == {"css", "nat", "eip", "vpc", "otros"}
    for servicio, ruta in logos.items():
        if servicio == "otros":
            continue
        archivo = _STATIC / ruta.removeprefix("/static/")
        assert archivo.is_file(), ruta
        if archivo.suffix == ".svg":
            raiz = ET.fromstring(archivo.read_text(encoding="utf-8"))
            assert raiz.get("viewBox") == "0 0 1024 1024", ruta
            assert raiz.find("{http://www.w3.org/2000/svg}title") is not None, "accesible"


def test_cada_apply_recibe_las_filas_extra(monkeypatch, tmp_path):
    """Con dos applies, el segundo plan reemplaza la lista: sin las extras,
    las rutas desaparecerían de la pantalla."""
    vistas = []

    def _fake(terraform_dir, args, desde, hasta, tf_lines, plan=True, extras=None):
        vistas.append(extras)
        return 0
        yield  # noqa: unreachable — lo vuelve generador

    monkeypatch.setattr(main, "_correr_apply", _fake)
    extra = [main._item_ruta("rutas:maas", "Rutas → MaaS")]
    pasos = [{"args": [], "desde": 5, "hasta": 50, "sin_activar": []},
             {"args": [main._TARGET_ACTIVACION], "desde": 50, "hasta": 92, "sin_activar": []}]
    assert _correr(main._aplicar_pasos(tmp_path, pasos, [], extras=extra))[0] == 0
    assert vistas == [extra, extra]
