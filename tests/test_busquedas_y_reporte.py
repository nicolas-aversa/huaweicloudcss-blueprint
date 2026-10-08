"""Lo que se usa todos los días en OpenSearch Dashboards, listo: las búsquedas
del caso en Discover (todos los eventos con las columnas que importan, y los
críticos) y el reporte en PDF del dashboard (Reporting)."""
import json

import pytest

import capabilities as caps
import dashboards
import main
import plugins_vista as pv
import verticals


def _objetos(nd):
    return [json.loads(x) for x in nd.splitlines()]


# ── Búsquedas guardadas ─────────────────────────────────────────────────────
@pytest.mark.parametrize("slug, kql", [
    ("transacciones-billetera", "transaction.funnel.failed_at_code:*"),   # un campo que está solo en las fallidas
    ("fraud-detection", "fraud.is_fraud >= 1"),                            # un indicador que se suma
    ("fortianalyzer-soc", 'subtype:("ips")'),                              # un filter
    ("ventas-ecommerce", ""),                                              # sin pronóstico de fallas
])
def test_los_eventos_criticos_salen_del_pronostico_de_fallas(slug, kql):
    assert dashboards.consulta_de_criticos(caps.get_capability_spec(slug)) == kql


def test_un_dataset_nuevo_usa_su_pronostico_de_criticos():
    spec = {"forecasts": [{"feature_name": "event_volume", "aggregation_query": {"event_volume": {"value_count": {"field": "x"}}}},
                          {"feature_name": "critical_events", "aggregation_query": {"critical_events": {"value_count": {"field": "status_falla.keyword"}}}}]}
    assert dashboards.consulta_de_criticos(spec) == "status_falla:*"


def test_las_columnas_que_importan():
    campos = [{"field_path": "@timestamp", "type": "date"}, {"field_path": "nota", "type": "text"},
              {"field_path": "monto", "type": "double", "role": "measure"},
              {"field_path": "cliente", "type": "keyword", "role": "entity_id"},
              {"field_path": "estado", "type": "keyword", "role": "primary_dimension"}]
    assert dashboards._columnas(campos, {"volume_field": "estado.keyword"}) == ["cliente", "estado", "monto", "nota"]


def test_las_busquedas_van_con_el_dashboard_y_no_se_duplican():
    slug = "transacciones-billetera"
    v = verticals.get_vertical(slug)
    nd = dashboards.build_ndjson(slug)
    con = dashboards.con_busquedas_guardadas(nd, slug, caps.get_capability_spec(slug), v["fields"], v["label"])
    busquedas = [o for o in _objetos(con) if o["type"] == "search"]
    assert [b["attributes"]["title"] for b in busquedas] == [
        f"[{slug}] {v['label']}: todos los eventos", f"[{slug}] {v['label']}: eventos críticos"]
    patron = next(o for o in _objetos(nd) if o["type"] == "index-pattern")
    for b in busquedas:
        assert b["references"] == [{"name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern",
                                    "id": patron["id"]}]
        assert b["attributes"]["sort"] == [["@timestamp", "desc"]] and len(b["attributes"]["columns"]) == 6
    q = json.loads(busquedas[1]["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])["query"]
    assert q == {"query": "transaction.funnel.failed_at_code:*", "language": "kuery"}
    assert dashboards.con_busquedas_guardadas(con, slug, caps.get_capability_spec(slug), v["fields"], v["label"]) == con
    assert [b["id"] for b in busquedas] == [x["id"] for x in dashboards.busquedas_del_ndjson(con)]
    # Sin pronóstico de fallas, solo la de todos los eventos.
    sin = dashboards.con_busquedas_guardadas(nd, slug, {}, v["fields"], v["label"])
    assert len([o for o in _objetos(sin) if o["type"] == "search"]) == 1


def test_al_importar_quedan_sus_ids(monkeypatch, tmp_path):
    main._write_pipelines_registry(tmp_path, {"transacciones-billetera": {"index": "transacciones-billetera-%{+YYYY.MM}"}})
    monkeypatch.setattr(main, "_import_dashboards_via_opensearch", lambda *a, **k: True)
    assert main._import_dashboards("transacciones-billetera", {"public_endpoint": "1.2.3.4:9200"}, "pw", True, tmp_path)
    b = main._read_pipelines_registry(tmp_path)["transacciones-billetera"]["busquedas"]
    assert len(b) == 2 and all(x["id"] for x in b)


# ── Reporting ───────────────────────────────────────────────────────────────
def test_la_definicion_del_reporte():
    d = pv.definicion_de_reporte("pozos", "DASH", "https://consola/x")["reportDefinition"]
    assert d["name"] == "[pozos] Dashboard en PDF (plataforma)"
    assert d["source"] == {"description": "El dashboard de pozos, en PDF", "type": "Dashboard",
                           "origin": "https://consola/x", "id": "DASH"}
    # Los nombres de los enums del backend (con "pdf", CSS 3.4: "No enum constant
    # ...ReportDefinition.FileFormat.pdf").
    assert d["format"]["fileFormat"] == "Pdf" and d["trigger"] == {"triggerType": "OnDemand"}


class _R:
    def __init__(self, status, data=None, text=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = text if text is not None else json.dumps(self._d)

    def json(self):
        return self._d


def test_el_reporte_se_crea_y_si_ya_estaba_se_deja(monkeypatch):
    """En el tenant Global ("" en el header, como lo pide Dashboards): creadas
    sin el header quedaban con tenant "null", y con "global" con "global"; la
    API las devolvía y Dashboards → Reporting no mostraba ninguna (CSS 3.4)."""
    pedidos = []
    nombre = "[pozos] Dashboard en PDF (plataforma)"

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        pedidos.append((m, url, json_body, headers))
        if m == "GET" and headers is None:   # sin tenant: las de antes
            return _R(200, {"reportDefinitionDetailsList": [
                {"id": "VIEJA", "tenant": "null", "reportDefinition": {"name": nombre}},
                {"id": "OTRA", "tenant": "null", "reportDefinition": {"name": "[ventas] otra"}}]})
        if m == "GET" and headers == {"securitytenant": "global"}:
            return _R(200, {"reportDefinitionDetailsList": [
                {"id": "EN-GLOBAL", "tenant": "global", "reportDefinition": {"name": nombre}}]})
        if m == "GET":
            return _R(200, {"reportDefinitionDetailsList": []})
        return _R(200, {"reportDefinitionId": "R1"})

    monkeypatch.setattr(main, "_os_req", req)
    r = main._provisionar_reporte("http://x", "a", "p", "pozos", "DASH", "https://consola/x", force=False)
    assert r["ok"] and r["id"] == "R1"
    global_ = {"securitytenant": ""}
    assert ("POST", "http://x/_plugins/_reports/definition",
            pv.definicion_de_reporte("pozos", "DASH", "https://consola/x"), global_) in pedidos
    borradas = [(u, h) for m, u, _, h in pedidos if m == "DELETE"]
    assert borradas == [("http://x/_plugins/_reports/definition/VIEJA", None),
                        ("http://x/_plugins/_reports/definition/EN-GLOBAL", {"securitytenant": "global"})],         "solo las suyas, cada una en su tenant"
    # Ya estaba en el Global: se deja.
    ya = {"reportDefinitionDetailsList": [{"id": "R0", "tenant": "global", "reportDefinition": {"name": nombre}}]}
    monkeypatch.setattr(main, "_os_req", lambda m, url, *a, headers=None, **k:
                        _R(200, ya if headers == {"securitytenant": ""} else {}))
    assert main._provisionar_reporte("http://x", "a", "p", "pozos", "DASH", "", force=False) ==         {"ok": True, "reason": "ya estaba", "id": "R0"}


def test_sin_el_plugin_se_dice(monkeypatch):
    monkeypatch.setattr(main, "_os_req", lambda m, url, *a, **k: _R(400, text="no handler found for uri [/_plugins/_reports/definitions]"))
    assert main._provisionar_reporte("http://x", "a", "p", "pozos", "DASH", "", force=False) == \
        {"ok": False, "reason": "Reporting no está en este cluster"}


def test_la_provision_lo_hace_para_cada_dashboard():
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    cuerpo = src[i:src.index("\n    msg = ", i)]
    assert "res = _provisionar_reporte(_base_analistas, user, password, slug, did, origen, request.force)" in cuerpo
    assert '_guardar_estados(terraform_dir, slug, {"reporte": e and {**e, "id": res.get("id", "")}})' in cuerpo


# ── La tarjeta del dashboard ────────────────────────────────────────────────
def test_la_tarjeta_del_dashboard_lleva_las_busquedas_y_el_reporte():
    entry = {"dashboards_imported": True, "dashboard_id": "DASH",
             "busquedas": [{"id": "S1", "titulo": "[x] X: todos los eventos"}, {"id": "S2", "titulo": "[x] X: eventos críticos"}]}
    t = pv._dashboard(entry, "https://d", {"ok": True, "id": "R1"})
    assert t["que"] == "Gráficos armados con los campos del caso, con el rango de fechas de sus datos, sus búsquedas en Discover y su reporte en PDF."
    assert [(f["texto"], f["url"]) for f in t["filas"]] == [
        ("Búsqueda en Discover: X: todos los eventos", "https://d/app/discover#/view/S1"),
        ("Búsqueda en Discover: X: eventos críticos", "https://d/app/discover#/view/S2"),
        ("Reporte en PDF (Reporting)", "https://d/app/reports-dashboards#/report_definition_details/R1")]
    mal = pv._dashboard(entry, "https://d", {"ok": False, "motivo": "Reporting no está en este cluster"})
    assert mal["filas"][-1] == {"texto": "Reporte en PDF (Reporting)", "estado": pv.FALLA,
                                "detalle": "Reporting no está en este cluster", "numero": "", "url": ""}


def test_el_reporte_se_comprueba_con_el_dashboard(monkeypatch):
    def req(m, url, *a, **k):
        if url.endswith("/.kibana/_doc/dashboard:DASH"):
            return _R(200, {"found": True})
        return _R(200, {}) if url.endswith("/definition/R1") else _R(404)

    monkeypatch.setattr(main, "_os_req", req)
    estados = {"reporte": {"ok": True, "id": "R1"}}
    v = main._verificar_en_el_cluster("http://x", "a", "p", "pozos", {}, {}, {"dashboard_id": "DASH"}, estados)
    assert v["dashboard"] == {"ok": True, "detalle": "dashboard en Dashboards, con su reporte en Reporting"}
    estados["reporte"]["id"] = "R9"
    v = main._verificar_en_el_cluster("http://x", "a", "p", "pozos", {}, {}, {"dashboard_id": "DASH"}, estados)
    assert v["dashboard"] == {"ok": False, "detalle": "dashboard en Dashboards; el reporte no está en el cluster"}
