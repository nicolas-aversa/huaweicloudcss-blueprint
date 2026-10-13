"""Búsqueda híbrida (por palabras y por significado) sobre el texto libre de
un caso.

Por qué híbrida y no solo semántica: en los logs importan las coincidencias
exactas (una IP, un código, un número de cuenta) y la semántica sola las
pierde, porque busca "parecidos". La híbrida corre las dos y mezcla los
resultados: lo literal sale arriba y además encuentra lo que está dicho con
otras palabras ("cobro rechazado" → "pago denegado").

Cómo:

* Los vectores los calcula NVIDIA NIM (nemotron-3-embed-1b, multilingüe): el
  cluster se los pide por un conector de ml-commons, como el asistente le pide
  respuestas a MaaS. El CSS no deja desplegar modelos locales: el motor (DJL)
  baja PyTorch de internet y el nodo no lo puede guardar ("Failed to save
  pytorch index file"), sea TorchScript u ONNX; y MaaS (ap-southeast-1) no
  tiene embeddings. Los textos del campo viajan a NVIDIA: con datos de un
  cliente, se avisa.
* No se calcula un vector por evento (100.000 eventos en un nodo sin GPU son
  horas): un Transform deja una fila por texto DISTINTO, con cuántas veces
  aparece, en `busqueda-<caso>`; ese índice tiene el pipeline de embeddings
  por defecto, así cada texto nuevo se vectoriza al entrar. El índice se arma
  adentro del cluster: los textos no pasan por la plataforma. Solo "Probar una
  búsqueda" trae los resultados de esa prueba para mostrarlos (como el
  asistente), sin guardarlos.
* Solo en casos con un campo de texto libre (un mensaje, una descripción, un
  motivo): sobre códigos o categorías no aporta, y el plan lo dice.
"""
from __future__ import annotations

import re
from typing import Any

# Quién calcula los vectores. Multilingüe (castellano entre 34 idiomas),
# 2048 dimensiones, normalizados. La API es la de OpenAI.
PROVEEDOR = {
    "nombre": "NVIDIA NIM",
    "url": "https://integrate.api.nvidia.com/v1/embeddings",
    "modelo": "nvidia/nemotron-3-embed-1b",
    # Global Accelerator de AWS: IPs fijas, las que piden las rutas del CSS.
    "ips": ("75.2.113.119", "99.83.136.103"),
}
DESTINO_PERMITIDO = r"^https://integrate\.api\.nvidia\.com/.*$"
DIMENSION = 2048
# Textos por pedido: la API acepta hasta 256; el tier gratis, ~40 pedidos por minuto.
LOTE = 100
# Un modelo para los textos (al cargar) y otro para lo que se busca: el de
# NVIDIA rinde mejor si sabe cuál es cuál (`input_type`).
NOMBRES_DE_MODELO = {"passage": "plataforma-embeddings-textos", "query": "plataforma-embeddings-consultas"}
PIPELINE_DE_INGESTA = "plataforma-embeddings"
PIPELINE_DE_BUSQUEDA = "plataforma-hibrida"
# Lo que pesa cada parte en la mezcla: más el significado que la palabra
# exacta, pero lo literal sigue sumando.
PESOS = (0.3, 0.7)
# Cuántos textos distintos tiene que tener el campo: con menos es una
# categoría (no hace falta buscar), con más vectorizarlos tardaría demasiado
# (el tier gratis de NVIDIA: ~40 pedidos por minuto).
MIN_TEXTOS, MAX_TEXTOS = 5, 20000

# Nombres que dicen "texto libre", en orden de preferencia: un mensaje, una
# descripción, un nombre (de producto, de regla, un título) y recién después un
# motivo, que suele ser casi una categoría.
_PISTAS = (
    re.compile(r"(^|[._])(msg|message|mensaje|logdesc|log_message)([._]|$)", re.I),
    re.compile(r"(^|[._])(desc|description|descripcion|detail|detalle|summary|resumen|comment|comentario|nota|note)([._]|$)", re.I),
    re.compile(r"(^|[._])(rule\.name|attack|title|titulo|product_name|nombre_producto)([._]|$)", re.I),
    re.compile(r"(^|[._])(\w*reason|motivo|razon|causa|cause|diagnos\w*|sintoma\w*|symptom\w*)([._]|$)", re.I),
)
# Lo que no es texto aunque el nombre engañe: identificadores y códigos.
_NO_ES_TEXTO = re.compile(r"(^|[._])(id|ids|code|codigo|cod|uuid|hash|key|ref)([._]|$)|_id$|_code$", re.I)


def campos_de_texto_libre(fields: list[dict[str, Any]]) -> list[str]:
    """Los campos donde se podría buscar por significado, en orden: los de tipo
    `text` y después los de texto cuyo nombre dice mensaje, descripción, título
    o motivo. Al provisionar se queda con el primero que tiene frases (en
    FortiAnalyzer, `logdesc` es una categoría y `msg` no)."""
    textos = [f for f in fields or [] if (f.get("field_path") or "").strip()
              and (f.get("type") or "") in ("text", "string", "keyword")
              and not _NO_ES_TEXTO.search(f.get("field_path") or "")
              and f.get("role") not in ("entity_id", "timestamp")]
    orden = [f["field_path"].strip() for f in textos if f.get("type") == "text"]
    for pista in _PISTAS:
        orden += [f["field_path"].strip() for f in textos if pista.search(f["field_path"])]
    return list(dict.fromkeys(orden))


def campo_de_texto_libre(fields: list[dict[str, Any]]) -> str:
    """El primer candidato; '' si el caso no tiene texto libre."""
    candidatos = campos_de_texto_libre(fields)
    return candidatos[0] if candidatos else ""


def son_frases(valores: list[Any]) -> bool:
    """Si al menos la mitad de los textos tienen dos palabras o más. Los códigos
    ("sqli", "path_traversal", "Adobe.Flash.Execution") no: buscar por
    significado sobre ellos no aporta. Sin muestra, no se descarta."""
    valores = [str(v) for v in valores or [] if str(v).strip()]
    return not valores or 2 * sum(1 for v in valores if len(v.split()) >= 2) >= len(valores)


def campo_agrupable(campo: str, tipo: str) -> str:
    """El campo que se puede agrupar: un `text` se agrupa por su `.keyword`."""
    return f"{campo}.keyword" if tipo == "text" else campo


def tipo_del_campo(fields: list[dict[str, Any]], campo: str) -> str:
    return next((str(f.get("type") or "") for f in fields or [] if f.get("field_path") == campo), "keyword")


def indice(slug: str) -> str:
    return f"busqueda-{slug}"


def nombre_del_transform(slug: str) -> str:
    return f"{slug}-busqueda"


def conector(tipo: str, api_key: str) -> dict[str, Any]:
    """`POST _plugins/_ml/connectors/_create`: el pedido de vectores a NVIDIA,
    para los textos (`passage`) o para lo que se busca (`query`)."""
    return {
        "name": NOMBRES_DE_MODELO[tipo], "version": "1", "protocol": "http",
        "description": f"Embeddings de {PROVEEDOR['nombre']} ({tipo}) para la búsqueda híbrida (plataforma)",
        "parameters": {"model": PROVEEDOR["modelo"], "input_type": tipo},
        "credential": {"api_key": api_key},
        "actions": [{
            "action_type": "predict", "method": "POST", "url": PROVEEDOR["url"],
            "headers": {"Authorization": "Bearer ${credential.api_key}", "Content-Type": "application/json"},
            "request_body": ('{ "input": ${parameters.input}, "model": "${parameters.model}", '
                             '"input_type": "${parameters.input_type}", "encoding_format": "float" }'),
            "pre_process_function": "connector.pre_process.openai.embedding",
            "post_process_function": "connector.post_process.openai.embedding",
        }],
    }


def registro_del_modelo(tipo: str, connector_id: str) -> dict[str, Any]:
    """`POST _plugins/_ml/models/_register?deploy=true`: un modelo remoto (no
    se instala nada en el nodo)."""
    return {"name": NOMBRES_DE_MODELO[tipo], "function_name": "remote", "connector_id": connector_id,
            "description": f"{PROVEEDOR['modelo']} ({tipo}) para la búsqueda híbrida (plataforma)"}


def pipeline_de_ingesta(model_id: str) -> dict[str, Any]:
    """Vectoriza al entrar, de a `LOTE` textos por pedido a NVIDIA."""
    return {"description": "Vectoriza el texto de la búsqueda híbrida (plataforma)",
            "processors": [{"text_embedding": {"model_id": model_id, "field_map": {"texto": "embedding"},
                                               "batch_size": LOTE}}]}


def pipeline_de_busqueda() -> dict[str, Any]:
    """Mezcla los puntajes de las dos búsquedas: cada uno llevado a 0..1 y
    promediados con `PESOS`."""
    return {"description": "Búsqueda híbrida: por palabras y por significado (plataforma)",
            "phase_results_processors": [{"normalization-processor": {
                "normalization": {"technique": "min_max"},
                "combination": {"technique": "arithmetic_mean", "parameters": {"weights": list(PESOS)}}}}]}


def mapping_del_indice() -> dict[str, Any]:
    """Una fila por texto distinto: el texto (con análisis en castellano), cuántas
    veces aparece y su vector, que pone el pipeline por defecto al entrar."""
    return {
        "settings": {"index.knn": True, "index.default_pipeline": PIPELINE_DE_INGESTA,
                     "number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {"properties": {
            "texto": {"type": "text", "fields": {"es": {"type": "text", "analyzer": "spanish"},
                                                 "keyword": {"type": "keyword", "ignore_above": 1024}}},
            "cuenta": {"type": "long"},
            # Los vectores de NVIDIA vienen normalizados: el producto interno es el coseno.
            "embedding": {"type": "knn_vector", "dimension": DIMENSION,
                          "method": {"name": "hnsw", "engine": "faiss", "space_type": "innerproduct"}},
        }},
    }


def transform(slug: str, index_pattern: str, campo: str, tipo: str) -> dict[str, Any]:
    """El Transform que deja una fila por texto distinto en `busqueda-<caso>`,
    continuo: un texto nuevo en los logs entra (y se vectoriza) solo.

    Cada hora y no cada pocos minutos: en cada pasada reescribe (y el pipeline
    vuelve a vectorizar) cada texto que apareció de nuevo, y con ingesta en
    vivo eso es carga de CPU del nodo. Solo los eventos que tienen el campo:
    los demás dejaban una fila `texto: null` que no se puede vectorizar."""
    agrupable = campo_agrupable(campo, tipo)
    return {"transform": {
        "enabled": True, "continuous": True,
        "schedule": {"interval": {"period": 60, "unit": "Minutes", "start_time": 1}},
        "description": f"Textos distintos de {campo} para la búsqueda híbrida de {slug} (plataforma)",
        "source_index": index_pattern, "target_index": indice(slug),
        "data_selection_query": {"exists": {"field": agrupable}}, "page_size": 200,
        "groups": [{"terms": {"source_field": agrupable, "target_field": "texto"}}],
        "aggregations": {"cuenta": {"value_count": {"field": agrupable}}},
    }}


# Los textos de verdad (una fila sin texto no cuenta ni se vectoriza).
CON_TEXTO = {"exists": {"field": "texto"}}
SIN_VECTOR = {"bool": {"must": [CON_TEXTO], "must_not": [{"exists": {"field": "embedding"}}]}}


def consulta_lexica(q: str, n: int = 5) -> dict[str, Any]:
    return {"size": n, "_source": ["texto", "cuenta"],
            "query": {"multi_match": {"query": q, "fields": ["texto", "texto.es"]}}}


def consulta_hibrida(q: str, model_id: str, n: int = 5) -> dict[str, Any]:
    return {"size": n, "_source": ["texto", "cuenta"],
            "query": {"hybrid": {"queries": [
                {"multi_match": {"query": q, "fields": ["texto", "texto.es"]}},
                {"neural": {"embedding": {"query_text": q, "model_id": model_id, "k": max(n, 10)}}},
            ]}}}


def consultas_para_comparar(model_id: str) -> dict[str, dict[str, Any]]:
    """Las dos consultas para comparar en Search Relevance (Dashboards), lado a
    lado: por palabras y la híbrida. `%SearchText%` es lo que escribe quien
    prueba; la híbrida va con el pipeline `PIPELINE_DE_BUSQUEDA`."""
    palabras = {"multi_match": {"query": "%SearchText%", "fields": ["texto", "texto.es"]}}
    return {"palabras": {"query": palabras},
            "hibrida": {"query": {"hybrid": {"queries": [
                palabras, {"neural": {"embedding": {"query_text": "%SearchText%", "model_id": model_id, "k": 10}}}]}}}}


def nombres_de_configuracion(label: str) -> dict[str, str]:
    """Cómo se llaman en Search Relevance las dos búsquedas de un caso."""
    return {"palabras": f"{label} · por palabras", "hibrida": f"{label} · híbrida"}


def configuraciones_de_busqueda(slug: str, label: str, model_id: str) -> list[dict[str, Any]]:
    """`PUT _plugins/_search_relevance/search_configurations`: las dos búsquedas
    del caso, listas para elegir en Search Relevance → Single Query Comparison
    (Dashboards), sin pegar consultas. El `query` va como texto."""
    import json

    consultas = consultas_para_comparar(model_id)
    nombres = nombres_de_configuracion(label)
    return [
        {"name": nombres["palabras"], "index": indice(slug), "query": json.dumps(consultas["palabras"]),
         "description": f"Búsqueda por palabras sobre los textos de {label} (plataforma)"},
        {"name": nombres["hibrida"], "index": indice(slug), "query": json.dumps(consultas["hibrida"]),
         "searchPipeline": PIPELINE_DE_BUSQUEDA,
         "description": f"Búsqueda híbrida (palabras + significado, NVIDIA) sobre los textos de {label} (plataforma)"},
    ]


def resultados(cuerpo: dict[str, Any]) -> list[dict[str, Any]]:
    """`[{texto, cuenta, puntaje}]` de una respuesta de `_search`."""
    return [{"texto": (h.get("_source") or {}).get("texto", ""), "cuenta": (h.get("_source") or {}).get("cuenta"),
             "puntaje": h.get("_score")}
            for h in ((cuerpo or {}).get("hits") or {}).get("hits") or []]
