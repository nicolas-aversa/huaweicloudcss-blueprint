"""
capabilities.py
===============

Builders **puros** (sin I/O) de los artefactos de OpenSearch que la plataforma
provisiona por REST para demostrar *capabilities* más allá de logs+dashboards:

- **Analítica conversacional** — UN solo agente ml-commons (LLM + un PPLTool por
  vertical) que traduce preguntas en lenguaje natural a PPL y las ejecuta sobre
  la fuente correcta según la pregunta.
- **Forecasting** — 3 forecasters por vertical (volumen / fallos-críticos /
  entidades únicas), definidos en el spec de cada vertical.

Cada builder devuelve el `dict` (o `str`) del *body* que espera la API de
OpenSearch; la orquestación REST (crear/pollear/borrar) vive en `main.py`. El
system prompt del PPLTool y el `system_instruction` del agente se **generan
desde el schema** del tipo (enum de operaciones, campos, índice) — ese es el
acelerador: convierte la parte tediosa/frágil (enumerar campos y reglas) en un
click.

`_CAPABILITY_SPECS` mapea `slug -> params del vertical`: `fintech-transactions`,
`oil-gas` (Volve/Equinor), `media-retail-ecommerce` (sample de OpenSearch) y
`health` (Synthea).
"""

from __future__ import annotations

import os
import re
from typing import Any

import verticals as _verticals


def _sanitize_desc(s: str) -> str:
    """OpenSearch valida las descripciones de modelos/connectors: solo permite
    letras, números, espacios y `.,!?():@-_'/"`. Un `→` o un acento hace fallar el
    `_register` con 400. Colapsamos cualquier char no permitido a un espacio para
    que una descripción nunca rompa el provisioning en silencio."""
    return re.sub(r"[^A-Za-z0-9 .,!?():@\-_'/\"]+", " ", s or "").strip()

# ── Defaults del connector MaaS ──────────────────────────────────────────────
# Endpoint que el CLUSTER usa para llamar a MaaS (distinto del que usa el backend
# de la plataforma). El operador probó `api-ap-southeast-1`. Configurable por env.
DEFAULT_MAAS_CONNECTOR_ENDPOINT = "api-ap-southeast-1.modelarts-maas.com"
DEFAULT_MAAS_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_MAAS_PPL_MODEL = "deepseek-v4-flash"


def maas_connector_endpoint() -> str:
    return os.getenv("MAAS_CONNECTOR_ENDPOINT", DEFAULT_MAAS_CONNECTOR_ENDPOINT).strip() or DEFAULT_MAAS_CONNECTOR_ENDPOINT


def maas_llm_model() -> str:
    """Razonador del agente conversacional de OpenSearch."""
    return os.getenv("MAAS_LLM_MODEL", DEFAULT_MAAS_LLM_MODEL).strip() or DEFAULT_MAAS_LLM_MODEL


def maas_ppl_model() -> str:
    """Traductor NL→PPL del agente conversacional de OpenSearch."""
    return os.getenv("MAAS_PPL_MODEL", DEFAULT_MAAS_PPL_MODEL).strip() or DEFAULT_MAAS_PPL_MODEL


def trusted_endpoint_regex(endpoint: str | None = None) -> str:
    """Regex para `plugins.ml_commons.trusted_connector_endpoints_regex` que
    matchea el endpoint MaaS usado por el connector."""
    ep = (endpoint or maas_connector_endpoint()).replace(".", r"\.")
    return rf"^https://{ep}/.*$"


# ── Cluster settings (ml-commons) ────────────────────────────────────────────

def build_cluster_settings(endpoint: str | None = None) -> dict[str, Any]:
    """PUT /_cluster/settings (persistent): habilita connectors remotos, correr
    en el nodo de datos (cluster de 1 nodo, sin ML node dedicado) y memoria de
    conversación para el agente."""
    return {
        "persistent": {
            "plugins.ml_commons.trusted_connector_endpoints_regex": [trusted_endpoint_regex(endpoint)],
            "plugins.ml_commons.only_run_on_ml_node": False,
            "plugins.ml_commons.memory_feature_enabled": True,
            "plugins.ml_commons.connector_access_control_enabled": False,
            "cluster.max_shards_per_node": 5000,
        }
    }


# ── Connectors (MaaS, OpenAI-compat) ─────────────────────────────────────────

# El path del cluster a MaaS sale por el NAT/SNAT (ver terraform): el primer hop
# puede tardar y MaaS responde con timeouts intermitentes. Timeouts HOLGADOS +
# reintentos para que un blip transitorio o una generación lenta no mate la tool
# call. En particular, el connector del PPLTool manda un system prompt grande
# (schema + operaciones + ejemplos) → la generación de PPL con deepseek tarda más
# que un chat corto; con read_timeout apretado el connector cortaba y ml-commons
# lo reportaba como "remote endpoint error". El timeout del proxy de Dashboards NO
# es la restricción (el `_execute` completa y devuelve 200), así que holgado es OK.
_CLIENT_CONFIG = {
    "max_connection": 200,
    "connection_timeout": 30000,
    "read_timeout": 120000,
    "retry_backoff_millis": 1000,
    "retry_timeout_seconds": 60,
    "max_retry_times": 5,
    "retry_backoff_policy": "constant",
    "skip_ssl_verification": True,
}


def _connector_action(request_body: str, endpoint: str) -> dict[str, Any]:
    return {
        "action_type": "PREDICT",
        "method": "POST",
        "url": "https://${parameters.endpoint}/openai/v1/chat/completions",
        "headers": {
            "Authorization": "Bearer ${credential.maas_key}",
            "Content-Type": "application/json",
        },
        "request_body": request_body,
    }


def build_llm_connector(api_key: str, endpoint: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Connector del LLM del agente conversacional: chat genérico
    (system_instruction + prompt) sin thinking."""
    endpoint = endpoint or maas_connector_endpoint()
    model = model or maas_llm_model()
    request_body = (
        '{ "model": "${parameters.model}", "messages": ['
        '{"role": "system", "content": "${parameters.system_instruction:-You are a helpful assistant}"}, '
        '{"role": "user", "content": "${parameters.prompt}"}], '
        '"temperature": 0, "chat_template_kwargs": {"thinking": false} }'
    )
    return {
        "name": "MaaS LLM (platform)",
        "description": _sanitize_desc("LLM MaaS (no-thinking) para el agente conversacional"),
        "version": "1.0",
        "protocol": "http",
        "parameters": {"endpoint": endpoint, "model": model},
        "credential": {"maas_key": api_key},
        "actions": [_connector_action(request_body, endpoint)],
        "client_config": _CLIENT_CONFIG,
    }


def build_ppl_connector(api_key: str, ppl_system_prompt: str,
                        endpoint: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Connector del PPLTool. El system prompt entra por `${parameters.system_prompt}`
    (mismo patrón que `system_instruction` en el connector LLM): así UN solo modelo
    PPL sirve a TODOS los verticales — cada PPLTool del agente pasa el prompt de SU
    vertical como parámetro del tool. `ppl_system_prompt` queda como default del
    connector (para `_predict` directo sin parámetro)."""
    endpoint = endpoint or maas_connector_endpoint()
    model = model or maas_ppl_model()
    request_body = (
        '{ "model": "${parameters.model}", "messages": ['
        '{"role": "system", "content": "${parameters.system_prompt}"}, '
        '{"role": "user", "content": "${parameters.prompt}"}], '
        '"temperature": 0, "chat_template_kwargs": {"thinking": false} }'
    )
    return {
        "name": "MaaS DeepSeek PPLTool (platform)",
        "description": _sanitize_desc("DeepSeek no-thinking para generar PPL"),
        "version": "1.0",
        "protocol": "http",
        "parameters": {"endpoint": endpoint, "model": model,
                       "response_filter": "$.choices[0].message.content",
                       "system_prompt": ppl_system_prompt.replace("\n", "\\n")},
        "credential": {"maas_key": api_key},
        "actions": [_connector_action(request_body, endpoint)],
        "client_config": _CLIENT_CONFIG,
    }


# ── Model group + remote model ───────────────────────────────────────────────

def build_model_group(name: str = "platform-maas-deepseek") -> dict[str, Any]:
    return {"name": name, "description": _sanitize_desc("Modelos MaaS DeepSeek provisionados por la plataforma")}


def build_remote_model(name: str, connector_id: str, model_group_id: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "function_name": "remote",
        "model_group_id": model_group_id,
        "description": _sanitize_desc(description),
        "connector_id": connector_id,
    }


# ── Prompts generados desde el schema ────────────────────────────────────────

_AGENT_REACT_PROMPT = (
    "Assistant uses tools to answer questions.\n"
    "${parameters.tool_descriptions}\n\n"
    "${parameters.chat_history}\n\n"
    "${parameters.prompt.format_instruction}\n\n"
    "H: ${parameters.question}\n\n"
    "${parameters.scratchpad}\n\nA:"
)


def build_ppl_system_prompt(index_pattern: str, operations: list[str],
                            fields: dict[str, str], success_code: str = "",
                            label: str = "") -> str:
    """System prompt del PPLTool: enseña el índice, campos y enum de operaciones,
    con reglas PPL. Generado desde el schema (mismo estilo que el prompt curado
    del operador). Genérico: funciona para cualquier vertical."""
    ops = ", ".join(operations) if operations else "(a definir)"
    field_lines = "\n".join(f"- {path}: {desc}" for path, desc in fields.items()) if fields else "(a definir)"
    examples = ""
    if fields:
        first_field = next(iter(fields))
        examples = (
            f"# Count by field:\nsource={index_pattern} | stats count() as total by {first_field} | sort -total | head 5\n\n"
            f"# Total count:\nsource={index_pattern} | stats count() as total\n\n"
        )
        if success_code:
            examples += (
                f"# Success vs failure:\nsource={index_pattern} | eval ok=if(transaction.response_code='{success_code}',1,0) | stats sum(ok) as success, count() as total\n\n"
                f"# Failures breakdown:\nsource={index_pattern} | eval failed=if(transaction.response_code!='{success_code}',1,0) | stats sum(failed) as failed, count() as total by {first_field} | sort -failed\n\n"
            )
    return (
        "You are a PPL query generator for OpenSearch. Output ONLY the raw PPL query. "
        "No explanation, no markdown, no backticks. "
        f"CRITICAL: Always start with source={index_pattern} with NO spaces around =.\n\n"
        f"DATA SOURCE: {label or index_pattern}\n\n"
        f"OPERATIONS:\n{ops}\n\n"
        f"FIELDS:\n{field_lines}\n\n"
        "CRITICAL RULES:\n"
        "1. NEVER use eval AFTER stats. NEVER calculate rates or percentages in the query.\n"
        "2. NEVER use: JOIN, subqueries, append, row_number(), arithmetic after stats.\n"
        "3. eval ONLY before stats for binary flags.\n"
        "4. NEVER use head unless the user asks for top N.\n"
        "5. Single quotes for strings.\n"
        "6. Return raw numbers only - let the LLM calculate rates.\n\n"
        "CORRECT PATTERNS:\n"
        f"{examples}"
        f"# Time histogram:\nsource={index_pattern} | stats count() as total by span(@timestamp, 1d)"
    )


def build_agent_system_instruction(verticals: list[dict[str, Any]]) -> str:
    """`system_instruction` del agente conversacional multi-vertical.
    Recibe una lista de dicts: {label, tool_name, index_pattern, operations, fields, success_code}.
    El LLM decide qué PPLTool usar según la pregunta del usuario."""
    tool_lines = []
    for v in verticals:
        ops = ", ".join(v.get("operations", [])) if v.get("operations") else "(a definir)"
        tool_lines.append(
            f"- {v['label']} (tool: {v['tool_name']}, index: {v['index_pattern']}): "
            f"operations: {ops}"
        )
    tools_desc = "\n".join(tool_lines)
    return (
        "You are a helpful analytics assistant for OpenSearch.\n\n"
        "You have access to multiple data sources. Choose the right tool based on the user's question:\n"
        f"{tools_desc}\n\n"
        "To get data, call the appropriate PPLTool. Pass it the user's question in natural language; "
        "PPLTool generates and runs the PPL query itself and returns the numbers.\n\n"
        "Resolve references ('esas', 'those', 'esos') from the chat history. "
        "After PPLTool returns, answer in the user's language, with thousands separators. "
        "Never invent numbers: if PPLTool fails, say so instead of guessing."
    )


# ── Agente conversacional ────────────────────────────────────────────────────

def build_conversational_agent(llm_model_id: str, ppl_model_id: str,
                               system_instruction: str,
                               verticals: list[dict[str, Any]],
                               name: str = "Platform Conversational Root") -> dict[str, Any]:
    """Agente conversacional ml-commons con **múltiples PPLTools** (uno por vertical).
    Cada vertical aporta: {tool_name, label, index_pattern, ppl_system_prompt}.
    El LLM (`llm_model_id`) razona y decide cuál PPLTool invocar según la pregunta.
    Vive en OpenSearch y se consulta por Dev Tools:
    `POST /_plugins/_ml/agents/<id>/_execute`."""
    tools = []
    for v in verticals:
        tools.append({
            "type": "PPLTool",
            "name": v["tool_name"],
            "description": (
                f"Genera y ejecuta una query PPL de OpenSearch sobre {v['label']} "
                f"(index: {v['index_pattern']}). Pasale la pregunta del usuario en lenguaje natural."
            ),
            "parameters": {
                "model_type": "FINETUNE",
                "index": v["index_pattern"],
                "model_id": ppl_model_id,
                "execute": "true",
                "system_prompt": v["ppl_system_prompt"].replace("\n", "\\n"),
            },
            "include_output_in_agent_response": False,
        })
    return {
        "name": name,
        "type": "conversational",
        "description": _sanitize_desc("Agente conversacional autogenerado por la plataforma (NL a PPL a resultados)"),
        "app_type": "os_chat",
        "llm": {
            "model_id": llm_model_id,
            "parameters": {
                "max_iteration": "5",
                "response_filter": "$.choices[0].message.content",
                "system_instruction": system_instruction.replace("\n", "\\n"),
                "prompt": _AGENT_REACT_PROMPT,
            },
        },
        "memory": {"type": "conversation_index"},
        "tools": tools,
    }


# ── Forecasting ──────────────────────────────────────────────────────────────

def build_forecaster(index_pattern: str, volume_field: str,
                     time_field: str = "@timestamp", interval_minutes: int = 240,
                     horizon: int = 8, name: str = "fintech-volume-forecast",
                     feature_name: str = "txn_volume",
                     aggregation_query: dict[str, Any] | None = None,
                     description: str = "Forecast de volumen de transacciones (autogenerado)",
                     history: int = 4380,
                     window_delay_minutes: int = 10080,
                     suggested_seasonality: int = 0) -> dict[str, Any]:
    """Forecaster configurable. Por defecto cuenta docs (value_count sobre
    volume_field). Se puede pasar ``aggregation_query`` custom para otros forecasts
    (ej: cardinality de clientes únicos, value_count de fallos).

    El caller (``_provision_capabilities``) deriva ``interval_minutes``,
    ``history`` y ``window_delay_minutes`` del RANGO REAL de ``@timestamp`` del
    índice: la ventana de análisis del RCF es
    ``[now − window_delay − history×interval, now − window_delay]`` y tiene que
    caer sobre la serie (≥40 puntos poblados), o el forecaster queda en INIT
    vacío. Por eso ``window_delay_minutes`` (retraso hasta el fin de los datos)
    NO es fijo. ``history`` tope 10000 (doc OpenSearch).

    ``suggested_seasonality`` solo se incluye si > 2×shingle_size (16); por
    debajo de eso OpenSearch lo ignora, así que se omite (0 = no enviar)."""
    agg = aggregation_query or {feature_name: {"value_count": {"field": volume_field}}}
    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "time_field": time_field,
        "indices": [index_pattern],
        "feature_attributes": [
            {
                "feature_name": feature_name,
                "feature_enabled": True,
                "aggregation_query": agg,
            }
        ],
        "forecast_interval": {"period": {"interval": interval_minutes, "unit": "Minutes"}},
        "window_delay": {"period": {"interval": window_delay_minutes, "unit": "Minutes"}},
        "horizon": horizon,
        "history": min(history, 10000),
        "shingle_size": 8,
        "category_field": [],
    }
    if suggested_seasonality > 16:
        body["suggested_seasonality"] = suggested_seasonality
    return body


# ── Specs por vertical ───────────────────────────────────────────────────────
# Specs por vertical: se leen del registro declarativo verticals/ (cada vertical
# aporta su `capability` + `extra_capabilities` backend-only). Antes vivían acá
# como un literal de ~550 líneas.
_CAPABILITY_SPECS: dict[str, dict[str, Any]] = _verticals.capability_specs()


def get_capability_slugs() -> list[str]:
    """Slugs con un bundle de capabilities definido."""
    return list(_CAPABILITY_SPECS)


def get_capability_spec(slug: str) -> dict[str, Any] | None:
    return _CAPABILITY_SPECS.get(slug)


# ── Spec derivado de los campos detectados (despliegue productivo) ───────────
# Los verticales de demo traen su spec curado arriba. Un log productivo no tiene
# spec: se arma acá con el MISMO shape a partir de los campos del paso 2 (el
# schema que detectó el LLM) + los enums descubiertos del índice ya ingestado.
# Así el cliente se lleva en SU cuenta lo mismo que vio en la demo (chatbot +
# forecasts), pero sobre sus datos.

_MEASURE_TYPES = ("integer", "float", "long", "double")
# Campos que sirven para contar entidades únicas (cardinality) en el forecast.
_ENTITY_HINT_RE = re.compile(r"(^|[._])(id|user|customer|client|account|session)([._]|$)", re.IGNORECASE)

# Campos que indican eventos "problemáticos" (fallos, bloqueos, criticidad).
# En los verticales de demo: transaction.funnel.failed_at_code, security.denied,
# downtime, triage. value_count sobre ellos = count de eventos críticos.
_CRITICAL_FIELD_RE = re.compile(
    r"(^|[._])(failed|error|denied|blocked|rejected|cancelled|canceled|"
    r"down|downtime|critical|triage|alarm|alert|fault|outage|"
    r"exception|timeout|invalid|unauthor)([._]|$)",
    re.IGNORECASE,
)
# Valores de enum que indican un evento problemático.
_CRITICAL_VALUES = frozenset(
    {"failed", "error", "denied", "blocked", "rejected", "cancelled", "canceled",
     "down", "critical", "emergency", "alarm", "alert", "fault", "timeout", "invalid"}
)

# Campos que parecen un código de respuesta/estado → candidato a success_code.
_RESPONSE_CODE_RE = re.compile(
    r"(^|[._])(response_code|status_code|result_code|return_code|"
    r"outcome|result|response|status|rc|resp_code)([._]|$)",
    re.IGNORECASE,
)
# Valores que típicamente indican "éxito" en un código de respuesta.
_SUCCESS_VALUES = frozenset(
    {"000", "0", "00", "200", "201", "204", "success", "ok", "OK",
     "COMPLETED", "COMPLETED", "ACTIVE", "PASS", "APPROVED", "DONE"}
)


def _field_desc(f: dict[str, Any], values: "list[str] | None" = None) -> str:
    """Descripción de un campo para el prompt del PPLTool: la etiqueta de negocio
    + unidad + los valores reales del índice (si se descubrieron)."""
    desc = (f.get("business_label") or f.get("raw_name") or "").strip()
    unit = (f.get("unit") or "").strip()
    if unit:
        desc += f" (in {unit})"
    if (f.get("type") or "") in _MEASURE_TYPES:
        desc += " — numeric measure: sum/avg it"
    if values:
        shown = ", ".join(values[:12])
        desc += f" — values: {shown}" + ("…" if len(values) > 12 else "")
    return desc or "(sin descripción)"


def build_spec_from_fields(slug: str, index_pattern: str, fields: list[dict[str, Any]],
                           label: str = "", enums: "dict[str, list[str]] | None" = None
                           ) -> dict[str, Any]:
    """Spec de capabilities para un slug SIN spec curado (log productivo).

    Parameters
    ----------
    fields
        Campos del paso 2 (`field_path`, `type`, `business_label`, `dimension`).
    enums
        `{field_path: [valores]}` descubiertos del índice (ver `_discover_enums`
        en main.py). Ordenado de menor a mayor cardinalidad por el caller.

    Devuelve el mismo shape que `_CAPABILITY_SPECS[...]`, así el resto del
    provisioning (PPLTool, agente, forecasters) no distingue demo de productivo.
    """
    enums = enums or {}
    usable = [f for f in (fields or []) if (f.get("field_path") or "").strip()]

    def _is_dim(f: dict[str, Any]) -> bool:
        return bool(f.get("dimension"))

    # FIELDS del prompt: dimensiones + medidas/fechas/ips. Se excluye el texto
    # libre y los ids opacos (dimension=False y no numérico): no aportan al PPL
    # y ensucian el prompt.
    prompt_fields: dict[str, str] = {}
    for f in usable:
        path = f["field_path"].strip()
        ftype = (f.get("type") or "").strip()
        if not _is_dim(f) and ftype not in _MEASURE_TYPES + ("date", "ip"):
            continue
        prompt_fields[path] = _field_desc(f, enums.get(path))

    dims = [f for f in usable if _is_dim(f)]
    measures = [f for f in usable if (f.get("type") or "") in _MEASURE_TYPES]

    # Helper: encontrar campo por role (primario) con fallback a regex.
    def _by_role(role: str) -> "dict | None":
        return next((f for f in usable if (f.get("role") or "") == role), None)

    # ── Dimensión principal: role → enum descubierto → primer dim ──
    primary = _by_role("primary_dimension")
    if not primary:
        primary = next((f for f in dims if f["field_path"] in enums), dims[0] if dims else None)
    operations = list(enums.get(primary["field_path"], [])) if primary else []

    # ── volume_field: dim principal → keyword → numérico → primero (.keyword si text) ──
    volume_field = ""
    if primary:
        volume_field = primary["field_path"]
        if (primary.get("type") or "") == "text":
            volume_field = f"{volume_field}.keyword"
    else:
        kw = next((f for f in usable if (f.get("type") or "") in ("keyword", "boolean")), None)
        if not kw:
            kw = next((f for f in usable if (f.get("type") or "") in _MEASURE_TYPES), None)
        if not kw:
            kw = usable[0] if usable else None
        if kw:
            volume_field = kw["field_path"]
            if (kw.get("type") or "") == "text":
                volume_field = f"{volume_field}.keyword"

    forecasts: list[dict[str, Any]] = []
    if volume_field:
        forecasts.append({
            "name": f"{slug}-volume-forecast",
            "feature_name": "event_volume",
            "aggregation_query": {"event_volume": {"value_count": {"field": volume_field}}},
            "description": "Forecast de volumen de eventos por intervalo",
        })

    # ── Forecast de eventos críticos: role → regex nombre → enum con valores críticos ──
    critical_field = _by_role("critical_indicator")
    if not critical_field:
        critical_field = next((f for f in usable if _CRITICAL_FIELD_RE.search(f["field_path"])), None)
    if not critical_field:
        for f in dims:
            vals = enums.get(f["field_path"], [])
            if vals and any(v.lower() in _CRITICAL_VALUES for v in vals):
                critical_field = f
                break
    if critical_field:
        cf_path = critical_field["field_path"]
        if (critical_field.get("type") or "") == "text":
            cf_path = f"{cf_path}.keyword"
        forecasts.append({
            "name": f"{slug}-critical-forecast",
            "feature_name": "critical_events",
            "aggregation_query": {"critical_events": {"value_count": {"field": cf_path}}},
            "description": f"Forecast de eventos criticos/fallidos ({critical_field.get('business_label') or 'campo critico'}) por intervalo",
        })

    # ── Forecast de entidades únicas: role → regex ──
    entity = _by_role("entity_id")
    if not entity:
        entity = next((f for f in usable if _ENTITY_HINT_RE.search(f["field_path"])
                       and (f.get("type") or "") not in _MEASURE_TYPES), None)
    if entity:
        forecasts.append({
            "name": f"{slug}-entities-forecast",
            "feature_name": "unique_entities",
            "aggregation_query": {"unique_entities": {"cardinality": {"field": entity["field_path"]}}},
            "description": f"Forecast de {entity.get('business_label') or 'entidades'} únicos por intervalo",
            "history": 2000,
        })

    # ── Forecast de medida: role → primer numérico ──
    measure_field = _by_role("measure")
    if not measure_field and measures:
        measure_field = measures[0]
    if measure_field:
        forecasts.append({
            "name": f"{slug}-measure-forecast",
            "feature_name": "measure_sum",
            "aggregation_query": {"measure_sum": {"sum": {"field": measure_field["field_path"]}}},
            "description": f"Forecast de {measure_field.get('business_label') or 'la medida principal'} por intervalo",
        })

    # Capar a 3 forecasts (mismo tope que los verticales de demo).
    if len(forecasts) > 3:
        _priority = {"volume": 0, "critical": 1, "entities": 2, "measure": 3}
        forecasts.sort(key=lambda fc: _priority.get(
            next((k for k in _priority if k in fc["name"]), 9), 9))
        forecasts = forecasts[:3]

    # ── success_code: role → regex → más frecuente ──
    success_code = ""
    success_field = _by_role("success_indicator")
    if not success_field:
        success_field = next((f for f in usable if _RESPONSE_CODE_RE.search(f["field_path"])), None)
    if success_field:
        vals = enums.get(success_field["field_path"], [])
        if vals:
            match = next((v for v in vals if v in _SUCCESS_VALUES), None)
            success_code = match or vals[0]

    spec = {
        "label": label or "Tus logs",
        "index_pattern": index_pattern,
        "operations": operations,
        "success_code": success_code,
        "fields": prompt_fields,
        "volume_field": volume_field,
        "forecast_interval_minutes": 240,
        "forecast_horizon": 8,
        "forecasts": forecasts,
    }

    # Asegurar que campos con rol semántico estén en el prompt del PPLTool
    # aunque no sean dimensionales — el chatbot necesita conocerlos para
    # responder "cuántos fallos?", "qué % de éxito?", etc.
    for f in usable:
        path = f["field_path"].strip()
        if path in prompt_fields:
            continue
        role = (f.get("role") or "").strip()
        if role in ("critical_indicator", "success_indicator", "entity_id", "measure"):
            prompt_fields[path] = _field_desc(f, enums.get(path))
        elif _CRITICAL_FIELD_RE.search(path) or _RESPONSE_CODE_RE.search(path):
            prompt_fields[path] = _field_desc(f, enums.get(path))

    return spec
