"""
insights.py
===========

Lógica PURA (sin I/O) de "Rendimiento": las consultas más pesadas del cluster
según Query Insights (`GET _insights/top_queries`), que ya viene activo en CSS
3.4 (latencia, CPU y memoria; top 10 por ventana de 5 min, exportado a un
índice local por 7 días). Con `from`/`to` se leen las últimas 24 h.

Se dejan afuera las consultas internas (índices que empiezan con punto: las
del propio Alerting, ml-commons, etc.): lo que interesa es lo que se le
preguntó a los datos, incluido lo que consulta el asistente.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TIPOS = ("latency", "cpu", "memory")


def ventana(ahora: datetime, horas: int = 24) -> tuple[str, str]:
    """`from` y `to` para `_insights/top_queries` (ISO UTC)."""
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return (ahora - timedelta(hours=horas)).astimezone(timezone.utc).strftime(fmt), ahora.astimezone(timezone.utc).strftime(fmt)


def _de_datos(q: dict) -> bool:
    indices = q.get("indices") or []
    return bool(indices) and not all(str(i).startswith(".") for i in indices)


def _medida(q: dict, tipo: str) -> float:
    return float(((q.get("measurements") or {}).get(tipo) or {}).get("number") or 0)


def forma_de_la_consulta(source: dict) -> str:
    """Qué hace la consulta, en pocas palabras: agregación (con sus tipos),
    búsqueda con filtro o conteo."""
    source = source or {}
    aggs = source.get("aggregations") or source.get("aggs") or {}
    if aggs:
        tipos = []
        for a in aggs.values():
            tipos += [k for k in (a or {}) if k not in ("aggregations", "aggs", "meta")]
        return "agregación" + (f" ({', '.join(sorted(set(tipos))[:3])})" if tipos else "")
    if source.get("query"):
        return "búsqueda con filtro" if source.get("size", 10) else "conteo con filtro"
    return "conteo total" if source.get("size") == 0 else "búsqueda"


def consultas(top: list[dict], tipo: str, n: int = 15) -> list[dict]:
    """Las `n` consultas sobre datos más pesadas según `tipo`, en unidades que
    se leen: latencia en ms, CPU en ms (viene en ns), memoria en KB (bytes)."""
    filas = sorted((q for q in top or [] if _de_datos(q)), key=lambda q: -_medida(q, tipo))[:n]
    fuera = []
    for q in filas:
        ts = q.get("timestamp")
        fuera.append({
            "hora": datetime.fromtimestamp(float(ts) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
            "indices": ", ".join(q.get("indices") or []),
            "latencia_ms": round(_medida(q, "latency")),
            "cpu_ms": round(_medida(q, "cpu") / 1e6, 1),
            "memoria_kb": round(_medida(q, "memory") / 1024, 1),
            "forma": forma_de_la_consulta(q.get("source") or {}),
            "shards": q.get("total_shards"),
        })
    return fuera
