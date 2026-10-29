"""Los dashboards que un SA arma o corrige en un entorno, guardados por caso.

Vivían en `.kibana` del cluster y se perdían al destruir el entorno (o antes:
la plataforma reescribe los suyos, con el mismo id, cada vez que aplica el
index template). "Guardar cambios" los copia por caso; el próximo deploy de
ese caso, de cualquier SA, los carga encima de los de la plataforma.
"""
import inspect
import json

import pytest
from fastapi.testclient import TestClient

import auth
import dashboards_guardados as dg
import main

LOTE = "2026-10-20T15:17:52.000001+00:00"       # la fecha del _bulk de la plataforma
DESPUES = "2026-10-20T19:02:11.123Z"            # lo que pone Dashboards al guardar


def _obj(tipo, oid, refs=(), **attrs):
    return {"type": tipo, "id": oid, "attributes": {"title": oid, **attrs},
            "references": [{"type": t, "id": i, "name": f"r{n}"} for n, (t, i) in enumerate(refs)]}


def _plataforma():
    return dg.objetos_del_ndjson("\n".join(json.dumps(o) for o in (
        _obj("index-pattern", "billetera-*"),
        _obj("visualization", "v-montos", [("index-pattern", "billetera-*")], visState="{\"type\":\"line\"}"),
        _obj("dashboard", "d-billetera", [("visualization", "v-montos")], panelsJSON="[1]",
             timeFrom="2025-07-01", timeTo="2026-07-02"),
    )))


def _en_kibana(*extra, cambios=None):
    """Lo de la plataforma tal cual lo escribió (mismo lote), más `extra`."""
    out = {}
    for k, o in _plataforma().items():
        out[k] = {**o, "updated_at": LOTE}
    for k, o in (cambios or {}).items():
        out[k] = {**out[k], **o}
    for o in extra:
        out[f"{o['type']}:{o['id']}"] = {**o, "updated_at": o.get("updated_at", DESPUES)}
    return out


def _estados(elegidos):
    return {k: o["estado"] for k, o in elegidos.items()}


# ── Qué cuenta como cambio ──────────────────────────────────────────────────
def test_lo_que_escribio_la_plataforma_no_es_un_cambio():
    assert dg.del_caso(_en_kibana(), _plataforma()) == {}


def test_un_dashboard_de_la_plataforma_editado_es_modificado():
    editado = {"dashboard:d-billetera": {"updated_at": DESPUES,
                                         "attributes": {"title": "d-billetera", "panelsJSON": "[1,2]"}}}
    elegidos = dg.del_caso(_en_kibana(cambios=editado), _plataforma())
    assert _estados(elegidos) == {"dashboard:d-billetera": "modificado"}


def test_si_la_plataforma_mejoro_su_dashboard_la_copia_vieja_no_es_un_cambio():
    """El cluster tiene la versión del deploy (misma fecha que el lote) y la
    plataforma hoy genera otra: guardarla congelaría la vieja para todos."""
    vieja = {"dashboard:d-billetera": {"attributes": {"title": "d-billetera", "panelsJSON": "[viejo]"}}}
    assert dg.del_caso(_en_kibana(cambios=vieja), _plataforma()) == {}


def test_que_la_plataforma_le_ajuste_las_fechas_no_es_un_cambio():
    """`_ajustar_tiempo_de_dashboards` reescribe timeFrom/timeTo con un update (en un _bulk)."""
    ajustado = {"dashboard:d-billetera": {"updated_at": DESPUES, "attributes": {
        "title": "d-billetera", "panelsJSON": "[1]", "timeFrom": "2026-09-01", "timeTo": "2026-10-20",
        "timeRestore": True}}}
    assert dg.del_caso(_en_kibana(cambios=ajustado), _plataforma()) == {}


def test_lo_nuevo_del_caso_se_encuentra_aunque_apunte_a_traves_de_otro_objeto():
    viz = _obj("visualization", "v-mia", [("index-pattern", "billetera-*")])
    tablero = _obj("dashboard", "d-mio", [("visualization", "v-mia")])            # solo vía la viz nueva
    otro = _obj("dashboard", "d-con-lo-de-siempre", [("visualization", "v-montos")])
    elegidos = dg.del_caso(_en_kibana(viz, tablero, otro), _plataforma())
    assert _estados(elegidos) == {"visualization:v-mia": "nuevo", "dashboard:d-mio": "nuevo",
                                  "dashboard:d-con-lo-de-siempre": "nuevo"}


def test_tsvb_y_vega_son_del_caso_por_el_nombre_del_index_pattern():
    tsvb = _obj("visualization", "v-tsvb", visState=json.dumps({"params": {"index_pattern": "billetera-*"}}))
    vega = _obj("visualization", "v-vega", visState=json.dumps({"params": {"spec": "{index: billetera-*}"}}))
    ajena = _obj("visualization", "v-ajena", visState=json.dumps({"params": {"index_pattern": "mi-billetera-*"}}))
    elegidos = dg.del_caso(_en_kibana(tsvb, vega, ajena), _plataforma())
    assert set(elegidos) == {"visualization:v-tsvb", "visualization:v-vega"}


def test_un_index_pattern_de_un_sa_viaja_con_lo_que_lo_usa():
    patron = _obj("index-pattern", "billetera-2026*")
    viz = _obj("visualization", "v-2026", [("index-pattern", "billetera-2026*")], visState="billetera-*")
    suelto = _obj("index-pattern", "nadie-lo-usa*")
    elegidos = dg.del_caso(_en_kibana(patron, viz, suelto), _plataforma())
    assert set(elegidos) == {"visualization:v-2026", "index-pattern:billetera-2026*"}


def test_un_panel_de_texto_viaja_con_el_dashboard_que_lo_usa():
    # Un markdown no apunta a nada: es del caso porque lo usa un dashboard del caso.
    texto = _obj("visualization", "v-texto", visState=json.dumps({"type": "markdown"}))
    otro_texto = _obj("visualization", "v-texto-2", visState=json.dumps({"type": "markdown"}))
    tablero = _obj("dashboard", "d-mio", [("visualization", "v-texto"), ("visualization", "v-montos")])
    editado = {"dashboard:d-billetera": {"updated_at": DESPUES, "attributes": {"title": "d-billetera", "panelsJSON": "[1,2]"},
                                         "references": [{"type": "visualization", "id": "v-montos", "name": "r0"},
                                                        {"type": "visualization", "id": "v-texto-2", "name": "r1"}]}}
    suelto = _obj("visualization", "v-texto-de-nadie", visState=json.dumps({"type": "markdown"}))
    elegidos = dg.del_caso(_en_kibana(texto, otro_texto, tablero, suelto, cambios=editado), _plataforma())
    assert _estados(elegidos) == {"dashboard:d-mio": "nuevo", "visualization:v-texto": "nuevo",
                                  "dashboard:d-billetera": "modificado", "visualization:v-texto-2": "nuevo"}


def test_lo_ya_guardado_igual_no_se_vuelve_a_guardar():
    viz = _obj("visualization", "v-mia", [("index-pattern", "billetera-*")])
    elegidos = dg.del_caso(_en_kibana(viz), _plataforma())
    assert dg.pendientes(elegidos, {"visualization:v-mia": {**viz}}) == []
    cambiada = {**viz, "attributes": {"title": "v-mia", "visState": "otra"}}
    assert len(dg.pendientes(elegidos, {"visualization:v-mia": cambiada})) == 1


def test_lo_que_no_es_de_ningun_caso_se_informa():
    suelto = _obj("dashboard", "d-de-nada")
    en_kibana = _en_kibana(suelto)
    elegidos = dg.del_caso(en_kibana, _plataforma())
    assert dg.sueltos(en_kibana, set(_plataforma()), set(elegidos)) == ["d-de-nada"]


# ── Dónde queda ─────────────────────────────────────────────────────────────
def test_guardar_suma_por_id_y_arma_el_ndjson(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    dg.guardar("billetera", [{**_obj("visualization", "v1"), "titulo": "v1", "estado": "nuevo"}], "ana@x.com")
    dg.guardar("billetera", [{**_obj("dashboard", "d1"), "titulo": "d1", "estado": "nuevo"}], "beto@x.com")
    guardado = dg.leer("billetera")
    assert set(guardado) == {"visualization:v1", "dashboard:d1"}
    assert guardado["visualization:v1"]["guardado_por"] == "ana@x.com"
    lineas = [json.loads(l) for l in dg.ndjson("billetera").splitlines()]
    assert {(l["type"], l["id"]) for l in lineas} == {("visualization", "v1"), ("dashboard", "d1")}
    assert all(set(l) == {"type", "id", "attributes", "references"} for l in lineas)
    assert dg.ndjson("otro-caso") == ""


# ── El botón ────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, hits):
        self.status_code, self._hits = 200, hits

    def json(self):
        return {"hits": {"hits": self._hits}}


def _hits(en_kibana):
    return [{"_id": k, "_source": {"type": o["type"], o["type"]: o["attributes"],
                                   "references": o["references"], "updated_at": o["updated_at"]}}
            for k, o in en_kibana.items()]


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda d: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_read_https_enabled_from_state", lambda d: True)
    monkeypatch.setattr(main, "_cluster_admin_password", lambda d: "pw")
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda d: {"billetera": {"label": "Billetera"}})
    plataforma = "\n".join(json.dumps(o) for o in _plataforma().values())
    monkeypatch.setattr(main, "_ndjson_de_la_plataforma", lambda *a: (plataforma, "d-billetera"))
    estado = {"kibana": _en_kibana()}
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: _Resp(_hits(estado["kibana"])))
    return estado


def test_guardar_cambios_guarda_por_caso_y_dice_que(entorno):
    entorno["kibana"] = _en_kibana(_obj("dashboard", "Ventas por canal", [("visualization", "v-montos")]),
                                   _obj("dashboard", "d-de-nada"))
    r = TestClient(main.app).post("/api/v1/dashboards/guardar").json()

    assert r["guardados"] == 1 and r["sueltos"] == ["d-de-nada"]
    assert r["casos"] == [{"slug": "billetera", "nombre": "Billetera", "total": 1,
                           "guardados": [{"titulo": "Ventas por canal", "estado": "nuevo", "tipo": "dashboard"}]}]
    assert set(dg.leer("billetera")) == {"dashboard:Ventas por canal"}

    # Apretarlo de nuevo sin cambios no guarda nada.
    assert TestClient(main.app).post("/api/v1/dashboards/guardar").json()["guardados"] == 0


def test_lo_guardado_y_recargado_no_vuelve_a_aparecer_como_cambio(entorno):
    """El deploy carga lo guardado con otra fecha que la del lote: no es un cambio nuevo."""
    entorno["kibana"] = _en_kibana(_obj("dashboard", "mio", [("visualization", "v-montos")]))
    TestClient(main.app).post("/api/v1/dashboards/guardar")
    recargado = _en_kibana(_obj("dashboard", "mio", [("visualization", "v-montos")]))
    recargado["dashboard:mio"]["updated_at"] = "2026-10-25T10:00:00+00:00"
    entorno["kibana"] = recargado
    assert TestClient(main.app).post("/api/v1/dashboards/guardar").json()["guardados"] == 0


# ── La carga en el deploy ───────────────────────────────────────────────────
def test_el_deploy_carga_lo_guardado_despues_de_lo_de_la_plataforma(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    escrito = []
    monkeypatch.setattr(main, "_import_dashboards_via_opensearch", lambda nd, slug, *a, **k: escrito.append((slug, nd)) or True)
    assert main._cargar_dashboards_guardados("billetera", {"public_endpoint": "x"}, "pw", True) is None
    dg.guardar("billetera", [{**_obj("dashboard", "mio"), "titulo": "mio", "estado": "nuevo"}], "ana@x.com")
    assert main._cargar_dashboards_guardados("billetera", {"public_endpoint": "x"}, "pw", True) is True
    assert escrito and escrito[0][0] == "billetera" and '"id": "mio"' in escrito[0][1]

    fuente = inspect.getsource(main.apply_schema)
    plataforma = fuente.index('runs.step(run, f"Dashboards · {s}", True)')
    assert plataforma < fuente.index("_cargar_dashboards_guardados(s, cluster")
