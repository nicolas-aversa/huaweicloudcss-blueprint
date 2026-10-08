"""Los backtests de forecast se lanzan juntos y se esperan juntos.

Antes cada uno se esperaba hasta que terminaba y recién ahí se lanzaba el
siguiente: 3 por caso en fila, ~45 s por caso, ~7 minutos para 9 casos con
todo ya creado (16 la primera vez)."""
import main


def _estados(secuencias, llamados):
    """Un `_forecast_test_state` falso: cada fc_id devuelve su secuencia."""
    its = {k: iter(v) for k, v in secuencias.items()}

    def fake(base, user, password, fc_id, task_id="", tries=8, delay=None):
        llamados.append((fc_id, task_id, tries))
        return next(its[fc_id])
    return fake


def test_se_esperan_juntos(monkeypatch):
    llamados, esperas = [], []
    monkeypatch.setattr(main, "_forecast_test_state", _estados({
        "A": [(False, "EN_CURSO (INIT_TEST)"), (True, "TEST_COMPLETE")],
        "B": [(True, "TEST_COMPLETE")],
        "C": [(False, "EN_CURSO (RUNNING)"), (False, "EN_CURSO (RUNNING)"), (False, "FAILED")],
    }, llamados))
    monkeypatch.setattr(main.time, "sleep", lambda s: esperas.append(s))
    lanzados = [{"fc_id": f, "task_id": f"t{f}"} for f in "ABC"]
    r = main._esperar_backtests("http://x", "a", "p", lanzados)
    assert r == {"A": (True, "TEST_COMPLETE"), "B": (True, "TEST_COMPLETE"), "C": (False, "FAILED")}
    # Ronda 1: los tres; ronda 2: A y C; ronda 3: C. Una espera entre rondas, no por backtest.
    assert [f for f, _, _ in llamados] == ["A", "B", "C", "A", "C", "C"]
    assert all(t == 1 and tid == f"t{f}" for f, tid, t in llamados), "una mirada por ronda, con su task_id"
    assert len(esperas) == 2


def test_lo_que_sigue_corriendo_queda_en_curso(monkeypatch):
    llamados = []
    monkeypatch.setattr(main, "_forecast_test_state", _estados({"A": [(False, "EN_CURSO (RUNNING)")] * 3}, llamados))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    assert main._esperar_backtests("http://x", "a", "p", [{"fc_id": "A"}], rondas=3) == {"A": (False, "EN_CURSO (RUNNING)")}
    assert len(llamados) == 3


def test_sin_lanzados_no_espera(monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("no debería esperar")))
    assert main._esperar_backtests("http://x", "a", "p", []) == {}


def test_lanzar_devuelve_el_task_id(monkeypatch):
    class _R:
        status_code = 200

        def json(self):
            return {"task_id": "T7"}

    pedidos = []
    monkeypatch.setattr(main, "_os_req", lambda m, url, *a, **k: pedidos.append((m, url)) or _R())
    assert main._lanzar_backtest("http://x", "a", "p", "FC") == "T7"
    assert pedidos == [("POST", "http://x/_plugins/_forecast/forecasters/FC/_run_once")]
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: None)
    assert main._lanzar_backtest("http://x", "a", "p", "FC") == ""


# ── El botón del paso 3 ─────────────────────────────────────────────────────
def _html():
    import pathlib
    return (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_volver_a_provisionar_no_rehace_todo():
    """Con los plugins ya provisionados el botón mandaba `force` y rehacía todo
    (agente, pronósticos, anomalías y alertas de los 9 casos): ~7 minutos."""
    html = _html()
    assert "provisionCapabilitiesFromInfra(e.currentTarget, pipelines, false));" in html
    assert "provisionCapabilitiesFromInfra(e.currentTarget, pipelines, capsDone)" not in html


def test_no_hay_boton_de_rehacer_todo():
    """Era redundante con "Volver a provisionar plugins"."""
    html = _html()
    assert "infra-capabilities-rehacer" not in html and ">Rehacer todo<" not in html
