"""Tests del historial persistido de ejecuciones (`runs.py`, vista Actividad).

Lo que se cubre y por qué:
- que un run **sobreviva al reinicio del proceso** — el agujero de la cola de
  jobs vieja, que solo vivía en memoria y dejaba el historial invisible aunque
  los `.json` siguieran en disco;
- que los eventos `log` estén **topeados**, porque un `terraform apply` emite
  miles de líneas y el run entero se reescribe en cada flush;
- que el `prune` no deje crecer el volumen de datos para siempre.
"""

import json
import time

import pytest

import auth
import runs


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(auth, "current_user_var", auth.current_user_var)
    runs._RUNS.clear()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runs, "runs_dir", lambda: _dir(tmp_path))
    return tmp_path


def _dir(tmp_path):
    d = tmp_path / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_start_append_finish_persists(isolated):
    run = runs.start("deploy", detail="siem · 1 pipeline")
    runs.append(run, {"type": "progress", "percent": 5, "phase": "Terraform apply"})
    runs.step(run, "terraform apply", True)
    runs.step(run, "Dashboards · siem", False, "el cluster devolvió 503")
    runs.finish(run, "complete", detail="listo")

    on_disk = json.loads((isolated / "runs" / f"{run['id']}.json").read_text(encoding="utf-8"))
    assert on_disk["kind"] == "deploy"
    assert on_disk["status"] == "complete"
    assert on_disk["detail"] == "listo"
    assert on_disk["finished"] is not None
    kinds = [e["type"] for e in on_disk["events"]]
    assert kinds == ["progress", "step", "step"]
    assert all("ts" in e for e in on_disk["events"])

    fila = runs.list_runs()[0]
    assert fila["steps_ok"] == 1 and fila["steps_failed"] == 1
    assert "events" not in fila or isinstance(fila["events"], int), "el listado no manda los eventos"


def test_run_survives_process_restart(isolated):
    """El listado se arma del DISCO: sin esto, reiniciar el contenedor borraba el
    historial de la vista aunque los archivos siguieran ahí."""
    run = runs.start("schema", detail="siem")
    runs.step(run, "Index template", True)
    runs.finish(run)

    runs._RUNS.clear()  # simula el reinicio del proceso

    filas = runs.list_runs()
    assert [f["id"] for f in filas] == [run["id"]]
    assert filas[0]["kind"] == "schema"
    recuperado = runs.get(run["id"])
    assert recuperado is not None and recuperado["events"][0]["name"] == "Index template"


def test_complete_event_sets_status(isolated):
    run = runs.start("deploy")
    runs.append(run, {"type": "complete"})
    assert run["status"] == "complete"

    otro = runs.start("deploy")
    runs.append(otro, {"type": "error", "message": "se rompió"})
    assert otro["status"] == "error"


def test_log_events_are_capped(isolated, monkeypatch):
    monkeypatch.setattr(runs, "_MAX_LOG_EVENTS", 5)
    run = runs.start("deploy")
    for i in range(50):
        runs.append(run, {"type": "log", "message": f"linea {i}"})
    runs.step(run, "terraform apply", True)

    logs = [e for e in run["events"] if e["type"] == "log"]
    assert len(logs) == 6, "5 líneas + el aviso de truncado"
    assert "truncada" in logs[-1]["message"]
    assert run["log_truncated"] is True
    # Los eventos que NO son log siguen entrando después del tope.
    assert run["events"][-1]["type"] == "step"


def test_log_events_are_capped_by_bytes(isolated, monkeypatch):
    monkeypatch.setattr(runs, "_MAX_LOG_BYTES", 100)
    run = runs.start("deploy")
    for _ in range(20):
        runs.append(run, {"type": "log", "message": "x" * 30})
    assert run["log_truncated"] is True
    assert run["log_bytes"] <= 100


def test_running_id_finds_only_running(isolated):
    assert runs.running_id() is None
    run = runs.start("deploy")
    assert runs.running_id() == run["id"]
    assert runs.running_id(kind="schema") is None
    runs.finish(run)
    assert runs.running_id() is None


def test_list_runs_is_newest_first_and_limited(isolated):
    ids = []
    for i in range(4):
        r = runs.start("deploy", detail=f"n{i}")
        r["created"] = 1000 + i          # orden determinista, sin depender del reloj
        runs._flush(r, isolated / "runs")
        ids.append(r["id"])

    filas = runs.list_runs(limit=2)
    assert [f["id"] for f in filas] == [ids[3], ids[2]]


def test_prune_removes_old_and_excess(isolated, monkeypatch):
    monkeypatch.setattr(runs, "_MAX_RUNS", 3)
    d = isolated / "runs"
    for i in range(6):
        runs.start("deploy", detail=f"n{i}")
    assert len(list(d.glob("*.json"))) <= 3 + 1, "prune corre al crear un run nuevo"

    # Y por antigüedad: un run más viejo que el corte se va aunque entre en el cupo.
    viejo = runs.start("deploy", detail="antiguo")
    f = d / f"{viejo['id']}.json"
    old = time.time() - (runs._MAX_AGE_DAYS + 1) * 86400
    import os
    os.utime(f, (old, old))
    runs.prune(d)
    assert not f.exists()


def test_get_unknown_run_is_none(isolated):
    assert runs.get("no-existe") is None
    runs.append("no-existe", {"type": "log", "message": "x"})  # no explota
    runs.finish("no-existe")
