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
    for ruta in ("_RUTA_MAAS", "_RUTA_FUENTES"):
        assert re.search(rf'_item_ruta\(\*{ruta},\s*"Configurando"\)', cuerpo), ruta
    # Una sola etiqueta por ruta: el anuncio, el deploy y la vista previa la comparten.
    assert "Rutas →" not in src
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
def test_las_rutas_se_llaman_como_en_la_consola():
    assert main._RUTA_MAAS == ("rutas:maas", "Rutas del cluster · IPs servicio MaaS")
    assert main._RUTA_FUENTES == ("rutas:fuentes", "Rutas del Logstash · IPs de las fuentes")
    assert progreso_tf._ETIQUETAS["snat"] == "SNAT · salida a MaaS"
    assert progreso_tf._ETIQUETAS["nat"] == "Public Gateway"


def test_los_logos_son_los_oficiales_de_draw_io():
    """`build_logos_servicios.py` los baja de huaweicloud-latam/drawio-libraries;
    el SVG de draw.io trae su propio fondo de color (el mosaico de la Consola)."""
    import build_logos_servicios as b
    assert set(b.LOGOS) == {"css", "nat", "eip", "vpc"}
    for archivo in b.LOGOS:
        svg = (_STATIC / "servicios" / f"{archivo}.svg").read_text(encoding="utf-8")
        assert "Pixso" not in svg, "la <desc> del editor se saca"
    # El `<title>` va como primer hijo del <svg>.
    import base64
    item = {"data": "data:image/svg+xml;base64,"
            + base64.b64encode(b'<svg viewBox="0 0 72 72"><desc>Created with Pixso.</desc><g/></svg>').decode()}
    assert b.svg_de(item, "EIP") == '<svg viewBox="0 0 72 72"><title>EIP</title><g/></svg>\n'


def test_cada_servicio_tiene_su_logo_y_existe():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    bloque = html[html.index("const PROGRESO_GRUPOS = {"):]
    bloque = bloque[:bloque.index("};")]
    logos = dict(re.findall(r"(\w+): \{ nombre: '[^']+', logo: '([^']*)' \}", bloque))
    assert set(logos) == {"css", "nat", "eip", "vpc", "otros"}
    for servicio, ruta in logos.items():
        if servicio == "otros":
            continue
        assert ruta == f"/static/servicios/{servicio}.svg", "el oficial, de build_logos_servicios.py"
        archivo = _STATIC / ruta.removeprefix("/static/")
        assert archivo.is_file(), ruta
        if archivo.suffix == ".svg":
            raiz = ET.fromstring(archivo.read_text(encoding="utf-8"))
            assert raiz.get("viewBox") == "0 0 72 72", f"{ruta}: no es el ícono oficial de draw.io"
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


# ── La vista previa (?preview=deploy) ───────────────────────────────────────
def test_la_vista_previa_reproduce_un_deploy_real_sin_terraform(monkeypatch):
    """El mismo parser y los mismos eventos que el deploy, sin tocar la nube."""
    from fastapi.testclient import TestClient

    def _sin_terraform(*a, **k):
        raise AssertionError("la vista previa no corre procesos")
    monkeypatch.setattr(main.subprocess, "Popen", _sin_terraform)
    monkeypatch.setattr(main.subprocess, "run", _sin_terraform)

    res = TestClient(main.app).get("/api/v1/dev/deploy-preview?paso=0")
    eventos = [json.loads(b[len("data: "):]) for b in res.text.split("\n\n") if b.startswith("data: ")]

    plan = next(e for e in eventos if e["type"] == "plan")
    assert {c["grupo"] for c in plan["items"]} == {"css", "nat", "eip", "vpc"}
    assert "rutas:maas" in [c["key"] for c in plan["items"]]
    pcts = [e["percent"] for e in eventos if e["type"] == "progress"]
    assert pcts == sorted(pcts) and pcts[-1] >= main._PCT_APPLY_HASTA
    ultimo = {}
    for e in eventos:
        if e["type"] == "item":
            ultimo[e["key"]] = e
    assert set(ultimo) == {c["key"] for c in plan["items"]}, "cada fila avanza"
    assert all(e["done"] for e in ultimo.values()), [k for k, e in ultimo.items() if not e["done"]]
    assert eventos[-1]["type"] == "complete"


def test_la_muestra_no_trae_datos_de_la_cuenta():
    texto = main._MUESTRA_APPLY.read_text(encoding="utf-8")
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", texto), "un UUID de la cuenta"
    assert not re.search(r"\b[0-9a-f]{32}\b", texto), "un project id"
    assert "myhuaweicloud.com" not in texto


def test_preview_deploy_muestra_la_pantalla_real():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    i = html.index("function renderInfraView(")
    vista = html[i:html.index("if (!active) {", i)]
    assert "provisioningAnimationHTML();\n          return;" not in vista, "volvió la animación sola"
    j = html.index("async function _vistaPreviaDeploy(paso)")
    previa = html[j:html.index("(function maybePreviewDeployAnim()", j)]
    assert "/api/v1/dev/deploy-preview?paso=" in previa
    assert "updateDeployProgress(d)" in previa and "finishDeployProgress(ok)" in previa


def test_el_total_cuenta_las_filas_que_no_son_de_terraform():
    """La pantalla mostraba 10 filas (con "Rutas → MaaS") y el mensaje decía
    "de 9 listos"."""
    p = progreso_tf.ProgresoApply(adicionales=1)
    for d in ("huaweicloud_vpc_eip.nat_eip", "huaweicloud_nat_gateway.nat"):
        p._registrar(d, "crear")
    for linea in ("Plan: 2 to add, 0 to change, 0 to destroy.\n",
                  "huaweicloud_vpc_eip.nat_eip: Creating...\n",
                  "huaweicloud_nat_gateway.nat: Creating...\n"):
        eventos = p.linea(linea)
    assert eventos[-1]["message"] == "Desplegando vía Terraform… · 0 de 3 listos"


def test_la_vista_previa_cuenta_las_rutas():
    from fastapi.testclient import TestClient
    texto = TestClient(main.app).get("/api/v1/dev/deploy-preview?paso=0").text
    assert "de 10 listos" in texto and "de 9 listos" not in texto


def test_el_deploy_cuenta_las_filas_extra_en_el_total(monkeypatch, tmp_path):
    salida = ("  # huaweicloud_vpc_eip.nat_eip will be created\n"
              "  # huaweicloud_nat_gateway.nat will be created\n"
              "Plan: 2 to add, 0 to change, 0 to destroy.\n"
              "huaweicloud_vpc_eip.nat_eip: Creating...\n"
              "huaweicloud_nat_gateway.nat: Creating...\n")

    class _Proc:
        def __init__(self, *a, **k):
            self.stdout = io.StringIO(salida)
            self.returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(main.subprocess, "Popen", _Proc)
    extra = main._item_ruta("rutas:maas", "Rutas → MaaS")
    _, eventos = _correr(main._correr_apply(tmp_path, [], 5, 92, [], extras=[extra]))
    assert [e for e in eventos if e["type"] == "progress"][-1]["message"] == \
        "Desplegando vía Terraform… · 0 de 3 listos"


def test_preview_deploy_arranca_la_reproduccion():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    i = html.index("(function maybePreviewDeployAnim()")
    assert "_vistaPreviaDeploy(params.get('paso')" in html[i:html.index("})();", i)]
