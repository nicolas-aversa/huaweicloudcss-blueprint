"""Base de conocimiento del entorno: los documentos que sube el usuario
(manuales, políticas, runbooks…) y un pipeline RAG que responde con ellos.

Todo lo hace OpenSearch; la plataforma solo manda el archivo, como lo haría
cualquier aplicación (y como se puede hacer desde Dev Tools):

- pipeline de ingesta `conocimiento-ingesta`: `attachment` (ingest-attachment,
  Apache Tika) saca el texto de PDF, Word, PowerPoint, Excel, HTML, Markdown…;
  `text_chunking` (neural-search) lo parte en fragmentos; el binario no se guarda;
- índice `conocimiento-plataforma`: un documento por archivo, con sus
  fragmentos analizados en español e inglés (con el análisis estándar
  "reiniciar" no encontraba "reinician");
- pipeline de búsqueda `plataforma-rag`: `retrieval_augmented_generation` arma
  el contexto con los fragmentos de los documentos encontrados y el LLM de MaaS
  responde; `highlight` da el pasaje que coincidió.

Probado en CSS 3.4 (tiene `attachment` y `text_chunking`). El RAG le pasa al
modelo los fragmentos de los documentos encontrados: para documentos de
cientos de páginas lo nativo es buscar por fragmento con embeddings, y MaaS
hoy no tiene modelo de embeddings. Las citas que arma el modelo no son
confiables ("SEARCH RESULT 2"): las fuentes salen de los resultados.
"""
from __future__ import annotations

import re
from typing import Any

INDICE = "conocimiento-plataforma"
PIPELINE_DE_INGESTA = "conocimiento-ingesta"
PIPELINE = "plataforma-rag"
NOMBRE_DEL_MODELO = "platform-rag"
NOMBRE_DEL_CONNECTOR = "MaaS RAG (platform)"
MAX_BYTES = 10 * 1024 * 1024
TOKENS_POR_FRAGMENTO = 300
SOLAPE = 0.15
DOCUMENTOS_POR_RESPUESTA = 3
RELEVANCIA_MINIMA = 0.5   # de la puntuación del mejor resultado, para mostrarlo
SIN_TEXTO = "el documento no tiene texto"
# Lo que entiende `attachment` (Tika) y tiene sentido como documento.
EXTENSIONES = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".odt", ".rtf",
               ".txt", ".md", ".markdown", ".csv", ".json", ".log", ".html", ".htm")


def build_pipeline_de_ingesta() -> dict[str, Any]:
    """`PUT _ingest/pipeline/conocimiento-ingesta`."""
    return {
        "description": "Base de conocimiento: texto del archivo (attachment) partido en fragmentos (text_chunking)",
        "processors": [
            {"attachment": {"field": "archivo", "target_field": "adjunto", "indexed_chars": -1,
                            "properties": ["content", "content_type", "language", "content_length"]}},
            # Un PDF escaneado no trae texto: sin `content` el rename falla y el
            # error dice por qué.
            {"rename": {"field": "adjunto.content", "target_field": "texto"}},
            # Tika devuelve un par de blancos para un PDF sin texto (escaneado):
            # se rechaza en vez de indexar un documento vacío.
            {"fail": {"if": "ctx.texto == null || ctx.texto.trim().isEmpty()", "message": SIN_TEXTO}},
            {"text_chunking": {"field_map": {"texto": "fragmentos"}, "algorithm": {"fixed_token_length": {
                "token_limit": TOKENS_POR_FRAGMENTO, "overlap_rate": SOLAPE, "tokenizer": "standard"}}}},
            # Ni el binario ni el texto entero: quedan los fragmentos.
            {"remove": {"field": ["archivo", "texto"]}},
        ],
    }


def mapping() -> dict[str, Any]:
    """`PUT conocimiento-plataforma`."""
    analizado = {"type": "text", "fields": {"es": {"type": "text", "analyzer": "spanish"},
                                            "en": {"type": "text", "analyzer": "english"}}}
    return {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0, "index.default_pipeline": PIPELINE_DE_INGESTA},
        "mappings": {"properties": {
            "titulo": {"type": "text", "fields": {"k": {"type": "keyword", "ignore_above": 512}}},
            "fragmentos": analizado,
            "adjunto": {"properties": {"content_type": {"type": "keyword"}, "language": {"type": "keyword"},
                                       "content_length": {"type": "long"}}},
        }},
    }


def documento(titulo: str, contenido_b64: str) -> dict[str, Any]:
    """Lo que se indexa: el título y el archivo en base64 (el pipeline hace el resto)."""
    return {"titulo": titulo, "archivo": contenido_b64}


def validar(nombre: str, tamano: int) -> str:
    """El motivo si el archivo no se puede subir ('' si está bien)."""
    ext = ("." + nombre.rsplit(".", 1)[-1].lower()) if "." in nombre else ""
    if ext not in EXTENSIONES:
        return f"formato no soportado ({ext or 'sin extensión'})"
    if tamano > MAX_BYTES:
        return f"pesa más de {MAX_BYTES // (1024 * 1024)} MB"
    if not tamano:
        return "está vacío"
    return ""


def motivo_de_ingesta(texto_del_error: str) -> str:
    """El error de la ingesta, dicho para el usuario."""
    t = texto_del_error or ""
    if SIN_TEXTO in t or ("adjunto.content" in t and ("not present" in t or "doesn't exist" in t or "cannot be found" in t)):
        return "no tiene texto (¿es un PDF escaneado o una imagen?)"
    m = re.search(r'"reason"\s*:\s*"([^"]{1,200})', t)
    return m.group(1) if m else t[:200]


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
        "model_id": model_id, "context_field_list": ["titulo", "fragmentos"],
        "system_prompt": ("Respondés SOLO con la información de los documentos que te pasan. "
                          "Si la respuesta no está en ellos, decí que no está; nunca inventes."),
        "user_instructions": ("Respondé en el idioma de la pregunta, claro y breve. "
                              "No cites números de resultado: las fuentes se muestran aparte."),
    }}]}


_CAMPOS = ["fragmentos", "fragmentos.es", "fragmentos.en"]


def busqueda(pregunta: str, modelo: str, k: int = DOCUMENTOS_POR_RESPUESTA) -> dict[str, Any]:
    """`POST conocimiento-plataforma/_search?search_pipeline=plataforma-rag`.
    El procesador lee el contexto del `_source`: los fragmentos tienen que venir."""
    return {
        "size": k,
        "_source": ["titulo", "fragmentos", "adjunto.content_type"],
        "query": {"multi_match": {"query": pregunta, "type": "most_fields", "fields": _CAMPOS + ["titulo^2"]}},
        "highlight": {"pre_tags": [""], "post_tags": [""], "number_of_fragments": 1, "fragment_size": 280,
                      "fields": {c: {} for c in _CAMPOS}},
        "ext": {"generative_qa_parameters": {"llm_model": modelo, "llm_question": pregunta,
                                              "context_size": k, "timeout": 60}},
    }


def respuesta(resp: dict[str, Any]) -> dict[str, Any]:
    """La respuesta del modelo y los documentos consultados (los relevantes),
    cada uno con el pasaje que coincidió."""
    ext = (resp or {}).get("ext") or {}
    texto = ((ext.get("retrieval_augmented_generation") or {}).get("answer") or "").strip()
    hits = ((resp or {}).get("hits") or {}).get("hits") or []
    # Solo lo que pesa al menos la mitad del mejor resultado: con pocos
    # documentos la búsqueda trae todos, y "¿política de vacaciones?" mostraba
    # "Política de mantenimiento" solo por la palabra "política".
    mejor = max((h.get("_score") or 0 for h in hits), default=0)
    fuentes = []
    for h in hits:
        s = h.get("_source") or {}
        if not s.get("titulo") or (mejor and (h.get("_score") or 0) < mejor * RELEVANCIA_MINIMA):
            continue
        resaltado = h.get("highlight") or {}
        pasaje = next((resaltado[c][0] for c in _CAMPOS if resaltado.get(c)), None) \
            or ((s.get("fragmentos") or [""])[0])[:280]
        fuentes.append({"titulo": s["titulo"], "extracto": pasaje})
    return {"respuesta": texto, "fuentes": fuentes}


def listado(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Los documentos cargados, desde una búsqueda de todos."""
    fuera = []
    for h in ((resp or {}).get("hits") or {}).get("hits") or []:
        s = h.get("_source") or {}
        adj = s.get("adjunto") or {}
        fuera.append({"titulo": s.get("titulo", ""), "tipo": (adj.get("content_type") or "").split(";")[0],
                      "caracteres": adj.get("content_length")})
    return fuera


LISTAR = {"size": 500, "_source": ["titulo", "adjunto.content_type", "adjunto.content_length"],
          "sort": [{"titulo.k": "asc"}]}
