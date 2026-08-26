"""
log_format_catalog.py
=====================

Catálogo de formatos de log que el path determinístico sabe parsear sin LLM.

Para cada formato hay dos funciones:
  - ``detect(line: str) -> bool``: ¿la primera línea matchea el formato?
  - ``generate(lines: list[str]) -> dict``: filter_code + fields + hints.

La detección se hace en orden de especificidad descendente (CEF antes que
space_kv, etc.). El primer match gana.

Diseño:
  - El generador devuelve siempre ``{"filter_code": str, "fields": list,
    "multiline_hint": bool}``. Si el log parecía multilinea (líneas 2+ sin
    timestamp en cabeza), seteamos ``multiline_hint=True`` para que el
    frontend recomiende codec `multiline` en el nodo Input.
  - El mapeo a ECS de los fields nombrados se delega a `_get_ecs_mapping`
    del módulo maas_integrator (lo importamos lazy para evitar ciclo).
  - Los grok patterns que usamos (COMBINEDAPACHELOG, SYSLOGLINE, etc.) son
    los built-in de Logstash 7.10+; no requieren patterns_dir custom.

Cobertura objetivo: ~95% de logs reales de cliente en demos
(web, security, sistemas, transaccionales). Resto cae al LLM.
"""

from __future__ import annotations

import re
from typing import Callable, NamedTuple


# ---------------------------------------------------------------------------
# Multi-timestamp: el `date` filter de Logstash acepta lista de patterns y
# usa el primero que matchee. Cubrimos las 7 familias más comunes en logs
# reales para no depender de que el cliente venga con ISO 8601.
# ---------------------------------------------------------------------------
DATE_PATTERNS_COMMON = [
    "yyyyMMddHHmmssSSS",             # 20261004235759139 (típico de cores financieros)
    "yyyyMMddHHmmss",                # 20261004235759
    "ISO8601",                       # 2026-05-19T10:15:30.123Z
    "yyyy-MM-dd HH:mm:ss",           # 2026-05-19 10:15:30
    "yyyy-MM-dd HH:mm:ss.SSS",       # 2026-05-19 10:15:30.123
    "dd/MMM/yyyy:HH:mm:ss Z",        # Apache: 10/Oct/2000:13:55:36 -0700
    "MMM dd HH:mm:ss",               # Syslog (dos dígitos): May 19 10:15:30
    "MMM  d HH:mm:ss",               # Syslog (un dígito, doble espacio): May  9 10:15:30
    "UNIX_MS",                       # 1716120930123 (epoch en milisegundos)
    "UNIX",                          # 1716120930 (epoch en segundos)
]


def _format_date_filter(source_field: str, patterns: list[str] | None = None) -> list[str]:
    """Emite líneas del bloque `date { ... }` con una lista de patterns.

    Si ``patterns`` es None, usa la lista común. ``source_field`` es el
    nombre del campo que contiene el timestamp como string (`event_timestamp`,
    `timestamp`, etc.).
    """
    pats = patterns or DATE_PATTERNS_COMMON
    pattern_str = ", ".join(f'"{p}"' for p in pats)
    return [
        '  date {',
        f'    match => ["{source_field}", {pattern_str}]',
        '    target => "@timestamp"',
        f'    remove_field => ["{source_field}"]',
        '  }',
    ]


# ---------------------------------------------------------------------------
# Helpers para emitir el bloque `mutate { rename/convert/remove_field }`
# desde una lista de (raw_name, ecs_path, type).
# ---------------------------------------------------------------------------
def _build_field_entry(raw_name: str, ecs_path: str, ftype: str, is_ecs: bool) -> dict:
    """Construye un FieldMapping enriquecido — shape que espera el frontend.

    ``field_path`` es el camino final del campo en OpenSearch (después del
    rename del mutate). Para los formats del catálogo (grok + rename ECS),
    el campo vive en ``ecs_path`` — ej. ``source.ip``, ``http.response.status_code``.
    """
    from maas_integrator import is_dimension, infer_role
    dim = is_dimension(ecs_path, ftype)
    role = infer_role(ecs_path, ftype)
    return {
        "raw_name": raw_name,
        "field_path": ecs_path,
        "ecs_path": ecs_path,
        "type": ftype,
        "business_label": raw_name.replace("_", " ").title(),
        "unit": None,
        "dimension": dim,
        "role": role,
        "is_ecs": is_ecs,
        "ecs_type_official": None,
        "normalized_path": ecs_path,
        "ecs_overlay_path": None,
    }


def _bracket_path(ecs_path: str) -> str:
    """Convierte `transaction.id` → `[transaction][id]` para Logstash refs."""
    return f"[{ecs_path.replace('.', '][')}]"


def _emit_mutate(rename: dict[str, str], convert: dict[str, str],
                 remove_fields: list[str]) -> list[str]:
    """Emite un bloque mutate con rename/convert/remove_field. Skip vacíos."""
    if not rename and not convert and not remove_fields:
        return []
    lines = ['  mutate {']
    if rename:
        lines.append('    rename => {')
        for k, v in sorted(rename.items()):
            lines.append(f'      "{k}" => "{v}"')
        lines.append('    }')
    if convert:
        lines.append('    convert => {')
        for k, v in sorted(convert.items()):
            lines.append(f'      "{k}" => "{v}"')
        lines.append('    }')
    if remove_fields:
        rf = ", ".join(f'"{x}"' for x in remove_fields)
        lines.append(f'    remove_field => [{rf}]')
    lines.append('  }')
    return lines


# ---------------------------------------------------------------------------
# Multiline detection: si las líneas 2+ no arrancan con un timestamp ni con
# un patrón claro de inicio de evento, es muy probable que sea un stack
# trace y el cliente necesita configurar codec multiline en el INPUT.
# ---------------------------------------------------------------------------
_LEADING_TIMESTAMP = re.compile(
    # Cubrimos las 6 familias de "esto-es-inicio-de-evento":
    #   1. ISO 8601 desnudo:  2026-05-19T10:15:30
    #   2. ISO entre brackets [Log4j]:  [2026-05-19 10:15:30,123]
    #   3. Apache:  10/Oct/2026:13:55:36
    #   4. Syslog 3164:  May 19 14:30
    #   5. Syslog 5424 con PRI:  <13>
    #   6. CEF:0|...
    r'^(?:'
    r'\d{4}-\d{2}-\d{2}'
    r'|\[\d{4}-\d{2}-\d{2}'
    r'|\d{2}/\w{3}/\d{4}'
    r'|\w{3}\s+\d{1,2}\s+\d{2}:\d{2}'
    r'|\<\d+\>'
    r'|CEF:'
    r')'
)


def detect_multiline(lines: list[str]) -> bool:
    """True si parece multilinea: hay >=2 líneas y las 2+ no empiezan con ts."""
    if len(lines) < 2:
        return False
    # Si la primera linea no tiene timestamp, no es un caso clásico de
    # multiline (más bien es texto suelto).
    if not _LEADING_TIMESTAMP.match(lines[0]):
        return False
    # Si AL MENOS una de las líneas siguientes no arranca con un timestamp,
    # es señal fuerte de continuación (stack trace, exception, etc.).
    for line in lines[1:]:
        if line.strip() and not _LEADING_TIMESTAMP.match(line):
            return True
    return False


# ===========================================================================
# Generador genérico para grok + rename ECS. Usado por Apache, Syslog, CEF.
# ===========================================================================
def _grok_then_rename(
    raw_line: str,
    grok_pattern: str,
    field_mappings: list[tuple[str, str, str]],
    date_source: str | None = None,
    date_patterns: list[str] | None = None,
    pre_date_lines: list[str] | None = None,
    post_grok_kv_source: str | None = None,
) -> dict:
    """Builder común: grok → date → kv (opcional) → mutate rename/convert.

    Parameters
    ----------
    raw_line
        La línea original (para inferir tipos por valor si aplica).
    grok_pattern
        El pattern grok que se inserta en ``match => { "message" => ... }``.
    field_mappings
        Lista de tuplas ``(grok_capture_name, ecs_path, type)``. El
        ``grok_capture_name`` es el nombre del campo que grok produce
        (ej. ``clientip`` del COMBINEDAPACHELOG). El generator lo renombra a
        ``ecs_path`` y le aplica ``convert`` si el tipo es numérico.
    date_source
        Si no es None, se inserta un bloque ``date`` que parsea el campo
        con ese nombre (típicamente la captura grok del timestamp).
    date_patterns
        Patterns custom para ese ``date`` filter. None = lista común.
    pre_date_lines
        Líneas extra a insertar ANTES del date filter (ej. el `kv` filter
        para extraer los key=value de CEF que están dentro de la captura
        ``extension`` del grok).
    post_grok_kv_source
        Si es no-None, agrega un ``kv`` filter que parsea esa captura del
        grok como key=value pairs (para CEF, principalmente).
    """
    filter_lines = ['filter {']

    # Bloque grok base.
    pattern_escaped = grok_pattern.replace('\\', '\\\\').replace('"', '\\"')
    filter_lines.extend([
        '  grok {',
        f'    match => {{ "message" => "{pattern_escaped}" }}',
        '  }',
    ])

    # KV adicional para sub-campos (CEF extension).
    if post_grok_kv_source:
        filter_lines.extend([
            '  kv {',
            f'    source => "{post_grok_kv_source}"',
            '    field_split => " "',
            '    value_split => "="',
            '  }',
        ])

    # Pre-date (para casos especiales).
    if pre_date_lines:
        filter_lines.extend(pre_date_lines)

    # Date filter.
    if date_source:
        filter_lines.extend(_format_date_filter(date_source, date_patterns))

    # Rename + convert + remove_field.
    rename = {}
    convert = {}
    fields = []
    for capture, ecs_path, ftype in field_mappings:
        from maas_integrator import _is_in_ecs  # lazy import: ciclo
        is_ecs = _is_in_ecs(ecs_path)
        fields.append(_build_field_entry(capture, ecs_path, ftype, is_ecs))
        rename[capture] = _bracket_path(ecs_path)
        if ftype in ("integer", "float"):
            convert[_bracket_path(ecs_path)] = ftype

    mutate_lines = _emit_mutate(
        rename=rename,
        convert=convert,
        remove_fields=["message"],
    )
    filter_lines.extend(mutate_lines)
    filter_lines.append('}')

    return {
        "filter_code": "\n".join(filter_lines),
        "fields": fields,
    }


# ===========================================================================
# DETECTORES + GENERADORES POR FORMATO
# ===========================================================================

# --- Apache Common Log Format ----------------------------------------------
_APACHE_COMMON_RX = re.compile(
    r'^\S+ \S+ \S+ \[\d{1,2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2}[^]]*\] "[^"]*" \d+ \S+\s*$'
)


def detect_apache_common(line: str) -> bool:
    """Apache CLF: ``host ident auth [date] "request" status bytes``."""
    return bool(_APACHE_COMMON_RX.match(line.strip()))


def generate_apache_common(lines: list[str]) -> dict:
    """Genera filter para Apache Common Log Format usando grok built-in."""
    result = _grok_then_rename(
        raw_line=lines[0],
        grok_pattern="%{COMMONAPACHELOG}",
        field_mappings=[
            ("clientip",  "source.ip",                       "ip"),
            ("ident",     "user.id",                         "keyword"),
            ("auth",      "user.name",                       "keyword"),
            ("verb",      "http.request.method",             "keyword"),
            ("request",   "url.original",                    "keyword"),
            ("httpversion","http.version",                   "keyword"),
            ("response",  "http.response.status_code",       "integer"),
            ("bytes",     "http.response.body.bytes",        "integer"),
        ],
        date_source="timestamp",
        date_patterns=["dd/MMM/yyyy:HH:mm:ss Z"],
    )
    result["multiline_hint"] = detect_multiline(lines)
    return result


# --- Apache Combined Log Format --------------------------------------------
_APACHE_COMBINED_RX = re.compile(
    r'^\S+ \S+ \S+ \[\d{1,2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2}[^]]*\] "[^"]*" \d+ \S+ "[^"]*" "[^"]*"\s*$'
)


def detect_apache_combined(line: str) -> bool:
    """Apache Combined: CLF + ``"referrer" "user-agent"``."""
    return bool(_APACHE_COMBINED_RX.match(line.strip()))


def generate_apache_combined(lines: list[str]) -> dict:
    result = _grok_then_rename(
        raw_line=lines[0],
        grok_pattern="%{COMBINEDAPACHELOG}",
        field_mappings=[
            ("clientip",  "source.ip",                       "ip"),
            ("ident",     "user.id",                         "keyword"),
            ("auth",      "user.name",                       "keyword"),
            ("verb",      "http.request.method",             "keyword"),
            ("request",   "url.original",                    "keyword"),
            ("httpversion","http.version",                   "keyword"),
            ("response",  "http.response.status_code",       "integer"),
            ("bytes",     "http.response.body.bytes",        "integer"),
            ("referrer",  "http.request.referrer",           "keyword"),
            ("agent",     "user_agent.original",             "keyword"),
        ],
        date_source="timestamp",
        date_patterns=["dd/MMM/yyyy:HH:mm:ss Z"],
    )
    result["multiline_hint"] = detect_multiline(lines)
    return result


# --- Syslog RFC 5424 -------------------------------------------------------
_SYSLOG_5424_RX = re.compile(r'^<\d+>1 \d{4}-\d{2}-\d{2}T')


def detect_syslog_5424(line: str) -> bool:
    """RFC 5424: ``<PRI>1 ISO8601 host app pid msgid [structured] msg``."""
    return bool(_SYSLOG_5424_RX.match(line.strip()))


def generate_syslog_5424(lines: list[str]) -> dict:
    result = _grok_then_rename(
        raw_line=lines[0],
        grok_pattern=(
            "<%{POSINT:syslog_pri}>%{POSINT:syslog_ver} %{TIMESTAMP_ISO8601:event_timestamp} "
            "%{IPORHOST:syslog_host} %{DATA:syslog_app} %{DATA:syslog_pid} "
            "%{DATA:syslog_msgid} %{GREEDYDATA:syslog_msg}"
        ),
        field_mappings=[
            ("syslog_host", "host.hostname",       "keyword"),
            ("syslog_app",  "process.name",        "keyword"),
            ("syslog_pid",  "process.pid",         "keyword"),
            ("syslog_msgid","event.id",            "keyword"),
            ("syslog_msg",  "message",             "text"),
        ],
        date_source="event_timestamp",
        date_patterns=["ISO8601"],
    )
    result["multiline_hint"] = detect_multiline(lines)
    return result


# --- Syslog RFC 3164 (BSD legacy) ------------------------------------------
_SYSLOG_3164_RX = re.compile(r'^(?:<\d+>)?\w{3}\s+\d{1,2} \d{2}:\d{2}:\d{2} ')


def detect_syslog_3164(line: str) -> bool:
    """RFC 3164: ``<PRI>MMM dd HH:mm:ss host process[pid]: msg``."""
    return bool(_SYSLOG_3164_RX.match(line.strip()))


def generate_syslog_3164(lines: list[str]) -> dict:
    result = _grok_then_rename(
        raw_line=lines[0],
        grok_pattern="%{SYSLOGLINE}",
        field_mappings=[
            ("logsource", "host.hostname",       "keyword"),
            ("program",   "process.name",        "keyword"),
            ("pid",       "process.pid",         "keyword"),
            ("priority",  "log.syslog.priority", "integer"),
            ("facility",  "log.syslog.facility.code", "integer"),
            ("severity",  "log.syslog.severity.code", "integer"),
        ],
        date_source="timestamp",
        date_patterns=["MMM  d HH:mm:ss", "MMM dd HH:mm:ss"],
    )
    result["multiline_hint"] = detect_multiline(lines)
    return result


# --- CEF (Common Event Format) ---------------------------------------------
_CEF_RX = re.compile(r'^CEF:\d+\|')

# Mapeo de campos estándar CEF a ECS (los más comunes; la extensión completa
# tiene ~150 campos pero estos cubren security events típicos).
_CEF_EXTENSION_MAP = [
    ("src",       "source.ip",                 "ip"),
    ("dst",       "destination.ip",            "ip"),
    ("spt",       "source.port",               "integer"),
    ("dpt",       "destination.port",          "integer"),
    ("suser",     "source.user.name",          "keyword"),
    ("duser",     "destination.user.name",     "keyword"),
    ("shost",     "source.hostname",           "keyword"),
    ("dhost",     "destination.hostname",      "keyword"),
    ("proto",     "network.protocol",          "keyword"),
    ("act",       "event.action",              "keyword"),
    ("msg",       "message",                   "text"),
    ("rt",        "event.created",             "date"),
    ("cs1",       "labels.cs1",                "keyword"),
    ("cs2",       "labels.cs2",                "keyword"),
    ("in",        "source.bytes",              "integer"),
    ("out",       "destination.bytes",         "integer"),
]


def detect_cef(line: str) -> bool:
    """CEF: ``CEF:<version>|<vendor>|<product>|<version>|<sig>|<name>|<sev>|<ext>``."""
    return bool(_CEF_RX.match(line.strip()))


def generate_cef(lines: list[str]) -> dict:
    """CEF tiene 7 campos pipe-separated en el header + extensión k=v.

    Estrategia: grok para el header, kv para la extensión, rename de los
    fields conocidos a ECS, dejar el resto como `labels.*` (lo hace el
    schema mapeo posterior — no acá).
    """
    # Mapping del header CEF (los 7 campos fijos).
    header_fields = [
        ("cef_version",    "labels.cef_version",       "keyword"),
        ("device_vendor",  "observer.vendor",          "keyword"),
        ("device_product", "observer.product",         "keyword"),
        ("device_version", "observer.version",         "keyword"),
        ("signature_id",   "event.code",               "keyword"),
        ("name",           "event.action",             "keyword"),
        ("severity",       "event.severity",           "integer"),
    ]
    result = _grok_then_rename(
        raw_line=lines[0],
        grok_pattern=(
            "CEF:%{INT:cef_version}\\|%{DATA:device_vendor}\\|%{DATA:device_product}\\|"
            "%{DATA:device_version}\\|%{DATA:signature_id}\\|%{DATA:name}\\|"
            "%{DATA:severity}\\|%{GREEDYDATA:cef_extension}"
        ),
        field_mappings=header_fields + _CEF_EXTENSION_MAP,
        post_grok_kv_source="cef_extension",
    )
    result["multiline_hint"] = detect_multiline(lines)
    return result


# --- Log4j / Java-style (bracketed timestamp + level + thread + origin) ----
#
# Cubre el formato típico de Log4j, java.util.logging, Spring, Apache
# Commons Logging, Oracle Data Loader, etc.:
#   [2026-05-19 10:15:30,123] DEBUG - [main] com.foo.Bar.method(): msg
#   [2026-05-19T10:15:30.123] INFO  [pool-1-thread] Bar - Doing stuff
#
# El nivel suele ser TRACE/DEBUG/INFO/WARN/ERROR/FATAL (a veces con padding
# para alinear visualmente: " INFO").
# El thread va entre brackets ([main], [Thread-3], [pool-1-thread-2]).
# El origin puede ser Class.method() o Class (Logback style) o vacío.

_LOG4J_RX = re.compile(
    r'^\[\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d+\]'
    r'\s+(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|SEVERE|FINE|FINER|FINEST|CONFIG)\b'
)


def detect_log4j(line: str) -> bool:
    """Log4j-style: ``[YYYY-MM-DD HH:MM:SS,SSS] LEVEL ...``."""
    return bool(_LOG4J_RX.match(line.strip()))


def generate_log4j(lines: list[str]) -> dict:
    """Filter para Log4j. Grok con varias variantes del formato origin/thread.

    Estrategia: un grok con dos branches via lista de patterns — el primero
    matchea formato Oracle Data Loader / similar (con `-` y `[thread]` y
    `Class.method()`), el segundo cae a Logback-style más laxo. Si ninguno
    matchea, el filter no rompe (grok tagea `_grokparsefailure` y el evento
    sigue, lo cual el sandbox de validación reporta).
    """
    # NOTA: el pattern usa `\[%{DATA:event_timestamp}\]` para capturar el
    # contenido del primer bracket sea cual sea el separador interno
    # (espacio o T) y la precisión (mili/microsegundos, coma o punto).
    grok_patterns = [
        # Oracle Data Loader / Log4j clásico:
        # [2010-04-24 07:51:54,393] DEBUG - [main] BulkOpsClient.main(): Execution begin.
        (
            r"\[%{DATA:event_timestamp}\]\s+%{LOGLEVEL:log_level}\s+-\s+"
            r"\[%{DATA:thread_name}\]\s+%{DATA:log_origin}:\s+%{GREEDYDATA:event_message}"
        ),
        # Logback / Slf4j sin separador `-` y con clase sola:
        # [2026-05-19 10:15:30.123] INFO  [pool-1-thread-2] com.foo.Bar - Doing stuff
        (
            r"\[%{DATA:event_timestamp}\]\s+%{LOGLEVEL:log_level}\s+"
            r"\[%{DATA:thread_name}\]\s+%{DATA:log_origin}\s+-\s+%{GREEDYDATA:event_message}"
        ),
        # Variante minimalista (sin thread):
        # [2026-05-19 10:15:30,123] ERROR Some message here
        (
            r"\[%{DATA:event_timestamp}\]\s+%{LOGLEVEL:log_level}\s+%{GREEDYDATA:event_message}"
        ),
    ]
    field_mappings = [
        ("log_level",     "log.level",                  "keyword"),
        ("thread_name",   "process.thread.name",        "keyword"),
        ("log_origin",    "log.origin.function",        "keyword"),
        ("event_message", "event.original",             "text"),
    ]

    # Grok bloque con lista de patterns (Logstash itera en orden y usa el 1ro).
    filter_lines = ['filter {']
    filter_lines.append('  grok {')
    filter_lines.append('    match => { "message" => [')
    for i, gp in enumerate(grok_patterns):
        gp_esc = gp.replace('"', '\\"')
        sep = "," if i < len(grok_patterns) - 1 else ""
        filter_lines.append(f'      "{gp_esc}"{sep}')
    filter_lines.append('    ] }')
    filter_lines.append('  }')

    # Date filter con múltiples patterns (Log4j coma/punto, ISO).
    filter_lines.extend(_format_date_filter(
        "event_timestamp",
        patterns=[
            "yyyy-MM-dd HH:mm:ss,SSS",   # 2010-04-24 07:51:54,393  (típico)
            "yyyy-MM-dd HH:mm:ss.SSS",   # 2010-04-24 07:51:54.393  (Logback)
            "yyyy-MM-dd'T'HH:mm:ss.SSS", # 2010-04-24T07:51:54.393
            "ISO8601",                   # fallback general
        ],
    ))

    # Rename + convert + remove_field.
    rename = {}
    fields = []
    # Entrada sintética para el timestamp: aparece en la tabla de mapping
    # del frontend (UX), aunque el rename real ya lo hizo el `date` filter
    # consumiendo `event_timestamp` → `@timestamp` y removiendo el campo.
    fields.append(_build_field_entry("event_timestamp", "@timestamp", "date", True))
    for capture, ecs_path, ftype in field_mappings:
        from maas_integrator import _is_in_ecs
        is_ecs = _is_in_ecs(ecs_path)
        fields.append(_build_field_entry(capture, ecs_path, ftype, is_ecs))
        rename[capture] = _bracket_path(ecs_path)

    filter_lines.extend(_emit_mutate(rename, convert={}, remove_fields=["message"]))
    filter_lines.append('}')

    return {
        "filter_code": "\n".join(filter_lines),
        "fields": fields,
        "multiline_hint": detect_multiline(lines),
    }


# --- CSV --------------------------------------------------------------------
_CSV_HEADER_RX = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*(?:,[a-zA-Z_][a-zA-Z0-9_]*){2,}$')


def detect_csv(lines: list[str]) -> bool:
    """CSV: múltiples líneas, mismo número de comas, sin `=`.

    Heurística:
      - Más de 1 línea.
      - Línea 0 tiene >=3 comas y NO tiene `=`.
      - Líneas 1+ tienen el mismo número de comas que la línea 0 (±2 para
        tolerar valores opcionales).
    """
    if len(lines) < 2:
        return False
    first = lines[0]
    if "=" in first or first.count(",") < 3:
        return False
    base_commas = first.count(",")
    matches = sum(
        1 for line in lines[1:]
        if line.strip() and abs(line.count(",") - base_commas) <= 2
    )
    # Al menos 50% de las líneas restantes deben matchear el shape.
    return matches >= max(1, (len(lines) - 1) // 2)


def generate_csv(lines: list[str]) -> dict:
    """Genera filter para CSV. Si la primera línea parece header (identifiers),
    la usa como columnas; sino emite `column_1`, `column_2`, etc.
    """
    from maas_integrator import _get_ecs_mapping, _is_in_ecs

    first = lines[0].strip()
    raw_columns = [c.strip() for c in first.split(",")]
    has_header = bool(_CSV_HEADER_RX.match(first))

    if has_header:
        columns = raw_columns
    else:
        columns = [f"column_{i+1}" for i in range(len(raw_columns))]

    # Si tiene header, dropear la primera línea del input via `if [message]
    # == "<header>" { drop {} }` — Logstash way. Pero eso requiere conocer
    # el contenido exacto. Forma robusta: usar el filtro `csv` y dejar que
    # los analytics filtren después por la línea espuria si es necesario.
    # Para la demo, omitimos el drop y la primera fila va a parsear como
    # un evento con los nombres de las columnas como values — el cliente
    # lo detecta enseguida y configura `skip_header` cuando despliega.

    filter_lines = ['filter {']
    cols_str = ", ".join(f'"{c}"' for c in columns)
    filter_lines.extend([
        '  csv {',
        '    source => "message"',
        f'    columns => [{cols_str}]',
        '    separator => ","',
        '    skip_header => true',
        '  }',
    ])

    # Rename de cada columna a su ECS path.
    rename = {}
    convert = {}
    fields = []
    timestamp_col: str | None = None
    for col in columns:
        ecs_path, _ = _get_ecs_mapping(col)
        ftype = "string"  # Por defecto; el frontend lo refina en step 2.
        is_ecs = _is_in_ecs(ecs_path)
        fields.append(_build_field_entry(col, ecs_path, ftype, is_ecs))
        if ecs_path == "@timestamp":
            timestamp_col = col
            continue
        rename[col] = _bracket_path(ecs_path)

    if timestamp_col:
        filter_lines.extend(_format_date_filter(timestamp_col))

    filter_lines.extend(_emit_mutate(rename, convert, remove_fields=["message"]))
    filter_lines.append('}')

    return {
        "filter_code": "\n".join(filter_lines),
        "fields": fields,
        "multiline_hint": False,  # CSV con líneas individuales no es multiline.
    }


# ===========================================================================
# CATÁLOGO + DISPATCH
# ===========================================================================

class FormatEntry(NamedTuple):
    name: str
    detect: Callable[..., bool]
    generate: Callable[[list[str]], dict]
    detect_takes_lines: bool  # CSV necesita varias líneas; el resto solo la 1ra.


# Orden importa: más específico primero. CEF tiene prefijo único; Syslog
# tiene patterns claros; Apache tiene shape rígido; CSV es el más laxo.
CATALOG: list[FormatEntry] = [
    FormatEntry("cef",             detect_cef,             generate_cef,             False),
    FormatEntry("syslog_5424",     detect_syslog_5424,     generate_syslog_5424,     False),
    FormatEntry("syslog_3164",     detect_syslog_3164,     generate_syslog_3164,     False),
    FormatEntry("apache_combined", detect_apache_combined, generate_apache_combined, False),
    FormatEntry("apache_common",   detect_apache_common,   generate_apache_common,   False),
    # log4j ANTES de csv: si el log empieza con `[YYYY-MM-DD ...` y tiene
    # comas (típicas en milis con coma `,393`), CSV podría dar falso positivo.
    FormatEntry("log4j",           detect_log4j,           generate_log4j,           False),
    FormatEntry("csv",             detect_csv,             generate_csv,             True),
]


def try_match(lines: list[str]) -> dict | None:
    """Itera el catálogo y devuelve el primer formato que matchee.

    Returns
    -------
    dict | None
        ``{"filter_code", "fields", "multiline_hint"}`` si algún detector
        matcheó, o ``None`` si ningún formato del catálogo aplica (el
        caller cae a los formatos básicos JSON/pipe/space_kv o al LLM).
    """
    if not lines:
        return None
    first = lines[0]
    for entry in CATALOG:
        if entry.detect_takes_lines:
            if entry.detect(lines):
                return entry.generate(lines)
        else:
            if entry.detect(first):
                return entry.generate(lines)
    return None
