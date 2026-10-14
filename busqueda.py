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
import unicodedata
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
    re.compile(r"(^|[._])(desc|description|descripcion|detail|detalle|summary|resumen|comments?|comentarios?|notas?|notes?|"
               r"observaci[oó]n|observaciones|observations?)([._]|$)", re.I),
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
    # A igual pista, primero lo que no es una categoría (`dimension`, del
    # análisis): en logística, la observación del repartidor antes que el motivo.
    textos.sort(key=lambda f: f.get("dimension") is True)
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


def campo_del_transform(cuerpo: Any) -> str:
    """El campo que agrupa un Transform ya creado (`GET _plugins/_transform/<id>`)."""
    try:
        return str(cuerpo["transform"]["groups"][0]["terms"]["source_field"])
    except (KeyError, IndexError, TypeError):
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


PERIODO_DEL_TRANSFORM_MIN = 60


def transform(slug: str, index_pattern: str, campo: str, tipo: str, ahora_ms: "int | None" = None) -> dict[str, Any]:
    """El Transform que deja una fila por texto distinto en `busqueda-<caso>`,
    continuo: un texto nuevo en los logs entra (y se vectoriza) solo.

    Cada hora y no cada pocos minutos: en cada pasada reescribe (y el pipeline
    vuelve a vectorizar) cada texto que apareció de nuevo, y con ingesta en
    vivo eso es carga de CPU del nodo. Solo los eventos que tienen el campo:
    los demás dejaban una fila `texto: null` que no se puede vectorizar.

    La primera pasada, al minuto: el scheduler corre en `start_time + n·período`,
    y con `start_time: 1` eso era la próxima hora en punto — "Activar los
    plugins" encontraba el índice vacío (sin evaluación ni frases sugeridas)."""
    import time

    agrupable = campo_agrupable(campo, tipo)
    ahora_ms = int(time.time() * 1000) if ahora_ms is None else ahora_ms
    return {"transform": {
        "enabled": True, "continuous": True,
        "schedule": {"interval": {"period": PERIODO_DEL_TRANSFORM_MIN, "unit": "Minutes",
                                  "start_time": ahora_ms - (PERIODO_DEL_TRANSFORM_MIN - 1) * 60_000}},
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
    prueba; la híbrida va con el pipeline `PIPELINE_DE_BUSQUEDA`. Sin el vector
    en los resultados: son 2048 números por fila que nadie lee (y pesan)."""
    palabras = {"multi_match": {"query": "%SearchText%", "fields": ["texto", "texto.es"]}}
    campos = ["texto", "cuenta"]
    return {"palabras": {"query": palabras, "_source": campos},
            "hibrida": {"query": {"hybrid": {"queries": [
                palabras, {"neural": {"embedding": {"query_text": "%SearchText%", "model_id": model_id, "k": 10}}}]}},
                "_source": campos}}


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


# ── Qué escribir en la barra de Search ──────────────────────────────────────
# Para ver la diferencia hace falta una búsqueda que la híbrida resuelva y la
# búsqueda por palabras no. El LLM propone, desde los textos del caso,
# búsquedas dichas con otras palabras y el texto que cada una debería traer; la
# plataforma las corre en el cluster y sugiere solo las que lo prueban. Sale de
# los datos: anda con cualquier dataset, sin una lista a mano por caso.
MUESTRA_PARA_SUGERIR = 40
CANDIDATAS = 8
SUGERENCIAS = 3
PUESTO_MAXIMO = 5
_LARGO_DE_BUSQUEDA = (3, 80)
_LARGO_EN_EL_PROMPT = 200

_PROMPT_DE_BUSQUEDAS = """Estos son textos de un índice de búsqueda, uno por línea: {de_donde}. Quiero \
mostrar que la búsqueda por significado encuentra lo que la búsqueda por palabras no encuentra. Interpretá \
cada texto según de qué son los datos.

Proponé {n} búsquedas en castellano, como las escribiría una persona en un buscador (de 2 a 5 palabras). \
Cada una apunta a UNO de estos textos (uno distinto cada vez) y lo describe **sin usar sus palabras**: con \
sinónimos, contando la situación o el problema de otra forma, o en castellano si el texto está en otro \
idioma. Sin comillas, códigos, números ni nombres propios del texto.

Respondé solo JSON: {{"busquedas": [{{"busqueda": "...", "texto": "<el texto al que apunta, copiado tal cual>"}}]}}

Textos:
{textos}
"""


def muestra_para_sugerir() -> dict[str, Any]:
    """Los textos más frecuentes del índice: de ahí salen las búsquedas."""
    return {"size": MUESTRA_PARA_SUGERIR, "_source": ["texto", "cuenta"], "query": CON_TEXTO,
            "sort": [{"cuenta": "desc"}]}


def _en_una_linea(texto: Any) -> str:
    return " ".join(str(texto or "").split())[:_LARGO_EN_EL_PROMPT]


def prompt_de_busquedas(textos: list[str], campo: str = "", label: str = "") -> str:
    """El pedido al LLM, con de dónde son los textos: sin eso, "Health" en los
    logs de un firewall volvía como "bienestar físico"."""
    return _PROMPT_DE_BUSQUEDAS.format(n=CANDIDATAS, de_donde=_de_donde(campo, label),
                                       textos="\n".join(_en_una_linea(t) for t in textos))


def _de_donde(campo: str, label: str) -> str:
    return (f"los valores del campo «{campo}» de «{label}»" if campo and label
            else f"los valores del campo «{campo}»" if campo else "los textos de un caso")


def _la_lista(datos: Any, clave: str) -> "list | None":
    """La lista que pidió el prompt. El LLM a veces equivoca la clave (visto:
    `"buquedas"` por `"busquedas"`, y se perdían todas): la primera lista."""
    if not isinstance(datos, dict):
        return None
    if isinstance(datos.get(clave), list):
        return datos[clave]
    return next((v for v in datos.values() if isinstance(v, list)), None)


def candidatas(datos: Any, textos: list[str]) -> list[dict[str, str]]:
    """Las búsquedas que propuso el LLM, validadas: cada una apunta a uno de
    los textos (como quedó en el prompt) y a uno distinto."""
    por_linea = {_en_una_linea(t): t for t in textos}
    propuestas = _la_lista(datos, "busquedas")
    fuera: list[dict[str, str]] = []
    usados: set[str] = set()
    for x in propuestas if isinstance(propuestas, list) else []:
        if not isinstance(x, dict):
            continue
        q = " ".join(str(x.get("busqueda") or "").split())
        objetivo = por_linea.get(_en_una_linea(x.get("texto")))
        if (not objetivo or objetivo in usados or q.lower() == objetivo.lower()
                or not _LARGO_DE_BUSQUEDA[0] <= len(q) <= _LARGO_DE_BUSQUEDA[1]):
            continue
        usados.add(objetivo)
        fuera.append({"busqueda": q, "objetivo": objetivo})
        if len(fuera) >= CANDIDATAS:
            break
    return fuera


# Cuántos puestos más abajo tiene que quedar el texto por palabras para que
# la diferencia se vea. Con datos en castellano, la búsqueda por palabras casi
# siempre trae el texto entre sus diez (comparte "de", "con" y las raíces):
# pedir que no lo trajera dejaba un dataset entero sin sugerencias.
VENTAJA_MINIMA = 3


def probar(candidata: dict[str, str], por_palabras: list[dict[str, Any]],
           hibrida: list[dict[str, Any]]) -> "dict[str, Any] | None":
    """La sugerencia, si prueba la diferencia: la híbrida trae su texto entre
    los primeros y por palabras no aparece, o aparece bastante más abajo."""
    lexica = [r.get("texto") for r in por_palabras]
    primeros = [r.get("texto") for r in hibrida][:PUESTO_MAXIMO]
    objetivo = candidata["objetivo"]
    if objetivo not in primeros:
        return None
    puesto = primeros.index(objetivo) + 1
    puesto_por_palabras = lexica.index(objetivo) + 1 if objetivo in lexica else 0
    if puesto_por_palabras and puesto_por_palabras < puesto + VENTAJA_MINIMA:
        return None
    return {"busqueda": candidata["busqueda"], "encuentra": objetivo, "puesto": puesto,
            "por_palabras": len(lexica), "puesto_por_palabras": puesto_por_palabras}


def elegir(probadas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Las mejores: el texto más arriba en la híbrida, y por palabras ausente o
    lo más abajo posible."""
    return sorted(probadas, key=lambda p: (p["puesto"], -(p.get("puesto_por_palabras") or 99), p["por_palabras"]))[:SUGERENCIAS]


# ── Preguntas del asistente que muestran la búsqueda por significado ────────
# Una búsqueda que apunta a UN texto ("traslado para examen médico agendado" →
# «traslado para estudio programado») hace preguntas que parecen un filtro, como
# las de "Sobre los datos". Lo que la búsqueda por significado agrega es juntar
# un TEMA que en los datos está escrito de muchas formas («El producto llegó
# roto», «Caja golpeada en una esquina», «La caja llegó aplastada»…). El LLM
# propone temas con sus formas; la plataforma busca cada tema en el cluster como
# lo hace el agente y se queda con los que el significado encuentra y las
# palabras no.
MUESTRA_PARA_TEMAS = 120
TEMAS_CANDIDATOS = 8
PREGUNTAS_POR_SIGNIFICADO = 5
MIN_FORMAS = 3
# Cuántas frases trae la búsqueda del agente (el `k` de su VectorDBTool).
RESULTADOS_DEL_AGENTE = 25
_LARGO_DE_PREGUNTA = (15, 220)

_PROMPT_DE_TEMAS = """Estos son textos {de_donde}, uno por línea, con cuántas veces aparece cada uno entre \
paréntesis.

Quiero preguntas para un asistente de datos que muestren lo que agrega buscar por SIGNIFICADO: la persona \
pregunta por un tema con sus palabras, el asistente encuentra todas las formas en que ese tema está escrito en \
los datos y recién ahí cuenta. Una búsqueda por palabras encontraría una sola forma, o ninguna.

Proponé {n} temas. Cada tema:
- junta AL MENOS 3 textos de la lista que hablan de lo mismo pero están escritos con palabras distintas entre sí \
(sinónimos, término técnico y coloquial, otra forma de contar la misma situación). No valen variantes de la misma \
frase ("X" y "X, control").
- se pregunta como lo diría alguien del negocio que NO sabe cómo está escrito el dato: con palabras que no están \
en esos textos.

La pregunta: una duda real del negocio (un problema, un riesgo, un reclamo, algo que conviene vigilar) que se \
responde contando esos registros y repartiéndolos {cortes}. Una sola cosa a contar y un solo corte. Ejemplo, si \
los textos fueran de guardias médicas: «¿Cuántos pacientes llegaron con problemas para respirar y en qué zona \
pasa más?» junta «disnea súbita», «falta de aire al caminar», «broncoespasmo», «le cuesta respirar».

Respondé solo JSON: {{"temas": [{{"pregunta": "...", "busqueda": "<el tema en 2 a 5 palabras, sin palabras de \
los textos>", "textos": ["<copiado tal cual de la lista, sin la cuenta>", "...", "..."]}}]}}

Textos:
{textos}
"""


def muestra_para_temas() -> dict[str, Any]:
    """Los textos más frecuentes, con su cuenta: más que para las búsquedas de
    Search, porque un tema necesita varias formas de decir lo mismo."""
    return {"size": MUESTRA_PARA_TEMAS, "_source": ["texto", "cuenta"], "query": CON_TEXTO,
            "sort": [{"cuenta": "desc"}]}


def _plano(texto: Any) -> str:
    """Para comparar textos: sin mayúsculas, acentos ni espacios de más."""
    sin_acentos = unicodedata.normalize("NFKD", str(texto or "").lower()).encode("ascii", "ignore").decode()
    return " ".join(sin_acentos.split())


_CUENTA_AL_FINAL = re.compile(r"\s*\(\d+\)\s*$")


def _clave(texto: Any) -> str:
    """El texto como quedó en el prompt, sin la cuenta: el LLM a veces la
    copia ("La caja llegó aplastada (85)") y a veces no."""
    return _plano(_CUENTA_AL_FINAL.sub("", _en_una_linea(texto)))


_CORTES_EN_EL_PROMPT = 8


def cortes_de(fields: list[dict[str, Any]], campo: str = "") -> list[str]:
    """Por qué se puede repartir: las dimensiones del caso (su nombre de
    negocio si lo tiene), sin el campo de los textos. Sin la lista, las
    preguntas pedían "en qué barrios" o "en qué rango de edad" a datos que no
    los tienen."""
    fuera: list[str] = []
    for f in fields or []:
        ruta = (f.get("field_path") or "").strip()
        if not f.get("dimension") or not ruta or ruta == campo:
            continue
        nombre = (f.get("business_label") or "").strip() or ruta.rsplit(".", 1)[-1].replace("_", " ")
        if nombre.lower() not in (x.lower() for x in fuera):
            fuera.append(nombre)
    return fuera[:_CORTES_EN_EL_PROMPT]


def prompt_de_temas(filas: list[dict[str, Any]], campo: str = "", label: str = "",
                    cortes: "list[str] | None" = None) -> str:
    """`filas`: [{texto, cuenta}] del índice de búsqueda del caso."""
    de_donde = (f"del campo «{campo}» de «{label}»" if campo and label
                else f"del campo «{campo}»" if campo else "de un caso")
    return _PROMPT_DE_TEMAS.format(
        de_donde=de_donde, n=TEMAS_CANDIDATOS,
        cortes=(f"por uno de estos campos: {', '.join(cortes)}; o por fecha" if cortes else "en el tiempo"),
        textos="\n".join(f"{_en_una_linea(f.get('texto'))} ({int(f.get('cuenta') or 0)})" for f in filas))


def _variante_de(texto: str, otro: str) -> bool:
    """«litiasis vesicular, control» es la misma forma que «litiasis vesicular»."""
    return _plano(texto).startswith(_plano(otro) + ",")


def temas(datos: Any, filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Los temas que propuso el LLM, validados: cada forma es un texto de la
    lista, sin variantes de una misma frase, y al menos MIN_FORMAS."""
    por_clave = {_clave(f.get("texto")): f["texto"] for f in filas if f.get("texto")}
    fuera: list[dict[str, Any]] = []
    propuestos = _la_lista(datos, "temas")
    for t in propuestos if isinstance(propuestos, list) else []:
        if not isinstance(t, dict):
            continue
        pregunta = " ".join(str(t.get("pregunta") or "").split())
        q = " ".join(str(t.get("busqueda") or "").split())
        formas: list[str] = []
        for x in t.get("textos") if isinstance(t.get("textos"), list) else []:
            forma = por_clave.get(_clave(x))
            if forma and not any(forma == f or _variante_de(forma, f) or _variante_de(f, forma) for f in formas):
                formas.append(forma)
        if (len(formas) >= MIN_FORMAS and _LARGO_DE_BUSQUEDA[0] <= len(q) <= _LARGO_DE_BUSQUEDA[1]
                and _LARGO_DE_PREGUNTA[0] <= len(pregunta) <= _LARGO_DE_PREGUNTA[1]):
            fuera.append({"pregunta": pregunta, "busqueda": q, "formas": formas})
        if len(fuera) >= TEMAS_CANDIDATOS:
            break
    return fuera


def consulta_por_significado(q: str, model_id: str, n: int = RESULTADOS_DEL_AGENTE) -> dict[str, Any]:
    """La búsqueda del agente: solo por vector (su VectorDBTool), sin palabras."""
    return {"size": n, "_source": ["texto", "cuenta"],
            "query": {"neural": {"embedding": {"query_text": q, "model_id": model_id, "k": n}}}}


def _cuantas_trae(formas: list[str], encontrados: list[dict[str, Any]]) -> int:
    textos = [r.get("texto") or "" for r in encontrados]
    return sum(any(h == f or _variante_de(h, f) for h in textos) for f in formas)


def medir(tema: dict[str, Any], por_significado: list[dict[str, Any]],
          por_palabras: list[dict[str, Any]]) -> "dict[str, Any] | None":
    """El tema, con cuántas de sus formas trae cada búsqueda; None si no
    muestra la diferencia (el significado trae menos de MIN_FORMAS, o las
    palabras traen las mismas)."""
    n, m = _cuantas_trae(tema["formas"], por_significado), _cuantas_trae(tema["formas"], por_palabras)
    if n < MIN_FORMAS or n <= m:
        return None
    return {**tema, "por_significado": n, "por_palabras": m}


def elegir_preguntas(medidas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Las que más diferencia muestran: más formas que el significado trae y
    las palabras no; a igual diferencia, más formas."""
    return sorted(medidas, key=lambda t: (-(t["por_significado"] - t["por_palabras"]),
                                          -t["por_significado"]))[:PREGUNTAS_POR_SIGNIFICADO]


# ── La evaluación en Search Relevance ───────────────────────────────────────
# Single Query Comparison compara una búsqueda por vez. Para tener números, la
# plataforma deja armado en Search Relevance, por caso: un query set con
# búsquedas realistas, los juicios de un LLM de MaaS (el "juez") sobre lo que
# traen las dos configuraciones, y los experimentos ya corridos: Query Set
# Comparison (cuánto cambian los resultados) y un Search Evaluation de cada
# configuración (NDCG, precisión, MAP). Las búsquedas son mitad con palabras de
# los textos y mitad con otras: con solo las que fallan por palabras (las que
# se sugieren para Search), los números saldrían a favor de la híbrida de antemano.
CONSULTAS_A_EVALUAR = 10
RESULTADOS_A_JUZGAR = 10
NOMBRE_DEL_JUEZ = "plataforma-juez-de-relevancia"
_LARGO_DEL_NOMBRE = 50        # el tope de Search Relevance para un nombre
_LARGO_DE_LA_DESCRIPCION = 250

_PROMPT_DE_CONSULTAS = """Estos son textos de un índice de búsqueda, uno por línea: {de_donde}.

Escribí {n} búsquedas realistas, como las escribiría en un buscador alguien que busca entre estos textos \
(de 1 a 5 palabras). La mitad con palabras que aparecen en los textos (tal cual, aunque estén en otro \
idioma) y la otra mitad en castellano, diciendo lo que busca con otras palabras. Variadas: no todas sobre \
el mismo tema. Sin comillas.

Respondé solo JSON: {{"consultas": ["...", "..."]}}

Textos:
{textos}
"""


def prompt_de_consultas(textos: list[str], campo: str = "", label: str = "") -> str:
    return _PROMPT_DE_CONSULTAS.format(n=CONSULTAS_A_EVALUAR, de_donde=_de_donde(campo, label),
                                       textos="\n".join(_en_una_linea(t) for t in textos))


def consultas_a_evaluar(datos: Any) -> list[str]:
    """Las búsquedas que propuso el LLM para evaluar: texto, de largo
    razonable y sin repetir."""
    propuestas = _la_lista(datos, "consultas")
    fuera: list[str] = []
    vistas: set[str] = set()
    for x in propuestas if isinstance(propuestas, list) else []:
        q = " ".join(x.split()) if isinstance(x, str) else ""
        if not 2 <= len(q) <= _LARGO_DE_BUSQUEDA[1] or q.lower() in vistas:
            continue
        vistas.add(q.lower())
        fuera.append(q)
        if len(fuera) >= CONSULTAS_A_EVALUAR:
            break
    return fuera


def nombres_de_evaluacion(label: str) -> dict[str, str]:
    """Cómo se llaman en Search Relevance el query set y los juicios del caso."""
    return {"query_set": f"{label} · búsquedas"[:_LARGO_DEL_NOMBRE],
            "juicios": f"{label} · juicios del LLM"[:_LARGO_DEL_NOMBRE]}


def query_set(label: str, consultas: list[str]) -> dict[str, Any]:
    """`PUT _plugins/_search_relevance/query_sets` (manual)."""
    return {"name": nombres_de_evaluacion(label)["query_set"],
            "description": (f"Búsquedas realistas sobre los textos de {label}, para comparar la búsqueda "
                            "por palabras con la híbrida (plataforma)")[:_LARGO_DE_LA_DESCRIPCION],
            "sampling": "manual", "querySetQueries": [{"queryText": q} for q in consultas]}


def juicios(label: str, query_set_id: str, configuraciones: list[str], model_id: str) -> dict[str, Any]:
    """`PUT _plugins/_search_relevance/judgments` (LLM_JUDGMENT): el juez
    califica lo que traen las dos configuraciones, mirando solo el texto."""
    return {"name": nombres_de_evaluacion(label)["juicios"], "type": "LLM_JUDGMENT", "modelId": model_id,
            "querySetId": query_set_id, "searchConfigurationList": list(configuraciones),
            "size": RESULTADOS_A_JUZGAR, "contextFields": ["texto"]}


def experimentos(query_set_id: str, configuraciones: list[str], judgment_id: str) -> list[dict[str, Any]]:
    """Query Set Comparison de las dos configuraciones y un Search Evaluation
    de cada una (el plugin acepta una sola por evaluación)."""
    comunes = {"querySetId": query_set_id, "size": RESULTADOS_A_JUZGAR}
    return ([{**comunes, "searchConfigurationList": list(configuraciones), "type": "PAIRWISE_COMPARISON"}]
            + [{**comunes, "searchConfigurationList": [c], "judgmentList": [judgment_id],
                "type": "POINTWISE_EVALUATION"} for c in configuraciones])
