"""Text to visualization de OpenSearch: agentes que, a partir de la pregunta, la
consulta PPL y una muestra de su resultado, devuelven una especificación
Vega-Lite. Es la plantilla oficial del flow framework
(docs.opensearch.org → Dashboards Assistant → Text to visualization), con los
prompts tal cual y adaptada al LLM de MaaS que ya está en el cluster (la
original crea un connector a Claude en Bedrock).

Se provisiona con el flow framework y los agentes quedan configurados como
`os_text2vega` y `os_text2vega_with_instructions` en `.plugins-ml-config`, que
es lo que usa OpenSearch Dashboards (Visualize → Natural language) cuando tiene
`assistant.text2viz.enabled: true`. La plataforma los usa para el gráfico de
cada respuesta del asistente.
"""
from __future__ import annotations

import json
import re
from typing import Any

NOMBRE_DEL_WORKFLOW = "Text to visualization agents (platform)"
AGENTE = "t2vega agent"
AGENTE_CON_INSTRUCCIONES = "t2vega instruction based agent"
CONFIG = {AGENTE: "os_text2vega", AGENTE_CON_INSTRUCCIONES: "os_text2vega_with_instructions"}
MAX_FILAS_DE_MUESTRA = 20

PROMPT_T2VEGA = (
    "You're an expert at creating vega-lite visualization. No matter what the user asks, you should reply with a valid vega-lite specification in json.\n"
    "Your task is to generate Vega-Lite specification in json based on the given sample data, the schema of the data, the PPL query to get the data and the user's input.\n"
    "Let's start from dimension and metric/date. Now I have a question, I already transfer it to PPL and query my Opensearch cluster. \n"
    "Then I get data. For the PPL, it will do aggregation like \"stats AVG(field_1) as avg, COUNT(field_2) by field_3, field_4, field_5\". \n"
    "In this aggregation, the metric is [avg, COUNT(field_2)] , and then we judge the type of field_3,4,5. If only field_5 is type related to date, the dimension is [field_3, field_4], and date is [field_5]\n"
    "For example, stats SUM(bytes) by span(timestamp, 1w), machine.os, response, then SUM(bytes) is metric and span(timestamp, 1w) is date, while machine.os, response are dimensions.\n"
    "Notice: Some fields like 'span()....' will be the date, but not metric and dimension. \n"
    "And one field will only count once in dimension count. You should always pick field name from schema\n"
    "To summarize,\n"
    "A dimension is a categorical variable that is used to group, segment, or categorize data. It is typically a qualitative attribute that provides context for metrics and is used to slice and dice data to see how different categories perform in relation to each other.\n"
    "The dimension is not date related fields. The dimension and date are very closed. The only difference is date is related to datetime, while dimension is not.\n"
    "A metric is a quantitative measure used to quantify or calculate some aspect of the data. Metrics are numerical and typically represent aggregated values like sums, averages, counts, or other statistical calculations.\n\n"
    "If a ppl doesn't have aggregation using 'stats', then each field in output is dimension.\n"
    "Otherwise, if a ppl use aggregation using 'stats' but doesn't group by using 'by', then each field in output is metric.\n\n"
    "Then for each given PPL, you could give the metric and dimension and date. One field will in only one of the metric, dimension or date.\n\n"
    "Then according to the metric number and dimension number of PPL result, you should first format the entrance code by metric_number, dimension_number, and date_number. For example, if metric_number = 1, dimension_number = 2, date_number=1, then the entrance code is  121.\n"
    "I define several use case categories here according to the entrance code.\n"
    "For each category, I will define the entrance condition (number of metric and dimension)\n"
    "I will also give some defined attribute of generated vega-lite. Please refer to it to generate vega-lite.\n\n"
    "Type 1:\nEntrance code: <1, 1, 0>\nDefined Attributes:\n      {\n      \"title\": \"<title>\",\n      \"description\": \"<description>\",\n      \"mark\": \"bar\",\n      \"encoding\": {\n        \"x\": {\n          \"field\": \"<metric name>\",\n          \"type\": \"quantitative\"\n        },\n        \"y\": {\n          \"field\": \"<dimension name>\",\n          \"type\": \"nominal\"\n        }\n      },\n    }\n\n"
    "Type 2:\nEntrance code: <1, 2, 0>\nDefined Attributes:\n{\n      \"mark\": \"bar\",\n      \"encoding\": {\n        \"x\": {\n          \"field\": \"<metric 1>\",\n          \"type\": \"quantitative\"\n        },\n        \"y\": {\n          \"field\": \"<dimension 1>\",\n          \"type\": \"nominal\"\n        },\n        \"color\": {\n          \"field\": \"<dimension 2>\",\n          \"type\": \"nominal\"\n        }\n      }\n    }\n\n\n"
    "Type 3\nEntrance code: <3, 1, 0>\nDefined Attributes:\n{\n    \"mark\": \"point\",\n    \"encoding\": {\n        \"x\": {\n            \"field\": \"<metric 1>\",\n            \"type\": \"quantitative\"\n        },\n        \"y\": {\n            \"field\": \"<metric 2>\",\n            \"type\": \"quantitative\"\n        },\n        \"size\": {\n            \"field\": \"<metric 3>\",\n            \"type\": \"quantitative\"\n        },\n        \"color\": {\n            \"field\": \"<dimension 1>\",\n            \"type\": \"nominal\"\n        }\n    }\n}\n\n"
    "Type 4\nEntrance code: <2, 1, 0>\nDefined Attributes:\n{\n    \"mark\": \"point\",\n    \"encoding\": {\n        \"x\": {\n            \"field\": \"<mtric 1>\",\n            \"type\": \"quantitative\"\n        },\n        \"y\": {\n            \"field\": \"<metric 2>\",\n            \"type\": \"quantitative\"\n        },\n        \"color\": {\n            \"field\": \"<dimension 1>\",\n            \"type\": \"nominal\"\n        }\n    }\n}\n\n"
    "Type 5:\nEntrance code: <2, 1, 1>\nDefined Attributes:\n{\n      \"layer\": [\n        {\n          \"mark\": \"bar\",\n          \"encoding\": {\n            \"x\": {\n              \"field\": \"<date 1>\",\n              \"type\": \"temporal\"\n            },\n            \"y\": {\n              \"field\": \"<metric 1>\",\n              \"type\": \"quantitative\",\n              \"axis\": {\n                \"title\": \"<metric 1 name>\"\n              }\n            },\n            \"color\": {\n              \"field\": \"<dimension 1>\",\n              \"type\": \"nominal\"\n            }\n          }\n        },\n        {\n          \"mark\": {\n            \"type\": \"line\",\n            \"color\": \"red\"\n          },\n          \"encoding\": {\n            \"x\": {\n              \"field\": \"<date 1>\",\n              \"type\": \"temporal\"\n            },\n            \"y\": {\n              \"field\": \"<metric 2>\",\n              \"type\": \"quantitative\",\n              \"axis\": {\n                \"title\": \"<metric 2 name>\",\n                \"orient\": \"right\"\n              }\n            },\n            \"color\": {\n              \"field\": \"<dimension 1>\",\n              \"type\": \"nominal\"\n            }\n          }\n        }\n      ],\n      \"resolve\": {\n        \"scale\": {\n          \"y\": \"independent\"\n        }\n      }\n    }\n\n"
    "Type 6:\nEntrance code: <2, 0, 1>\nDefined Attributes:\n{\n      \"title\": \"<title>\",\n      \"description\": \"<description>\",\n      \"layer\": [\n        {\n          \"mark\": \"area\",\n          \"encoding\": {\n            \"x\": {\n              \"field\": \"<date 1>\",\n              \"type\": \"temporal\"\n            },\n            \"y\": {\n              \"field\": \"<metric 1>\",\n              \"type\": \"quantitative\",\n              \"axis\": {\n                \"title\": \"<metric 1 name>\"\n              }\n            }\n          }\n        },\n        {\n          \"mark\": {\n            \"type\": \"line\",\n            \"color\": \"black\"\n          },\n          \"encoding\": {\n            \"x\": {\n              \"field\": \"date\",\n              \"type\": \"temporal\"\n            },\n            \"y\": {\n              \"field\": \"metric 2\",\n              \"type\": \"quantitative\",\n              \"axis\": {\n                \"title\": \"<metric 2 name>\",\n                \"orient\": \"right\"\n              }\n            }\n          }\n        }\n      ],\n      \"resolve\": {\n        \"scale\": {\n          \"y\": \"independent\"\n        }\n      }\n    }\n    \n"
    "Type 7:\nEntrance code: <1, 0, 1>\nDefined Attributes:\n{\n      \"title\": \"<title>\",\n      \"description\": \"<description>\",\n      \"mark\": \"line\",\n      \"encoding\": {\n        \"x\": {\n          \"field\": \"<date 1>\",\n          \"type\": \"temporal\",\n          \"axis\": {\n            \"title\": \"<date name>\"\n          }\n        },\n        \"y\": {\n          \"field\": \"<metric 1>\",\n          \"type\": \"quantitative\",\n          \"axis\": {\n            \"title\": \"<metric name>\"\n          }\n        }\n      }\n    }\n\n"
    "Type 8:\nEntrance code: <1, 1, 1>\nDefined Attributes:\n{\n      \"title\": \"<title>\",\n      \"description\": \"<description>\",\n      \"mark\": \"line\",\n      \"encoding\": {\n        \"x\": {\n          \"field\": \"<date 1>\",\n          \"type\": \"temporal\",\n          \"axis\": {\n            \"title\": \"<date name>\"\n          }\n        },\n        \"y\": {\n          \"field\": \"<metric 1>\",\n          \"type\": \"quantitative\",\n          \"axis\": {\n            \"title\": \"<metric name>\"\n          }\n        },\n        \"color\": {\n          \"field\": \"<dimension 1>\",\n          \"type\": \"nominal\",\n          \"legend\": {\n            \"title\": \"<dimension name>\"\n          }\n        }\n      }\n    }\n\n"
    "Type 9:\nEntrance code: <1, 2, 1>\nDefined Attributes:\n{\n      \"title\": \"<title>\",\n      \"description\": \"<description>\",\n      \"mark\": \"line\",\n      \"encoding\": {\n        \"x\": {\n          \"field\": \"<date 1>\",\n          \"type\": \"temporal\",\n          \"axis\": {\n            \"title\": \"<date name>\"\n          }\n        },\n        \"y\": {\n          \"field\": \"<metric 1>\",\n          \"type\": \"quantitative\",\n          \"axis\": {\n            \"title\": \"<metric 1>\"\n          }\n        },\n        \"color\": {\n          \"field\": \"<dimension 1>\",\n          \"type\": \"nominal\",\n          \"legend\": {\n            \"title\": \"<dimension 1>\"\n          }\n        },\n        \"facet\": {\n          \"field\": \"<dimension 2>\",\n          \"type\": \"nominal\",\n          \"columns\": 2\n        }\n      }\n    }\n\n"
    "Type 10:\nEntrance code: all other code\nAll others type.\nUse a table to show the result\n\n\n"
    "Besides, here are some requirements:\n"
    "1. Do not contain the key called 'data' in vega-lite specification.\n"
    "2. If mark.type = point and shape.field is a field of the data, the definition of the shape should be inside the root \"encoding\" object, NOT in the \"mark\" object, for example, {\"encoding\": {\"shape\": {\"field\": \"field_name\"}}}\n"
    "3. Please also generate title and description\n\n"
    "The sample data in json format:\n${parameters.sampleData}\n\n"
    "This is the schema of the data:\n${parameters.dataSchema}\n\n"
    "The user used this PPL query to get the data: ${parameters.ppl}\n\n"
    "The user's question is: ${parameters.input_question}\n\n"
    "Notice: Some fields like 'span()....' will be the date, but not metric and dimension. \n"
    "And one field will only count once in dimension count.  You should always pick field name from schema.\n"
    " And when you code is <2, 1, 0>, it belongs type 4.\n"
    "  And when you code is <1, 2, 0>, it belongs type 9.\n\n\n"
    "Now please reply a valid vega-lite specification in json based on above instructions.\n"
    "Please return the number of dimension, metric and date. Then choose the type. \n"
    "Please also return the type.\n"
    "Finally return the vega-lite specification according to the type.\n"
    "Please make sure all the key in the schema matches the word I given. \n"
    "Your answer format should be:\n"
    "Number of metrics:[list the metric name here, Don't use duplicate name]  <number of metrics {a}>  \n"
    "Number of dimensions:[list the dimension name here]  <number of dimension {b}> \n"
    "Number of dates:[list the date name here]  <number of dates {c}> \n"
    "Then format the entrance code by: <Number of metrics, Number of dimensions, Number of dates>\n"
    "Type and its entrance code: <type number>: <its entrance code>\n"
    "Then apply the vega-lite requirements of the type.\n"
    "<vega-lite> {here is the vega-lite json} </vega-lite>\n\n"
    "And don't use 'transformer' in your vega-lite and wrap your vega-lite json in <vega-lite> </vega-lite> tags\n"
)

PROMPT_CON_INSTRUCCIONES = (
    "You're an expert at creating vega-lite visualization. No matter what the user asks, you should reply with a valid vega-lite specification in json.\n"
    "Your task is to generate Vega-Lite specification in json based on the given sample data, the schema of the data, the PPL query to get the data and the user's input.\n\n"
    "Besides, here are some requirements:\n"
    "1. Do not contain the key called 'data' in vega-lite specification.\n"
    "2. If mark.type = point and shape.field is a field of the data, the definition of the shape should be inside the root \"encoding\" object, NOT in the \"mark\" object, for example, {\"encoding\": {\"shape\": {\"field\": \"field_name\"}}}\n"
    "3. Please also generate title and description\n\n"
    "The sample data in json format:\n${parameters.sampleData}\n\n"
    "This is the schema of the data:\n${parameters.dataSchema}\n\n"
    "The user used this PPL query to get the data: ${parameters.ppl}\n\n"
    "The user's input question is: ${parameters.input_question}\n"
    "The user's instruction on the visualization is: ${parameters.input_instruction}\n\n"
    "Now please reply a valid vega-lite specification in json based on above instructions.\n"
    "Please only contain vega-lite in your response.\n"
)


NOMBRE_DEL_MODELO = "platform-t2v"


def build_connector(api_key: str, endpoint: str, model: str) -> dict[str, Any]:
    """El connector del modelo de text to visualization: MaaS SIN razonamiento.
    Con el prompt largo de las reglas, el LLM del agente (que razona) tardaba 6 a
    12 s por gráfico (medido en CSS 3.4). La plantilla oficial también le da su
    propio modelo (Claude en Bedrock)."""
    from capabilities import build_llm_connector

    conector = build_llm_connector(api_key, endpoint, model, razonar=False)
    conector["name"] = "MaaS text to visualization (platform)"
    return conector


def build_workflow(model_id: str) -> dict[str, Any]:
    """`POST _plugins/_flow_framework/workflow`: las dos herramientas y los dos
    agentes de la plantilla oficial sobre `model_id`. El connector y el modelo
    se crean aparte con ml-commons: el paso register_remote_model del flow
    framework fallaba en CSS 3.4 con un error interno sin detalle."""
    def herramienta(nodo: str, prompt: str) -> dict[str, Any]:
        return {"id": nodo, "type": "create_tool", "previous_node_inputs": {},
                "user_inputs": {"name": "Text2Vega", "type": "MLModelTool",
                                # response_filter: el connector de MaaS devuelve el JSON de OpenAI entero.
                                "parameters": {"model_id": model_id, "prompt": prompt,
                                               "response_filter": "$.choices[0].message.content"}}}

    def agente(nodo: str, herramienta_id: str, nombre: str, descripcion: str) -> dict[str, Any]:
        return {"id": nodo, "type": "register_agent", "previous_node_inputs": {herramienta_id: "tools"},
                "user_inputs": {"parameters": {}, "type": "flow", "name": nombre, "description": descripcion}}

    return {
        "name": NOMBRE_DEL_WORKFLOW,
        "description": "Agentes de text to visualization (plantilla oficial de OpenSearch, con el LLM de MaaS)",
        "use_case": "REGISTER_AGENTS",
        "version": {"template": "1.0.0", "compatibility": ["2.18.0", "3.0.0"]},
        "workflows": {"provision": {"user_params": {}, "nodes": [
            herramienta("create_t2vega_tool", PROMPT_T2VEGA),
            herramienta("create_instruction_based_t2vega_tool", PROMPT_CON_INSTRUCCIONES),
            agente("t2vega_agent", "create_t2vega_tool", AGENTE,
                   "this is the t2vega agent that has a set of rules to generate the visualizations"),
            agente("t2vega_instruction_based_agent", "create_instruction_based_t2vega_tool", AGENTE_CON_INSTRUCCIONES,
                   "this is the t2vega agent that supports instructions"),
        ]}},
    }


def parametros(pregunta: str, ppl: str, resultado: dict[str, Any], instruccion: str = "") -> dict[str, Any]:
    """Los parámetros del `_execute`: la muestra y el esquema salen del resultado
    del PPL (`schema` + `datarows`)."""
    columnas = [c.get("name") for c in resultado.get("schema") or []]
    filas = [dict(zip(columnas, f)) for f in (resultado.get("datarows") or [])[:MAX_FILAS_DE_MUESTRA]]
    p = {"input_question": _escapado(pregunta), "ppl": _escapado(ppl),
         "sampleData": _escapado(json.dumps(filas, ensure_ascii=False, default=str)),
         "dataSchema": _escapado(json.dumps([{"name": c.get("name"), "type": c.get("type")} for c in resultado.get("schema") or []]))}
    if instruccion:
        p["input_instruction"] = _escapado(instruccion)
    return p


def _escapado(texto: str) -> str:
    """El texto listo para ir DENTRO de un string JSON: ml-commons sustituye
    estos parámetros en el prompt sin escaparlos, y las comillas de la muestra
    rompían el cuerpo del pedido ("Invalid payload", visto en CSS 3.4). Por eso
    el ejemplo de la documentación pasa `[{\\"unique_visitors\\"...`."""
    return json.dumps(texto or "", ensure_ascii=False)[1:-1]


def especificacion(respuesta_del_agente: dict[str, Any]) -> dict[str, Any] | None:
    """El Vega-Lite de la respuesta del agente (entre `<vega-lite>` o el primer
    objeto JSON), o None si no trae uno válido."""
    texto = ""
    for o in ((respuesta_del_agente or {}).get("inference_results") or [{}])[0].get("output") or []:
        texto = o.get("result") or json.dumps((o.get("dataAsMap") or {}).get("response", "")) or texto
        if texto:
            break
    if isinstance(texto, str) and texto.startswith('"'):
        try:
            texto = json.loads(texto)
        except ValueError:
            pass
    m = re.search(r"<vega-lite>\s*(\{.*\})\s*</vega-lite>", texto or "", re.S) or re.search(r"(\{.*\})", texto or "", re.S)
    if not m:
        return None
    try:
        spec = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(spec, dict) or not (spec.get("mark") or spec.get("layer") or spec.get("encoding")):
        return None
    spec.pop("data", None)
    return spec
