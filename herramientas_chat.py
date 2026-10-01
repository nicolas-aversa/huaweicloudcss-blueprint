"""
herramientas_chat.py
====================

Lógica PURA (sin I/O) de las respuestas del asistente que no salen de una
consulta PPL sino de las herramientas de ml-commons / skills de OpenSearch 3.4
(`POST _plugins/_ml/tools/_execute/<tool>`), probadas sobre datos reales:

- **Explicar una anomalía o un hallazgo:** `DataDistributionTool` compara la
  distribución de cada campo en el intervalo contra un período de referencia
  (el anterior) y dice qué cambió y cuánto. `LogPatternAnalysisTool` hace lo
  mismo con los valores de un campo categórico (p. ej. `event.action` en el
  SIEM: en la anomalía del 21/10, `ssh_login` pasó del 22 % al 84 %). El LLM
  redacta sobre esos números: antes armaba consultas PPL a ciegas y terminaba
  en "la consulta no permite determinar por qué".
- **"¿Qué alertas / anomalías hubo?":** se contesta con las alertas y los
  resultados del detector del caso, no con PPL (no están en el índice del caso).

La orquestación (llamar a las herramientas, al LLM) vive en `main.py`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

FORMATO = "%Y-%m-%d %H:%M:%S"
# La referencia contra la que se compara el intervalo: el período anterior, de
# al menos un día (un intervalo de una hora contra la hora previa compara ruido).
REFERENCIA_MINIMA = timedelta(hours=24)
# Un hallazgo es un instante: se mira la hora de alrededor.
MARGEN_DE_UN_EVENTO = timedelta(hours=1)


def _fecha(valor: str) -> datetime:
    return datetime.strptime(valor.strip()[:19], FORMATO)


def ventanas(desde: str, hasta: str = "") -> dict[str, str]:
    """El intervalo a explicar y su referencia, como las piden las herramientas.
    Sin `hasta` (un hallazgo) es la hora alrededor del evento."""
    d = _fecha(desde)
    h = _fecha(hasta) if hasta else None
    if h is None or h <= d:
        d, h = d - MARGEN_DE_UN_EVENTO, d + MARGEN_DE_UN_EVENTO
    referencia = max((h - d) * 3, REFERENCIA_MINIMA)
    return {"selectionTimeRangeStart": d.strftime(FORMATO), "selectionTimeRangeEnd": h.strftime(FORMATO),
            "baselineTimeRangeStart": (d - referencia).strftime(FORMATO), "baselineTimeRangeEnd": d.strftime(FORMATO)}


def resultado_de_herramienta(cuerpo: Any) -> Any:
    """El `result` de `_execute`: JSON si lo es, si no el texto. None si no vino."""
    try:
        crudo = cuerpo["inference_results"][0]["output"][0]["result"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(crudo, str):
        return crudo
    try:
        return json.loads(crudo)
    except ValueError:
        return crudo


def _pct(x: Any) -> str:
    return "—" if x is None else f"{round(float(x) * 100)} %"


def cambios_de_distribucion(analisis: Any, n: int = 6) -> list[dict]:
    """Los campos que más cambiaron (mayor divergencia), con el valor que más se
    movió de cada uno. Ignora los campos sin referencia comparable."""
    filas = (analisis or {}).get("comparisonAnalysis") if isinstance(analisis, dict) else None
    fuera = []
    for f in sorted(filas or [], key=lambda f: -(f.get("divergence") or 0)):
        cambios = [c for c in f.get("topChanges") or [] if c.get("baselinePercentage") is not None]
        if not cambios or not f.get("divergence"):
            continue
        c = max(cambios, key=lambda c: abs((c.get("selectionPercentage") or 0) - (c.get("baselinePercentage") or 0)))
        fuera.append({"campo": f.get("field", ""), "valor": str(c.get("value", "")),
                      "intervalo": c.get("selectionPercentage"), "referencia": c.get("baselinePercentage"),
                      "divergencia": round(float(f["divergence"]), 2)})
        if len(fuera) >= n:
            break
    return fuera


def cambios_de_patrones(analisis: Any, n: int = 6) -> list[dict]:
    """Los valores que más SUBIERON en el intervalo respecto de la referencia
    (los nuevos, sin referencia, primero)."""
    filas = (analisis or {}).get("patternMapDifference") if isinstance(analisis, dict) else None
    subieron = [p for p in filas or [] if (p.get("selection") or 0) > (p.get("base") or 0)]

    def orden(p: dict) -> tuple:
        nuevo = not (p.get("base") or 0)     # sin referencia: el lift viene null
        return (0, -(p.get("selection") or 0)) if nuevo else (1, -(p.get("lift") or 0))

    subieron.sort(key=orden)
    return [{"valor": str(p.get("pattern", "")), "intervalo": p.get("selection"), "referencia": p.get("base")}
            for p in subieron[:n]]


def tabla_de_cambios(distribucion: list[dict], patrones: list[dict], campo_de_patrones: str = "") -> dict:
    """Para el gráfico y el detalle de la respuesta (mismo formato que una consulta)."""
    filas = [[p["valor"], campo_de_patrones, round((p["intervalo"] or 0) * 100), round((p["referencia"] or 0) * 100)]
             for p in patrones]
    filas += [[d["valor"], d["campo"], round((d["intervalo"] or 0) * 100), round((d["referencia"] or 0) * 100)]
              for d in distribucion]
    return {"schema": [{"name": "valor", "type": "string"}, {"name": "campo", "type": "string"},
                       {"name": "% en el intervalo", "type": "integer"}, {"name": "% antes", "type": "integer"}],
            "datarows": filas}


def prompt_de_explicacion(pregunta: str, contexto: str, v: dict[str, str],
                          distribucion: list[dict], patrones: list[dict], campo_de_patrones: str = "") -> str:
    lineas = [f"- {d['campo']} = {d['valor']}: {_pct(d['intervalo'])} del intervalo vs {_pct(d['referencia'])} antes"
              for d in distribucion]
    if patrones:
        lineas += [f"- {campo_de_patrones} = {p['valor']}: {_pct(p['intervalo'])} del intervalo vs "
                   f"{_pct(p['referencia'])} antes" for p in patrones]
    return (
        (f"Contexto: {contexto}\n" if contexto else "")
        + f"Pregunta del usuario: {pregunta}\n\n"
        f"Intervalo analizado: {v['selectionTimeRangeStart']} a {v['selectionTimeRangeEnd']} (UTC). "
        f"Referencia: {v['baselineTimeRangeStart']} a {v['baselineTimeRangeEnd']} (el período anterior).\n"
        "Lo que más cambió entre la referencia y el intervalo (medido sobre los documentos):\n"
        + "\n".join(lineas) + "\n\n"
        "Respondé en el MISMO idioma que el usuario: una frase con la conclusión (qué fue distinto en "
        "ese intervalo) y hasta tres viñetas con los cambios más importantes y sus porcentajes. Explicá "
        "con estos números por qué el intervalo se sale de lo normal; si sugieren una causa probable "
        "(por ejemplo, una campaña de logins fallidos o un pozo que dejó de parar), decila como "
        "probable. No digas que no se puede determinar. No muestres JSON ni nombres de herramientas."
    )


# ── "¿Qué alertas / anomalías hubo?" ────────────────────────────────────────
_ALERTAS = re.compile(r"\balertas?\b", re.I)
_ANOMALIAS = re.compile(r"\banomal[ií]as?\b", re.I)
# Que pida verlas, no que pregunte por una en particular ("¿por qué es una anomalía?").
_LISTADO = re.compile(r"\b(qu[eé]|cu[aá]l(es)?|cu[aá]nt[ao]s?|hubo|hay|se dispar|list|mostr|ver|[uú]ltim)", re.I)


def pide_listado(pregunta: str) -> str:
    """'alertas', 'anomalias' o '' según lo que pida la pregunta."""
    p = pregunta or ""
    if re.search(r"\bpor\s*qu[eé]\b", p, re.I) or not _LISTADO.search(p):
        return ""
    if _ALERTAS.search(p):
        return "alertas"
    if _ANOMALIAS.search(p):
        return "anomalias"
    return ""


_ALERTA = re.compile(r"Alert\((.*?)\)(?=Alert\(|\]|$)", re.S)


def alertas_del_texto(texto: str) -> list[dict]:
    """`SearchAlertsTool` devuelve `Alerts=[Alert(id=…, monitorName=…, …)…]` (texto,
    no JSON). Se rescatan los campos que le sirven a una persona."""
    fuera = []
    for m in _ALERTA.finditer(texto or ""):
        campos = dict(re.findall(r"(\w+)=([^,\]\)]*)", m.group(1)))
        fuera.append({"inicio": (campos.get("startTime") or "").replace("T", " ")[:19],
                      "estado": campos.get("state", ""), "severidad": campos.get("severity", ""),
                      "monitor": campos.get("monitorName", ""), "disparador": campos.get("triggerName", "")})
    return fuera


def tabla_de_alertas(alertas: list[dict]) -> dict:
    return {"schema": [{"name": n, "type": "string"} for n in ("inicio (UTC)", "estado", "severidad", "disparador")],
            "datarows": [[a["inicio"], a["estado"], a["severidad"], a["disparador"]] for a in alertas]}


def tabla_de_anomalias(anomalias: list[dict]) -> dict:
    return {"schema": [{"name": "inicio (UTC)", "type": "string"}, {"name": "fin (UTC)", "type": "string"},
                       {"name": "grado %", "type": "integer"}, {"name": "valores", "type": "string"}],
            "datarows": [[a["inicio"], a["fin"], round((a.get("grado") or 0) * 100),
                          " · ".join(f"{v['nombre']}: {v['valor']}" for v in a.get("valores") or [])]
                         for a in anomalias]}


def prompt_de_listado(pregunta: str, tipo: str, total: int, tabla: dict) -> str:
    filas = json.dumps(tabla["datarows"][:10], ensure_ascii=False)
    columnas = [c["name"] for c in tabla["schema"]]
    que = "alertas del caso (Alerting)" if tipo == "alertas" else "anomalías del detector del caso (Anomaly Detection)"
    return (
        f"Pregunta del usuario: {pregunta}\n"
        f"Hay {total} {que}. Las principales, columnas {columnas}: {filas}\n\n"
        "Respondé en el MISMO idioma que el usuario, en una o dos frases con los números (separador de "
        "miles) y, si suma, hasta tres viñetas con las más importantes (fecha y por qué se destacan). "
        "No muestres JSON."
    )
