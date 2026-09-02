"""Audit log append-only para la app hosteada.

Registra quién hizo qué (login, logout, deploy/destroy, provisioning, cambios de
settings, acciones de admin) en un JSONL bajo `DATA_ROOT/audit.log`. Best-effort:
nunca rompe el request si el log falla. Solo tiene sentido en modo hosteado; en
single-user igual funciona (user = '-').
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import auth as _auth


def _audit_file() -> Path:
    return _auth.DATA_ROOT / "audit.log"


def record(action: str, detail: str = "", user: str | None = None, ip: str | None = None) -> None:
    """Agrega una entrada al audit log. `user` se toma del contexto si no se pasa."""
    try:
        if user is None:
            ctx = _auth.current_user_var.get()
            user = ctx.email if ctx is not None else "-"
        entry = {
            "ts": int(time.time()),
            "user": user or "-",
            "action": action,
            "detail": (detail or "")[:500],
        }
        if ip:
            entry["ip"] = ip
        f = _audit_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def tail(n: int = 200) -> list[dict]:
    """Últimas `n` entradas, más nuevas primero. [] si no hay log."""
    try:
        f = _audit_file()
        if not f.is_file():
            return []
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        out: list[dict] = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        out.reverse()
        return out
    except Exception:
        return []
