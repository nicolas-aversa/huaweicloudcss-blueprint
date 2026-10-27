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


_NOMBRE_DE_ZONA = re.compile(r"^[A-Za-z0-9_/+\-]{1,60}$")


def _duracion(minutos: int) -> str:
    h, m = divmod(abs(minutos), 60)
    if not m:
        return "1 hora" if h == 1 else f"{h} horas"
    return f"{h} h {m} min" if h else f"{m} min"


def aviso_de_zona(offset_min: "int | None", zona: str = "") -> str:
    """Cómo pasar entre las fechas de los datos (`@timestamp` en UTC) y la
    hora del usuario. Sin esto el asistente contestaba "18:00 UTC" a quien
    mira el dashboard en Argentina, que muestra 15:00, y filtraba "el 15 de
    octubre" como día UTC. "" si el usuario está en UTC o no se sabe.
    `offset_min`: minutos que la hora local está por delante de UTC (-180 en
    Argentina)."""
    if not offset_min:
        return ""
    h, m = divmod(abs(offset_min), 60)
    utc = f"UTC{'+' if offset_min > 0 else '-'}{h:02d}:{m:02d}"
    nombre = f"{zona} ({utc})" if zona and _NOMBRE_DE_ZONA.match(zona) else utc
    a_local = ("sumá " if offset_min > 0 else "restá ") + _duracion(offset_min)
    a_utc = ("restá " if offset_min > 0 else "sumá ") + _duracion(offset_min)
    return (f"Las fechas de los datos están en UTC y el usuario está en {nombre}. Si nombra un día o una hora, "
            f"es en su zona: para filtrar, pasala a UTC ({a_utc}). En la respuesta, mostrá las horas en su zona "
            f"({a_local} a las de los datos) sin decir UTC; un día de un agrupamiento por día queda como está.")


def pregunta_para_el_agente(pregunta: str, fuente: str = "", indice: str = "", contexto: str = "",
                            zona: str = "") -> str:
    """La pregunta con lo que el usuario está mirando: el agente decide la
    herramienta, pero "¿cuántos hay?" sin fuente es ambiguo. `zona`: el
    `aviso_de_zona` del usuario."""
    partes = [pregunta.strip()]
    if fuente:
        partes.append(f"(El usuario está mirando los datos de {fuente}" + (f", índice {indice}" if indice else "") +
                      "; si la pregunta no dice otra cosa, se refiere a esos datos.)")
    if contexto:
        partes.append(f"(Contexto: {contexto.strip()})")
    if zona:
        partes.append(f"({zona})")
    return "\n".join(partes)


def respuesta(resp: dict[str, Any]) -> dict[str, str]:
    """`{respuesta, memory_id, parent_interaction_id}` del `_execute`."""
    fuera = {"respuesta": "", "memory_id": "", "parent_interaction_id": ""}
    for o in ((resp or {}).get("inference_results") or [{}])[0].get("output") or []:
        nombre = o.get("name")
        if nombre == "response":
            texto = str((o.get("dataAsMap") or {}).get("response") or o.get("result") or "").strip()
            # A veces el LLM del agente escribe los saltos de línea escapados
            # ("…:\\n\\n- **Último día**", visto en la ECS) y el chat los
            # mostraba tal cual: si no hay ningún salto real, son esos.
            if "\\n" in texto and "\n" not in texto:
                texto = texto.replace("\\n", "\n")
            fuera["respuesta"] = texto
        elif nombre in ("memory_id", "parent_interaction_id"):
            fuera[nombre] = str(o.get("result") or "")
    return fuera


def de_las_trazas(trazas: list[dict[str, Any]]) -> dict[str, Any]:
    """Las consultas PPL que corrió el agente, con su resultado o su error, y lo
    que buscó por significado (el tema y las frases que encontró)."""
    consultas: list[dict[str, Any]] = []
    busquedas: list[dict[str, Any]] = []
    for t in sorted(trazas or [], key=lambda x: int(x.get("trace_number") or 0)):
        origen = t.get("origin") or ""
        if origen.startswith("PPLTool"):
            consultas.append(_consulta(origen, t.get("input") or "", t.get("response") or ""))
        elif origen.startswith("BuscarTextos-"):
            busquedas.append(_busqueda(t.get("input") or "", t.get("response") or ""))
    return {"consultas": consultas, "busquedas": busquedas}


def _busqueda(entrada: str, salida: str) -> dict[str, Any]:
    """Una traza del VectorDBTool: la entrada es el tema (texto o JSON con
    `input`) y la salida, un documento JSON por línea con `_source`."""
    tema = entrada
    try:
        datos = json.loads(entrada)
        tema = (datos.get("input") or datos.get("question") or entrada) if isinstance(datos, dict) else entrada
    except ValueError:
        pass
    frases: list[dict[str, Any]] = []
    for linea in str(salida).splitlines():
        try:
            doc = json.loads(linea)
        except ValueError:
            continue
        fuente = (doc.get("_source") or {}) if isinstance(doc, dict) else {}
        if fuente.get("texto"):
            frases.append({"texto": str(fuente["texto"]), "cuenta": fuente.get("cuenta")})
    return {"tema": str(tema).strip()[:200], "frases": frases}


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
