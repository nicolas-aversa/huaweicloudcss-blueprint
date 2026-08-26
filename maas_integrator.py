"""
maas_integrator.py
===================

Capa de integración con el LLM expuesto por Huawei Cloud ModelArts as a
Service (MaaS).

MaaS es compatible con la API de OpenAI, por lo que se reutiliza la librería
oficial `openai` apuntando el `base_url` del cliente al endpoint de MaaS.

Responsabilidad única de este módulo: dado un log de muestra (ya anonimizado
por el cliente), pedirle al modelo `glm-5.2` que genere EXCLUSIVAMENTE el
bloque `filter {}` de Logstash, normalizando los campos al estándar ECS
(Elastic Common Schema).

Incluye RAG con información de plugins de Logstash (input, filter, output, codec)
extraída de la documentación oficial.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from openai import OpenAI, OpenAIError

from plugin_rag import get_plugin_context
from ecs_validator import classify_field, normalize_path, _load_spec
from log_format_catalog import try_match as _catalog_try_match
from log_format_catalog import detect_multiline, DATE_PATTERNS_COMMON

# Cargar mapeos validados desde JSON
_MAPPINGS_PATH = Path(__file__).parent / "docs" / "field_mappings.json"

@lru_cache(maxsize=1)
def _load_mappings() -> dict:
    """Carga mapeos validados desde field_mappings.json."""
    if not _MAPPINGS_PATH.exists():
        return {"exact_mappings": {}, "pattern_mappings": [], "type_inference": {}}
    with _MAPPINGS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _build_ecs_context() -> str:
    """Construye un resumen compacto de ECS 8.11 para inyectar en el prompt.

    1747 fields no caben en el prompt — y la mayoría son de threat / process
    inspector que casi nunca aparecen en logs financieros. Estrategia: listo
    los Field_Sets agrupados con sus leafs más útiles para parsing.

    Esto le da al LLM un mapa visual de la spec sin gastar 50K tokens. Si
    fields.csv crece o se actualiza a otra versión de ECS, este resumen se
    rearma solo (se basa en el CSV, no en strings hardcodeados).
    """
    spec = _load_spec()
    # Agrupar paths por su Field_Set (primer segmento).
    by_set: dict[str, list[str]] = {}
    for path in spec:
        if "." in path:
            root = path.split(".")[0]
            by_set.setdefault(root, []).append(path)
        else:
            by_set.setdefault("(top-level)", []).append(path)

    # Field_Sets que aparecen seguido en logs reales (mantener intactos).
    # Para los muy grandes (threat: 438 fields) listamos solo los 5 más
    # representativos para no comernos el prompt.
    common_sets = [
        "source", "destination", "client", "server", "host", "user",
        "http", "url", "user_agent", "network", "process", "event",
        "log", "error", "session", "trace", "span", "service",
        "observer", "transaction",
    ]

    lines = ["ESPECIFICACIÓN ECS 8.11 (fields disponibles, agrupados por Field_Set):"]
    for s in common_sets:
        fields = sorted(by_set.get(s, []))
        if not fields:
            continue
        # Truncar a 12 fields por set para no inflar el prompt.
        sample = fields[:12]
        more = f" (+{len(fields)-12} más)" if len(fields) > 12 else ""
        lines.append(f"  {s}: {', '.join(sample)}{more}")

    # Top-level (los que no tienen punto: @timestamp, message, tags, etc).
    top = sorted(by_set.get("(top-level)", []))
    if top:
        lines.append(f"  (top-level): {', '.join(top[:10])}")

    lines.append("")
    lines.append("REGLAS:")
    lines.append("- Si el campo del log coincide en sentido semántico con alguno de arriba, usá EL PATH EXACTO.")
    lines.append("- Si no encaja en ninguno, usá `labels.<nombre_original>` (custom).")
    lines.append("- @timestamp es top-level: emitilo SIN brackets en filter (date { target => \"@timestamp\" }).")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Configuración (vía variables de entorno)
# ---------------------------------------------------------------------------
# MAAS_API_KEY        -> API Key del servicio MaaS (obligatoria).
# MAAS_BASE_URL       -> URL base del endpoint MaaS compatible con OpenAI.
# MAAS_PIPELINE_MODEL -> Modelo para generar el filter de Logstash
#                       (por defecto: glm-5.2).
# MAAS_TIMEOUT        -> Timeout en segundos para la llamada al modelo
#                       (por defecto 240; subir si seguís viendo timeouts
#                       bajo carga, glm-5.2 a veces tarda >2min en CSS).
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.modelarts-maas.com/v1"
DEFAULT_MODEL = "glm-5.2"
DEFAULT_TIMEOUT = 240
# Modo de razonamiento de GLM-5.2 para la generación del pipeline. El modelo
# razona el parseo (qué plugin usar, namespacing, @timestamp, tipos) antes de
# emitir el JSON final. Se desactiva con MAAS_PIPELINE_THINKING=disabled.
DEFAULT_THINKING = "enabled"


# ---------------------------------------------------------------------------
# System Prompt Base
# ---------------------------------------------------------------------------
# El modelo devuelve JSON estructurado con dos cosas:
#   - filter_code: el bloque `filter { ... }` de Logstash deployable
#   - fields: lista de campos extraídos, con su path ECS y una etiqueta
#     amigable en español que el frontend usa para mostrar la tabla de
#     mapping al cliente (el "wow moment" de la demo).
# Forzamos JSON con response_format en la llamada y validamos con Pydantic.
SYSTEM_PROMPT_BASE = """\
Eres un ingeniero experto en Elastic Stack especializado en pipelines de
Logstash. Generás configs que conservan la semántica de dominio del log, sin
forzar esquemas genéricos.

Tu tarea: dado un log de muestra, generar (a) el bloque `filter { ... }` que lo
parsea, y (b) la lista de campos que extrae con una etiqueta amigable.

ESQUEMA DE SALIDA: NAMESPACED (no ECS).
Todos los campos parseados van bajo un parent `{namespace}` conservando sus
NOMBRES ORIGINALES (ej: `{namespace}.trxl_resp`, `{namespace}.status_code`).
NO renombres a ECS (`[source][ip]`, `[user][name]`, etc.). La mayoría de los
logs propietarios tienen campos que ECS oscurece; el cliente quiere ver sus
nombres reales.

FORMATO DE SALIDA (obligatorio):
Respondé EXCLUSIVAMENTE con un objeto JSON:

{
  "filter_code": "filter { ... }",
  "fields": [
    {
      "raw_name": "<nombre del campo en el log original>",
      "field_path": "<{namespace}.<nombre>, ej: {namespace}.trxl_resp>",
      "type": "<string | integer | float | boolean | date | ip>",
      "business_label": "<etiqueta amigable en español>",
      "unit": "<unidad si aplica: ms, USD, bytes; o null>",
      "dimension": <true | false>,
      "role": "<primary_dimension | success_indicator | critical_indicator | entity_id | measure | timestamp | null>"
    },
    ...
  ]
}

REGLAS para `filter_code`:
1. Usá los plugins apropiados (grok, kv, json, date, mutate, dissect) según el
   formato.
2. Poné los campos bajo el namespace con `target`:
   - kv:   `kv { source => "..." field_split => "|" value_split => "=" target => "{namespace}" }`
   - json: `json { source => "message" target => "{namespace}" }`
3. Si un campo del namespace contiene JSON anidado, parsealo:
   `json { source => "[{namespace}][campo]" target => "[{namespace}][campo]" skip_on_invalid_json => true }`
4. Si un campo es sub-delimitado (ej: `a=1~b=2`), usá un `kv` anidado con su
   propio `target`.
5. Tipado CONSERVADOR con `mutate convert`: convertí a numérico SOLO campos
   que son MEDIDAS agregables (monto, latencia, bytes, conteo, duración). NO
   conviertas IDs, códigos, status, versiones, números de cuenta/tarjeta,
   timestamps ni flags aunque el valor sea numérico — perderías ceros a la
   izquierda (ej. "000") y generarías conflictos de tipo en OpenSearch. En la
   duda, dejalo como keyword (no lo conviertas).
5b. LIMPIÁ el valor con `mutate gsub` ANTES de cada `convert` numérico, dejando
   solo dígitos, el punto decimal y el signo. `convert` usa `to_f`/`to_i` de Ruby,
   que parsea el PREFIJO y corta en el primer caracter no numérico — sin limpiar,
   el dato se pierde EN SILENCIO (queda 0 o un número truncado):
     "$399" / "₹399" / "USD 399"  -> to_f == 0.0   (el símbolo va adelante)
     "1,099" / "1.099,50"         -> to_f == 1.0   (corta en el separador de miles)
     "125 ms" / "45%"             -> to_f == 125.0 / 45.0  (ok: el número va primero)
   Sacá símbolos de moneda (`$ ₹ € £ ¥`), códigos (USD/ARS/EUR), espacios y
   separadores de miles. Ojo con el formato decimal: si usa coma decimal
   ("1.099,50"), primero sacá los puntos de miles y después pasá la coma a punto.
6. Manejá el timestamp del evento con `date { ... target => "@timestamp" }`
   cuando haya un campo de fecha claro (preferí el del momento del evento,
   tipo `entry_tim`/`event_time`, sobre write/exit).
7. Limpiá antes de indexar: con un `ruby`, borrá del namespace los campos cuyo
   valor sea el literal "null", "" o solo whitespace (un "null" string contra un
   campo numérico/date del index template rompe la indexación del documento
   ENTERO en OpenSearch). Después borrá `message` y los temporales con
   `remove_field`.
8. Sintaxis válida para Logstash 7.10/8.x.

REGLAS para `fields`:
1. Incluí los campos visibles en el evento indexado (no los temporales).
2. `business_label`: corto, en español, comprensible (ej: "Código de
   respuesta", "Tipo de mensaje"). Sin jerga técnica.
3. `unit` solo cuando aplica (ej: "ms"); null si no.
4. `dimension`: true si el campo sirve para AGRUPAR o FILTRAR — o sea, si tiene
   un conjunto acotado de valores que se repiten entre eventos (estado, tipo de
   operación, categoría, canal, país, severidad, marca). Con esos campos se arman
   los "Top N" del dashboard y las preguntas del chatbot.
   Poné false cuando NO sirve para agrupar:
   - texto libre / largo: descripciones, reviews, comentarios, títulos, mensajes;
   - URLs, links, rutas de imagen;
   - identificadores casi únicos (ids, uuids, emails, nº de orden) o listas de
     varios valores concatenados;
   - las MEDIDAS numéricas (monto, latencia, bytes): esas se suman, no se agrupan.
    Agrupar por un párrafo o por un id único genera un gráfico inútil (una barra
    por documento), así que en la duda poné false.
5. `role`: el rol semántico del campo para armar dashboards y forecasts. UNO solo
   por campo, o null si no aplica:
   - "primary_dimension": la dimensión principal de agrupación — el campo que
     define el tipo de evento u operación (ej: operation_code, status, class, well).
   - "success_indicator": campo cuyos valores distinguen éxito de fallo
     (ej: response_code, outcome, result, status_code).
   - "critical_indicator": campo que existe SOLO en eventos problemáticos
     (ej: failed_at_code, denied, downtime, triage, cancel_reason, error_code).
   - "entity_id": identificador de una entidad única que se repite entre eventos
     (ej: customer_id, patient, user_id, session_id). NO es transaction_id
     (eso identifica el evento, no la entidad).
   - "measure": la medida numérica principal del evento (ej: amount, revenue,
     cost, latency, bytes). El número que se suma o promedia.
   - "timestamp": la fecha/hora del evento (ej: entry_tim, event_time, created_at).
   - null: el campo no tiene un rol especial.
   Si dudás entre dos roles, preferí el más específico. No fuerces un role si no
   aplica — null es una respuesta válida.

NO uses Markdown. NO envuelvas el JSON en ```. Empezá con `{` y terminá con `}`.

EJEMPLO (pattern → filter, estilo namespaced):

Log envelope + kv pipe-separado:
  `[PEND] 20251004235800 - thread [B2AUTH02] table=...|trxl_resp=000|trxl_msg_typ=210|trxl_tech_detail={"data":{...}}`
Filter:
  grok extrae el envelope y deja el payload kv en `kv_payload`
  -> kv { source => "kv_payload" field_split => "|" value_split => "=" target => "{namespace}" }
  -> json anidado: json { source => "[{namespace}][trxl_tech_detail]" target => "[{namespace}][trxl_tech_detail]" skip_on_invalid_json => true }
  -> mutate convert { "[{namespace}][trxl_msg_typ]" => "integer" }
  -> date { match => ["[{namespace}][trxl_entry_tim]", "yyyyMMddHHmmssSSS"] target => "@timestamp" }
  -> mutate { remove_field => ["message", "kv_payload"] }

Medida con moneda/miles (regla 5b) — limpiar ANTES de convertir:
  `precio=₹1,099`  (sin el gsub, convert lo dejaría en 0)
Filter:
  -> mutate { gsub => ["[{namespace}][precio]", "[^0-9.\\-]", ""] }
  -> mutate { convert => { "[{namespace}][precio]" => "float" } }

{plugin_context}
"""


SYSTEM_PROMPT_JDBC = """\
Eres un ingeniero experto en Elastic Stack especializado en pipelines de
Logstash con input JDBC. Generás configs para datos ESTRUCTURADOS que ya vienen
como campos de Logstash (el plugin `jdbc` mapea columnas SQL → fields).

Tu tarea: dado un sample que representa las columnas de un result set, generar
(a) el bloque `filter { ... }` que enriquece/transforma esos campos, y (b) la
lista de campos con una etiqueta amigable.

IMPORTANTE: los datos ya están estructurados. NO uses grok, kv, json ni dissect
para parsear — las columnas ya son fields. El filter solo debe:
1. Mapear la columna de timestamp al @timestamp con `date { ... }`.
2. Convertir tipos numéricos con `mutate convert` (solo medidas: montos,
   latencias, bytes, conteos — no IDs ni códigos).
3. Limpiar valores nulos/string vacío con `ruby` (igual que en el path normal).
4. Remover campos temporales o innecesarios con `mutate remove_field`.

ESQUEMA DE SALIDA: NAMESPACED (no ECS).
Todos los campos van bajo un parent `{namespace}` conservando sus NOMBRES
ORIGINALES.

FORMATO DE SALIDA (obligatorio):
Respondé EXCLUSIVAMENTE con un objeto JSON:

{
  "filter_code": "filter { ... }",
  "fields": [
    {
      "raw_name": "<nombre de la columna SQL>",
      "field_path": "<{namespace}.<nombre>",
      "type": "<string | integer | float | boolean | date | ip>",
      "business_label": "<etiqueta amigable en español>",
      "unit": "<unidad si aplica: ms, USD, bytes; o null>",
      "dimension": <true | false>,
      "role": "<primary_dimension | success_indicator | critical_indicator | entity_id | measure | timestamp | null>"
    },
    ...
  ]
}

REGLAS para `filter_code`:
1. NO uses grok, kv, json ni dissect — los datos ya son campos.
2. Usá `date { match => ["[{namespace}][<ts_col>]", "<formato>"] target => "@timestamp" }`
   para mapear el timestamp.
3. Usá `mutate convert` para tipar campos numéricos (montos, cantidades,
   latencias). NO conviertas IDs, códigos, status, versiones.
4. Limpiá con `ruby` los campos cuyo valor sea "null", "" o whitespace.
5. Remové `message` y temporales con `mutate remove_field`.
6. Sintaxis válida para Logstash 7.10/8.x.

REGLAS para `fields`:
1. Incluí todas las columnas visibles del result set.
2. `business_label`: corto, en español, comprensible.
3. `dimension`: true si sirve para agrupar/filtrar (estado, tipo, categoría).
4. `role`: primary_dimension, success_indicator, critical_indicator, entity_id,
   measure, timestamp, o null.

NO uses Markdown. NO envuelvas el JSON en ```. Empezá con `{` y terminá con `}`.
"""


# ── API key configurable en runtime (⚙ Configuración de la plataforma) ──────
# La key configurada desde la UI se persiste acá y tiene PRIORIDAD sobre el
# MAAS_API_KEY del .env: así cada demo/deploy puede correr contra la cuenta
# MaaS del cliente en vez de consumir los recursos del operador. La usan TODOS
# los consumos de modelos: mapping/pipeline (este módulo) y los connectors del
# chatbot de OpenSearch (main._provision_capabilities). `chatbot.py` (copiloto)
# también la usa si se reexpone: sus endpoints están removidos hoy.
# Ruta del archivo de settings (⚙ Configuración). Configurable por env var para
# que el contenedor la persista en un volumen (sin crear archivos a mano); default
# junto al código para el arranque nativo.
_SETTINGS_PATH = Path(
    os.environ.get("PLATFORM_SETTINGS_PATH")
    or (Path(__file__).parent / ".platform_settings.json")
)


def get_maas_api_key() -> str:
    """Key configurada en la plataforma (settings file) > MAAS_API_KEY del env."""
    try:
        if _SETTINGS_PATH.is_file():
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            key = (data.get("maas_api_key") or "").strip()
            if key:
                return key
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[settings] no pude leer {_SETTINGS_PATH.name}: {exc!r}")
    return os.getenv("MAAS_API_KEY", "")


def set_maas_api_key(key: str) -> None:
    """Persiste (o borra, si viene vacía) la key configurada desde la UI."""
    data: dict = {}
    try:
        if _SETTINGS_PATH.is_file():
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    key = (key or "").strip()
    if key:
        data["maas_api_key"] = key
    else:
        data.pop("maas_api_key", None)
    _SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def maas_key_source() -> str:
    """De dónde sale la key efectiva: 'settings' | 'env' | 'none' (para la UI)."""
    try:
        if _SETTINGS_PATH.is_file():
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            if (data.get("maas_api_key") or "").strip():
                return "settings"
    except (OSError, json.JSONDecodeError):
        pass
    return "env" if os.getenv("MAAS_API_KEY", "") else "none"


def get_pipeline_model() -> str:
    """Modelo del análisis/generación de pipeline (MAAS_PIPELINE_MODEL o default)."""
    return os.getenv("MAAS_PIPELINE_MODEL", DEFAULT_MODEL)


# ── Cuenta Huawei Cloud del SA (⚙ Configuración) ────────────────────────────
# Cada SA corre su propia instancia con SU cuenta: estos settings (project id,
# región, VPC/subnet/SG/AZ, bucket de demos) se configuran por UI y pisan los
# valores del terraform.tfvars estático al armar el deploy. Ninguno es secreto.

_HUAWEI_FIELDS = (
    "project_id", "region", "vpc_id", "subnet_id",
    "security_group_id", "availability_zone", "demo_bucket",
)


def get_huawei_settings() -> dict:
    """Settings de cuenta Huawei guardados por UI (dict con _HUAWEI_FIELDS, '' si falta)."""
    data: dict = {}
    try:
        if _SETTINGS_PATH.is_file():
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8")).get("huawei") or {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[settings] no pude leer {_SETTINGS_PATH.name}: {exc!r}")
        data = {}
    return {f: (data.get(f) or "").strip() for f in _HUAWEI_FIELDS}


def set_huawei_settings(values: dict) -> None:
    """Persiste los settings de cuenta Huawei (solo campos conocidos; vacíos se borran)."""
    data: dict = {}
    try:
        if _SETTINGS_PATH.is_file():
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    huawei = {f: v for f in _HUAWEI_FIELDS if (v := (values.get(f) or "").strip())}
    if huawei:
        data["huawei"] = huawei
    else:
        data.pop("huawei", None)
    _SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_region() -> str:
    """Región efectiva: settings > HUAWEI_REGION del env > la-south-2 (default histórico)."""
    region = get_huawei_settings().get("region", "")
    return region or os.getenv("HUAWEI_REGION", "").strip() or "la-south-2"


def get_huawei_project_id() -> str:
    """Project ID efectivo: settings > HUAWEI_PROJECT_ID del env."""
    pid = get_huawei_settings().get("project_id", "")
    return pid or os.getenv("HUAWEI_PROJECT_ID", "").strip()


# ── Campos dimensionales (agrupables) ────────────────────────────────────────
# Un campo es "dimensión" si sirve para AGRUPAR/FILTRAR (estado, categoría,
# canal…). NO lo son el texto libre, las URLs, los ids casi únicos ni las
# medidas numéricas. Lo usan el dashboard auto-generado (Top-N) y el prompt del
# PPLTool del chatbot: agrupar por un párrafo o por un id único da un gráfico
# inútil (una barra por documento) y ensucia el prompt.

# Nombres que casi siempre son texto libre / links / ids opacos.
_NON_DIMENSION_RE = re.compile(
    r"(descr|about|content|comment|review|message|msg|body|text|title|summary|"
    r"link|url|href|img|image|photo|uuid|guid|hash|token|email|mail|"
    r"_id$|^id$|\bid$)",
    re.IGNORECASE,
)


def is_dimension(field_path: str, ftype: str, llm_flag: Any = None) -> bool:
    """¿El campo sirve para agrupar? Respeta la flag del LLM si vino; si no,
    cae a la heurística por tipo + nombre.

    `llm_flag` puede venir como bool o como el string "true"/"false" (algunos
    servings serializan así) — cualquier otra cosa se trata como ausente.
    """
    if isinstance(llm_flag, bool):
        return llm_flag
    if isinstance(llm_flag, str) and llm_flag.strip().lower() in ("true", "false"):
        return llm_flag.strip().lower() == "true"
    # Las medidas se suman, no se agrupan; date/ip tienen sus propios paneles.
    if (ftype or "").lower() in ("integer", "float", "long", "double", "date", "ip"):
        return False
    return not _NON_DIMENSION_RE.search(field_path or "")


# ── Rol semántico del campo ──────────────────────────────────────────────────
# El LLM lo asigna en el step 2. Si no viene, se infiere acá por nombre/tipo.
# Lo usan build_spec_from_fields (forecasts, success_code, operations) y
# _spec_from_fields (paneles del dashboard). Es metadata INTERNA de la plataforma
# — no llega al index template ni a OpenSearch.

_ROLE_ENTITY_RE = re.compile(r"(^|[._])(id|user|customer|client|account|session|patient)([._]|$)", re.IGNORECASE)
_ROLE_CRITICAL_RE = re.compile(
    r"(^|[._])(failed|error|denied|blocked|rejected|cancelled|canceled|"
    r"down|downtime|critical|triage|alarm|alert|fault|outage|exception|timeout)([._]|$)",
    re.IGNORECASE,
)
_ROLE_SUCCESS_RE = re.compile(
    r"(^|[._])(response_code|status_code|result_code|return_code|outcome|result|response|rc)([._]|$)",
    re.IGNORECASE,
)
_ROLE_TIMESTAMP_RE = re.compile(r"(^|[._])(time|timestamp|date|tim|created_at|event_time)([._]|$)", re.IGNORECASE)
_ROLE_MEASURE_RE = re.compile(
    r"(amount|price|cost|revenue|total|sum|latency|duration|bytes|size|volume|count|value|metric|measure)",
    re.IGNORECASE,
)

_VALID_ROLES = frozenset({
    "primary_dimension", "success_indicator", "critical_indicator",
    "entity_id", "measure", "timestamp",
})


def infer_role(field_path: str, ftype: str, llm_role: Any = None) -> "str | None":
    """Rol semántico del campo. Respeta el LLM si vino un valor válido;
    si no, infiere por nombre + tipo.

    El rol guía qué paneles del dashboard y qué forecasts se crean —
    reemplaza a las regex hardcodeadas en capabilities.py/dashboards.py.
    """
    if isinstance(llm_role, str):
        r = llm_role.strip().lower()
        if r in _VALID_ROLES:
            return r
        if r in ("none", "null", ""):
            return None
    path = field_path or ""
    ft = (ftype or "").lower()
    if ft == "date" or _ROLE_TIMESTAMP_RE.search(path):
        return "timestamp"
    if ft in ("integer", "float", "long", "double"):
        if _ROLE_ENTITY_RE.search(path):
            return None  # un ID numérico no es measure
        if _ROLE_MEASURE_RE.search(path):
            return "measure"
        return None
    if _ROLE_CRITICAL_RE.search(path):
        return "critical_indicator"
    if _ROLE_SUCCESS_RE.search(path):
        return "success_indicator"
    if _ROLE_ENTITY_RE.search(path):
        return "entity_id"
    return None


def _build_client() -> OpenAI:
    """Construye el cliente OpenAI apuntando al endpoint de MaaS.

    La API key sale de la configuración de la plataforma (⚙) o, si no hay,
    del env. Lanza ``RuntimeError`` si no hay ninguna, de modo que el fallo
    sea explícito al arrancar el flujo y no un error opaco de red después.
    """
    api_key = get_maas_api_key()
    if not api_key:
        raise RuntimeError(
            "No hay API Key de MaaS: configurala en ⚙ Configuración de la "
            "plataforma o definí MAAS_API_KEY en el entorno."
        )

    base_url = os.getenv("MAAS_BASE_URL", DEFAULT_BASE_URL)

    return OpenAI(api_key=api_key, base_url=base_url)


def _strip_markdown_fences(text: str) -> str:
    """Sanea la respuesta del modelo.

    Aunque el System Prompt prohíbe explícitamente el formato Markdown, los
    LLM ocasionalmente lo ignoran. Esta limpieza defensiva elimina las vallas
    de código (``` o ```ruby / ```conf) para garantizar que la salida sea
    directamente utilizable en un pipeline.
    """
    cleaned = text.strip()

    cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    return cleaned.strip()


# Con thinking activado, algunos servings de GLM embeben el razonamiento como
# <think>...</think> dentro del content en vez de mandarlo en reasoning_content.
# Lo sacamos antes de parsear para que el JSON quede limpio.
_THINK_TAG_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _thinking_param() -> dict:
    """`extra_body` que activa el modo thinking de GLM-5.2 en MaaS.

    GLM expone el razonamiento vía `thinking: {type: enabled|disabled}`
    (convención ZhipuAI que el endpoint OpenAI-compatible de MaaS replica). El
    chain-of-thought vuelve en `reasoning_content`, aparte del JSON final.
    Controlable con MAAS_PIPELINE_THINKING.
    """
    mode = os.getenv("MAAS_PIPELINE_THINKING", DEFAULT_THINKING).strip().lower()
    enabled = mode not in ("0", "off", "false", "disabled", "no")
    return {"thinking": {"type": "enabled" if enabled else "disabled"}}


def strip_logstash_comments(code: str) -> str:
    """Elimina comentarios `#` de una config Logstash sin romper strings.

    Cubre dos shapes:

    1. **Línea entera de comentario** (`  # ECS: duration en ns`): se descarta.
    2. **Comentario inline al final de una línea de código** (`mutate { ... } # comentario`):
       se trunca desde el primer `#` que NO esté dentro de un string quoted.

    Heurística para detectar "fuera de string": el prefijo hasta el `#` debe
    tener número par de comillas dobles y simples. No maneja escape sequences
    (``\\"``), pero ni el LLM ni el operador típico las usan en strings
    Logstash. False-positive en ese caso = cortar antes del comentario real
    (degradación aceptable, no rompe el config).

    Función exportada porque se invoca desde varios lugares:
      - `generate_logstash_filter` (path LLM, output del MaaS).
      - `generate_pipeline` endpoint (catch-all final — cubre cached example
        hardcodeado en el frontend y edición manual del operador en step 5).
    """
    out_lines = []
    for line in code.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        cut = -1
        for i, ch in enumerate(line):
            if ch != "#":
                continue
            prefix = line[:i]
            if prefix.count('"') % 2 == 0 and prefix.count("'") % 2 == 0:
                cut = i
                break
        if cut >= 0:
            line = line[:cut].rstrip()
            if not line:
                continue
        out_lines.append(line)
    return "\n".join(out_lines)


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Parsea la respuesta JSON del LLM con tolerancia a fences de Markdown.

    Aunque pedimos ``response_format={"type": "json_object"}`` y el prompt
    prohíbe Markdown, algunos modelos lo siguen envolviendo en ```json. Esta
    función pela esa capa antes de pasárselo a json.loads para evitar errores
    de parseo que tirarían 502 al frontend.
    """
    cleaned = _strip_markdown_fences(_THINK_TAG_RE.sub("", raw or ""))
    return json.loads(cleaned)


# Detección de IP por VALOR (no por nombre): solo IPv4 con octetos 0-255,
# anclado full-match. NO detectamos IPv6 por valor a propósito: una MAC
# ("a2:e9:00:ec:40:01") es hex con ':' e indistinguible de un IPv6 laxo →
# se tiparía `ip`, la MAC no coerce y la perderíamos (_ignored). Un IPv6 real
# cae a keyword (seguro, agregable). IPv4 es inequívoco (versiones "1.13.5" →
# 3 partes, fechas "2017.05.10" → octeto >255: ninguno matchea).
_IPV4_RE = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$"
)


def _infer_field_type(field_name: str, value: str) -> str:
    """Infiere el tipo de dato basándose en el nombre del campo y el valor."""
    name_lower = field_name.lower()
    mappings = _load_mappings()
    type_inf = mappings.get("type_inference", {})
    
    # 1. Por sufijo (desde JSON)
    by_suffix = type_inf.get("by_suffix", {})
    for suffix, ftype in by_suffix.items():
        if name_lower.endswith(suffix):
            return ftype
    
    # 2. Por prefijo (desde JSON)
    by_prefix = type_inf.get("by_prefix", {})
    for prefix, ftype in by_prefix.items():
        if name_lower.startswith(prefix):
            return ftype
    
    # 3. Por keyword (desde JSON), matcheando por TOKEN completo, no substring.
    # Substring rompe logs reales: "count" ⊂ "srccountry"/"account", "ip" ⊂
    # "description"/"recipient" → el campo se tipa numérico/ip, el valor string
    # no coerce y OpenSearch lo descarta (ignore_malformed → _ignored), perdiendo
    # agregabilidad. Tokenizamos el nombre por separadores no-alfanuméricos y
    # exigimos igualdad de token (ej. "event_count" → {"event","count"} matchea
    # "count"; "srccountry" → {"srccountry"} NO matchea). Ante la duda, keyword.
    name_tokens = set(re.split(r"[^a-z0-9]+", name_lower)) - {""}
    # También sin el sufijo numérico del token: "amount1".."amount4" → "amount"
    # (campos enumerados, muy comunes) deben matchear "amount", pero "srccountry"
    # sigue sin matchear "count" (no es "count" + dígitos).
    name_tokens |= {re.sub(r"\d+$", "", tok) for tok in name_tokens}
    name_tokens -= {""}
    by_keyword = type_inf.get("by_keyword", {})
    for keyword, ftype in by_keyword.items():
        if keyword in name_tokens:
            return ftype

    # 4. Por valor (si no es null/vacío)
    if value and value.lower() not in ("null", "none", "", "-"):
        # IP por valor: preserva el tipado `ip` aunque el nombre no lo delate
        # (ej. "srcip" ya no matchea el token "ip"); solo si el valor ES una IP
        # válida, así nunca rompe un campo de texto.
        if _IPV4_RE.match(value):
            return "ip"
        if value.isdigit():
            return "integer"
        try:
            float(value)
            return "float"
        except ValueError:
            pass
        if value.lower() in ("true", "false", "0", "1"):
            return "boolean"

    return "string"


# ECS type → Logstash `mutate convert` directive. Mantenemos esto como
# fuente única de verdad para evitar bugs de type-mismatch (ej. emitir
# ``convert => "integer"`` sobre un field ECS keyword, que después rompe el
# dynamic mapping de OpenSearch cuando llega un doc con string).
#
# Logstash 7.10 acepta: integer, integer_eu, float, float_eu, string, boolean.
# Para ECS types sin equivalente directo (ip, date, object, geo_point), no
# emitimos convert — el index template / dynamic mapping de OpenSearch se
# encarga del casting basado en el contenido.
_ECS_TO_LS_CONVERT: dict[str, str | None] = {
    # String-family → string (fuerza stringificación incluso si el source es int)
    "keyword": "string",
    "constant_keyword": "string",
    "wildcard": "string",
    "text": "string",
    "match_only_text": "string",
    # Integer-family
    "long": "integer",
    "integer": "integer",
    "short": "integer",
    "byte": "integer",
    "unsigned_long": "integer",
    # Float-family
    "double": "float",
    "float": "float",
    "half_float": "float",
    "scaled_float": "float",
    # Boolean
    "boolean": "boolean",
    # Sin convert directo en Logstash — dejamos al template.
    "ip": None,
    "date": None,
    "date_nanos": None,
    "geo_point": None,
    "object": None,
    "nested": None,
    "flattened": None,
    "version": None,
}


def _logstash_convert_for_ecs(ecs_path: str) -> str | None:
    """Devuelve el ``mutate convert`` apropiado para un path ECS.

    Returns
    -------
    str | None
        Directiva válida de Logstash (``"integer"``, ``"float"``, ``"string"``,
        ``"boolean"``), o ``None`` si el path no está en la spec ECS, o si
        el tipo ECS no tiene convert directo (ip/date/object). El caller
        debe caer a ``_infer_field_type`` cuando recibe ``None`` para no
        perder convert en campos custom (labels.*).
    """
    if not ecs_path or ecs_path == "@timestamp":
        return None
    info = classify_field(ecs_path)
    if not info["is_ecs"]:
        return None
    ecs_type = (info.get("ecs_type") or "").lower()
    return _ECS_TO_LS_CONVERT.get(ecs_type)


# Prefijos ECS condensados → su Field_Set canónico. Usado por
# `_get_ecs_mapping` para resolver nombres tipo `srcip` → `source.ip`,
# `clientip` → `client.ip`, `processname` → `process.name`, etc., sin
# tener que hardcodear cada alias en field_mappings.json. La regla:
# si el nombre arranca con uno de estos prefijos Y `<canonical>.<rest>`
# existe en fields.csv, usar ese path.
#
# Genérico (no es por vendor): cubre las convenciones condensadas que
# aparecen en firewall (FortiNet/PaloAlto), web, EDR, audit, DNS, TLS,
# URL inspection, etc. Los namespaces vienen directo de ECS 8.11.
_PREFIX_ALIASES: dict[str, str] = {
    # Network/security primitives
    "src":     "source",
    "dst":     "destination",
    "dest":    "destination",
    "obs":     "observer",
    # Identidad / host
    "host":    "host",
    "user":    "user",
    "client":  "client",
    "server":  "server",
    # Aplicación / web
    "http":    "http",
    "url":     "url",
    "log":     "log",
    "event":   "event",
    # Infra
    "file":    "file",
    "process": "process",
    "service": "service",
    # Protocolos
    "dns":     "dns",
    "tls":     "tls",
    "network": "network",
    # Rules / threats
    "rule":    "rule",
    "threat":  "threat",
}


def _is_in_ecs(path: str) -> bool:
    """``True`` si ``path`` (en cualquier formato) existe en la spec ECS.

    Acepta tanto ``"@timestamp"`` (top-level reservado de Logstash, fuera de
    la spec pero válido) como cualquier ``Field`` declarado en fields.csv.
    """
    if path == "@timestamp":
        return True
    return normalize_path(path) in _load_spec()


def _get_ecs_mapping(field_name: str) -> tuple[str, bool]:
    """Retorna ``(ecs_path, is_ecs)`` para el nombre crudo de un campo.

    Estrategia en cascada (de más a menos confianza):

    1. **fields.csv exacto**: si el nombre ya está en forma ECS canónica
       (``user.name``, ``host.id``), uso directo. Cubre el caso del cliente
       que ya viene normalizado.
    2. **field_mappings.json -> exact_mappings**: mapeos curados a mano para
       aliases comunes (``src_ip``, ``txn_id``, ``msg``). Es la lista
       authoritative del proyecto.
    3. **snake_case → dot contra fields.csv**: ``transaction_id`` →
       ``transaction.id`` (sí existe en ECS), ``log_level`` → ``log.level``.
       Domain-agnostic: funciona para finanzas, telco, logística, etc.
    4. **field_mappings.json -> pattern_mappings**: regex curadas
       (``^.*_id$`` → ``event.id``, ``^src_.*$`` → ``source.{field}``).
    5. **labels.{field}**: fallback para campos genuinamente custom.

    ``is_ecs`` refleja si el ``ecs_path`` resultante está en la spec oficial
    — el frontend lo usa para mostrar badge "ECS oficial" vs "Custom". Nota:
    `main.generate_filter_endpoint` lo recalcula con ``classify_field`` post
    LLM, pero acá lo seteamos correcto desde el deterministic path para que
    el shape del response sea consistente.
    """
    name_lower = field_name.lower()
    spec = _load_spec()
    mappings = _load_mappings()

    # 1. fields.csv exacto (cliente ya normalizó).
    info = classify_field(field_name)
    if info["is_ecs"]:
        return info["normalized"], True

    # 2. Mapeos exactos curados (alias del dominio).
    exact = mappings.get("exact_mappings", {})
    if name_lower in exact:
        ecs_path = exact[name_lower]["ecs_path"]
        return ecs_path, _is_in_ecs(ecs_path)

    # 3. snake_case → dot, brute-force sobre TODOS los separadores posibles.
    # Domain-agnostic. Para un campo con n segmentos hay n-1 boundaries y
    # cada uno puede ser `.` o `_` → 2^(n-1) combinaciones. Devolvemos la
    # primera que esté en la spec ECS, en orden lexicográfico (todos `.`
    # primero, todos `_` último), que tiende a preferir la forma "más
    # jerárquica" cuando hay ambigüedad.
    #
    # Cubre tres familias de naming que aparecen en logs reales:
    #   - `transaction_id`           → `transaction.id`           (Field.leaf)
    #   - `http_response_status_code`→ `http.response.status_code`(leaf con `_`)
    #   - `user_agent_name`          → `user_agent.name`          (Field_Set con `_`)
    if "_" in name_lower:
        parts = name_lower.split("_")
        n = len(parts)
        if n >= 2:
            for sep_bits in range(2 ** (n - 1)):
                # Bit i = 1 → separador i es `.`; 0 → `_`. Iteramos de 2^(n-1)-1
                # hacia 0 para que la primera combinación sea todos `.` (más
                # jerárquica), que es la que más probablemente está en ECS.
                bits = (2 ** (n - 1) - 1) - sep_bits
                segments = [parts[0]]
                for i in range(n - 1):
                    segments.append("." if (bits >> (n - 2 - i)) & 1 else "_")
                    segments.append(parts[i + 1])
                candidate = "".join(segments)
                # Saltamos la forma all-`_` (la original, ya descartada en step 1).
                if "." not in candidate:
                    continue
                if candidate in spec:
                    return candidate, True

    # 4. Prefix-alias matching para nombres CONDENSADOS sin separator.
    # Convenciones tipo `srcip`, `dstport`, `clientip`, `hostname`,
    # `processname`, `filepath`, `tlsversion`, `urldomain` — cualquier log
    # donde el dev fusionó namespace ECS con suffix sin guion bajo. La
    # snake_case→dot brute-force del step 3 no dispara porque no hay `_`.
    # Acá probamos cada prefijo ECS canónico: si `<canonical>.<suffix>`
    # existe en la spec, lo usamos.
    #
    # Genérico, no por vendor: el log puede ser firewall, web, EDR/audit,
    # TLS, DNS, lo que sea. Si el sufijo no existe en ECS para ese namespace
    # (ej. `srcintf` → `source.intf` no es canonical), cae al step 5/6.
    for prefix, canonical in _PREFIX_ALIASES.items():
        if name_lower.startswith(prefix) and len(name_lower) > len(prefix):
            candidate = f"{canonical}.{name_lower[len(prefix):]}"
            if candidate in spec:
                return candidate, True

    # 5. Patrones genéricos del JSON (sufijos/prefijos típicos).
    for pattern_map in mappings.get("pattern_mappings", []):
        pattern = pattern_map["pattern"]
        if re.match(pattern, name_lower):
            ecs_path = pattern_map["ecs_path"]
            if "{field}" in ecs_path:
                ecs_path = ecs_path.replace("{field}", field_name)
            return ecs_path, _is_in_ecs(ecs_path)

    # 6. Fallback: campo custom bajo `labels.*`.
    return f"labels.{field_name}", False


# Patrón para detectar timestamp ISO 8601 al inicio de la línea (formato
# típico en logs estructurados: "2026-05-19T10:15:30.123Z host=..."). Si se
# encuentra, se extrae primero con grok y se pasa por filtro `date` para
# preservar el tiempo del evento en @timestamp (en vez de quedar con el
# tiempo de ingesta de Logstash).
_ISO_TIMESTAMP_PREFIX = re.compile(
    r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?'
)


def _has_leading_timestamp(line: str) -> bool:
    """True si la línea empieza con timestamp ISO 8601 parseable por Logstash."""
    return bool(_ISO_TIMESTAMP_PREFIX.match(line.strip()))


def _generate_pipe_separated_filter(
    line: str, namespace: str = "data", ecs_overlay: bool = False
) -> dict[str, Any]:
    """Genera filter para logs pipe-separated (campo=valor|...) sin LLM.

    Si la línea trae un envelope (prefijo no-kv como `[PEND] ... [comp] `
    antes del primer `word=`), lo separamos para parsear solo el payload kv
    y emitir un grok que lo stripee en runtime.
    """
    envelope = _has_envelope(line)
    if envelope:
        m = re.search(r'[\w.\-]+=', line)
        kv_payload = line[m.start():] if m else line
    else:
        kv_payload = line
    parts = [p.strip() for p in kv_payload.split("|") if p.strip()]
    return _generate_kv_filter(
        parts, "pipe", namespace=namespace, ecs_overlay=ecs_overlay,
        envelope=envelope,
    )


def _generate_space_kv_filter(
    line: str, namespace: str = "data", ecs_overlay: bool = False
) -> dict[str, Any]:
    """Genera filter para logs key=value separados por espacios.

    Si la línea arranca con un timestamp ISO 8601 (típico en logs de pago,
    auth, etc.), lo separamos del resto: el timestamp queda fuera del `kv`
    y se procesa con `date` para que @timestamp refleje el evento real, no
    el momento en que Logstash lo ingirió.
    """
    has_ts = _has_leading_timestamp(line)
    if has_ts:
        rest = _ISO_TIMESTAMP_PREFIX.sub("", line, count=1).strip()
    else:
        rest = line
    parts = re.findall(r'(\w+=[^\s]+|\w+="[^"]+"|\w+)', rest)
    return _generate_kv_filter(
        parts, "space", has_timestamp=has_ts,
        namespace=namespace, ecs_overlay=ecs_overlay,
    )


def _flatten_json_paths(data, segments: list[str] | None = None):
    """Generador recursivo: yield ``(dotted_path, value, segments)`` por hoja.

    Para arrays sólo emite el primer elemento (decisión consensuada con el
    operador: la mayoría de los logs auth/audit tienen arrays de 1 elemento).
    Esto deja la posición ``[0]`` explícita en ``segments`` para que el rename
    de Logstash pueda referenciarlo como ``[users][0][user]``.
    """
    if segments is None:
        segments = []
    if isinstance(data, dict):
        for k, v in data.items():
            yield from _flatten_json_paths(v, segments + [str(k)])
    elif isinstance(data, list):
        if data:
            yield from _flatten_json_paths(data[0], segments + ["0"])
        # arrays vacíos: no yield, skip silenciosamente.
    else:
        dotted = ".".join(segments)
        yield (dotted, data, segments)


def _map_json_path_to_ecs(full_path: str, value, segments: list[str]) -> tuple[str, str, bool]:
    """Resuelve un path JSON anidado a un path ECS. Devuelve ``(ecs_path, ftype, is_ecs)``.

    Cascada de heurísticas, en orden de especificidad:

    1. **Timestamp wrappers** (``$date``, ``$dateNumberLong``, leaf ``timestamp``):
       devuelve ``"@timestamp"``. El caller emite un ``date`` filter
       apuntando al path Logstash original.

    2. **MongoDB local/remote**: ``local.*`` → ``server.*`` y ``remote.*`` →
       ``source.*``. En logs MongoDB ``local`` es la dirección donde
       escucha el mongod y ``remote`` la del cliente que conectó —
       semánticamente equivalente a ECS server/source.

    3. **Arrays auth de 1 elemento** (``users[0].user``, ``roles[0].role``):
       mapeos típicos a ``user.name``, ``user.domain``, ``user.roles``.

    4. **MongoDB-specific top-level** (``atype``, ``param.command``,
       ``param.ns``, ``result``): mapeos curados para auditoría MongoDB.

    5. **ECS dot-lookup directo**: el path completo (``user_agent.name``,
       ``http.response.status_code``) puede estar en la spec.

    6. **Leaf-name lookup** vía ``_get_ecs_mapping``: cuando solo el último
       segmento da pista (ej. ``cosa.extra.timestamp`` → ``@timestamp``).

    7. **Fallback**: ``labels.<full_path>``.
    """
    leaf = segments[-1] if segments else ""
    leaf_lower = leaf.lower()

    # 1. Timestamp wrappers
    if leaf in ("$date", "$dateNumberLong", "$timestamp"):
        return "@timestamp", "date", True
    if leaf_lower in ("timestamp", "@timestamp", "ts") and not isinstance(value, (dict, list)):
        # Solo si es hoja escalar; ``ts: {$date: ...}`` se maneja en (1).
        return "@timestamp", "date", True

    # 2. MongoDB local/remote → server/source. Si el sub-path mapeado
    # existe en ECS (server.ip, source.port, etc.), usar el tipo ECS
    # canónico para evitar mismatches.
    if len(segments) >= 2:
        first = segments[0].lower()
        if first in ("local", "remote"):
            tail = ".".join(segments[1:])
            mapped = f"server.{tail}" if first == "local" else f"source.{tail}"
            info_m = classify_field(mapped)
            ftype = info_m.get("ecs_type") if info_m["is_ecs"] else _infer_field_type(leaf, str(value))
            return mapped, ftype, info_m["is_ecs"]

    # 3. Arrays auth (users[0], roles[0])
    if len(segments) >= 3 and segments[1] == "0":
        first = segments[0].lower()
        if first == "users" and leaf_lower in ("user", "username", "name"):
            return "user.name", "keyword", True
        if first == "users" and leaf_lower in ("db", "database", "domain"):
            return "user.domain", "keyword", True
        if first == "roles" and leaf_lower in ("role", "name"):
            return "user.roles", "keyword", True
        if first == "roles" and leaf_lower in ("db", "database", "domain"):
            return "user.domain", "keyword", True

    # 4. MongoDB-specific top-level. Mapeos curados a paths ECS oficiales.
    # NOTA: estos asignan tipos que YA coinciden con la spec ECS 8.11 — no
    # introducir un mapping con type-mismatch (causaría rechazo de docs en
    # OpenSearch al mezclar con otras fuentes). Antes había `result →
    # event.code, integer` pero event.code es keyword en ECS, así que se
    # removió y `result` ahora cae al fallback `labels.result` (custom,
    # free-typed). El operador puede renombrarlo a `event.outcome` desde
    # el step 2 del wizard si quiere mapear 0/1 → success/failure.
    if full_path == "atype":
        return "event.action", "keyword", True
    if full_path == "param.command":
        return "event.type", "keyword", True
    if full_path == "param.ns":
        return "event.dataset", "keyword", True

    # 5. ECS dot-lookup directo (full_path completo)
    info = classify_field(full_path)
    if info["is_ecs"]:
        # Tipo canónico ECS prevalece sobre el inferido del valor — evita
        # warnings de type-mismatch en el wizard y rechazo de docs por
        # mapping conflict en OpenSearch (ver _ECS_TO_LS_CONVERT).
        return info["normalized"], info.get("ecs_type") or _infer_field_type(leaf, str(value)), True

    # 6. Leaf-name lookup
    ecs_path, is_ecs = _get_ecs_mapping(leaf)
    if is_ecs:
        official = classify_field(ecs_path).get("ecs_type")
        return ecs_path, official or _infer_field_type(leaf, str(value)), True

    # 7. Fallback: labels (no ECS, type inferred del valor)
    return f"labels.{full_path}", _infer_field_type(leaf, str(value)), False


def _logstash_path(segments: list[str]) -> str:
    """Convierte ['ts', '$date'] en '[ts][$date]'. Logstash field reference."""
    return "".join(f"[{s}]" for s in segments)


def _generate_json_filter(
    line: str, namespace: str = "data", ecs_overlay: bool = False
) -> dict[str, Any]:
    """Genera filter NAMESPACED para logs JSON.

    Usa ``json { source => "message" target => "<ns>" }`` para meter toda la
    estructura anidada bajo el namespace, conservando los nombres y tipos
    nativos del JSON (numbers siguen numbers, no hace falta convert). Enumera
    las hojas para poblar la lista de campos del step 2. ECS queda como overlay
    opcional, no se renombra nada.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    ns = (namespace or "data").strip() or "data"
    leaves = list(_flatten_json_paths(data))

    # Campo de tiempo del evento (si el JSON lo trae, ej. "@timestamp",
    # "timestamp", "time"): lo promovemos al @timestamp real vía un date filter.
    # Sin esto, el evento queda con la hora de INGESTA y el del log se pierde
    # como `<ns>.@timestamp` (un string más). `segments` nos da la ruta exacta
    # para el field reference de Logstash (`[ns][seg1][seg2]…`).
    ts_pairs = [(full_path, str(value)) for full_path, value, _ in leaves]
    ts_field = _pick_timestamp_field(ts_pairs)
    ts_segments = next((seg for fp, _v, seg in leaves if fp == ts_field), None)

    fields: list[dict] = []
    for full_path, value, _segments in leaves:
        # El campo de tiempo se tipa `date` (también en el index template);
        # el resto sale del inferidor por nombre/valor.
        ftype = "date" if full_path == ts_field else _infer_field_type(full_path, str(value))
        field_path = f"{ns}.{full_path}"
        fields.append({
            "raw_name": full_path,
            "field_path": field_path,
            "ecs_path": field_path,
            "type": ftype,
            "business_label": full_path.replace("_", " ").replace(".", " · ").title(),
            "unit": None,
            "dimension": is_dimension(field_path, ftype),
            "role": infer_role(field_path, ftype),
            "is_ecs": False,
            "ecs_type_official": None,
            "normalized_path": field_path,
            "ecs_overlay_path": None,
        })

    filter_lines = ['filter {']
    filter_lines.extend([
        '  json {',
        '    source => "message"',
        f'    target => "{ns}"',
        '  }',
        '',
        '  mutate {',
        '    remove_field => ["message"]',
        '  }',
    ])

    # Promover el timestamp del log a @timestamp (si lo hay).
    if ts_field and ts_segments:
        ts_ref = "".join(f"[{seg}]" for seg in [ns, *ts_segments])
        date_patterns_str = ", ".join(f'"{p}"' for p in _TS_DATE_PATTERNS)
        filter_lines.extend([
            '',
            '  date {',
            f'    match => ["{ts_ref}", {date_patterns_str}]',
            '    target => "@timestamp"',
            '  }',
        ])

    # Overlay ECS opcional sobre las hojas top-level (los nombres planos).
    if ecs_overlay:
        overlay_lines, overlay_map = _ecs_overlay_directives(
            [f["raw_name"] for f in fields], ns
        )
        if overlay_lines:
            filter_lines.append('')
            filter_lines.extend(overlay_lines)
            for f in fields:
                if f["raw_name"] in overlay_map:
                    f["ecs_overlay_path"] = overlay_map[f["raw_name"]]

    filter_lines.append('}')
    return {"filter_code": "\n".join(filter_lines), "fields": fields}


# Tipado inteligente POR NOMBRE. Solo convertimos a numérico los campos cuyo
# nombre implica una MEDIDA agregable (monto, latencia, bytes, conteo…). IDs,
# códigos, timestamps, cuentas, tarjetas, versiones, flags, etc. quedan keyword
# aunque el valor parezca numérico: convertirlos pierde ceros a la izquierda
# (ej. "000") y genera conflictos de tipo en OpenSearch cuando otra línea trae
# un valor no-numérico (ej. trxl_sw_version=49944 vs 1.13.5_fix_DHWS).
_MEASURE_NAME = re.compile(
    r'(amount|importe|monto|total|subtotal|sum|balance|saldo|price|precio|'
    r'fee|cost|costo|qty|quantity|cantidad|count|bytes|size|tamano|'
    r'latency|elapsed|duration|duracion|score|rate|percent|pct)',
    re.I,
)
_NEVER_NUMERIC = re.compile(
    r'(id|code|codigo|version|account|cuenta|card|tarjeta|seq|status|resp|'
    r'flag|type|typ|num|number|numero|port|tim|time|date|fecha|hora|stamp|'
    r'year|month|day|hour|phone|dni|cuit|cuil|zip|postal|pin|hash|token)',
    re.I,
)


def _should_convert_numeric(name: str, inferred_type: str) -> bool:
    """True si conviene convertir el campo a numérico (es una medida agregable
    y su nombre no cae en la denylist de IDs/códigos/timestamps)."""
    if inferred_type not in ("integer", "float"):
        return False
    if _NEVER_NUMERIC.search(name):
        return False
    return bool(_MEASURE_NAME.search(name))


# Detección del campo de tiempo del evento para @timestamp. Nombre tipo
# timestamp + valor parseable (ISO, epoch, o dígitos compactos).
_TS_NAME = re.compile(
    r'(timestamp|^ts$|_ts$|_tim$|entry_tim|event_?time|datetime|_dt$|fecha_?hora)',
    re.I,
)
# Patterns de date para @timestamp — incluye el compacto yyyyMMddHHmmssSSS
# (típico de cores financieros) que DATE_PATTERNS_COMMON no cubre.
_TS_DATE_PATTERNS = [
    "yyyyMMddHHmmssSSS", "yyyyMMddHHmmss",
    "ISO8601",
    "yyyy-MM-dd HH:mm:ss.SSS", "yyyy-MM-dd HH:mm:ss",
    "UNIX_MS", "UNIX",
]


def _looks_like_timestamp_value(v: str) -> bool:
    vs = v.strip()
    return bool(
        re.match(r'^\d{8,}$', vs)            # epoch / compacto (>=8 dígitos)
        or re.match(r'^\d{4}-\d{2}-\d{2}', vs)  # ISO date
        or _has_leading_timestamp(vs)
    )


def _pick_timestamp_field(kv_pairs: list[tuple[str, str]]) -> str | None:
    """De los pares (key, value), elige el campo a usar para @timestamp.

    Prefiere nombres con 'entry'/'event' (el momento del evento) sobre otros
    timestamps (write/exit). Si ninguno califica, devuelve None y @timestamp
    queda en ingest time (el cliente lo ajusta como regla de negocio).
    """
    candidates = [
        k for k, v in kv_pairs
        if v and v.strip().lower() != "null"
        and _TS_NAME.search(k) and _looks_like_timestamp_value(v)
    ]
    if not candidates:
        return None
    for pref in ("entry", "event"):
        for c in candidates:
            if pref in c.lower():
                return c
    return candidates[0]


def _has_envelope(line: str) -> bool:
    """True si la línea tiene un prefijo NO-kv antes del payload key=value
    (ej. `[PEND] ... [comp] table=...|...`). Se detecta si la línea no
    empieza directamente con un token `key=`."""
    return not re.match(r'^\s*[\w.\-]+=', line)


def _generate_kv_filter(
    parts: list[str],
    format_type: str,
    has_timestamp: bool = False,
    namespace: str = "data",
    ecs_overlay: bool = False,
    envelope: bool = False,
) -> dict[str, Any]:
    """Genera filter key-value en modo NAMESPACED.

    En vez de renombrar cada campo a su path ECS, mete TODOS los campos bajo
    ``target => "<namespace>"`` conservando los nombres originales del log.
    El parsing es inteligente (kv/json + type convert inferido + manejo de
    timestamps) pero NO impone ECS: la mayoría de los logs propietarios tienen
    semántica de dominio que ECS oscurece. ECS queda como overlay OPCIONAL
    (``ecs_overlay=True``), que copia un puñado de campos estándar a sus paths
    ECS sin tocar el resto del evento.

    Parameters
    ----------
    parts
        Lista de strings ``"key=value"`` ya extraídos del log.
    format_type
        ``"pipe"`` | ``"space"`` | ``"json"``. Determina el plugin inicial.
    has_timestamp
        Si la línea arrancaba con timestamp ISO 8601, se anteponen
        ``grok`` + ``date`` para normalizarlo a ``@timestamp``.
    namespace
        Parent bajo el cual viven todos los campos (``<ns>.<campo>``).
    ecs_overlay
        Si True, se anexan directivas que copian campos estándar a ECS.
    """
    ns = (namespace or "data").strip() or "data"
    fields: list[dict[str, Any]] = []

    # Recolectamos los pares (key, value) para: detectar el par date+time y
    # elegir el campo de @timestamp.
    kv_pairs: list[tuple[str, str]] = []
    for part in parts:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        kv_pairs.append((k.strip(), v.strip().strip('"')))
    parsed_keys = {k for k, _ in kv_pairs}
    has_date_time_pair = "date" in parsed_keys and "time" in parsed_keys

    # Campo de tiempo del evento (si lo hay) → alimenta @timestamp vía date.
    ts_field = None if has_date_time_pair else _pick_timestamp_field(kv_pairs)

    for key, val in kv_pairs:
        if not key or len(key) > 50:
            continue

        # El par date+time se consume aparte (date filter) → no se lista como
        # campo plano.
        if has_date_time_pair and key in ("date", "time"):
            continue

        inferred = _infer_field_type(key, val)
        convert_to = _should_convert_numeric(key, inferred)

        # Tipo MOSTRADO = el efectivo que se va a indexar. Solo las medidas
        # van numéricas; el campo de tiempo va date; el resto queda keyword
        # (string) salvo ip, que dejamos informativo aunque no se convierta.
        if convert_to:
            ftype = inferred
        elif key == ts_field:
            ftype = "date"
        elif inferred == "ip":
            ftype = "ip"
        else:
            ftype = "string"

        label = key.replace("_", " ").title()
        field_path = f"{ns}.{key}"

        # El `type` alimenta el INDEX TEMPLATE de OpenSearch (la fuente de
        # verdad de tipos), no un `mutate convert` en Logstash. Logstash solo
        # parsea; el template tipa (ver index_template.build_index_template).
        fields.append({
            "raw_name": key,
            "field_path": field_path,
            "ecs_path": field_path,          # back-compat: el path real del campo
            "type": ftype,
            "business_label": label,
            "unit": None,
            "dimension": is_dimension(field_path, ftype),
            "role": infer_role(field_path, ftype),
            "is_ecs": False,
            "ecs_type_official": None,
            "normalized_path": field_path,
            "ecs_overlay_path": None,
        })

    filter_lines = ['filter {']

    # Parse inicial: kv/json con `target => "<ns>"` → todos los campos quedan
    # bajo el namespace conservando sus nombres originales.
    if has_timestamp and format_type == "space":
        # Timestamp ISO al inicio: lo extraemos con grok ANTES del kv para
        # preservar el evento real en @timestamp.
        date_patterns_str = ", ".join(f'"{p}"' for p in DATE_PATTERNS_COMMON)
        filter_lines.extend([
            '  grok {',
            '    match => { "message" => "%{TIMESTAMP_ISO8601:event_timestamp} %{GREEDYDATA:kv_data}" }',
            '  }',
            '  date {',
            f'    match => ["event_timestamp", {date_patterns_str}]',
            '    target => "@timestamp"',
            '    remove_field => ["event_timestamp"]',
            '  }',
            '  kv {',
            '    source => "kv_data"',
            '    field_split => " "',
            '    value_split => "="',
            f'    target => "{ns}"',
            '  }',
            '',
        ])
    elif format_type == "json":
        filter_lines.extend([
            '  json {',
            '    source => "message"',
            f'    target => "{ns}"',
            '  }',
            '',
        ])
    elif envelope:
        # La línea trae un prefijo NO-kv (envelope: status, timestamp, thread,
        # componente…) antes del payload key=value. Lo separamos con grok para
        # que el kv no se coma basura: `log_prefix` captura el prefijo mínimo y
        # `kv_payload` arranca en el primer `word=`.
        fs = "|" if format_type == "pipe" else " "
        filter_lines.extend([
            '  grok {',
            r'    match => { "message" => "^(?<log_prefix>.*?)(?<kv_payload>\b[\w.\-]+=.*)$" }',
            '  }',
            '  kv {',
            '    source => "kv_payload"',
            f'    field_split => "{fs}"',
            '    value_split => "="',
            f'    target => "{ns}"',
            '  }',
            '',
        ])
    else:
        filter_lines.extend([
            '  kv {',
            '    source => "message"',
            f'    field_split => "{"|" if format_type == "pipe" else " "}"',
            '    value_split => "="',
            f'    target => "{ns}"',
            '  }',
            '',
        ])

    # Higiene de nulls (baseline): borra los campos del namespace cuyo valor es
    # el literal "null", vacío o whitespace. Sin esto, un `amount=null` (string)
    # contra un campo `double` del index template tiraba mapper_parsing_exception
    # y OpenSearch rechazaba el documento ENTERO. Los logs reales siempre traen
    # nulls; esto los saca antes de indexar (el `ignore_malformed` del template es
    # el segundo cinturón para basura no-null).
    filter_lines.extend([
        '  ruby {',
        "    code => '",
        f'      ns_h = event.get("{ns}")',
        '      if ns_h.is_a?(Hash)',
        '        ns_h.delete_if { |k, v| v.nil? || v == "null" || (v.is_a?(String) && v.strip.empty?) }',
        f'        event.set("{ns}", ns_h)',
        '      end',
        "    '",
        '  }',
        '',
    ])

    # Campo de tiempo del evento → @timestamp (no lo removemos: queda también
    # bajo el namespace para consulta). El cliente puede ajustar el field/pattern
    # como regla de negocio si la heurística eligió otro timestamp.
    if ts_field:
        ts_patterns_str = ", ".join(f'"{p}"' for p in _TS_DATE_PATTERNS)
        filter_lines.extend([
            '  date {',
            f'    match => ["[{ns}][{ts_field}]", {ts_patterns_str}]',
            '    target => "@timestamp"',
            '    timezone => "UTC"',
            '    tag_on_failure => ["_dateparsefailure_event_ts"]',
            '  }',
            '',
        ])

    # Par `date`+`time` separados → @timestamp. Los campos viven bajo el
    # namespace, así que las referencias son `%{[<ns>][date]}`.
    if has_date_time_pair:
        filter_lines.extend([
            '  mutate {',
            f'    add_field => {{ "[@metadata][event_dt]" => "%{{[{ns}][date]}} %{{[{ns}][time]}}" }}',
            '  }',
            '  date {',
            '    match => ["[@metadata][event_dt]",',
            '              "yyyy-MM-dd HH:mm:ss",',
            '              "yyyy/MM/dd HH:mm:ss",',
            '              "dd-MM-yyyy HH:mm:ss"]',
            '    target => "@timestamp"',
            f'    remove_field => ["[{ns}][date]", "[{ns}][time]"]',
            '  }',
            '',
        ])

    # NOTA: el tipado (convert) ya NO vive en Logstash. Los tipos se aplican
    # vía el INDEX TEMPLATE de OpenSearch (index_template.build_index_template),
    # que es la fuente de verdad. Logstash solo parsea/estructura.

    # Limpiar el `message` crudo (ya parseado al namespace) y los temporales
    # del grok (kv_data del path space+ts, kv_payload del envelope).
    remove_fields_list = ["message"]
    if has_timestamp and format_type == "space":
        remove_fields_list.append("kv_data")
    if envelope:
        remove_fields_list.append("kv_payload")
    rf = ", ".join(f'"{x}"' for x in remove_fields_list)
    filter_lines.append('  mutate {')
    filter_lines.append(f'    remove_field => [{rf}]')
    filter_lines.append('  }')

    # Overlay ECS opcional: copia campos estándar a sus paths ECS sin tocar
    # el resto del evento (que sigue bajo `<ns>.*`).
    if ecs_overlay:
        overlay_lines, overlay_map = _ecs_overlay_directives(
            [f["raw_name"] for f in fields], ns
        )
        if overlay_lines:
            filter_lines.append('')
            filter_lines.extend(overlay_lines)
            for f in fields:
                if f["raw_name"] in overlay_map:
                    f["ecs_overlay_path"] = overlay_map[f["raw_name"]]

    filter_lines.append('}')
    return {"filter_code": "\n".join(filter_lines), "fields": fields}


# Set curado de paths ECS que valen la pena como overlay: campos estándar
# que sirven para correlación/SIEM. Deliberadamente chico — el overlay copia
# solo estos; el resto del evento queda fiel al dominio bajo el namespace.
_ECS_OVERLAY_WHITELIST = {
    "@timestamp", "event.outcome", "event.action", "event.code",
    "event.duration", "user.id", "user.name", "source.ip",
    "destination.ip", "host.name", "geo.location",
    "http.response.status_code", "network.bytes",
}


def _ecs_overlay_directives(
    raw_names: list[str], ns: str
) -> tuple[list[str], dict[str, str]]:
    """Devuelve ``(lines, overlay_map)`` para copiar campos estándar a ECS.

    Solo campos cuyo ``_get_ecs_mapping`` da un path ECS oficial Y está en
    ``_ECS_OVERLAY_WHITELIST``. El resto del evento queda intacto bajo
    ``<ns>.*``. ``overlay_map`` = ``{raw_name: ecs_path}`` para que el
    frontend muestre el chip "→ user.id".
    """
    copies: list[tuple[str, str, str]] = []  # (src_path, ecs_bracket, ecs_dotted)
    ts_field: str | None = None
    overlay_map: dict[str, str] = {}
    seen_targets: set[str] = set()

    for raw in raw_names:
        ecs_path, is_ecs = _get_ecs_mapping(raw)
        if not is_ecs or ecs_path not in _ECS_OVERLAY_WHITELIST:
            continue
        if ecs_path in seen_targets:
            continue  # primer campo gana; evitamos colisiones de target
        seen_targets.add(ecs_path)
        overlay_map[raw] = ecs_path
        if ecs_path == "@timestamp":
            ts_field = raw
            continue
        src = f"[{ns}][{raw}]"
        dst = "[" + ecs_path.replace(".", "][") + "]"
        copies.append((src, dst, ecs_path))

    if not copies and not ts_field:
        return [], {}

    lines = ['  # ── Overlay ECS (campos estándar, opcional) ──']
    if copies:
        lines.append('  mutate {')
        lines.append('    copy => {')
        for src, dst, _ in copies:
            lines.append(f'      "{src}" => "{dst}"')
        lines.append('    }')
        conv: dict[str, str] = {}
        for _, dst, ecs_path in copies:
            cd = _logstash_convert_for_ecs(ecs_path)
            if cd:
                conv[dst] = cd
        if conv:
            lines.append('    convert => {')
            for k, v in sorted(conv.items()):
                lines.append(f'      "{k}" => "{v}"')
            lines.append('    }')
        lines.append('  }')
    if ts_field:
        date_patterns_str = ", ".join(f'"{p}"' for p in DATE_PATTERNS_COMMON)
        lines.append('  date {')
        lines.append(f'    match => ["[{ns}][{ts_field}]", {date_patterns_str}]')
        lines.append('    target => "@timestamp"')
        lines.append('  }')
    return lines, overlay_map


def _detect_log_format(line: str) -> str:
    """Detecta el formato del log."""
    line = line.strip()
    
    # JSON
    if line.startswith("{") and line.endswith("}"):
        return "json"
    
    # Pipe-separated con muchos campos
    if "|" in line and line.count("=") >= 5:
        return "pipe"
    
    # Key-value con espacios
    if "=" in line and line.count("=") >= 3 and " " in line:
        return "space_kv"
    
    # Desconocido - usar LLM
    return "unknown"


def _regenerate_with_feedback(
    sample_log: str, namespace: str, ecs_overlay: bool,
    feedback: str, previous_filter: str,
    input_type: str = "",
) -> dict[str, Any]:
    """Corrige un filter que falló la validación en el sandbox.

    Va directo al LLM (sin parsers determinísticos) con el filter anterior y
    el error observado, pidiendo la versión corregida en el mismo shape
    ``{filter_code, fields}`` que el generador normal.
    """
    client = _build_client()
    model = get_pipeline_model()
    ns = (namespace or "data").strip() or "data"
    is_jdbc = (input_type or "").strip().lower() == "jdbc"
    base_prompt = SYSTEM_PROMPT_JDBC if is_jdbc else SYSTEM_PROMPT_BASE
    system_prompt = (
        base_prompt
        .replace("{plugin_context}", get_plugin_context(sample_log))
        .replace("{namespace}", ns)
    )
    prev_block = (
        f"FILTER ANTERIOR (falló la validación):\n{previous_filter}\n\n"
        if previous_filter else ""
    )
    user_content = (
        "El filter generado para este log FALLÓ al validarlo contra un "
        "OpenSearch real. Corregilo y devolvé el JSON {filter_code, fields} "
        f"completo (no un diff). Usá `{ns}` como namespace.\n\n"
        f"{prev_block}"
        f"RESULTADO DE LA VALIDACIÓN (corregí esto):\n{feedback}\n\n"
        f"LOG DE MUESTRA:\n{sample_log[:4000]}"
    )
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            timeout=600,
            extra_body=_thinking_param(),
        )
    except OpenAIError as exc:
        raise RuntimeError(f"Error al invocar el modelo MaaS: {exc}") from exc

    content = response.choices[0].message.content or ""
    if not content.strip():
        raise RuntimeError("El modelo MaaS devolvió una respuesta vacía.")
    try:
        parsed = _parse_llm_json(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"El modelo MaaS devolvió un JSON inválido: {exc}.") from exc

    filter_code = parsed.get("filter_code")
    if not isinstance(filter_code, str) or not filter_code.strip():
        raise RuntimeError("El JSON del modelo no incluye un `filter_code` válido.")
    fields = parsed.get("fields", [])
    if not isinstance(fields, list):
        fields = []
    normalized_fields = []
    for f in fields:
        if not isinstance(f, dict):
            continue
        raw = f.get("raw_name") or ""
        fpath = f.get("field_path") or f.get("ecs_path") or (f"{ns}.{raw}" if raw else "")
        ftype = f.get("type") or "string"
        normalized_fields.append({
            "raw_name": raw,
            "field_path": fpath,
            "ecs_path": fpath,
            "type": ftype,
            "business_label": f.get("business_label") or raw,
            "unit": f.get("unit"),
            "dimension": is_dimension(fpath or raw, ftype, f.get("dimension")),
            "role": infer_role(fpath or raw, ftype, f.get("role")),
            "is_ecs": False,
            "ecs_type_official": None,
            "normalized_path": fpath,
            "ecs_overlay_path": None,
        })
    return {
        "filter_code": strip_logstash_comments(filter_code).strip(),
        "fields": normalized_fields,
        "multiline_hint": detect_multiline(sample_log.split("\n")),
    }


def generate_logstash_filter(
    sample_log: str, namespace: str = "data", ecs_overlay: bool = False,
    feedback: str = "", previous_filter: str = "",
    input_type: str = "",
) -> dict[str, Any]:
    """Genera el filter de Logstash + lista de campos detectados.

    Detecta el formato del log y usa parser determinístico cuando es posible.
    Solo llama al LLM para formatos no reconocidos.

    El esquema de salida es NAMESPACED por default: los campos viven bajo
    ``<namespace>.<campo>`` conservando sus nombres originales, sin imponer
    ECS. ``ecs_overlay=True`` agrega encima un puñado de campos ECS estándar.

    Parameters
    ----------
    sample_log:
        Línea (o líneas) de log ya anonimizada(s) por el cliente.
    namespace:
        Parent bajo el cual viven los campos parseados (default ``"data"``).
    ecs_overlay:
        Si True, anexa directivas que copian campos estándar a paths ECS.
    feedback:
        Errores de la validación en sandbox (o notas del SA). Si viene,
        se SALTEAN los parsers determinísticos (reproducirían el mismo
        resultado) y se va directo al LLM con el filter anterior + el
        feedback para que lo corrija.
    previous_filter:
        El filter que falló la validación (acompaña a ``feedback``).

    Returns
    -------
    dict
        ``{"filter_code": "...", "fields": [...]}``.

    Raises
    ------
    ValueError
        Si ``sample_log`` está vacío.
    RuntimeError
        Si la API Key no está configurada, si la llamada al LLM falla, o si
        la respuesta no parsea como JSON con el shape esperado.
    """
    if not sample_log or not sample_log.strip():
        raise ValueError("El log de muestra no puede estar vacío.")

    stripped = sample_log.strip()
    lines = stripped.split("\n")
    first_line = lines[0] if lines else ""

    # Con feedback de validación: directo al LLM (los parsers determinísticos
    # reproducirían exactamente el filter que acaba de fallar).
    if (feedback or "").strip():
        return _regenerate_with_feedback(
            stripped, namespace=namespace, ecs_overlay=ecs_overlay,
            feedback=feedback.strip(), previous_filter=(previous_filter or "").strip(),
            input_type=input_type,
        )

    # JDBC: datos estructurados — skip parsers determinísticos, ir al LLM con
    # prompt especializado (no grok/kv/json, solo date + mutate).
    is_jdbc = (input_type or "").strip().lower() == "jdbc"

    # 0) JSON (minificado O pretty-printed): el detector básico solo mira la
    # 1ra línea, así que un JSON indentado (1ra línea = "{") no lo reconocía y
    # caía al LLM. Probamos parsear el BLOB COMPLETO: un objeto JSON es
    # inequívoco (no hay heurística que adivinar), y `_generate_json_filter`
    # aplana los anidados + promueve el @timestamp del log.
    # JSONL (una línea = un objeto JSON): si cada línea empieza con {, parsear
    # solo la primera (el filter `json` es el mismo para todas).
    if not is_jdbc and lines and all(l.strip().startswith("{") for l in lines if l.strip()):
        json_result = _generate_json_filter(
            lines[0].strip(), namespace=namespace, ecs_overlay=ecs_overlay
        )
        if json_result:
            json_result["multiline_hint"] = detect_multiline(lines)
            return json_result
    elif not is_jdbc and stripped.startswith("{") and stripped.endswith("}"):
        json_result = _generate_json_filter(
            stripped, namespace=namespace, ecs_overlay=ecs_overlay
        )
        if json_result:
            json_result["multiline_hint"] = detect_multiline(lines)
            return json_result

    # 1) Catálogo de formatos especializados (Apache, Syslog, CEF, CSV).
    # Estos detectores son más específicos que los del path básico y los
    # corremos PRIMERO. Si alguno matchea, ya tenemos filter listo —
    # multiline_hint propagado por si el cliente subió un stack trace.
    if not is_jdbc:
        catalog_result = _catalog_try_match(lines)
        if catalog_result is not None:
            return catalog_result

    # 2) Formatos básicos (pipe-kv, space-kv). JSON ya se manejó arriba.
    fmt = _detect_log_format(first_line) if not is_jdbc else ""

    if fmt == "pipe":
        result = _generate_pipe_separated_filter(
            first_line, namespace=namespace, ecs_overlay=ecs_overlay
        )
        result["multiline_hint"] = detect_multiline(lines)
        return result
    if fmt == "space_kv":
        result = _generate_space_kv_filter(
            first_line, namespace=namespace, ecs_overlay=ecs_overlay
        )
        result["multiline_hint"] = detect_multiline(lines)
        return result
    if fmt == "space_kv":
        result = _generate_space_kv_filter(
            first_line, namespace=namespace, ecs_overlay=ecs_overlay
        )
        result["multiline_hint"] = detect_multiline(lines)
        return result

    # 3) Formato no reconocido: cae al LLM. Truncamos agresivo para no quemar
    # contexto: 1ra línea, capando los values de cada par k=v a 20 chars en
    # logs pipe-separated muy anchos (típico de logs financieros con 50+
    # campos) y a 1000 chars en logs estilo prosa.
    truncated_lines = []
    for line in lines[:1]:
        if "|" in line and "=" in line:
            parts = line.split("|")[:30]
            truncated_parts = []
            for part in parts:
                if "=" in part:
                    key, val = part.split("=", 1)
                    truncated_parts.append(f"{key}={val[:20]}")
                else:
                    truncated_parts.append(part[:20])
            truncated_lines.append("|".join(truncated_parts))
        else:
            truncated_lines.append(line[:1000])
    sample_log = "\n".join(truncated_lines)

    client = _build_client()
    model = get_pipeline_model()
    timeout = 600  # 10 minutos hardcodeado: glm-5.2 en CSS a veces tarda >2min.

    plugin_context = get_plugin_context(sample_log)
    ns = (namespace or "data").strip() or "data"
    base_prompt = SYSTEM_PROMPT_JDBC if is_jdbc else SYSTEM_PROMPT_BASE
    system_prompt = (
        base_prompt
        .replace("{plugin_context}", plugin_context)
        .replace("{namespace}", ns)
    )
    overlay_note = (
        "\n\nAdemás, agregá al final un overlay ECS: copiá (con `mutate copy`, "
        "sin renombrar) los campos estándar que existan (timestamp→@timestamp "
        "vía date, outcome→[event][outcome], user→[user][id], ip origen→"
        "[source][ip]) dejando el resto intacto bajo el namespace."
        if ecs_overlay else ""
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Generá el JSON {filter_code, fields} para el "
                        f"siguiente log de muestra. Usá `{ns}` como namespace "
                        f"de los campos parseados.{overlay_note}\n\n{sample_log.strip()}"
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
            timeout=timeout,
            extra_body=_thinking_param(),
        )
    except OpenAIError as exc:
        error_msg = str(exc)
        if "timed out" in error_msg.lower():
            raise RuntimeError(
                "El modelo tardó demasiado en responder. "
                "Posibles causas:\n"
                "1. El contexto es muy grande\n"
                "2. El modelo está sobrecargado\n"
                "3. Problemas de conectividad\n"
                "Intenta con un log más corto o verifica el estado del modelo."
            ) from exc
        raise RuntimeError(f"Error al invocar el modelo MaaS: {exc}") from exc

    content = response.choices[0].message.content or ""
    if not content.strip():
        raise RuntimeError("El modelo MaaS devolvió una respuesta vacía.")

    try:
        parsed = _parse_llm_json(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"El modelo MaaS devolvió un JSON inválido: {exc}. "
            f"Primeros 200 chars de la respuesta: {content[:200]}"
        ) from exc

    filter_code = parsed.get("filter_code")
    fields = parsed.get("fields", [])
    if not isinstance(filter_code, str) or not filter_code.strip():
        raise RuntimeError(
            "El JSON del modelo no incluye un `filter_code` válido."
        )
    if not isinstance(fields, list):
        fields = []

    # Normalización al shape namespaced: el LLM devuelve `field_path` pero el
    # resto del backend/frontend espera el set completo de keys. `ecs_path` se
    # setea = field_path por back-compat (es el path real del campo); `is_ecs`
    # queda False en modo namespaced.
    normalized_fields = []
    for f in fields:
        if not isinstance(f, dict):
            continue
        raw = f.get("raw_name") or ""
        fpath = f.get("field_path") or f.get("ecs_path") or (f"{ns}.{raw}" if raw else "")
        ftype = f.get("type") or "string"
        normalized_fields.append({
            "raw_name": raw,
            "field_path": fpath,
            "ecs_path": fpath,
            "type": ftype,
            "business_label": f.get("business_label") or raw,
            "unit": f.get("unit"),
            "dimension": is_dimension(fpath or raw, ftype, f.get("dimension")),
            "role": infer_role(fpath or raw, ftype, f.get("role")),
            "is_ecs": False,
            "ecs_type_official": None,
            "normalized_path": fpath,
            "ecs_overlay_path": None,
        })

    # Sanitización: el LLM a veces emite comentarios `#` (a pesar del system
    # prompt). Los strippeamos acá. Hay que ejecutar lo mismo en
    # /generate-pipeline para cubrir paths que NO pasan por este generator
    # (cached example en el frontend, edición manual en el textarea de
    # step 5). Por eso `strip_logstash_comments` es top-level y exportada.
    filter_code = strip_logstash_comments(filter_code)

    return {
        "filter_code": filter_code.strip(),
        "fields": normalized_fields,
        "multiline_hint": detect_multiline(lines),
    }


# ---------------------------------------------------------------------------
# Synthetic log shapes (LLM-generated templates)
# ---------------------------------------------------------------------------
# Para la demo necesitamos miles de eventos con variación realista. Una sola
# call al LLM produce ~15 "shapes" distintos (success/error, distintos
# users/IPs/comandos), y después `synthetic_logs.generate_synthetic_logs`
# los multiplica con jitter de timestamp e IP. Híbrido: realismo LLM +
# escala heurística, sin gastar 40 calls.
# ---------------------------------------------------------------------------

def _clean_shape(s: str) -> str | None:
    """Normaliza una shape generada por el LLM, descartando si queda inválida.

    Casos defensivos que vimos en outputs reales del LLM (incluso con
    system prompt explícito prohibiéndolos):
      - Markdown fences ``` o ```log al inicio/final.
      - Prefijos de lista numerada (``1. ``, ``2. `` etc.).
      - Líneas que son enteramente un comentario (``# ...``).
      - Whitespace excesivo.

    Devuelve la shape saneada, o ``None`` si tras saneamiento queda vacía
    o sigue siendo solo un comentario.
    """
    if not isinstance(s, str):
        return None
    s = s.strip()
    # Strip markdown fences.
    s = re.sub(r"^```\w*\n?", "", s)
    s = re.sub(r"\n?```$", "", s)
    # Strip numbered-list prefix.
    s = re.sub(r"^\d+\.\s+", "", s)
    s = s.strip()
    if not s or s.startswith("#"):
        return None
    return s


def _shape_matches_format(shape: str, raw_log: str) -> bool:
    """Comparación rápida de "sentinels" de formato entre shape y raw.

    El LLM a veces aluciona devolviendo shapes en un formato distinto al
    sample (ej. raw es Apache → LLM devuelve JSON). Si Logstash espera
    `%{COMBINEDAPACHELOG}` y le llegan eventos JSON, falla con
    ``_grokparsefailure`` en TODOS los eventos sintéticos. Esta función
    filtra esos shapes alucinados.

    Heurística por regex, no exhaustiva — cubre los 5 formatos del
    catálogo + k=v genérico. Para formatos exóticos cae al fallback
    "similar length", que no es ideal pero evita falsos negativos.
    """
    s, r = shape.strip(), raw_log.strip()
    if not s or not r:
        return False

    # JSON: ambos arrancan con `{` y terminan con `}`.
    if r.startswith("{") and r.endswith("}"):
        return s.startswith("{") and s.endswith("}")

    # CEF: prefijo único.
    if r.startswith("CEF:"):
        return s.startswith("CEF:")

    # Syslog (5424 con `<PRI>` o 3164 con `MMM dd hh:mm`).
    syslog_rx = re.compile(r"^(?:<\d+>|\w{3}\s+\d{1,2}\s+\d{2}:\d{2})")
    if syslog_rx.match(r):
        return bool(syslog_rx.match(s))

    # Apache CLF/Combined: IPv4 al inicio.
    apache_rx = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}")
    if apache_rx.match(r):
        return bool(apache_rx.match(s))

    # Log4j-style con timestamp entre brackets: `[YYYY-MM-DD HH:MM:SS,SSS]`.
    log4j_rx = re.compile(r"^\[\d{4}-\d{2}-\d{2}")
    if log4j_rx.match(r):
        return bool(log4j_rx.match(s))

    # k=v: el raw tiene >=3 pares. La shape debe tener al menos 2.
    if r.count("=") >= 3:
        return s.count("=") >= 2

    # Fallback: longitud similar (±50%). Para formatos exóticos
    # sin patrón discernible — evita aceptar respuestas absurdamente
    # cortas o largas que probablemente son alucinación.
    return 0.5 * len(r) <= len(s) <= max(20, 1.5 * len(r))


_SHAPES_SYSTEM_PROMPT = """\
Sos un generador de logs sintéticos para demos de observabilidad.

Recibís UN log de muestra y devolvés N variantes realistas, manteniendo el
MISMO formato exacto que el sample (Apache, Syslog, JSON, k=v, prosa, etc.)
pero con valores DISTINTOS y VEROSÍMILES para el dominio:

- IPs realistas (mix de internas 10.x/192.168.x/172.16.x y públicas).
- Users con nombres humanos plausibles.
- HTTP status codes válidos (mix de 2xx/3xx/4xx/5xx).
- Comandos coherentes con el dominio (si el log es MongoDB, usar find/insert/
  update/aggregate; si es web, métodos HTTP; etc.).
- Timestamps distintos (no importa cuáles — el caller los va a redistribuir
  igual; vos solo asegurate que varíen para que las variantes se vean reales).
- Valores numéricos (montos, latencias, bytes) en rangos plausibles.

Cubrí distintos escenarios: la mayoría success, algunos warning, algunos
error. Si el log es de auditoría, mezclar acciones (login/logout/CRUD).

REGLAS estrictas:
1. MISMO formato exacto del sample. Si era JSON, devolvés JSON con los
   mismos keys. Si era Apache, devolvés líneas Apache. NO mezcles formatos.
2. Respondé EXCLUSIVAMENTE un objeto JSON con esta estructura:
   {"shapes": ["<linea1>", "<linea2>", ..., "<lineaN>"]}
3. NO uses Markdown. NO uses fences ```. NO agregues comentarios.
4. Cada string en "shapes" debe ser UNA línea exacta de log, sin saltos
   de línea internos (escapealos con \\n si el formato original los tenía).
"""


def generate_synthetic_shapes(raw_log: str, n_shapes: int = 15) -> list[str] | None:
    """Pide a glm-5.2 N variantes realistas del log de muestra.

    Returns
    -------
    list[str] | None
        Lista de ``n_shapes`` líneas en el mismo formato que ``raw_log``, o
        ``None`` si el LLM no está disponible / la respuesta no parsea /
        la lista está vacía. El caller decide qué hacer con None (típicamente
        cae a heurística pura).

    Notas de diseño:
      - ``temperature=0.8``: queremos variación real entre shapes, no la
        misma respuesta cada vez. Es DIFERENTE al filter generator que usa
        ``temperature=0`` para determinismo del filter.
      - Errores SE SUPRIMEN. Esta función es best-effort — si MaaS no anda,
        la demo sigue con heurística. Loggeamos a stdout para diagnóstico
        pero no raise.
    """
    if not raw_log or not raw_log.strip():
        return None
    if n_shapes <= 0:
        return None

    # Wrapping outer: cualquier excepción que se escape a los handlers
    # internos (IndexError por choices vacío, ValidationError del SDK al
    # deserializar respuesta rara, httpx.HTTPError, etc.) cae acá. La
    # función es best-effort por contrato: si algo sale mal, devolvemos
    # None y el caller cae a heurística pura.
    try:
        try:
            client = _build_client()
        except RuntimeError as exc:
            # MAAS_API_KEY no configurada → caer a heurística silenciosamente.
            print(f"[synthetic_shapes] LLM no disponible: {exc}")
            return None

        model = get_pipeline_model()
        # Truncar sample agresivo: el LLM solo necesita el FORMATO, no necesita
        # ver 10K caracteres. 2000 alcanza para que entienda Apache, Syslog,
        # MongoDB nested, etc.
        sample = raw_log[:2000]

        user_msg = (
            f"Sample log:\n{sample}\n\n"
            f"Generá {n_shapes} variantes realistas distintas. "
            f'Respondé JSON: {{"shapes": ["<linea1>", ..., "<linea{n_shapes}>"]}}'
        )

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SHAPES_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=0.8,
                # 120s: glm-5.2 en MaaS sostiene ~30-50 tok/s; con ~3-5K tokens
                # de output, 60s era ajustado y a veces caía por timeout. El
                # filter generator usa 600s, acá 120 es middle ground.
                timeout=120,
            )
        except OpenAIError as exc:
            print(f"[synthetic_shapes] LLM call falló: {exc}")
            return None

        # Defensive: si MaaS devuelve choices=[] (content-filter, length cutoff,
        # etc.) sería IndexError abajo. Chequeamos primero.
        if not getattr(response, "choices", None):
            print("[synthetic_shapes] LLM devolvió choices vacíos")
            return None

        content = response.choices[0].message.content or ""
        if not content.strip():
            print("[synthetic_shapes] LLM devolvió respuesta vacía")
            return None

        try:
            parsed = _parse_llm_json(content)
        except json.JSONDecodeError as exc:
            print(f"[synthetic_shapes] JSON inválido del LLM: {exc}")
            return None

        shapes = parsed.get("shapes")
        if not isinstance(shapes, list) or not shapes:
            print(f"[synthetic_shapes] respuesta sin lista 'shapes' válida: {parsed}")
            return None

        # Pipeline de saneamiento en 3 pasos:
        #   1. _clean_shape: strip de fences/comentarios/list-prefixes.
        #   2. Filtro de longitud mínima (descarta tokens basura).
        #   3. _shape_matches_format: descarta shapes en formato distinto al raw
        #      (el LLM a veces aluciona Apache → JSON, etc.).
        cleaned = []
        for s in shapes:
            c = _clean_shape(s)
            if c is None or len(c) <= 5:
                continue
            if not _shape_matches_format(c, raw_log):
                continue
            cleaned.append(c)

        if not cleaned:
            print("[synthetic_shapes] todas las shapes filtradas (alucinación de formato o saneamiento)")
            return None
        return cleaned

    except Exception as exc:
        # Safety net final. Si llegamos acá hay un bug o un error de SDK
        # que no anticipamos. Loggeamos tipo+mensaje para diagnosticar en
        # iteración futura, pero NO raise — el deploy debe seguir.
        print(f"[synthetic_shapes] error inesperado ({type(exc).__name__}): {exc}")
        return None
