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
