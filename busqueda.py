"""Búsqueda híbrida (por palabras y por significado) sobre el texto libre de
un caso.

Por qué híbrida y no solo semántica: en los logs importan las coincidencias
exactas (una IP, un código, un número de cuenta) y la semántica sola las
pierde, porque busca "parecidos". La híbrida corre las dos y mezcla los
resultados: lo literal sale arriba y además encuentra lo que está dicho con
otras palabras ("cobro rechazado" → "pago denegado").

Cómo, sin salida a internet y sin cargar el nodo:

* El modelo de embeddings (multilingüe, open source) no lo puede bajar el
  cluster: lo sube la plataforma una vez al bucket de demos y el cluster lo
  registra desde un link firmado de OBS (`url` en `_register`).
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

# El modelo: multilingüe (castellano e inglés en el mismo espacio), 384
# dimensiones, ~490 MB. Lo que pide `_register` con `url` sale de su config.json.
MODELO = {
    "nombre": "plataforma-embeddings-multilingue",
    "origen": ("https://artifacts.opensearch.org/models/ml-models/huggingface/sentence-transformers/"
               "paraphrase-multilingual-MiniLM-L12-v2/1.0.1/torch_script/"),
    "archivo": "sentence-transformers_paraphrase-multilingual-MiniLM-L12-v2-1.0.1-torch_script.zip",
    "version": "1.0.1",
    "bytes": 488135181,
    "hash": "a2ae3c4f161bd8e5a99a19ba5589443d33a120bb2bd67aa9da102c8b201f1277",
    "config": {"model_type": "bert", "embedding_dimension": 384, "framework_type": "sentence_transformers"},
}
DIMENSION = 384
CARPETA_EN_OBS = "css-demos/modelos"
PIPELINE_DE_INGESTA = "plataforma-embeddings"
PIPELINE_DE_BUSQUEDA = "plataforma-hibrida"
# Lo que pesa cada parte en la mezcla: más el significado que la palabra
# exacta, pero lo literal sigue sumando.
PESOS = (0.3, 0.7)
# Cuántos textos distintos tiene que tener el campo: con menos es una
# categoría (no hace falta buscar), con más el modelo tardaría demasiado en
# un nodo sin GPU.
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


def campo_de_texto_libre(fields: list[dict[str, Any]]) -> str:
    """El campo donde buscar por significado: el de tipo `text` si hay; si
    no, el de texto (string/keyword) cuyo nombre dice mensaje, descripción,
    motivo o título, en ese orden. '' si el caso no tiene texto libre."""
    textos = [f for f in fields or [] if (f.get("field_path") or "").strip()
              and (f.get("type") or "") in ("text", "string", "keyword")
              and not _NO_ES_TEXTO.search(f.get("field_path") or "")
              and f.get("role") not in ("entity_id", "timestamp")]
    for f in textos:
        if f.get("type") == "text":
            return f["field_path"].strip()
    for pista in _PISTAS:
        for f in textos:
            if pista.search(f["field_path"]):
                return f["field_path"].strip()
    return ""


def campo_agrupable(campo: str, tipo: str) -> str:
    """El campo que se puede agrupar: un `text` se agrupa por su `.keyword`."""
    return f"{campo}.keyword" if tipo == "text" else campo


def tipo_del_campo(fields: list[dict[str, Any]], campo: str) -> str:
    return next((str(f.get("type") or "") for f in fields or [] if f.get("field_path") == campo), "keyword")


def indice(slug: str) -> str:
    return f"busqueda-{slug}"


def nombre_del_transform(slug: str) -> str:
    return f"{slug}-busqueda"


def clave_en_obs() -> str:
    return f"{CARPETA_EN_OBS}/{MODELO['archivo']}"


def registro_del_modelo(url: str) -> dict[str, Any]:
    """`POST _plugins/_ml/models/_register` desde un link (el firmado de OBS)."""
    return {
        "name": MODELO["nombre"], "version": MODELO["version"],
        "description": "Embeddings multilingües para la búsqueda híbrida (plataforma)",
        "model_format": "TORCH_SCRIPT", "function_name": "TEXT_EMBEDDING",
        "model_content_hash_value": MODELO["hash"], "model_config": dict(MODELO["config"]),
        "url": url,
    }


def ajustes_del_cluster() -> dict[str, Any]:
    """Registrar un modelo desde un link está apagado por defecto."""
    return {"persistent": {"plugins.ml_commons.allow_registering_model_via_url": True,
                           "plugins.ml_commons.only_run_on_ml_node": False}}


def pipeline_de_ingesta(model_id: str) -> dict[str, Any]:
    return {"description": "Vectoriza el texto de la búsqueda híbrida (plataforma)",
            "processors": [{"text_embedding": {"model_id": model_id, "field_map": {"texto": "embedding"}}}]}


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
            "embedding": {"type": "knn_vector", "dimension": DIMENSION,
                          "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"}},
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


def resultados(cuerpo: dict[str, Any]) -> list[dict[str, Any]]:
    """`[{texto, cuenta, puntaje}]` de una respuesta de `_search`."""
    return [{"texto": (h.get("_source") or {}).get("texto", ""), "cuenta": (h.get("_source") or {}).get("cuenta"),
             "puntaje": h.get("_score")}
            for h in ((cuerpo or {}).get("hits") or {}).get("hits") or []]
