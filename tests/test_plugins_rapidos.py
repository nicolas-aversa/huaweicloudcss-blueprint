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
    assert "force=request.force, registrar_agente=False," in cuerpo
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
