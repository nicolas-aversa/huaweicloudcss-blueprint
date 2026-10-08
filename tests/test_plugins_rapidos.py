"""Provisionar plugins más rápido y sin pronósticos vacíos.

Medido en un deploy de 8 casos (un nodo, cola de búsquedas de 1000): 1417 s, y
de 24 backtests se dibujaban 10. Los backtests se lanzaban juntos y el cluster
rechazaba sus búsquedas (104.391 rechazadas): quedaban truncados, o el tercero
ni arrancaba; el estado de la tarea no lo mostraba. Y el agente se rearmaba en
cada caso, recorriendo todos los índices del entorno cada vez."""
import main


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data or {}
        self.text = str(self._d)

    def json(self):
        return self._d


def test_un_backtest_se_lanza_con_la_cola_casi_vacia_y_se_reintenta(monkeypatch):
    esperas, lanzados = [], []
    monkeypatch.setattr(main, "_esperar_cluster_libre",
                        lambda b, u, p, umbral=None, max_s=None: esperas.append((umbral, max_s)) or True)
    respuestas = iter(["", "T2"])
    monkeypatch.setattr(main, "_lanzar_backtest", lambda b, u, p, f: lanzados.append(f) or next(respuestas))
    assert main._lanzar_backtest_de_a_uno("http://x", "a", "p", "FC") == "T2"
    assert lanzados == ["FC", "FC"], "si no arrancó, una vez más"
    assert esperas == [(main._COLA_BACKTEST, main._ESPERA_BACKTEST_MAX_S)] * 2
    monkeypatch.setattr(main, "_lanzar_backtest", lambda *a: "")
    assert main._lanzar_backtest_de_a_uno("http://x", "a", "p", "FC") == ""


def test_el_task_id_como_lo_devuelve_css(monkeypatch):
    """CSS 3.4 responde `{"task_id": …}` (visto en el cluster): leer solo
    `taskId` daba "no arrancó" a backtests que sí arrancaron, y el reintento
    lanzaba un segundo backtest (409 "current test hasn't finished")."""
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: _R(200, {"task_id": "jyNtGaEB"}))
    assert main._lanzar_backtest("http://x", "a", "p", "FC") == "jyNtGaEB"
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: _R(200, {"taskId": "T"}))
    assert main._lanzar_backtest("http://x", "a", "p", "FC") == "T", "la forma de la documentación, también"


def test_si_ya_hay_uno_corriendo_es_ese(monkeypatch):
    pedidos = []

    def req(m, url, *a, **k):
        pedidos.append((m, url))
        if url.endswith("/_run_once"):
            r = _R(409, {"error": {"reason": "cannot start a new test for FC since current test hasn't finished."}})
            r.text = "cannot start a new test for FC since current test hasn't finished."
            return r
        return _R(200, {"run_once_task": {"task_id": "CORRIENDO"}})

    monkeypatch.setattr(main, "_os_req", req)
    assert main._lanzar_backtest("http://x", "a", "p", "FC") == "CORRIENDO"
    assert ("GET", "http://x/_plugins/_forecast/forecasters/FC?task=true") in pedidos


def test_si_el_run_once_no_arranca_se_dice_por_que(monkeypatch, capsys):
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: _R(429, {"error": "rejected execution"}))
    assert main._lanzar_backtest("http://x", "a", "p", "FC") == ""
    assert "el backtest de FC no arrancó" in capsys.readouterr().out


def test_los_backtests_se_juzgan_por_los_pasos_que_escribieron(monkeypatch):
    """INIT_TEST_FAILED con todos sus pasos anduvo; TEST_COMPLETE con el 18 %
    no (los dos casos se vieron en el cluster)."""
    pasos = {"T1": 2000, "T2": 360, "T3": None}
    monkeypatch.setattr(main, "_pasos_del_backtest", lambda b, u, p, t: pasos[t])
    lanzados = [{"fc_id": "A", "task_id": "T1"}, {"fc_id": "B", "task_id": "T2"}, {"fc_id": "C", "task_id": ""},
                {"fc_id": "D", "task_id": "T4"}, {"fc_id": "E", "task_id": "T3"}]
    estados = {"A": (False, "INIT_TEST_FAILED: all shards failed"), "B": (True, "TEST_COMPLETE"),
               "C": (False, "EN_CURSO (sin tarea todavía)"), "D": (False, "EN_CURSO (INIT_TEST)"),
               "E": (False, "FAILED")}
    j = main._juzgar_backtests("http://x", "a", "p", lanzados, estados, 2000)
    assert j["A"] == (True, "TEST_COMPLETE")
    assert j["B"] == (False, "parcial: 360 de 2000 pasos")
    assert j["C"] == (False, "no arrancó (el cluster rechazó el pedido)")
    assert j["D"] == (False, "EN_CURSO (INIT_TEST)"), "el que sigue corriendo, en curso"
    assert j["E"] == (False, "FAILED"), "sin poder medir, lo que dice la tarea"


def test_los_pasos_cuentan_solo_los_reales(monkeypatch):
    pedidos = []
    monkeypatch.setattr(main, "_os_req", lambda m, url, u, p, json_body=None, timeout=30:
                        pedidos.append((url, json_body)) or _R(200, {"count": 7}))
    assert main._pasos_del_backtest("http://x", "a", "p", "T") == 7
    url, cuerpo = pedidos[0]
    assert url == "http://x/opensearch-forecast-results*/_count"
    assert cuerpo["query"]["bool"]["must_not"] == [{"exists": {"field": "horizon_index"}}]
    assert main._pasos_del_backtest("http://x", "a", "p", "") == 0
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: None)
    assert main._pasos_del_backtest("http://x", "a", "p", "T") is None


def test_los_truncados_se_relanzan_una_vez(monkeypatch):
    """El bloque de forecasting: de a uno, juzgados por cobertura, y lo que no
    llegó (salvo lo que sigue corriendo) se relanza una vez."""
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("estados = _juzgar_backtests(base, user, password, lanzados,")
    bloque = src[i:i + 1500]
    assert "and not estados[x[\"fc_id\"]][1].startswith(FORECAST_EN_CURSO)]" in bloque
    assert bloque.count("_juzgar_backtests(") == 2
    # El caso entra al registro (y a la vista) apenas tiene sus forecasters.
    assert bloque.index("registry[slug] = ids") < src.index("# ── Anomaly Detection + Alerting", i) - i


# ── El agente, una vez por run ──────────────────────────────────────────────
def test_el_agente_se_arma_una_vez_al_final(monkeypatch, tmp_path):
    registro = {"a": {"ppl_model_id": "P", "llm_model_id": "L"}, "b": {"ppl_model_id": "P", "llm_model_id": "L"}}
    monkeypatch.setattr(main, "_read_capabilities", lambda td: registro)
    llamadas, pasos = [], []

    def registrar(base, u, p, key, td, reg, ppl, llm, slug_actual=""):
        llamadas.append((ppl, llm, slug_actual))
        for ids in reg.values():
            ids["agent_id"] = "AG"
        return "AG", {"ok": True, "note": "t2v listo"}

    monkeypatch.setattr(main, "_registrar_agente", registrar)
    monkeypatch.setattr(main.runs, "step", lambda run, n, ok, r="": pasos.append((n, ok, r)))
    import maas_integrator
    monkeypatch.setattr(maas_integrator, "get_maas_api_key", lambda: "K")
    resultado = {"a": {"conversational": {"ok": True, "agente_pendiente": True, "note": "x"}},
                 "b": {"conversational": {"ok": True, "agente_pendiente": True, "note": "x"}},
                 "c": {"conversational": {"ok": False, "reason": "sin key"}}}
    main._registrar_agente_del_run({"public_endpoint": "x:9200"}, "admin", "pw", False, tmp_path, resultado, {})
    assert llamadas == [("P", "L", "")], "una sola vez, con todas las fuentes"
    assert pasos == [("Asistente", True, "un agente para 2 casos"), ("text2viz", True, "t2v listo")]
    assert resultado["a"]["conversational"] == {"ok": True, "agent_id": "AG", "note": ""}
    # Nada pendiente (re-provisión con el agente ya hecho): no se toca.
    llamadas.clear()
    main._registrar_agente_del_run({"public_endpoint": "x:9200"}, "admin", "pw", False, tmp_path,
                                   {"a": {"conversational": {"ok": True, "reason": "ya provisionado"}}}, {})
    assert llamadas == []


def test_provisionar_plugins_no_arma_el_agente_en_cada_caso():
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    cuerpo = src[i:src.index("\n    msg = ", i)]
    assert "registrar_agente=False,   # el force ya bajó todo, arriba" in cuerpo
    assert cuerpo.index("registrar_agente=False") < cuerpo.index("_registrar_agente_del_run(")
    j = src.index("def _provision_capabilities(")
    caso = src[j:src.index("    # ── Forecasting", j)]
    assert "if not registrar_agente:" in caso and "\"agente_pendiente\": True" in caso


def test_el_agente_es_de_los_casos_del_entorno(monkeypatch, tmp_path):
    """Recorrer todos los curados sumaba las variantes de FortiAnalyzer (soc,
    traffic, utm, event) sobre los mismos índices: 8 herramientas para 4
    casos (visto en CSS 3.4)."""
    main._write_pipelines_registry(tmp_path, {"fortianalyzer": {}, "transacciones-billetera": {}})
    monkeypatch.setattr(main, "_index_ready_for_capabilities", lambda *a, **k: (True, ""))
    fuentes = main._fuentes_del_agente("http://x", "a", "p", tmp_path)
    assert sorted(f["tool_name"] for f in fuentes) == ["PPLTool-fortianalyzer", "PPLTool-transacciones-billetera"]
    # Sin registro (un entorno de antes), los curados como antes.
    main._write_pipelines_registry(tmp_path, {})
    assert len(main._fuentes_del_agente("http://x", "a", "p", tmp_path)) > 2


def test_el_agente_se_arma_apenas_hay_modelos_y_los_siguientes_lo_reusan(monkeypatch, tmp_path):
    registro = {"a": {"ppl_model_id": "P", "llm_model_id": "L"}}
    monkeypatch.setattr(main, "_read_capabilities", lambda td: registro)
    monkeypatch.setattr(main, "_write_capabilities", lambda td, reg: None)
    llamadas = []
    monkeypatch.setattr(main, "_registrar_agente", lambda *a, **k: llamadas.append(1) or ("AG", {}))
    monkeypatch.setattr(main.runs, "step", lambda *a, **k: None)
    import maas_integrator
    monkeypatch.setattr(maas_integrator, "get_maas_api_key", lambda: "K")
    pend = lambda: {"conversational": {"ok": True, "agente_pendiente": True}}
    resultado = {"a": pend()}
    assert main._registrar_agente_del_run({"public_endpoint": "x:9200"}, "admin", "pw", False, tmp_path, resultado, {}) == "AG"
    resultado["b"] = pend()
    assert main._registrar_agente_del_run({"public_endpoint": "x:9200"}, "admin", "pw", False, tmp_path, resultado, {}, "AG") == "AG"
    assert llamadas == [1], "el segundo caso reusa el agente, no lo rearma"
    assert registro["b"]["agent_id"] == "AG" and resultado["b"]["conversational"] == {"ok": True, "agent_id": "AG", "note": ""}


def test_el_run_arma_el_agente_despues_de_cada_caso_no_al_final():
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    cuerpo = src[i:src.index("\n    msg = ", i)]
    bucle = cuerpo[cuerpo.index("    for slug in slugs:"):cuerpo.index("    # Por si el primero no pudo")]
    assert "agente_del_run = _registrar_agente_del_run(cluster, user, password, request.https_enabled," in bucle


def test_volver_a_provisionar_rejuzga_los_backtests_que_ya_estan(monkeypatch):
    """Sin esto la tarjeta se quedaba con el "no arrancó" de la provisión
    anterior aunque el backtest hubiera corrido."""
    tareas = {"F1": "T1", "F2": "T2"}
    monkeypatch.setattr(main, "_os_req", lambda m, url, *a, **k: _R(200, {
        "forecaster": {"name": url.split("/")[-1].split("?")[0], "history": 600},
        "run_once_task": {"task_id": tareas[url.split("/")[-1].split("?")[0]]}}))
    monkeypatch.setattr(main, "_esperar_backtests", lambda b, u, p, lanzados, rondas=8:
                        {x["fc_id"]: (True, "TEST_COMPLETE") for x in lanzados})
    monkeypatch.setattr(main, "_pasos_del_backtest", lambda b, u, p, t: 600 if t == "T1" else 100)
    r = main._rejuzgar_backtests("http://x", "a", "p", ["F1", "F2"])
    assert r["states"] == ["F1=TEST_COMPLETE", "F2=parcial: 100 de 600 pasos"]
    assert r["ok"] and r["note"] == "backtest OK (1/2) · fallaron: F2=parcial: 100 de 600 pasos"
    import plugins_vista as pv
    assert "forecasting" in pv.estados_desde_resultado({"forecast": r}), "se guarda (no es un «ya provisionado»)"
    # Sin tareas legibles, el de siempre.
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: None)
    assert main._rejuzgar_backtests("http://x", "a", "p", ["F1"])["reason"] == "ya provisionado"


def test_la_provision_rejuzga_cuando_ya_estan():
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    j = src.index("def _provision_capabilities(")
    caso = src[j:src.index("    # ── Anomaly Detection", j)]
    assert 'result["forecast"] = _rejuzgar_backtests(base, user, password, forecaster_ids)' in caso


def test_si_la_tarea_tarda_en_figurar_se_vuelve_a_leer(monkeypatch):
    """Visto en CSS 3.4: el 409 llegó antes que la tarea, la primera lectura de
    `?task=true` vino vacía y un backtest que terminó bien quedaba "no arrancó"."""
    lecturas, esperas = [], []

    def req(m, url, *a, **k):
        if url.endswith("/_run_once"):
            r = _R(409)
            r.text = "cannot start a new test for FC since current test hasn't finished."
            return r
        lecturas.append(url)
        return _R(200, {"run_once_task": {"task_id": "TARDE"}} if len(lecturas) == 3 else {})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main.time, "sleep", lambda s: esperas.append(s))
    assert main._lanzar_backtest("http://x", "a", "p", "FC") == "TARDE"
    assert len(lecturas) == 3 and esperas == [main._ESPERA_LECTURA_TAREA_S] * 2
    lecturas.clear()
    monkeypatch.setattr(main, "_os_req", lambda m, url, *a, **k: req(m, url) if url.endswith("/_run_once")
                        else lecturas.append(url) or _R(200, {}))
    assert main._lanzar_backtest("http://x", "a", "p", "FC") == ""
    assert len(lecturas) == main._LECTURAS_DE_LA_TAREA, "con tope"


def test_el_asistente_va_antes_que_los_casos():
    """Esperaba al primer caso entero (cola libre, backtests, anomalías,
    Security Analytics): con un SIEM primero, minutos sin chat."""
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    cuerpo = src[i:src.index("\n    msg = ", i)]
    bucle = cuerpo.index("    for slug in slugs:")
    assert -1 < cuerpo.index("solo_conversacional=True") < bucle
    assert -1 < cuerpo.index("_registrar_agente_del_run(") < bucle
    # Con un force, todo se baja una vez antes de crear nada: bajando caso por
    # caso, el segundo se llevaba el agente recién armado.
    assert cuerpo.index("_teardown_slug_caps(_base, user, password, registro[s])") < cuerpo.index("solo_conversacional=True")
    assert "force=request.force" not in cuerpo


def test_solo_el_asistente_no_crea_el_resto(monkeypatch, tmp_path):
    """La primera pasada deja los modelos (el agente lo arma el run) y nada más:
    ni forecasters ni detectores."""
    monkeypatch.setenv("MAAS_API_KEY", "KEY")
    persistido = {}
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {})
    monkeypatch.setattr(main, "_write_capabilities", lambda td, reg: persistido.update(reg))
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_cluster_hwc_creds", lambda td: ("", ""))
    monkeypatch.setattr(main, "_ml_commons_available", lambda b, u, p: True)
    monkeypatch.setattr(main, "_resolve_capability_spec", lambda *a: {
        "index_pattern": "ventas-*", "operations": [], "fields": [], "label": "Ventas",
        "volume_field": "monto", "forecasts": [{"name": "f", "feature_name": "x"}]})
    pedidos = []
    ids = iter(["C1", "C2", "T1", "T2"])

    def req(m, url, *a, json_body=None, **k):
        pedidos.append(url)
        if url.endswith("/model_groups/_register"):
            return _R(200, {"model_group_id": "MG"})
        if url.endswith("/connectors/_create"):
            return _R(200, {"connector_id": next(ids)})
        if url.endswith("/models/_register"):
            return _R(200, {"task_id": next(ids)})
        return _R(200, {})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_ml_wait_model", lambda b, u, p, t: {"T1": "M1", "T2": "M2"}[t])
    monkeypatch.setattr(main, "_ml_wait_deployed", lambda b, u, p, m: (True, "DEPLOYED"))
    r = main._provision_capabilities({"public_endpoint": "x:9200"}, "ventas", "admin", "pw", False,
                                     registrar_agente=False, solo_conversacional=True)
    assert r["conversational"]["agente_pendiente"] and set(r) == {"conversational"}
    assert persistido["ventas"]["ppl_model_id"] == "M1" and persistido["ventas"]["llm_model_id"] == "M2"
    assert not [u for u in pedidos if "_forecast" in u or "_anomaly" in u or "_alerting" in u]


def test_con_force_todo_se_baja_una_vez_antes_de_crear(monkeypatch):
    """El agente es de todo el cluster y queda anotado en cada caso: bajando
    caso por caso, el segundo borraba el agente que acababa de armar el primero."""
    from fastapi.testclient import TestClient
    orden = []
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_teardown_orphans_by_name", lambda *a: orden.append("huérfanos"))
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {"siem": {"agent_id": "AG"}, "ventas-ecommerce": {"agent_id": "AG"}})
    monkeypatch.setattr(main, "_write_capabilities", lambda td, reg: orden.append(("registro", dict(reg))))
    monkeypatch.setattr(main, "_teardown_slug_caps", lambda b, u, p, ids: orden.append(("bajar", ids.get("agent_id"))))
    monkeypatch.setattr(main, "_provision_capabilities", lambda *a, **k: orden.append(("crear", a[1], k.get("force", False))) or {})
    for f in ("_provisionar_analista", "_provisionar_perfil", "_provisionar_reporte"):
        monkeypatch.setattr(main, f, lambda *a, **k: {"ok": True, "reason": "ok"})
    monkeypatch.setattr(main, "_revisar_meses_de_seguridad", lambda *a, **k: None)
    for f in ("_registrar_capacidades", "_asegurar_ppl_v3", "_provisionar_canal"):
        monkeypatch.setattr(main, f, lambda *a, **k: None)   # contra la IP de prueba, timeouts
    r = TestClient(main.app).post("/api/v1/onboarding/provision-capabilities", json={
        "opensearch_password": "pw", "slugs": ["siem", "ventas-ecommerce"], "force": True})
    assert r.status_code == 200
    bajadas = [i for i, x in enumerate(orden) if isinstance(x, tuple) and x[0] == "bajar"]
    creadas = [i for i, x in enumerate(orden) if isinstance(x, tuple) and x[0] == "crear"]
    assert len(bajadas) == 2 and max(bajadas) < min(creadas), orden
    assert all(not orden[i][2] for i in creadas), "ninguna pasada vuelve a bajar"
    assert ("registro", {"siem": {}, "ventas-ecommerce": {}}) in orden


def test_el_agente_queda_en_todos_los_casos_que_consulta(monkeypatch, tmp_path):
    """El chat ofrece los casos con agente: anotado solo en el primero (el que
    tenía los modelos), los demás aparecían de a uno a medida que se
    provisionaban, aunque el agente ya los consultaba a todos."""
    monkeypatch.setattr(main, "_fuentes_del_agente", lambda *a, **k: [
        {"tool_name": "PPLTool-siem"}, {"tool_name": "PPLTool-ventas"}, {"tool_name": "PPLTool-pozos"}])
    monkeypatch.setattr(main, "_search_ids", lambda *a, **k: [])
    monkeypatch.setattr(main, "_ml_create", lambda *a, **k: "AG")
    monkeypatch.setattr(main, "_set_os_chat_root_agent", lambda *a: True)
    monkeypatch.setattr(main, "_provisionar_text2viz", lambda *a: {})
    escrito = {}
    monkeypatch.setattr(main, "_write_capabilities", lambda td, reg: escrito.update(reg))
    import capabilities
    monkeypatch.setattr(capabilities, "build_agent_system_instruction", lambda v: "")
    monkeypatch.setattr(capabilities, "build_conversational_agent", lambda *a: {})
    registro = {"siem": {"llm_model_id": "M", "ppl_model_id": "P"}}
    assert main._registrar_agente("http://x", "a", "p", "KEY", tmp_path, registro, "P", "M")[0] == "AG"
    assert {s: v.get("agent_id") for s, v in escrito.items()} == {"siem": "AG", "ventas": "AG", "pozos": "AG"}
