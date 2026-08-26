# Chatbot OpenSearch (NL → PPL) — Secuencia universal de comandos

Corré cada bloque en **Dev Tools** de OpenSearch Dashboards, EN ORDEN. Reemplazá los
placeholders: `<MAAS_API_KEY>`, `<INDEX>`, `<FUENTE>`, y los ids que devuelve cada paso
(`<MODEL_GROUP_ID>`, `<PPL_CONNECTOR_ID>`, `<LLM_CONNECTOR_ID>`, `<TASK_ID>`, `<MODEL_ID>`,
`<AGENT_ID>`).

Lo único específico del log es el `system_prompt` del connector PPL (6.3): `DATA SOURCE` /
`FIELDS` / `OPERATIONS`. El resto es igual para cualquier fuente.

> El **Builder** (`Exportar Starter Kit`) genera esta misma secuencia ya parametrizada con el
> schema del log que pegaste. Este documento es la versión genérica de referencia.

---

### 6.1 Habilitar connectors remotos
```
PUT _cluster/settings
{
  "persistent": {
    "plugins.ml_commons.trusted_connector_endpoints_regex": [
      "^https://api-ap-southeast-1\\.modelarts-maas\\.com/.*$"
    ],
    "plugins.ml_commons.only_run_on_ml_node": false,
    "plugins.ml_commons.memory_feature_enabled": true,
    "plugins.ml_commons.connector_access_control_enabled": false,
    "cluster.max_shards_per_node": 5000
  }
}
```
### 6.2 Model group → guardá `model_group_id`
```
POST _plugins/_ml/model_groups/_register
{
  "name": "platform-maas-deepseek",
  "description": "Modelos MaaS DeepSeek provisionados por la plataforma"
}
```
### 6.3 Connector PPL (NL→PPL) → guardá `connector_id`
```
POST _plugins/_ml/connectors/_create
{
  "name": "MaaS DeepSeek PPLTool (platform)",
  "description": "DeepSeek no-thinking para generar PPL",
  "version": "1.0",
  "protocol": "http",
  "parameters": {
    "endpoint": "api-ap-southeast-1.modelarts-maas.com",
    "model": "deepseek-v3.2",
    "response_filter": "$.choices[0].message.content",
    "system_prompt": "You are a PPL query generator for OpenSearch. Output ONLY the raw PPL query. No explanation, no markdown, no backticks. CRITICAL: Always start with source=<INDEX>* with NO spaces around =.\\n\\nDATA SOURCE: <FUENTE>\\n\\nOPERATIONS:\\n(a definir)\\n\\nFIELDS:\\n(a definir)\\n\\nCRITICAL RULES:\\n1. NEVER use eval AFTER stats. NEVER calculate rates or percentages in the query.\\n2. NEVER use: JOIN, subqueries, append, row_number(), arithmetic after stats.\\n3. eval ONLY before stats for binary flags.\\n4. NEVER use head unless the user asks for top N.\\n5. Single quotes for strings.\\n6. Return raw numbers only - let the LLM calculate rates.\\n\\nCORRECT PATTERNS:\\n# Time histogram:\\nsource=<INDEX>* | stats count() as total by span(@timestamp, 1d)"
  },
  "credential": {
    "maas_key": "<MAAS_API_KEY>"
  },
  "actions": [
    {
      "action_type": "PREDICT",
      "method": "POST",
      "url": "https://${parameters.endpoint}/openai/v1/chat/completions",
      "headers": {
        "Authorization": "Bearer ${credential.maas_key}",
        "Content-Type": "application/json"
      },
      "request_body": "{ \"model\": \"${parameters.model}\", \"messages\": [{\"role\": \"system\", \"content\": \"${parameters.system_prompt}\"}, {\"role\": \"user\", \"content\": \"${parameters.prompt}\"}], \"chat_template_kwargs\": {\"thinking\": false} }"
    }
  ],
  "client_config": {
    "max_connection": 200,
    "connection_timeout": 30000,
    "read_timeout": 120000,
    "retry_backoff_millis": 1000,
    "retry_timeout_seconds": 60,
    "max_retry_times": 5,
    "retry_backoff_policy": "constant",
    "skip_ssl_verification": true
  }
}
```
### 6.4 Connector LLM (razonador) → guardá `connector_id`
```
POST _plugins/_ml/connectors/_create
{
  "name": "MaaS LLM (platform)",
  "description": "LLM MaaS (no-thinking) para el agente conversacional",
  "version": "1.0",
  "protocol": "http",
  "parameters": {
    "endpoint": "api-ap-southeast-1.modelarts-maas.com",
    "model": "deepseek-v3.2"
  },
  "credential": {
    "maas_key": "<MAAS_API_KEY>"
  },
  "actions": [
    {
      "action_type": "PREDICT",
      "method": "POST",
      "url": "https://${parameters.endpoint}/openai/v1/chat/completions",
      "headers": {
        "Authorization": "Bearer ${credential.maas_key}",
        "Content-Type": "application/json"
      },
      "request_body": "{ \"model\": \"${parameters.model}\", \"messages\": [{\"role\": \"system\", \"content\": \"${parameters.system_instruction:-You are a helpful assistant}\"}, {\"role\": \"user\", \"content\": \"${parameters.prompt}\"}], \"chat_template_kwargs\": {\"thinking\": false} }"
    }
  ],
  "client_config": {
    "max_connection": 200,
    "connection_timeout": 30000,
    "read_timeout": 120000,
    "retry_backoff_millis": 1000,
    "retry_timeout_seconds": 60,
    "max_retry_times": 5,
    "retry_backoff_policy": "constant",
    "skip_ssl_verification": true
  }
}
```
### 6.5 Registrar los 2 modelos remotos → cada uno devuelve `task_id`
```
POST _plugins/_ml/models/_register
{
  "name": "platform-ppl",
  "function_name": "remote",
  "model_group_id": "<MODEL_GROUP_ID>",
  "description": "NL to PPL",
  "connector_id": "<PPL_CONNECTOR_ID>"
}

POST _plugins/_ml/models/_register
{
  "name": "platform-llm",
  "function_name": "remote",
  "model_group_id": "<MODEL_GROUP_ID>",
  "description": "LLM frasea resultados",
  "connector_id": "<LLM_CONNECTOR_ID>"
}
```
### 6.6 Resolver `model_id` de cada task y desplegar
```
GET _plugins/_ml/tasks/<TASK_ID>            # tomá el model_id
POST _plugins/_ml/models/<MODEL_ID>/_deploy   # uno por cada modelo (ppl y llm)
GET _plugins/_ml/models/<MODEL_ID>          # confirmá model_state: DEPLOYED
```
### 6.7 Registrar el agente root → guardá `agent_id`
```
POST _plugins/_ml/agents/_register
{
  "name": "Platform Conversational Root",
  "type": "conversational",
  "description": "Agente conversacional autogenerado por la plataforma (NL a PPL a resultados)",
  "app_type": "os_chat",
  "llm": {
    "model_id": "<LLM_MODEL_ID>",
    "parameters": {
      "max_iteration": "5",
      "response_filter": "$.choices[0].message.content",
      "system_instruction": "You are a helpful analytics assistant for OpenSearch.\\n\\nYou have access to multiple data sources. Choose the right tool based on the user's question:\\n- <FUENTE> (tool: PPLTool-<slug>, index: <INDEX>*): operations: (a definir)\\n\\nTo get data, call the appropriate PPLTool. Pass it the user's question in natural language; PPLTool generates and runs the PPL query itself and returns the numbers.\\n\\nResolve references ('esas', 'those', 'esos') from the chat history. After PPLTool returns, answer in the user's language, with thousands separators. Never invent numbers: if PPLTool fails, say so instead of guessing.",
      "prompt": "Assistant uses tools to answer questions.\n${parameters.tool_descriptions}\n\n${parameters.chat_history}\n\n${parameters.prompt.format_instruction}\n\nH: ${parameters.question}\n\n${parameters.scratchpad}\n\nA:"
    }
  },
  "memory": {
    "type": "conversation_index"
  },
  "tools": [
    {
      "type": "PPLTool",
      "name": "PPLTool-<slug>",
      "description": "Genera y ejecuta una query PPL de OpenSearch sobre <FUENTE> (index: <INDEX>*). Pasale la pregunta del usuario en lenguaje natural.",
      "parameters": {
        "model_type": "FINETUNE",
        "index": "<INDEX>*",
        "model_id": "<PPL_MODEL_ID>",
        "execute": "true",
        "system_prompt": "You are a PPL query generator for OpenSearch. Output ONLY the raw PPL query. No explanation, no markdown, no backticks. CRITICAL: Always start with source=<INDEX>* with NO spaces around =.\\n\\nDATA SOURCE: <FUENTE>\\n\\nOPERATIONS:\\n(a definir)\\n\\nFIELDS:\\n(a definir)\\n\\nCRITICAL RULES:\\n1. NEVER use eval AFTER stats. NEVER calculate rates or percentages in the query.\\n2. NEVER use: JOIN, subqueries, append, row_number(), arithmetic after stats.\\n3. eval ONLY before stats for binary flags.\\n4. NEVER use head unless the user asks for top N.\\n5. Single quotes for strings.\\n6. Return raw numbers only - let the LLM calculate rates.\\n\\nCORRECT PATTERNS:\\n# Time histogram:\\nsource=<INDEX>* | stats count() as total by span(@timestamp, 1d)"
      },
      "include_output_in_agent_response": false
    }
  ]
}
```
### 6.8 Apuntar el Assistant al agente root (+ reiniciar Dashboards)
```
PUT .plugins-ml-config/_doc/os_chat
{
  "type": "os_chat_root_agent",
  "configuration": {
    "agent_id": "<AGENT_ID>"
  }
}
```
### 6.9 Probar
```
POST _plugins/_ml/agents/<AGENT_ID>/_execute
{
  "parameters": {
    "question": "¿Cuántos eventos hay en total?"
  }
}
```

> `OPERATIONS` en el prompt PPL puede venir vacío: se completa solo cuando ya tenés datos ingeridos (se descubren los valores de las dimensiones). Editá el system prompt si querés fijarlos a mano.
