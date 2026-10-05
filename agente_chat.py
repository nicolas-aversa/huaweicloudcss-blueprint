"""El chat de la plataforma conversando con el agente de OpenSearch (ml-commons):
la pregunta va al agente, que decide qué herramientas usar (PPLTool por
fuente de datos, SearchIndexTool sobre la base de conocimiento), y la memoria de
la conversación la guarda OpenSearch (`memory_id`).

Lógica pura: armar la pregunta, leer la respuesta del `_execute` y las trazas
(`_plugins/_ml/memory/message/<id>/traces`), de donde salen las consultas PPL
que corrió con su resultado (para la tabla y el gráfico) y los documentos que
consultó. Formatos medidos en CSS 3.4:

- traza de un PPLTool: `{"ppl": "...", "executionResult": "<json con schema y
  datarows>"}`, o un texto "Failed to run the tool … with the error message …";
- traza de SearchIndexTool: un hit JSON por línea (`_source` con titulo y
  fragmentos).
"""
from __future__ import annotations

import json
from typing import Any

HERRAMIENTA_DE_DOCUMENTOS = "Documentos"
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
    """Las consultas PPL (con resultado o error) y los documentos consultados."""
    consultas: list[dict[str, Any]] = []
    documentos: list[dict[str, Any]] = []
    vistos: set = set()
    for t in sorted(trazas or [], key=lambda x: int(x.get("trace_number") or 0)):
        origen = t.get("origin") or ""
        salida = t.get("response") or ""
        if origen.startswith("PPLTool"):
            consultas.append(_consulta(origen, t.get("input") or "", salida))
        elif origen == HERRAMIENTA_DE_DOCUMENTOS:
            for linea in salida.splitlines():
                try:
                    hit = json.loads(linea)
                except ValueError:
                    continue
                s = hit.get("_source") or {}
                if s.get("titulo") and s["titulo"] not in vistos:
                    vistos.add(s["titulo"])
                    documentos.append({"titulo": s["titulo"], "extracto": " ".join(s.get("fragmentos") or [])[:280]})
    return {"consultas": consultas, "fuentes": documentos}


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


def ultima_buena(consultas: list[dict[str, Any]]) -> dict[str, Any] | None:
    """La última consulta que corrió bien: es la que respondió (las anteriores
    suelen ser intentos que el agente corrigió)."""
    return next((c for c in reversed(consultas or []) if c.get("ok")), None)
