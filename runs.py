"""Registro persistente de ejecuciones ("runs") — la vista **Actividad**.

Antes de esto no se podía responder *"¿corrió el apply? ¿se importaron los
dashboards?"* después del hecho: el stdout de Terraform se descartaba salvo que
fallara, y los pasos post-deploy (index template, dashboards, capabilities) solo
dejaban un `print()` que muere con el contenedor.

Un **run** es una operación con su línea de tiempo de eventos, persistida en
`{data_dir}/runs/<id>.json` después de cada evento — así sobrevive un refresh del
browser, un reinicio del contenedor y sirve de historial.

Generaliza la cola de jobs del deploy (que ya hacía esto, pero solo para el
deploy, sin poder listar el historial y sin releer el disco al arrancar):

- `kind`: ``deploy`` | ``ingestion`` | ``schema`` | ``capabilities`` | ``destroy``
- `status`: ``running`` | ``complete`` | ``error``
- eventos: ``{ts, type, ...}`` — ``progress`` (fase + %), ``log`` (línea cruda),
  ``step`` (sub-paso con ok/reason), ``error``, ``complete``

Los eventos ``log`` están **topeados** (`_MAX_LOG_EVENTS` / `_MAX_LOG_BYTES`): un
`terraform apply` puede emitir miles de líneas y el run entero se reescribe en
cada flush.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import auth

# Tope de líneas crudas por run: lo suficiente para reconstruir qué hizo el apply,
# sin que el .json crezca sin control (se reescribe entero en cada evento).
_MAX_LOG_EVENTS = 2000
_MAX_LOG_BYTES = 200_000
# Retención del historial: los runs viejos se podan al crear uno nuevo.
_MAX_RUNS = 50
_MAX_AGE_DAYS = 30

KINDS = ("deploy", "ingestion", "schema", "capabilities", "destroy")

_RUNS: dict[str, dict] = {}
_GUARD = threading.Lock()


def runs_dir() -> Path:
    """Directorio de runs del usuario actual (o global en single-user)."""
    ctx = auth.current_user_var.get()
    base = ctx.data_dir if ctx is not None else Path(__file__).parent
    d = base / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _current_user() -> str:
    ctx = auth.current_user_var.get()
    return ctx.user_id if ctx is not None else "-"


def _flush(run: dict, d: Path) -> None:
    """Persiste el run entero. Best-effort: nunca rompe la operación en curso."""
    try:
        (d / f"{run['id']}.json").write_text(json.dumps(run), encoding="utf-8")
    except OSError:
        pass


def _read_file(run_id: str, d: Path | None = None) -> dict | None:
    try:
        f = (d or runs_dir()) / f"{run_id}.json"
        if f.is_file():
            return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


# ── Ciclo de vida ────────────────────────────────────────────────────────────
def start(kind: str, detail: str = "", run_id: str | None = None) -> dict:
    """Abre un run y lo persiste. Devuelve el dict (mutable, vivo en memoria)."""
    d = runs_dir()
    prune(d)
    now = int(time.time())
    run = {
        "id": run_id or uuid.uuid4().hex[:16],
        "kind": kind if kind in KINDS else "deploy",
        "detail": detail,
        "status": "running",
        "created": now,
        "updated": now,
        "finished": None,
        "user": _current_user(),
        "events": [],
        "log_lines": 0,
        "log_bytes": 0,
        "log_truncated": False,
    }
    with _GUARD:
        _RUNS[run["id"]] = run
    _flush(run, d)
    return run


def append(run: dict | str, event: dict, d: Path | None = None) -> None:
    """Agrega un evento y persiste. `run` puede ser el dict o un id.

    Los eventos ``log`` se descartan una vez alcanzado el tope (se marca
    `log_truncated`), así el archivo no crece sin límite.
    """
    run = _resolve(run)
    if run is None:
        return
    d = d or runs_dir()
    evt = dict(event)
    evt.setdefault("ts", int(time.time()))
    with _GUARD:
        if evt.get("type") == "log":
            msg = str(evt.get("message", ""))
            if (run["log_lines"] >= _MAX_LOG_EVENTS
                    or run["log_bytes"] + len(msg) > _MAX_LOG_BYTES):
                if not run["log_truncated"]:
                    run["log_truncated"] = True
                    run["events"].append({
                        "ts": evt["ts"], "type": "log", "level": "warn",
                        "message": f"[… salida truncada a {_MAX_LOG_EVENTS} líneas …]"})
                else:
                    return
            else:
                run["log_lines"] += 1
                run["log_bytes"] += len(msg)
                run["events"].append(evt)
        else:
            run["events"].append(evt)
        run["updated"] = evt["ts"]
        if evt.get("type") in ("complete", "error"):
            run["status"] = evt["type"]
    _flush(run, d)


def step(run: dict | str, name: str, ok: bool, reason: str = "") -> None:
    """Registra un sub-paso con su resultado. Es lo que responde "¿se importaron
    los dashboards?" — un evento por acción, con el motivo si falló."""
    append(run, {"type": "step", "name": name, "ok": bool(ok), "reason": reason or ""})


def finish(run: dict | str, status: str = "complete", detail: str = "") -> None:
    run = _resolve(run)
    if run is None:
        return
    with _GUARD:
        run["status"] = status
        run["finished"] = int(time.time())
        run["updated"] = run["finished"]
        if detail:
            run["detail"] = detail
    _flush(run, runs_dir())


def _resolve(run: dict | str) -> dict | None:
    if isinstance(run, dict):
        return run
    with _GUARD:
        found = _RUNS.get(run)
    return found if found is not None else _read_file(run)


# ── Lectura ──────────────────────────────────────────────────────────────────
def get(run_id: str) -> dict | None:
    """Run completo (con eventos). Memoria primero: si está corriendo, ahí está
    lo más fresco; si no, se lee del disco."""
    with _GUARD:
        run = _RUNS.get(run_id)
        if run is not None:
            return json.loads(json.dumps(run))  # copia estable para serializar
    return _read_file(run_id)


def _summary(run: dict) -> dict:
    """Fila del listado: sin `events` (pueden ser miles)."""
    steps = [e for e in run.get("events", []) if e.get("type") == "step"]
    return {
        "id": run.get("id", ""),
        "kind": run.get("kind", ""),
        "detail": run.get("detail", ""),
        "status": run.get("status", ""),
        "created": run.get("created", 0),
        "finished": run.get("finished"),
        "duration": (run.get("finished") or run.get("updated", 0)) - run.get("created", 0),
        "steps_ok": sum(1 for s in steps if s.get("ok")),
        "steps_failed": sum(1 for s in steps if not s.get("ok")),
        "events": len(run.get("events", [])),
    }


def list_runs(limit: int = 50) -> list[dict]:
    """Historial del usuario, más nuevo primero.

    Lee del **disco** y no solo de memoria: `_RUNS` se pierde al reiniciar el
    contenedor, y sin esto el historial quedaba invisible aunque los .json
    siguieran ahí.
    """
    d = runs_dir()
    out: dict[str, dict] = {}
    for f in d.glob("*.json"):
        run = _read_file(f.stem, d)
        if run:
            out[run.get("id", f.stem)] = run
    with _GUARD:
        for rid, run in _RUNS.items():
            if run.get("user") == _current_user():
                out[rid] = run
    rows = [_summary(r) for r in out.values()]
    rows.sort(key=lambda r: r["created"], reverse=True)
    return rows[:max(1, limit)]


def running_id(kind: str | None = None) -> str | None:
    """Id del run en curso del usuario (para reenganchar tras un refresh)."""
    uid = _current_user()
    with _GUARD:
        for rid, r in _RUNS.items():
            if r.get("user") == uid and r.get("status") == "running":
                if kind is None or r.get("kind") == kind:
                    return rid
    return None


def prune(d: Path | None = None) -> int:
    """Borra runs viejos (por antigüedad y por cantidad). Devuelve cuántos borró.

    Sin esto los archivos se acumulan indefinidamente en el volumen de datos.
    """
    d = d or runs_dir()
    try:
        files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    cutoff = time.time() - _MAX_AGE_DAYS * 86400
    removed = 0
    for i, f in enumerate(files):
        try:
            if i >= _MAX_RUNS or f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                _RUNS.pop(f.stem, None)
                removed += 1
        except OSError:
            pass
    return removed
