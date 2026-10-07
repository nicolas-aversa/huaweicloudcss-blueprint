"""
pronosticos.py
==============

Lógica PURA (sin I/O) de lo que calculó cada forecaster y su error contra lo
que pasó: el número de la tarjeta de Forecasting en la vista Plugins.

El backtest (`_run_once`) recorre el último año hasta HOY en pasos del
intervalo del forecaster y, en cada paso, guarda el valor real de ese intervalo
(un resultado con `feature_data`, sin `horizon_index`) y un pronóstico por cada
paso del horizonte (`horizon_index` 1..N, con su banda). Como el dataset de demo
termina antes de hoy, los pasos más recientes pronostican sobre la nada. Por
eso se ancla en el último paso que todavía tiene datos reales DESPUÉS: así se
ve el pronóstico y, encima, lo que de verdad pasó.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Cuántos pasos reales se muestran antes del ancla (contexto del gráfico).
PASOS_DE_CONTEXTO = 30


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def intervalo_ms(forecaster: dict) -> int:
    """El intervalo del forecaster en ms (`forecast_interval.period`)."""
    p = ((forecaster or {}).get("forecast_interval") or {}).get("period") or {}
    unidad = {"Minutes": 60_000, "Hours": 3_600_000, "Days": 86_400_000}.get(p.get("unit"), 60_000)
    return int(p.get("interval") or 0) * unidad


def limite_del_ancla(fin_de_los_datos_ms: float, forecaster: dict) -> int:
    """El último fin de paso que deja el horizonte entero dentro de los datos.
    Entero: un float (1.78E12) no parsea como fecha en un range."""
    return int(fin_de_los_datos_ms) - intervalo_ms(forecaster) * int((forecaster or {}).get("horizon") or 0)


def valor_real(resultado: dict) -> "float | None":
    datos = resultado.get("feature_data") or []
    try:
        return float(datos[0].get("data")) if datos else None
    except (TypeError, ValueError):
        return None


# Cuántos pasos hacia atrás se buscan para anclar (el final del dataset puede
# tener ceros: en el SIEM los últimos pasos valen 0 y el pronóstico no se compara).
PASOS_DE_BUSQUEDA = 200
# Si ahí no hay un tramo comparable (un backtest parcial), los últimos que haya.
PASOS_DE_RESPALDO = 1000

# Estados de la tarea del backtest que todavía no terminó.
_CORRIENDO = {"CREATED", "INIT", "INIT_TEST", "RUNNING", "TEST"}


def motivo_sin_grafico(estado: str, pasos: "int | None", history: int) -> str:
    """Por qué un forecaster no tiene gráfico, dicho con lo que de verdad pasó:
    "todavía" solo si el backtest sigue corriendo (antes se decía siempre, y
    hacía esperar resultados que no iban a llegar)."""
    if estado.upper() in _CORRIENDO:
        return "el backtest todavía está corriendo: en unos minutos se ve"
    if not pasos:
        return ("el backtest no escribió resultados (el cluster rechazó sus búsquedas): "
                "«Volver a provisionar plugins» lo relanza")
    total = f" de {history}" if history else ""
    return (f"backtest parcial: {pasos}{total} pasos, sin un tramo con datos para comparar el pronóstico "
            "(el cluster rechazó parte de sus búsquedas)")


def elegir_ancla(reales: list[dict], horizonte: int) -> "int | None":
    """El fin del último paso con valor cuyo horizonte siguiente tiene datos en
    al menos la mitad de sus pasos: así el pronóstico se puede comparar con lo
    que pasó. `reales` ordenados por `data_end_time`."""
    valores = [(int(r["data_end_time"]), valor_real(r) or 0) for r in reales if r.get("data_end_time")]
    for i in range(len(valores) - horizonte - 1, -1, -1):
        siguientes = [v for _, v in valores[i + 1:i + 1 + horizonte]]
        if valores[i][1] and len(siguientes) == horizonte and sum(1 for v in siguientes if v) * 2 >= horizonte:
            return valores[i][0]
    return None


def _cercano(real: dict[int, float], t: int, tolerancia: int) -> "float | None":
    """El valor real del paso que termina en `t`. Los pasos del pronóstico y los
    del valor real pueden no caer en el mismo milisegundo: se toma el más cercano
    dentro de medio paso."""
    if t in real:
        return real[t]
    candidatos = [k for k in real if abs(k - t) <= tolerancia]
    return real[min(candidatos, key=lambda k: abs(k - t))] if candidatos else None


def serie(reales: list[dict], pronosticos: list[dict], ancla_ms: float, paso_ms: int = 0) -> dict:
    """Los puntos del gráfico: la serie real (alineada por el FIN de cada paso)
    y el pronóstico del ancla con su banda, más el error medio del horizonte
    contra lo que pasó (MAPE, sobre los pasos con valor real distinto de 0)."""
    real = {}
    for r in reales:
        v = valor_real(r)
        if v is not None and r.get("data_end_time"):
            real[int(r["data_end_time"])] = v
    puntos_reales = [{"t": _iso(t), "v": round(v, 2)} for t, v in sorted(real.items())]
    puntos_pron, errores = [], []
    for p in sorted((p for p in pronosticos if p.get("horizon_index")), key=lambda p: p["horizon_index"]):
        t = int(p.get("forecast_data_end_time") or 0)
        v = float(p.get("forecast_value") or 0)
        puntos_pron.append({"t": _iso(t), "v": round(v, 2),
                            "lo": round(float(p.get("forecast_lower_bound") or v), 2),
                            "hi": round(float(p.get("forecast_upper_bound") or v), 2)})
        observado = _cercano(real, t, paso_ms // 2)
        if observado:
            errores.append(abs(v - observado) / abs(observado))
    return {"ancla": _iso(ancla_ms), "real": puntos_reales, "pronostico": puntos_pron,
            "error_pct": round(sum(errores) / len(errores) * 100, 1) if errores else None}
