"""El chat de la plataforma conversando con el agente de OpenSearch (ml-commons):
la pregunta va al agente, que decide qué herramienta usar (un PPLTool por
fuente de datos), y la memoria de la conversación la guarda OpenSearch
(`memory_id`).

Lógica pura: armar la pregunta, leer la respuesta del `_execute` y las trazas
(`_plugins/_ml/memory/message/<id>/traces`), de donde salen las consultas PPL
que corrió con su resultado (para la tabla y el gráfico). Formatos medidos en
CSS 3.4:

- traza de un PPLTool: `{"ppl": "...", "executionResult": "<json con schema y
  datarows>"}`, o un texto "Failed to run the tool … with the error message …".
"""
from __future__ import annotations

import json
import re
from typing import Any

_FALLO = "Failed to run the tool"


def pregunta_para_el_agente(pregunta: str, fuente: str = "", indice: str = "", contexto: str = "") -> str:
    """La pregunta con lo que el usuario está mirando: el agente decide la
    herramienta, pero "¿cuántos hay?" sin fuente es ambiguo."""
    partes = [pregunta.strip()]
    if fuente:
        partes.append(f"(El usuario está mirando los datos de {fuente}" + (f", índice {indice}" if indice else "") +
                      "; si la pregunta no dice otra cosa, se refiere a esos datos.)")
    if contexto:
        partes.append(f"(Contexto: {contexto.strip()})")
    return "\n".join(partes)


def respuesta(resp: dict[str, Any]) -> dict[str, str]:
    """`{respuesta, memory_id, parent_interaction_id}` del `_execute`."""
    fuera = {"respuesta": "", "memory_id": "", "parent_interaction_id": ""}
    for o in ((resp or {}).get("inference_results") or [{}])[0].get("output") or []:
        nombre = o.get("name")
        if nombre == "response":
            fuera["respuesta"] = str((o.get("dataAsMap") or {}).get("response") or o.get("result") or "").strip()
        elif nombre in ("memory_id", "parent_interaction_id"):
            fuera[nombre] = str(o.get("result") or "")
    return fuera


def de_las_trazas(trazas: list[dict[str, Any]]) -> dict[str, Any]:
    """Las consultas PPL que corrió el agente, con su resultado o su error."""
    consultas: list[dict[str, Any]] = []
    for t in sorted(trazas or [], key=lambda x: int(x.get("trace_number") or 0)):
        origen = t.get("origin") or ""
        if origen.startswith("PPLTool"):
            consultas.append(_consulta(origen, t.get("input") or "", t.get("response") or ""))
    return {"consultas": consultas}


def _consulta(origen: str, entrada: str, salida: str) -> dict[str, Any]:
    pregunta = entrada
    try:
        pregunta = json.loads(entrada).get("question") or entrada
    except (ValueError, AttributeError):
        pass
    c: dict[str, Any] = {"herramienta": origen, "pregunta": pregunta, "ppl": "", "ok": False, "result": {}, "error": ""}
    if salida.startswith(_FALLO):
        c["error"] = salida.split("error message", 1)[-1].strip()[:400]
        return c
    try:
        datos = json.loads(salida)
        c["ppl"] = datos.get("ppl") or ""
        ejecucion = datos.get("executionResult")
        resultado = json.loads(ejecucion) if isinstance(ejecucion, str) else (ejecucion or {})
        c["result"] = {"schema": resultado.get("schema") or [], "datarows": resultado.get("datarows") or []}
        c["ok"] = True
    except (ValueError, AttributeError, TypeError):
        c["error"] = salida[:400]
    return c


# Lo que dice una consulta que no corrió porque el nodo estaba saturado (la
# cola de búsquedas llena: OpenSearch la rechaza y el PPLTool ve "all shards
# failed"). No es un error de la consulta: reintentada, anda.
_POR_CARGA = re.compile(r"all shards failed|rejected|es_rejected|too many requests|\b429\b|circuit.?break", re.I)


def fallo_por_carga(consultas: list[dict[str, Any]]) -> bool:
    """Ninguna consulta anduvo y alguna falló por carga del cluster."""
    return bool(consultas) and not any(c.get("ok") for c in consultas) \
        and any(_POR_CARGA.search(str(c.get("error") or "")) for c in consultas)


def ultima_buena(consultas: list[dict[str, Any]]) -> dict[str, Any] | None:
    """La última consulta que corrió bien: es la que respondió (las anteriores
    suelen ser intentos que el agente corrigió)."""
    return next((c for c in reversed(consultas or []) if c.get("ok")), None)
