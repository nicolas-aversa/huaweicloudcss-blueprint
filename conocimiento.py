"""Base de conocimiento del entorno: los documentos que sube el usuario
(manuales, políticas, runbooks…), partidos en fragmentos e indexados en el
cluster, y un pipeline de búsqueda RAG que responde con ellos.

Lógica pura (sin red): extraer el texto, partirlo, el mapping del índice, el
connector y el pipeline, y armar la búsqueda y la respuesta. main.py hace los
pedidos al cluster. Probado en CSS 3.4 con MaaS:

- el procesador `retrieval_augmented_generation` lee el contexto del `_source`:
  los campos de `context_field_list` tienen que venir en la búsqueda;
- con el análisis estándar "reiniciar" no encuentra "reinician": el texto se
  indexa además con análisis de español y de inglés (los documentos pueden
  venir en cualquiera de los dos) y se busca en los tres;
- las citas que arma el modelo no son confiables ("SEARCH RESULT 2"): las
  fuentes las dice la plataforma, con los títulos de lo que devolvió la búsqueda.

Los documentos no se guardan en la plataforma: van directo al cluster.
"""
from __future__ import annotations

import html
import io
import re
from typing import Any

INDICE = "conocimiento-plataforma"
PIPELINE = "plataforma-rag"
NOMBRE_DEL_MODELO = "platform-rag"
NOMBRE_DEL_CONNECTOR = "MaaS RAG (platform)"
MAX_BYTES = 10 * 1024 * 1024
FRAGMENTO = 1200          # caracteres por fragmento
SOLAPE = 150              # se repite entre fragmentos para no cortar una idea
FRAGMENTOS_POR_RESPUESTA = 5
RELEVANCIA_MINIMA = 0.5   # de la puntuación del mejor resultado, para mostrarlo
EXTENSIONES = (".txt", ".md", ".markdown", ".csv", ".json", ".log", ".html", ".htm", ".pdf")


class DocumentoInvalido(ValueError):
    """El archivo no se puede usar (formato, tamaño o sin texto)."""


def extraer_texto(nombre: str, contenido: bytes) -> str:
    """El texto del documento, según su extensión."""
    if len(contenido) > MAX_BYTES:
        raise DocumentoInvalido(f"pesa más de {MAX_BYTES // (1024 * 1024)} MB")
    ext = ("." + nombre.rsplit(".", 1)[-1].lower()) if "." in nombre else ""
    if ext not in EXTENSIONES:
        raise DocumentoInvalido(f"formato no soportado ({ext or 'sin extensión'}): "
                                f"se aceptan {', '.join(EXTENSIONES)}")
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            paginas = [p.extract_text() or "" for p in PdfReader(io.BytesIO(contenido)).pages]
        except Exception as exc:  # noqa: BLE001 — un PDF roto o cifrado
            raise DocumentoInvalido(f"no se pudo leer el PDF: {exc}") from exc
        texto = "\n\n".join(paginas)
    else:
        texto = _decodificar(contenido)
        if ext in (".html", ".htm"):
            texto = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", texto)
            texto = html.unescape(re.sub(r"(?s)<[^>]+>", " ", texto))
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n\s*\n\s*", "\n\n", texto).strip()
    if not texto:
        raise DocumentoInvalido("no tiene texto (¿es un PDF escaneado?)")
    return texto


def _decodificar(contenido: bytes) -> str:
    for cod in ("utf-8-sig", "latin-1"):
        try:
            return contenido.decode(cod)
        except UnicodeDecodeError:
            continue
    return contenido.decode("utf-8", errors="replace")


def fragmentar(texto: str, tam: int = FRAGMENTO, solape: int = SOLAPE) -> list[str]:
    """Fragmentos de hasta `tam` caracteres, cortando en párrafos (o, si un
    párrafo es más largo, en oraciones), con `solape` del anterior."""
    unidades: list[str] = []
    for parrafo in texto.split("\n\n"):
        parrafo = parrafo.strip()
        if not parrafo:
            continue
        if len(parrafo) <= tam:
            unidades.append(parrafo)
            continue
        for oracion in re.split(r"(?<=[.!?])\s+", parrafo):
            while len(oracion) > tam:
                unidades.append(oracion[:tam])
                oracion = oracion[tam:]
            if oracion:
                unidades.append(oracion)
    fragmentos: list[str] = []
    actual = ""
    for u in unidades:
        if actual and len(actual) + 2 + len(u) > tam:
            fragmentos.append(actual)
            actual = actual[-solape:] + "\n\n" + u if solape else u
            if len(actual) > tam:
                actual = u
        else:
            actual = f"{actual}\n\n{u}" if actual else u
    if actual:
        fragmentos.append(actual)
    return fragmentos


def documentos_para_indexar(titulo: str, texto: str) -> list[dict[str, Any]]:
    """Un documento de OpenSearch por fragmento."""
    partes = fragmentar(texto)
    return [{"titulo": titulo, "fragmento": i + 1, "fragmentos": len(partes), "texto": p}
            for i, p in enumerate(partes)]


def mapping() -> dict[str, Any]:
    """`PUT conocimiento-plataforma`: el texto con análisis estándar, de español
    y de inglés; el título también como keyword (para listar y borrar)."""
    return {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {"properties": {
            "titulo": {"type": "text", "fields": {"k": {"type": "keyword", "ignore_above": 512}}},
            "fragmento": {"type": "integer"},
            "fragmentos": {"type": "integer"},
            "texto": {"type": "text", "fields": {"es": {"type": "text", "analyzer": "spanish"},
                                                 "en": {"type": "text", "analyzer": "english"}}},
        }},
    }


def build_connector(api_key: str, endpoint: str, model: str) -> dict[str, Any]:
    """Connector del LLM del RAG: el procesador le pasa los mensajes armados
    (contexto + pregunta) en `${parameters.messages}`."""
    from capabilities import _CLIENT_CONFIG, _razona, _sanitize_desc
    return {
        "name": NOMBRE_DEL_CONNECTOR,
        "description": _sanitize_desc("LLM MaaS para responder con los documentos del entorno (RAG)"),
        "version": "1.0",
        "protocol": "http",
        "parameters": {"endpoint": endpoint, "model": model},
        "credential": {"maas_key": api_key},
        "actions": [{
            "action_type": "PREDICT", "method": "POST",
            "url": "https://${parameters.endpoint}/openai/v1/chat/completions",
            "headers": {"Authorization": "Bearer ${credential.maas_key}", "Content-Type": "application/json"},
            "request_body": ('{ "model": "${parameters.model}", "messages": ${parameters.messages}, '
                             '"temperature": 0, "chat_template_kwargs": {"thinking": ' + _razona(model) + '} }'),
        }],
        "client_config": _CLIENT_CONFIG,
    }


def build_pipeline(model_id: str) -> dict[str, Any]:
    """`PUT _search/pipeline/plataforma-rag`."""
    return {"response_processors": [{"retrieval_augmented_generation": {
        "tag": "plataforma", "description": "Responde con los documentos del entorno",
        "model_id": model_id, "context_field_list": ["titulo", "texto"],
        "system_prompt": ("Respondés SOLO con la información de los documentos que te pasan. "
                          "Si la respuesta no está en ellos, decí que no está; nunca inventes."),
        "user_instructions": ("Respondé en el idioma de la pregunta, claro y breve. "
                              "No cites números de resultado: las fuentes se muestran aparte."),
    }}]}


def busqueda(pregunta: str, modelo: str, k: int = FRAGMENTOS_POR_RESPUESTA) -> dict[str, Any]:
    """El cuerpo de `POST conocimiento-plataforma/_search?search_pipeline=…`."""
    return {
        "size": k,
        "_source": ["titulo", "fragmento", "fragmentos", "texto"],
        "query": {"multi_match": {"query": pregunta, "type": "most_fields",
                                  "fields": ["texto", "texto.es", "texto.en", "titulo^2", "titulo.k"]}},
        "ext": {"generative_qa_parameters": {"llm_model": modelo, "llm_question": pregunta,
                                              "context_size": k, "timeout": 60}},
    }


def respuesta(resp: dict[str, Any]) -> dict[str, Any]:
    """La respuesta del modelo y las fuentes reales (título y fragmento de cada
    resultado, sin repetir), desde la respuesta de la búsqueda."""
    ext = (resp or {}).get("ext") or {}
    texto = ((ext.get("retrieval_augmented_generation") or {}).get("answer") or "").strip()
    fuentes: list[dict[str, Any]] = []
    vistos = set()
    hits = ((resp or {}).get("hits") or {}).get("hits") or []
    # Solo lo que pesa al menos la mitad del mejor resultado: con pocos
    # documentos la búsqueda trae todos, y "¿política de vacaciones?" mostraba
    # "Política de mantenimiento" solo por la palabra "política".
    mejor = max((h.get("_score") or 0 for h in hits), default=0)
    for h in hits:
        s = h.get("_source") or {}
        clave = (s.get("titulo"), s.get("fragmento"))
        if clave in vistos or not s.get("titulo") or (mejor and (h.get("_score") or 0) < mejor * RELEVANCIA_MINIMA):
            continue
        vistos.add(clave)
        fuentes.append({"titulo": s["titulo"], "fragmento": s.get("fragmento"), "fragmentos": s.get("fragmentos"),
                        "extracto": (s.get("texto") or "")[:280]})
    return {"respuesta": texto, "fuentes": fuentes}


def listado(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Los documentos cargados (título y cuántos fragmentos), desde una agregación."""
    buckets = (((resp or {}).get("aggregations") or {}).get("docs") or {}).get("buckets") or []
    return [{"titulo": b["key"], "fragmentos": b["doc_count"]} for b in buckets]
