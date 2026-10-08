"""La vista "Plugins": por caso, lo que quedó en el cluster, qué se configuró
con sus datos, si quedó bien y el link directo a OpenSearch Dashboards.

La plataforma no reemplaza a Dashboards (los resultados se miran allá): guía
la demo. Antes había una pestaña por plugin que repetía lo que muestra
Dashboards, y el estado de cada plugin solo vivía en Actividad."""
import json
import pathlib
import shutil
import subprocess

import pytest

import main
import plugins_vista as pv
import verticals

_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"
BASE = "https://consola/elasticsearch/kibana/r/p/c"


# ── Links a Dashboards ──────────────────────────────────────────────────────
@pytest.mark.parametrize("url, base", [
    (BASE + "/app/login", BASE),
    ("https://1.2.3.4:5601/_dashboards", "https://1.2.3.4:5601/_dashboards"),
    ("https://example.invalid/app/home/", "https://example.invalid"),
    ("", ""),
])
def test_la_raiz_de_dashboards(url, base):
    assert pv.base_de_dashboards(url) == base


def test_sin_dashboards_no_hay_link():
    assert pv.link("", pv.APP_AD, "/detectors/x/results") == ""
    assert pv.link(BASE, pv.APP_AD, "/detectors/x/results") == BASE + "/app/anomaly-detection-dashboards#/detectors/x/results"


# ── Las tarjetas de un caso ─────────────────────────────────────────────────
def _pozos(**cambios):
    slug = "produccion-pozos"
    v = verticals.get_vertical(slug)
    spec = v["capability"]
    ids = {"forecaster_ids": ["F1", "F2", "F3"], "detector_id": "D", "monitor_id": "M", "agent_id": "A"}
    estados = {"forecasting": {"ok": True, "motivo": "", "ventana": {"interval_min": 30},
                               "forecasters": [{"id": "F1", "estado": "TEST_COMPLETE"},
                                               {"id": "F2", "estado": "parcial: 360 de 600 pasos"},
                                               {"id": "F3", "estado": "TEST_COMPLETE"}]},
               "anomalias": {"ok": True, "intervalo_min": 30}, "alertas": {"ok": True}, "perfil": {"ok": True}}
    args = dict(entry={"dashboards_imported": True, "dashboard_id": "DASH"}, ids=ids, spec=spec,
                perfil=v["perfil"], enmascarados=[], seguridad_reg={}, seguridad_spec={}, estados=estados,
                analista_creado=False, base=BASE, fields=v["fields"])
    args.update(cambios)
    return {t["plugin"]: t for t in pv.tarjetas_del_caso(slug, **args)}


def test_cada_plugin_con_lo_que_se_configuro_y_su_link():
    t = _pozos()
    assert list(t) == ["dashboard", "forecasting", "anomalias", "alertas", "perfil"], "el orden de la demo"
    assert t["dashboard"]["links"] == [{"texto": "Abrir el dashboard", "url": BASE + "/app/dashboards#/view/DASH"}]
    fc = t["forecasting"]
    assert fc["que"].startswith("Pronostica 3 medidas, 8 pasos hacia adelante, cada 30 min")
    assert [f["texto"] for f in fc["filas"]][0] == "produccion de oil (Sm3) por intervalo", "en palabras, no la ruta del campo"
    assert [f["url"] for f in fc["filas"]] == [BASE + f"/app/forecasting#/forecasters/F{i}" for i in (1, 2, 3)]
    assert [f["numero"] for f in fc["filas"]] == ["forecast:F1", "forecast:F2", "forecast:F3"]
    assert t["anomalias"]["links"][0]["url"] == BASE + "/app/anomaly-detection-dashboards#/detectors/D/results"
    assert t["anomalias"]["numero"] == "anomalias" and "produccion de oil" in t["anomalias"]["que"]
    assert t["alertas"]["links"][0]["url"] == BASE + "/app/alerting#/monitors/M?type=monitor"
    assert t["perfil"]["links"][0]["url"] == \
        BASE + "/app/opensearch_index_management_dashboards#/transform-details?id=produccion-pozos-perfil"
    assert "Una fila por Pozo" in t["perfil"]["que"]


def test_si_un_forecaster_no_se_creo_los_demas_no_se_corren():
    """De 3 specs se crearon 2 (el primero falló): cada fila con su medida."""
    specs = verticals.get_vertical("produccion-pozos")["capability"]["forecasts"]
    t = _pozos(ids={"forecaster_ids": ["FB", "FC"]},
               estados={"forecasting": {"ok": True, "forecasters": [
                   {"id": "FB", "nombre": specs[1]["name"], "estado": "TEST_COMPLETE"},
                   {"id": "FC", "nombre": specs[2]["name"], "estado": "TEST_COMPLETE"}]}})
    assert [f["texto"] for f in t["forecasting"]["filas"]] == ["produccion de gas (Sm3) por intervalo",
                                                               "lecturas con pozo caido (downtime)"]


def test_un_backtest_parcial_se_ve_y_dice_cual():
    fc = _pozos()["forecasting"]
    assert fc["estado"] == pv.PARCIAL
    assert [(f["estado"], f["detalle"]) for f in fc["filas"]] == [
        (pv.OK, "backtest completo"), (pv.PARCIAL, "backtest parcial: 360 de 600 pasos"), (pv.OK, "backtest completo")]


@pytest.mark.parametrize("texto, estado", [
    ("TEST_COMPLETE", pv.OK), ("", pv.OK), ("EN_CURSO (INIT_TEST)", pv.EN_CURSO),
    ("parcial: 1 de 600 pasos", pv.PARCIAL), ("no arrancó (el cluster rechazó el pedido)", pv.FALLA),
])
def test_el_estado_de_cada_backtest(texto, estado):
    assert pv._estado_de_backtest(texto)[0] == estado


@pytest.mark.parametrize("estados, peor", [
    ([pv.OK, pv.OK], pv.OK), ([pv.FALLA, pv.FALLA], pv.FALLA), ([pv.OK, pv.FALLA], pv.PARCIAL),
    ([pv.OK, pv.EN_CURSO], pv.EN_CURSO), ([pv.EN_CURSO, pv.FALLA], pv.PARCIAL), ([], pv.OK),
])
def test_el_estado_de_una_tarjeta_con_varias_piezas(estados, peor):
    assert pv._peor(estados) == peor


def test_lo_excluido_en_el_paso_2_no_es_una_falla():
    # Security Analytics apagado: el deploy no guarda su spec, igual figura como excluido.
    assert _pozos(entry={"excluir": ["security_analytics"]})["security_analytics"]["estado"] == pv.EXCLUIDO
    t = _pozos(entry={"dashboards_imported": True, "excluir": ["forecasting", "anomalias", "perfil"]})
    assert t["forecasting"]["estado"] == t["anomalias"]["estado"] == t["perfil"]["estado"] == pv.EXCLUIDO
    assert t["alertas"]["estado"] == pv.EXCLUIDO, "sin detector no hay alerta"


def test_antes_de_provisionar_no_se_inventa_nada():
    t = _pozos(ids={}, estados={})
    assert list(t) == ["dashboard"], "solo lo que deja el deploy"


def test_si_el_forecaster_no_se_creo_dice_por_que():
    t = _pozos(ids={"detector_id": "D"}, estados={"forecasting": {"ok": False, "motivo": "el índice no tiene datos"}})
    assert (t["forecasting"]["estado"], t["forecasting"]["motivo"]) == (pv.FALLA, "el índice no tiene datos")


def test_security_analytics_con_un_detector_por_tipo_de_log():
    spec = verticals.security_specs()["siem"]
    tipos = [lt["nombre"] for lt in spec["log_types"]]
    reg = {"detectores": {f"siem-{n}": {"id": f"ID-{n}", "log_type": n} for n in tipos},
           "reglas": {f"r{i}": "x" for i in range(7)}, "correlaciones": {c["nombre"]: "x" for c in spec["correlaciones"]}}
    t = _pozos(seguridad_reg=reg, seguridad_spec=spec)["security_analytics"]
    assert t["estado"] == pv.OK and t["que"].startswith("4 detectores con 7 reglas Sigma")
    assert [f["url"] for f in t["filas"]] == [BASE + f"/app/opensearch_security_analytics_dashboards#/detector-details/ID-{n}"
                                              for n in tipos]
    assert [x["texto"] for x in t["links"]] == ["Ver los hallazgos", "Ver las correlaciones"]
    # Una correlación que no se creó: no está todo bien.
    sin_corr = {**reg, "correlaciones": {}}
    assert _pozos(seguridad_reg=sin_corr, seguridad_spec=spec)["security_analytics"]["estado"] == pv.PARCIAL
    # Un detector que no se creó: la tarjeta lo dice, fila por fila.
    del reg["detectores"]["siem-" + tipos[0]]
    t = _pozos(seguridad_reg=reg, seguridad_spec=spec)["security_analytics"]
    assert t["estado"] == pv.PARCIAL
    assert [f["estado"] for f in t["filas"]].count(pv.FALLA) == 1


def test_el_analista_dice_con_que_usuario_entrar():
    # Un `.analistas.json` de un entorno anterior no alcanza: antes de provisionar, nada.
    assert "analista" not in _pozos(enmascarados=["well"], analista_creado=True, ids={}, estados={})
    t = _pozos(enmascarados=["well"], analista_creado=True)["analista"]
    assert "analista-produccion-pozos" in t["que"] and "Well" in t["que"]
    assert t["links"] == [{"texto": "Abrir Dashboards", "url": BASE + "/app/home"}]


def test_lo_de_todo_el_cluster():
    t = {x["plugin"]: x for x in pv.tarjetas_del_cluster(agente=True, text2viz={"ok": False, "motivo": "sin key"}, base=BASE)}
    assert list(t) == ["agente", "text2viz", "embeddings", "query_insights"]
    assert (t["text2viz"]["estado"], t["text2viz"]["motivo"]) == (pv.FALLA, "sin key")
    assert t["query_insights"]["links"][0]["url"] == BASE + "/app/query-insights-dashboards#/queryInsights"
    assert [x["plugin"] for x in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="")] == ["embeddings", "query_insights"]


# ── Lo que queda guardado de cada provisión ─────────────────────────────────
def test_del_resultado_de_la_provision_se_guarda_el_detalle():
    r = {"forecast": {"ok": True, "forecaster_ids": ["F1", "F2"], "states": ["A=TEST_COMPLETE", "B=parcial: 3 de 600 pasos"],
                      "window": {"interval_min": 30}, "note": "backtest OK (1/2)"},
         "anomalias": {"ok": False, "reason": "no se pudo crear el detector"},
         "alertas": {"ok": True, "monitor_id": "M", "reason": "ya provisionado"},
         "conversational": {"ok": True}}
    e = pv.estados_desde_resultado(r)
    assert e["forecasting"] == {"ok": True, "motivo": "backtest OK (1/2)", "ventana": {"interval_min": 30},
                                "forecasters": [{"id": "F1", "nombre": "A", "estado": "TEST_COMPLETE"},
                                                {"id": "F2", "nombre": "B", "estado": "parcial: 3 de 600 pasos"}]}
    assert e["anomalias"] == {"ok": False, "motivo": "no se pudo crear el detector"}
    assert "alertas" not in e, "un «ya provisionado» no trae nada nuevo"


def test_un_ya_estaba_no_pisa_lo_que_se_sabia():
    guardado = {"perfil": {"ok": True, "motivo": "T → perfil-x, una fila por Pozo"}}
    assert pv.mezclar(guardado, {"perfil": pv.estado_simple({"ok": True, "reason": "ya estaba"})}) == guardado
    # Sobre una falla vieja (timeout con el cluster saturado, pero quedó creado), sí.
    assert pv.mezclar({"perfil": {"ok": False, "motivo": "sin respuesta del cluster"}},
                      {"perfil": pv.estado_simple({"ok": True, "reason": "ya estaba"})})["perfil"]["ok"] is True
    assert pv.mezclar({}, {"perfil": pv.estado_simple({"ok": True, "reason": "ya estaba"})}) == \
        {"perfil": {"ok": True, "motivo": "ya estaba"}}
    assert pv.mezclar(guardado, {"perfil": pv.estado_simple({"ok": False, "reason": "403"})})["perfil"]["ok"] is False


def test_los_estados_se_guardan_por_caso(tmp_path):
    main._guardar_estados(tmp_path, "a", {"perfil": {"ok": True, "motivo": "x"}, "analista": None})
    main._guardar_estados(tmp_path, "_cluster", {"text2viz": {"ok": True, "motivo": ""}})
    assert main._read_estados(tmp_path) == {"a": {"perfil": {"ok": True, "motivo": "x"}},
                                            "_cluster": {"text2viz": {"ok": True, "motivo": ""}}}
    main._remove_capabilities(tmp_path)
    assert main._read_estados(tmp_path) == {}, "el destroy los borra con el registro"


def test_la_provision_guarda_cada_estado():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    cuerpo = src[i:src.index("\n    msg = ", i)]
    assert "_guardar_estados(terraform_dir, slug, plugins_vista.estados_desde_resultado(caps_result[slug] or {}))" in cuerpo
    assert '{"perfil": plugins_vista.estado_simple(res)}' in cuerpo
    assert '{"analista": plugins_vista.estado_simple(res)}' in cuerpo
    j = src.index("def _registrar_agente_del_run(")
    assert '_guardar_estados(terraform_dir, "_cluster", {"text2viz": plugins_vista.estado_simple(t2v)})' in \
        src[j:src.index("\ndef ", j + 10)]


# ── El dashboard: su id, para el link directo ──────────────────────────────
def test_el_id_del_dashboard_sale_del_ndjson():
    import dashboards
    nd = "\n".join(json.dumps(x) for x in [{"type": "index-pattern", "id": "ip"}, {"type": "visualization", "id": "v"},
                                           {"type": "dashboard", "id": "DASH"}])
    assert dashboards.id_del_dashboard(nd) == "DASH"
    assert dashboards.id_del_dashboard("no es json\n{}") == ""


def test_al_importar_el_dashboard_se_guarda_su_id(monkeypatch, tmp_path):
    main._write_pipelines_registry(tmp_path, {"mis-logs": {"index": "mis-logs-%{+YYYY.MM}"}})
    monkeypatch.setattr(main, "_import_dashboards_via_opensearch", lambda *a, **k: True)
    campos = [{"field_path": "status", "type": "keyword", "business_label": "Estado"},
              {"field_path": "monto", "type": "double", "business_label": "Monto", "role": "measure"}]
    assert main._import_dashboards("mis-logs", {"public_endpoint": "1.2.3.4:9200"}, "pw", True, tmp_path,
                                   fields=campos, index_name="mis-logs-%{+YYYY.MM}")
    import dashboards
    esperado = dashboards.id_del_dashboard(dashboards.build_ndjson_from_fields("mis-logs", "mis-logs-%{+YYYY.MM}", campos))
    assert esperado and main._read_pipelines_registry(tmp_path)["mis-logs"]["dashboard_id"] == esperado


# ── El perfil por entidad, como tabla del dashboard ────────────────────────
def _objetos(nd):
    return [json.loads(x) for x in nd.splitlines()]


def test_el_perfil_va_como_tabla_del_dashboard():
    import dashboards
    perfil = verticals.get_vertical("produccion-pozos")["perfil"]
    nd = dashboards.build_ndjson("produccion-pozos")
    antes = [o for o in _objetos(nd) if o["type"] == "dashboard"][0]
    n_antes = len(json.loads(antes["attributes"]["panelsJSON"]))
    con = dashboards.con_panel_de_perfil(nd, "produccion-pozos", perfil)
    objs = _objetos(con)
    assert objs[-1]["type"] == "dashboard", "el dashboard, después de lo que referencia"
    patron = next(o for o in objs if o["id"] == "perfil-produccion-pozos")
    assert "timeFieldName" not in patron["attributes"], "sin fecha: el rango del dashboard no lo vacía"
    campos = {f["name"]: f["type"] for f in json.loads(patron["attributes"]["fields"])}
    assert campos == {"entidad": "string", "petroleo": "number", "gas": "number", "horas": "number",
                      "lecturas": "number", "ultimo": "date"}
    tabla = next(o for o in objs if o["type"] == "visualization" and "Perfil por Pozo" in o["attributes"]["title"])
    vis = json.loads(tabla["attributes"]["visState"])
    assert [a["params"].get("customLabel") for a in vis["aggs"]] == \
        ["Petróleo", "Gas", "Horas en producción", "Lecturas", "Última lectura", "Pozo"]
    filas = vis["aggs"][-1]
    assert (filas["type"], filas["params"]["field"], filas["params"]["orderBy"]) == ("terms", "entidad", "1")
    assert tabla["references"][0]["id"] == "perfil-produccion-pozos"
    tablero = objs[-1]
    paneles = json.loads(tablero["attributes"]["panelsJSON"])
    assert len(paneles) == n_antes + 1 and paneles[-1]["gridData"]["w"] == 48
    assert paneles[-1]["gridData"]["y"] == max(p["gridData"]["y"] + p["gridData"]["h"] for p in paneles[:-1]), "abajo de todo"
    assert tablero["references"][-1] == {"name": f"panel_{n_antes}", "type": "visualization", "id": tabla["id"]}
    assert dashboards.id_del_dashboard(con) == dashboards.id_del_dashboard(nd), "el link directo no cambia"
    assert dashboards.con_panel_de_perfil(con, "produccion-pozos", perfil) == con, "no se duplica"
    assert dashboards.con_panel_de_perfil(nd, "produccion-pozos", None) == nd, "sin perfil (o excluido), igual"
    # Ordenada por la medida que declara el perfil, aunque no sea la primera.
    otra = dashboards.con_panel_de_perfil(nd, "produccion-pozos", {**perfil, "orden": "gas"})
    vis = json.loads(next(o for o in _objetos(otra) if o["id"] == tabla["id"])["attributes"]["visState"])
    assert vis["aggs"][-1]["params"]["orderBy"] == "2"


def test_los_filtros_del_dashboard_no_vacian_la_tabla():
    """La tabla del perfil comparte el dashboard con los controles: sin esto,
    elegir un pozo (un campo que `perfil-<slug>` no tiene) la dejaba vacía."""
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index('json_body={"doc": {"config": {"timepicker:timeDefaults":')
    assert '"courier:ignoreFilterIfFieldNotInIndex": True' in src[i:i + 600]


def test_un_dataset_nuevo_tambien_tiene_la_tabla(monkeypatch, tmp_path):
    campos = [{"field_path": "cliente", "type": "keyword", "business_label": "Cliente", "entity": True},
              {"field_path": "monto", "type": "double", "business_label": "Monto", "role": "measure"}]
    main._write_pipelines_registry(tmp_path, {"mis-logs": {"index": "mis-logs-%{+YYYY.MM}", "fields": campos}})
    enviado = {}
    monkeypatch.setattr(main, "_import_dashboards_via_opensearch",
                        lambda nd, *a, **k: enviado.setdefault("nd", nd) and True)
    assert main._import_dashboards("mis-logs", {"public_endpoint": "1.2.3.4:9200"}, "pw", True, tmp_path,
                                   fields=campos, index_name="mis-logs-%{+YYYY.MM}")
    assert any(o["id"] == "perfil-mis-logs" for o in _objetos(enviado["nd"]))
    # Apagado en el paso 2: sin tabla.
    main._write_pipelines_registry(tmp_path, {"mis-logs": {"index": "mis-logs-%{+YYYY.MM}", "fields": campos,
                                                           "excluir": ["perfil"]}})
    enviado.clear()
    main._import_dashboards("mis-logs", {"public_endpoint": "1.2.3.4:9200"}, "pw", True, tmp_path,
                            fields=campos, index_name="mis-logs-%{+YYYY.MM}")
    assert not any(o["id"] == "perfil-mis-logs" for o in _objetos(enviado["nd"]))


# ── /terraform/status y los números ─────────────────────────────────────────
def test_la_vista_se_arma_de_los_registros(monkeypatch, tmp_path):
    registro = {"produccion-pozos": {"dashboards_imported": True, "dashboard_id": "DASH"}, "sin-nada": {}}
    main._write_capabilities(tmp_path, {"produccion-pozos": {"forecaster_ids": ["F1"], "agent_id": "A"}})
    main._guardar_estados(tmp_path, "_cluster", {"text2viz": {"ok": True, "motivo": ""}})
    v = main._plugins_de_la_vista(tmp_path, registro, BASE + "/app/login")
    assert set(v) == {"produccion-pozos", "_cluster"}, "un caso sin nada no aparece"
    assert v["produccion-pozos"][0]["links"][0]["url"] == BASE + "/app/dashboards#/view/DASH"
    assert [t["plugin"] for t in v["_cluster"]] == ["agente", "text2viz", "embeddings", "query_insights"]
    # Si algo se rompe, /terraform/status igual responde (sin la vista).
    monkeypatch.setattr(main, "_read_estados", lambda td: 1 / 0)
    assert main._plugins_de_la_vista(tmp_path, registro, "") == {}


class _R:
    def __init__(self, status, data):
        self.status_code, self._d, self.text = status, data, json.dumps(data)

    def json(self):
        return self._d


def _cluster_falso(monkeypatch, tmp_path, caps_reg, seguridad=None, pipelines=None):
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_cluster_admin_password", lambda td: "pw")
    monkeypatch.setattr(main, "_read_https_enabled_from_state", lambda td: True)
    main._write_capabilities(tmp_path, caps_reg)
    monkeypatch.setattr(main, "_read_security", lambda td: seguridad or {})
    main._write_pipelines_registry(tmp_path, pipelines or {})


def test_los_numeros_de_un_caso(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    _cluster_falso(monkeypatch, tmp_path, {"produccion-pozos": {"forecaster_ids": ["F1", "F2"], "detector_id": "D"}},
                   pipelines={"produccion-pozos": {}})

    def pronostico(b, u, p, fc):
        if fc == "F2":
            raise RuntimeError("se cayó")
        return {"error_pct": 4.3, "error": ""}

    monkeypatch.setattr(main, "_pronostico_de", pronostico)
    anomalia = {"inicio": "2025-07-12 09:45:00", "fin": "2025-07-12 10:15:00", "grado": 0.9, "valores": []}
    monkeypatch.setattr(main, "_top_anomalias", lambda b, u, p, d, n: (37, [anomalia], ""))
    monkeypatch.setattr(main, "_estado_historico", lambda *a, **k: "FINISHED")
    monkeypatch.setattr(main, "_os_req", lambda m, url, *a, **k: _R(200, {"count": 412}) if url.endswith("/_count") else None)
    d = TestClient(main.app).get("/api/v1/plugins/numeros?slug=produccion-pozos").json()
    assert d["forecast:F1"] == {"error_pct": 4.3, "error": ""}
    assert d["forecast:F2"]["error_pct"] is None and "se cayó" in d["forecast:F2"]["error"], "uno que falla no tira el resto"
    assert d["anomalias"] == {"total": 37, "top": anomalia, "error": "", "estado": "FINISHED"}
    assert d["perfil"] == {"entidades": 412}


def test_los_numeros_de_todo_el_cluster(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    _cluster_falso(monkeypatch, tmp_path, {})
    monkeypatch.setattr(main, "_os_req", lambda m, url, *a, **k: _R(200, {"top_queries": [{}]}))
    import insights
    monkeypatch.setattr(insights, "consultas", lambda t, tipo: [{"latencia_ms": 1840}])
    d = TestClient(main.app).get("/api/v1/plugins/numeros").json()
    assert d == {"insights": {"latencia_ms": 1840, "n": 1, "error": ""}}


# ── Comprobado en el cluster ───────────────────────────────────────────────
def _cluster_verificable(monkeypatch, respuestas):
    """`_os_req` que contesta por el final de la ruta; lo que no está, 404."""
    pedidos = []

    def req(m, url, *a, **k):
        pedidos.append(url)
        for fin, (st, cuerpo) in respuestas.items():
            if url.endswith(fin):
                return _R(st, cuerpo)
        return _R(404, {"error": "not found"})

    monkeypatch.setattr(main, "_os_req", req)
    return pedidos


def test_lo_que_se_creo_se_comprueba_en_el_cluster(monkeypatch):
    _cluster_verificable(monkeypatch, {
        "forecasters/F1": (200, {"forecaster": {}}),
        "detectors/D?task=true": (200, {"historical_analysis_task": {"state": "FINISHED"}}),
        "monitors/M": (200, {"monitor": {"enabled": True}}),
        "detectors/SA1": (200, {"detector": {"enabled": True}}),
        "detectors/SA2": (200, {"detector": {"enabled": False}}),
        "produccion-pozos-perfil/_explain": (200, {"produccion-pozos-perfil": {"transform_metadata": {"status": "started"}}}),
        "dashboard:DASH": (200, {"found": True}),
    })
    v = main._verificar_en_el_cluster(
        "http://x:9200", "a", "p", "produccion-pozos",
        {"forecaster_ids": ["F1", "F2"], "detector_id": "D", "monitor_id": "M"},
        {"detectores": {"uno": {"id": "SA1"}, "dos": {"id": "SA2"}}}, {"dashboard_id": "DASH"})
    assert v["forecasting"] == {"ok": False, "detalle": "faltan 1 de 2 forecasters"}
    assert v["anomalias"] == {"ok": True, "detalle": "detector en el cluster, análisis finished"}
    assert v["alertas"] == {"ok": True, "detalle": "monitor prendido"}
    assert v["security_analytics"] == {"ok": False, "detalle": "dos: apagado"}
    assert v["perfil"] == {"ok": True, "detalle": "transform started"}
    assert v["dashboard"] == {"ok": True, "detalle": "dashboard en Dashboards"}


def test_lo_que_no_anda_se_dice(monkeypatch):
    _cluster_verificable(monkeypatch, {
        "detectors/D?task=true": (200, {"historical_analysis_task": {"state": "INIT_FAILURE"}}),
        "monitors/M": (200, {"monitor": {"enabled": False}}),
        "produccion-pozos-perfil/_explain": (200, {"produccion-pozos-perfil": {
            "transform_metadata": {"status": "failed", "failure_reason": "Failed to index the documents"}}}),
        "dashboard:DASH": (200, {"found": False}),
    })
    v = main._verificar_en_el_cluster("http://x:9200", "a", "p", "produccion-pozos",
                                      {"detector_id": "D", "monitor_id": "M"}, {}, {"dashboard_id": "DASH"})
    assert v["anomalias"] == {"ok": False, "detalle": "el análisis histórico falló"}
    assert v["alertas"] == {"ok": False, "detalle": "el monitor está apagado"}
    assert v["perfil"] == {"ok": False, "detalle": "el transform falló: Failed to index the documents"}
    assert v["dashboard"]["ok"] is False
    # Un transform que no existe: `_explain` contesta 200 con un texto.
    _cluster_verificable(monkeypatch, {"produccion-pozos-perfil/_explain": (200, {"produccion-pozos-perfil": "Failed to search"})})
    v = main._verificar_en_el_cluster("http://x:9200", "a", "p", "produccion-pozos", {"detector_id": "D"}, {}, {})
    assert v["perfil"] == {"ok": False, "detalle": "el transform no está en el cluster"}
    assert v["anomalias"] == {"ok": False, "detalle": "el detector no está en el cluster"}


def test_el_analista_y_lo_que_el_caso_no_tiene(monkeypatch):
    _cluster_verificable(monkeypatch, {"internalusers/analista-transacciones-billetera": (200, {})})
    v = main._verificar_en_el_cluster("http://x:9200", "a", "p", "transacciones-billetera", {"agent_id": "A"}, {}, {})
    assert v == {"analista": {"ok": True, "detalle": "usuario en el cluster"}}, "solo lo que el caso tiene"
    # Antes de provisionar (sin IDs) no se comprueba nada.
    assert main._verificar_en_el_cluster("http://x:9200", "a", "p", "transacciones-billetera", {}, {}, {}) == {}


def test_la_verificacion_viaja_con_los_numeros(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    _cluster_falso(monkeypatch, tmp_path, {"produccion-pozos": {"detector_id": "D"}}, pipelines={"produccion-pozos": {}})
    monkeypatch.setattr(main, "_top_anomalias", lambda *a: (0, [], ""))
    monkeypatch.setattr(main, "_estado_historico", lambda *a, **k: "FINISHED")
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: None)
    monkeypatch.setattr(main, "_verificar_en_el_cluster", lambda *a: {"anomalias": {"ok": True, "detalle": "x"}})
    d = TestClient(main.app).get("/api/v1/plugins/numeros?slug=produccion-pozos").json()
    assert d["verificado"] == {"anomalias": {"ok": True, "detalle": "x"}}
    # Si la verificación se rompe, los números igual llegan.
    monkeypatch.setattr(main, "_verificar_en_el_cluster", lambda *a: 1 / 0)
    d = TestClient(main.app).get("/api/v1/plugins/numeros?slug=produccion-pozos").json()
    assert "verificado" not in d and d["anomalias"]["total"] == 0


def test_la_vista_previa_usa_el_mismo_codigo():
    from fastapi.testclient import TestClient
    d = TestClient(main.app).get("/api/v1/dev/plugins-preview?slugs=siem,produccion-pozos,no-existe").json()
    assert set(d) == {"siem", "produccion-pozos", "_cluster"}
    assert {t["plugin"] for t in d["siem"]} >= {"security_analytics", "forecasting", "anomalias", "perfil"}


# ── El front ────────────────────────────────────────────────────────────────
def _funciones(html: str) -> str:
    i = html.index("    const _ESTADO_PLUGIN = {")
    return html[i:html.index("    async function verPlugins(btn) {", i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = { 'produccion-pozos': 'Producción de pozos' };
const _fmtPron = (v) => Number(v).toLocaleString('es-AR', { maximumFractionDigits: 1 });
const state = {};
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
const T = (plugin, estado, extra = {}) => ({ plugin, titulo: plugin, que: 'qué', estado, motivo: '', filas: [], links: [], numero: '', ...extra });
const plugins = {
  'produccion-pozos': [T('dashboard', 'ok'), T('forecasting', 'parcial'), T('perfil', 'excluido')],
  cts: [T('perfil', 'excluido')],
  _cluster: [T('query_insights', 'ok')],
};
check('sin plugins, nada', pluginsHTML({}) === '' && pluginsHTML(null) === '' && pluginsHTML({ _cluster: [T('q', 'ok')] }) === '');
const card = pluginsHTML(plugins);
check('el caso con su nombre y cuántos (sin los excluidos)', card.includes('data-slug="produccion-pozos"><span>Producción de pozos</span><span class="maestro__n">2</span>'), card);
check('un caso con todo excluido no aparece', !card.includes('data-slug="cts"'), card);
check('todo el cluster, al final', card.includes('data-slug="_cluster"><span>Todo el cluster</span>'), card);
check('sin provisionar, sin aviso', !card.includes('Provisionando'), card);
state.provisionandoPlugins = true;
check('provisionando, lo dice', pluginsHTML(plugins).includes('Provisionando: los demás casos aparecen a medida que terminan.'));
state.provisionandoPlugins = false;
const det = pluginsDetalleHTML([
  T('forecasting', 'parcial', { motivo: 'fallaron: A', filas: [{ texto: 'volumen', estado: 'parcial', detalle: 'backtest parcial: 3 de 600 pasos',
    numero: 'forecast:F1', url: 'https://d/app/forecasting#/forecasters/F1' }], links: [{ texto: 'Ver los forecasters', url: 'https://d/x' }] }),
  T('anomalias', 'ok', { numero: 'anomalias' }),
  T('perfil', 'excluido', { motivo: 'excluido en el paso 2' }),
]);
check('el estado en palabras y color', det.includes('<span class="sev sev--high">Parcial</span>') && det.includes('<span class="sev sev--ok">Listo</span>'), det);
check('el motivo', det.includes('<p class="plug__motivo">fallaron: A</p>'), det);
check('cada fila con su estado, su número y su link', det.includes('<span class="sev sev--high">backtest parcial: 3 de 600 pasos</span>')
  && det.includes('data-numero="forecast:F1" data-estado="parcial"') && det.includes('href="https://d/app/forecasting#/forecasters/F1" target="_blank" rel="noopener"'), det);
check('los links a Dashboards', det.includes('href="https://d/x" target="_blank" rel="noopener"><svg data-i="dashboard"></svg> Ver los forecasters</a>'), det);
check('lo excluido, apagado', det.includes('class="plug plug--excluido" data-plugin="perfil"'), det);
check('sin plugins', pluginsDetalleHTML([]).includes('no tiene plugins'));
// Los números.
check('error bajo en verde', numeroDePlugin('forecast:F', { error_pct: 4.3 }, 's', 'ok').includes('sev--ok">error medio 4,3 %'));
check('alto en rojo', numeroDePlugin('forecast:F', { error_pct: 93.5 }, 's', 'ok').includes('sev--critical'));
check('el motivo si la fila estaba bien', numeroDePlugin('forecast:F', { error_pct: null, error: 'no se pudo leer' }, 's', 'ok').includes('no se pudo leer'));
check('no se repite si la fila ya dice que es parcial', numeroDePlugin('forecast:F', { error_pct: null, error: 'backtest parcial' }, 's', 'parcial') === '');
const an = numeroDePlugin('anomalias', { total: 37, estado: 'FINISHED', top: { inicio: '2025-07-12 09:45:00', fin: '2025-07-12 10:15:00', grado: 0.93,
  valores: [{ nombre: 'oil', valor: 1840.5 }] } }, 'produccion-pozos');
check('cuántas y la más fuerte', an.includes('<strong>37</strong> anomalías · la más fuerte, grado 93 %, el 2025-07-12 09:45:00'), an);
check('y se explica con el asistente', an.includes('class="btn btn-secondary btn-sm anomalia__explicar" data-slug="produccion-pozos" data-inicio="2025-07-12 09:45:00" data-fin="2025-07-12 10:15:00" data-grado="93" data-valores="oil: 1.840,5"'), an);
check('sin anomalías', numeroDePlugin('anomalias', { total: 0, estado: 'FINISHED' }, 's').includes('el análisis terminó'));
check('un análisis que falló no "sigue corriendo"', numeroDePlugin('anomalias', { total: 0, estado: 'INIT_FAILURE' }, 's').includes('falló'));
check('sin estado, se dice', numeroDePlugin('anomalias', { total: 0, estado: 'DESCONOCIDO' }, 's').includes('no se pudo leer'));
check('tarjetas que no son una lista no rompen', _nPlugins('error') === 0 && pluginsHTML({ detail: 'No autenticado' }) === '');
check('eventos de seguridad', numeroDePlugin('seguridad:x', { eventos: 1842 }, 's').includes('<strong>1.842</strong> eventos detectados'));
check('entidades del perfil', numeroDePlugin('perfil', { entidades: 1 }, 's').includes('<strong>1</strong> fila en el perfil'));
check('la consulta más lenta', numeroDePlugin('insights', { latencia_ms: 1840 }, '').includes('<strong>1.840 ms</strong>'));
check('sin número, nada', numeroDePlugin('forecast:F', undefined, 's') === '');
// Lo comprobado en el cluster.
check('cada tarjeta, su lugar para la verificación', det.includes('<div class="plug__verif" data-verif="forecasting"></div>')
  && !det.includes('data-verif="perfil"'), det);
check('comprobado', verificacionHTML({ ok: true, detalle: 'monitor prendido' }) === '<svg data-i="check"></svg> Comprobado en el cluster: monitor prendido');
check('lo que falla', verificacionHTML({ ok: false, detalle: 'el <monitor> está apagado' }).includes('<svg data-i="alert-triangle"></svg> En el cluster: el &lt;monitor&gt; está apagado'));
check('sin verificar, nada', verificacionHTML(undefined) === '');

console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_vista_en_node(tmp_path):
    js = tmp_path / "plugins.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", _funciones(_INDEX.read_text(encoding="utf-8"))), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_mientras_se_provisiona_se_repinta_si_cambia_una_tarjeta(tmp_path):
    """Antes solo miraba forecasters, detector y monitor: un perfil que quedaba
    listo, o un backtest que terminaba, no se veía hasta recargar."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    const firmaDePlugins = ")
    firma = html[i:html.index("    async function refrescarPluginsEnCurso() {", i)]
    js = tmp_path / "firma.mjs"
    js.write_text(firma + r"""
const T = (plugin, estado, filas = []) => ({ plugin, estado, filas: filas.map(e => ({ estado: e })) });
const base = { a: [T('forecasting', 'en_curso', ['en_curso', 'ok'])] };
const fallos = [];
const igual = (x, y) => firmaDePlugins(x) === firmaDePlugins(y);
if (!igual(base, JSON.parse(JSON.stringify(base)))) fallos.push('igual');
if (igual(base, { a: [T('forecasting', 'ok', ['ok', 'ok'])] })) fallos.push('backtest que terminó');
if (igual(base, { a: [T('forecasting', 'en_curso', ['en_curso', 'ok']), T('perfil', 'ok')] })) fallos.push('plugin nuevo');
if (igual(base, { ...base, b: [T('dashboard', 'ok')] })) fallos.push('caso nuevo');
if (!igual(null, {}) || firmaDePlugins({ x: 'no es lista' }) !== '[["x",[]]]') fallos.push('sin datos');
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
""", encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "if (firmaDePlugins(data.plugins) !== firmaDePlugins((state.lastStatus || {}).plugins)) {" in html


def test_la_pestana_esta_conectada():
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function renderInfraView(data) {")
    vista = html[i:html.index("    async function hydrateActiveEnv() {", i)]
    assert "{ id: 'plugins', label: 'Plugins', icon: 'layers', cuenta: nPlugins, html: pluginsHTML(data.plugins) }," in vista
    assert vista.index("id: 'resumen'") < vista.index("id: 'plugins'"), "después del resumen"
    assert "body.querySelector('#infra-plugins')?.addEventListener('click'" in vista
    assert "if (explicar) return explicarAnomalia(explicar);" in vista
    assert ".find(b => b.dataset.slug === state.pluginsCaso)) || panel.querySelector('.plug__caso')]," in html, \
        "al abrir la pestaña se carga el caso que se miraba, o el primero"
    # Recién con los clicks enganchados (si no, el click al caso se perdía).
    assert vista.index("body.querySelector('#infra-plugins')?.addEventListener('click'") < \
        vista.index("cargarSeccionInfra(body, body.querySelector('[data-infra-tab].is-active')?.dataset.infraTab);")
    assert "fetch('/api/v1/plugins/numeros' + (slug === '_cluster' ? '' : '?slug=' + encodeURIComponent(slug)))" in html
