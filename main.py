"""
main.py
=======

API FastAPI del "AI-Driven Onboarding" del Solution Blueprint de analítica
transaccional para el sector financiero en Huawei Cloud (arquitectura
Single-Tenant).

Flujo orquestado por dos endpoints:

    1. `/api/v1/onboarding/generate-filter`: recibe el log crudo del nodo
       Input y devuelve el bloque `filter {}` generado por el LLM glm-5.2
       vía MaaS. El frontend lo dispara con el botón "Transformar con LLM"
       del nodo Filter.
    2. `/api/v1/onboarding/generate-pipeline`: recibe el `filter_code` ya
       generado más los configs de Input/Output y devuelve el `.conf`
       completo, listo para pegar en Logstash de CSS.

La validación sintáctica con Logstash efímero quedó deprecada (la del
sandbox de OpenSearch sigue activa en `/validate-mapping` para validación
SEMÁNTICA).

Ejecución local:

    uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field, field_validator

# Ejecución NATIVA (uvicorn en el host, sin contenedor que envuelva la app):
# nada carga el .env automáticamente, así que lo hacemos aquí, antes de que
# cualquier módulo lea variables de entorno.
load_dotenv()

from maas_integrator import (  # noqa: E402
    generate_logstash_filter,
    get_huawei_project_id,
    get_huawei_settings,
    get_region,
    strip_logstash_comments,
)
from index_template import (  # noqa: E402
    build_index_template,
    index_pattern_from_name,
    put_snippet,
)
from ecs_validator import classify_field, spec_loaded, spec_size  # noqa: E402
import verticals  # noqa: E402  (registro declarativo de los verticales de demo)
# OBSClient se importa lazy dentro de terraform_deploy para que el resto
# del backend arranque aunque esdk-obs-python no esté instalado.

# Sanity check del validador ECS al arrancar. Si docs/fields.csv falta o
# está vacío, el step 2 va a marcar TODO como custom — preferimos warnear
# explícito en stdout para que el operador se entere antes de la primera
# demo, en vez de descubrirlo viendo badges naranjas por todos lados.
if spec_loaded():
    print(f"[ecs] Spec ECS cargada: {spec_size()} fields disponibles para validación.")
else:
    print("[ecs] ⚠ docs/fields.csv no encontrado o vacío — el step 2 marcará TODOS los fields como custom.")

# Directorio de la SPA estática (servida por la propia API -> mismo origen,
# sin necesidad de CORS).
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Blueprint - Logstash Pipeline Generator",
    description=(
        "Genera y valida automáticamente la configuración de Logstash a "
        "partir de un log de muestra, usando un LLM en Huawei Cloud MaaS."
    ),
    version="1.0.0",
)

# Servir assets estáticos (logos, etc.) desde static/ en /static. El index.html
# se sigue sirviendo aparte en "/" (ver index()). Mountar acá no colisiona con
# las rutas /api/v1/* ni con "/".
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Multi-usuario hosteado (opcional) ────────────────────────────────────────
# Si hay APP_SECRET_KEY en el entorno, auth queda ACTIVO: cada SA entra con
# email+password y trabaja en su propio workspace aislado (settings + terraform
# + state). Sin esa var, el middleware es passthrough y la app corre single-user
# como siempre (dev local, tests, nativo). Ver auth.py.
import auth  # noqa: E402
import audit  # noqa: E402

app.add_middleware(auth.AuthMiddleware)


def _active_terraform_dir() -> Path:
    """Directorio de trabajo de Terraform: el del usuario logueado (hosteado) o
    el global `terraform/` (single-user). Aísla state/marker/registro por SA."""
    return auth.current_terraform_dir() or (Path(__file__).parent / "terraform")


# ── Lock de deploy/destroy por usuario ───────────────────────────────────────
# Un mismo SA no debe correr dos deploys/destroys a la vez (corrompería SU state).
# SAs distintos corren en paralelo (dirs y states separados). Lock in-process
# (un worker de uvicorn); en single-user la clave es "_global".
import contextlib as _contextlib  # noqa: E402
import threading as _threading  # noqa: E402

_deploy_locks: "dict[str, _threading.Lock]" = {}
_deploy_locks_guard = _threading.Lock()


def _deploy_lock_for_current() -> "_threading.Lock":
    ctx = auth.current_user_var.get()
    key = ctx.user_id if ctx is not None else "_global"
    with _deploy_locks_guard:
        return _deploy_locks.setdefault(key, _threading.Lock())


@_contextlib.contextmanager
def _deploy_guard():
    """Adquiere el lock del usuario o corta con 409 si ya hay uno en curso."""
    lock = _deploy_lock_for_current()
    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"stage": "deploy_lock",
                    "message": "Ya hay un despliegue o destroy en curso para tu usuario. "
                               "Esperá a que termine antes de lanzar otro."})
    try:
        yield
    finally:
        lock.release()


# ===========================================================================
# Modelos por plugin
# ===========================================================================
# Convención de clasificación (Logstash 7.10 + CSS UG part 3):
#   - Sin default => OBLIGATORIO (lo pide el form como "Básico").
#   - Con default => OPCIONAL con valor sensato (va a "Avanzado" en el form).
# Cuando CSS UG y Logstash 7.10 difieren, prevalece Logstash 7.10 (decisión
# del operador) y el default refleja lo más útil para el caso de onboarding.


def _default_obs_endpoint() -> str:
    """Endpoint OBS de la región efectiva (⚙ Configuración > env > la-south-2)."""
    return f"https://obs.{get_region()}.myhuaweicloud.com"

# --- INPUTS ----------------------------------------------------------------
class S3InputConfig(BaseModel):
    """Input s3/OBS (logstash-input-s3 — Logstash 7.10).

    Los nombres de campo siguen la spec de Logstash (`access_key_id`,
    `secret_access_key`). Para back-compat con clientes/tests viejos que
    enviaban `access_key`/`secret_key`, ambos alias se aceptan en la entrada.
    """

    # Básicos (sin default sensato para el operador)
    bucket: str = ""
    access_key_id: str = Field(
        default="",
        validation_alias=AliasChoices("access_key_id", "access_key"),
    )
    secret_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("secret_access_key", "secret_key"),
    )
    # Avanzados (con default sensato — la región sale de ⚙ Configuración)
    region: str = Field(default_factory=get_region)
    endpoint: str = Field(default_factory=lambda: _default_obs_endpoint())
    prefix: str = ""
    codec: str = "plain"
    charset: str = "UTF-8"
    interval: int = 60
    temporary_directory: str = "/opt/data/tmp/"
    # CSS UG (docs/logstash-llm-context.md, regla 8) recomienda delete=true
    # para evitar reprocesar archivos ya consumidos.
    delete: bool = True
    # CSS UG recomienda watch_for_new_files=true para que el plugin descubra
    # archivos nuevos en el bucket sin reiniciar Logstash.
    watch_for_new_files: bool = True
    # Backup: el plugin copia cada archivo procesado a este bucket antes de
    # borrarlo del origen. Para CTS apunta a `backup-mi-tracker-cts`.
    backup_to_bucket: str = ""


class BeatsInputConfig(BaseModel):
    """Input beats (logstash-input-beats — CSS Table 4-27)."""

    # Básico
    port: int = 5044
    # Avanzados
    host: str = "0.0.0.0"
    ssl: bool = False
    codec: str = "plain"


class FileInputConfig(BaseModel):
    """Input file (logstash-input-file — Logstash 7.10)."""

    # Básico
    path: str = ""
    # Avanzados
    start_position: str = "beginning"
    sincedb_path: str = "/dev/null"
    codec: str = "plain"


class KafkaInputConfig(BaseModel):
    """Input kafka (logstash-input-kafka — CSS Table 4-26)."""

    # Básicos
    bootstrap_servers: str = ""
    topics: list[str] = Field(default_factory=lambda: ["logs"])
    # Avanzados (CSS los marca Yes pero LS tiene defaults sensatos)
    group_id: str = "logstash"
    auto_offset_reset: str = "earliest"
    codec: str = "plain"
    consumer_threads: int = 1
    # SASL_SSL — Kafka con autenticación (self-hosted, Confluent, MSK, DMS…).
    # Vacío => plaintext (puerto 9092, sin auth). Con security_protocol=SASL_SSL
    # se emite el bloque SASL: mecanismo, credenciales (jaas inline) y truststore.
    security_protocol: str = ""  # "" | "SASL_SSL"
    sasl_mechanism: str = "PLAIN"  # PLAIN | SCRAM-SHA-256 | SCRAM-SHA-512
    sasl_username: str = ""
    sasl_password: str = ""
    ssl_truststore_location: str = ""
    ssl_truststore_password: str = ""

    @field_validator("topics", mode="before")
    @classmethod
    def _coerce_topics(cls, v: Any) -> Any:
        """El frontend legacy manda `topics` como string coma-separado; el plugin
        espera una lista. Toleramos ambos: str → lista (split por coma)."""
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()] or ["logs"]
        return v


class HttpInputConfig(BaseModel):
    """Input http (logstash-input-http — Logstash 7.10, todo opcional)."""

    # Básico (UX): el operador debe saber qué puerto se expone
    port: int = 8080
    # Avanzados
    host: str = "0.0.0.0"
    codec: str = "plain"
    response_code: int = 200


class JdbcInputConfig(BaseModel):
    """Input jdbc (logstash-input-jdbc — CSS Table 4-25)."""

    # Básicos (CSS marca los 6 como Yes)
    jdbc_driver_library: str = ""
    jdbc_driver_class: str = ""
    jdbc_connection_string: str = ""
    jdbc_user: str = ""
    jdbc_password: str = ""
    statement: str = ""
    # Avanzados
    schedule: str = "* * * * *"
    clean_run: bool = False
    tracking_column: str = ""
    use_column_value: bool = False
    jdbc_paging_enabled: bool = False


# --- OUTPUTS ---------------------------------------------------------------
class ElasticsearchOutputConfig(BaseModel):
    """Output elasticsearch (logstash-output-elasticsearch — CSS Table 4-23)."""

    # Básicos. Logstash acepta `hosts` como string o lista (multi-host); el
    # frontend manda siempre lista (lo natural para CSS multi-nodo) pero
    # aceptamos string también para back-compat con el formato legacy.
    hosts: list[str] | str = Field(default_factory=lambda: ["http://localhost:9200"])
    index: str = "log-%{+YYYY.MM}"
    # Sólo obligatorios para clúster en security-mode
    user: str = ""
    password: str = ""
    # Avanzados
    action: str = "index"
    document_id: str = ""
    manage_template: bool = False  # CSS recomienda false para evitar permisos
    ilm_enabled: bool = False  # idem
    ssl: bool = False
    # Default = path interno del CSS-managed Logstash (docs/logstash-llm-context.md:60).
    # El bloque `output { elasticsearch { ... cacert => ... } }` solo se emite
    # cuando ssl=true, así que en modo HTTP este valor se ignora.
    cacert: str = "/rds/datastore/logstash/v7.10.0/package/logstash-7.10.0/extend/certs"
    ssl_certificate_verification: bool = True


class S3OutputConfig(BaseModel):
    """Output s3/OBS (logstash-output-s3 — Logstash 7.10).

    Mismos aliases que S3InputConfig (`access_key_id`/`secret_access_key`).
    """

    # Básicos
    bucket: str = ""
    access_key_id: str = Field(
        default="",
        validation_alias=AliasChoices("access_key_id", "access_key"),
    )
    secret_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("secret_access_key", "secret_key"),
    )
    # Avanzados
    region: str = Field(default_factory=get_region)
    endpoint: str = Field(default_factory=lambda: _default_obs_endpoint())
    prefix: str = "transactions/year=%{+YYYY}/month=%{+MM}/day=%{+dd}/"
    codec: str = "json_lines"
    encoding: str = "gzip"
    time_file: int = 15
    restore: bool = True


class StdoutOutputConfig(BaseModel):
    """Output stdout (logstash-output-stdout — todo opcional)."""

    codec: str = "rubydebug"


class KafkaOutputConfig(BaseModel):
    """Output kafka (logstash-output-kafka — Logstash 7.10)."""

    # Básicos
    bootstrap_servers: str = ""
    topic_id: str = ""
    # Avanzados
    codec: str = "plain"
    compression_type: str = "gzip"
    acks: str = "1"
    client_id: str = ""


class MongoDBOutputConfig(BaseModel):
    """Output mongodb (logstash-output-mongodb — community plugin)."""

    # Básicos
    uri: str = ""
    database: str = ""
    collection: str = ""
    # Avanzados
    generateId: bool = False
    isodate: bool = False
    action: str = "insert"


class PostgreSQLOutputConfig(BaseModel):
    """Output postgresql (logstash-output-jdbc + driver Postgres)."""

    # Básicos
    connection_string: str = ""
    driver_jar_path: str = ""
    statement: str = ""
    # Avanzados (driver_class queda autocompletado, no se expone en el form)
    driver_class: str = "org.postgresql.Driver"
    user: str = ""
    password: str = ""
    flush_size: int = 1000
    idle_flush_time: int = 1


# --- Discriminadores por nodo ----------------------------------------------
class InputNodeConfig(BaseModel):
    """Config genérica de nodo Input: plugin_type + sub-config por tipo."""

    plugin_type: str = "s3"
    s3: S3InputConfig | None = None
    beats: BeatsInputConfig | None = None
    file: FileInputConfig | None = None
    kafka: KafkaInputConfig | None = None
    http: HttpInputConfig | None = None
    jdbc: JdbcInputConfig | None = None


class OutputNodeConfig(BaseModel):
    """Config genérica de nodo Output: plugin_type + sub-config por tipo."""

    plugin_type: str = "elasticsearch"
    elasticsearch: ElasticsearchOutputConfig | None = None
    s3: S3OutputConfig | None = None
    stdout: StdoutOutputConfig | None = None
    kafka: KafkaOutputConfig | None = None
    mongodb: MongoDBOutputConfig | None = None
    postgresql: PostgreSQLOutputConfig | None = None


# Legacy multi-output (mantenido por compatibilidad con clientes/tests viejos
# que envían `{"elasticsearch": {...}, "s3": {...}}` sin plugin_type).
class OutputConfig(BaseModel):
    elasticsearch: ElasticsearchOutputConfig | None = None
    s3: S3OutputConfig | None = None


# ===========================================================================
# Generadores de bloques Logstash (uno por plugin)
# ===========================================================================
def _kv(key: str, val: Any) -> str:
    """Formatea un par key => value como línea de config de Logstash."""
    if isinstance(val, bool):
        return f'    {key} => {"true" if val else "false"}'
    if isinstance(val, (int, float)):
        return f'    {key} => {val}'
    if isinstance(val, list):
        items = ", ".join(f'"{v}"' for v in val)
        return f'    {key} => [{items}]'
    return f'    {key} => "{val}"'


def _wrap(plugin_name: str, lines: list[str]) -> str:
    """Envuelve líneas dentro de `<plugin_name> { ... }`."""
    body = "\n".join(lines) if lines else ""
    return f"  {plugin_name} {{\n{body}\n  }}"


# --- INPUTS ----------------------------------------------------------------
def gen_input_s3(c: S3InputConfig) -> str:
    lines = [
        _kv("access_key_id", c.access_key_id),
        _kv("secret_access_key", c.secret_access_key),
        _kv("bucket", c.bucket),
        _kv("region", c.region),
    ]
    if c.endpoint:
        lines.append(_kv("endpoint", c.endpoint))
    if c.prefix:
        lines.append(_kv("prefix", c.prefix))
    lines.append(_kv("interval", c.interval))
    if c.codec == "plain" and c.charset != "UTF-8":
        lines.append(f'    codec => plain {{ charset => "{c.charset}" }}')
    else:
        lines.append(f'    codec => {c.codec}')
    if c.temporary_directory:
        lines.append(_kv("temporary_directory", c.temporary_directory))
    # Emitir SIEMPRE (no solo cuando es true): el default del plugin para
    # watch_for_new_files es true, así que para apagarlo (CTS one-shot read-only)
    # hay que escribirlo explícito. delete default del plugin es false.
    lines.append(f"    delete => {'true' if c.delete else 'false'}")
    lines.append(f"    watch_for_new_files => {'true' if c.watch_for_new_files else 'false'}")
    if c.backup_to_bucket:
        lines.append(_kv("backup_to_bucket", c.backup_to_bucket))
    return _wrap("s3", lines)


def gen_input_beats(c: BeatsInputConfig) -> str:
    lines = [_kv("port", c.port), _kv("host", c.host)]
    if c.ssl:
        lines.append(_kv("ssl", True))
    lines.append(f'    codec => {c.codec}')
    return _wrap("beats", lines)


def gen_input_file(c: FileInputConfig) -> str:
    lines = [
        _kv("path", c.path),
        _kv("start_position", c.start_position),
        _kv("sincedb_path", c.sincedb_path),
        f'    codec => {c.codec}',
    ]
    return _wrap("file", lines)


def gen_input_kafka(c: KafkaInputConfig) -> str:
    lines = [
        _kv("bootstrap_servers", c.bootstrap_servers),
        _kv("topics", c.topics),
        _kv("group_id", c.group_id),
        _kv("auto_offset_reset", c.auto_offset_reset),
        f'    codec => {c.codec}',
        _kv("consumer_threads", c.consumer_threads),
    ]
    # Kafka con SASL_SSL (cualquier broker con auth): mecanismo + credenciales
    # (jaas inline, sin archivo jaas aparte) + truststore del broker.
    if c.security_protocol == "SASL_SSL":
        # ScramLoginModule para SCRAM-*, PlainLoginModule para PLAIN.
        module = ("org.apache.kafka.common.security.scram.ScramLoginModule"
                  if c.sasl_mechanism.upper().startswith("SCRAM")
                  else "org.apache.kafka.common.security.plain.PlainLoginModule")
        # Comillas simples externas: el valor lleva comillas dobles adentro.
        jaas = (f'{module} required '
                f'username="{c.sasl_username}" password="{c.sasl_password}";')
        lines.append(_kv("security_protocol", "SASL_SSL"))
        lines.append(_kv("sasl_mechanism", c.sasl_mechanism))
        lines.append(f"    sasl_jaas_config => '{jaas}'")
        if c.ssl_truststore_location:
            lines.append(_kv("ssl_truststore_location", c.ssl_truststore_location))
        if c.ssl_truststore_password:
            lines.append(_kv("ssl_truststore_password", c.ssl_truststore_password))
    return _wrap("kafka", lines)


def gen_input_http(c: HttpInputConfig) -> str:
    lines = [
        _kv("port", c.port),
        _kv("host", c.host),
        f'    codec => {c.codec}',
        _kv("response_code", c.response_code),
    ]
    return _wrap("http", lines)


def gen_input_jdbc(c: JdbcInputConfig) -> str:
    lines = [
        _kv("jdbc_driver_library", c.jdbc_driver_library),
        _kv("jdbc_driver_class", c.jdbc_driver_class),
        _kv("jdbc_connection_string", c.jdbc_connection_string),
        _kv("jdbc_user", c.jdbc_user),
        _kv("jdbc_password", c.jdbc_password),
        _kv("statement", c.statement),
        _kv("schedule", c.schedule),
    ]
    if c.clean_run:
        lines.append(_kv("clean_run", True))
    if c.tracking_column:
        lines.append(_kv("tracking_column", c.tracking_column))
        lines.append(_kv("use_column_value", c.use_column_value))
    if c.jdbc_paging_enabled:
        lines.append(_kv("jdbc_paging_enabled", True))
    return _wrap("jdbc", lines)


# --- OUTPUTS ---------------------------------------------------------------
def gen_output_elasticsearch(c: ElasticsearchOutputConfig) -> str:
    # Normalizar `hosts` a lista para emitir siempre el formato `["h1","h2"]`,
    # que es lo que recomienda CSS para clusters multi-nodo.
    hosts_list = c.hosts if isinstance(c.hosts, list) else [c.hosts]
    
    # Filtrar hosts vacíos
    hosts_list = [h for h in hosts_list if h and h.strip()]
    
    # Normalizar protocolo en hosts según ssl
    # Asegurar que el protocolo en hosts coincida con ssl
    normalized_hosts = []
    for h in hosts_list:
        h = h.strip()
        has_https = h.startswith("https://")
        has_http = h.startswith("http://")
        
        if c.ssl:
            # ssl=true → debe ser https
            if has_http:
                h = "https://" + h[7:]
            elif not has_https:
                h = f"https://{h}"
        else:
            # ssl=false → debe ser http
            if has_https:
                h = "http://" + h[8:]
            elif not has_http:
                h = f"http://{h}"
        normalized_hosts.append(h)
    hosts_list = normalized_hosts
    
    # Si no hay hosts, generar marcador para Terraform: hosts => []
    # Terraform lo reemplazará con el endpoint real del OpenSearch
    if not hosts_list:
        hosts_str = ""
    else:
        hosts_formatted = []
        for h in hosts_list:
            hosts_formatted.append(f'"{h}"')
        hosts_str = ", ".join(hosts_formatted)
    
    lines = [
        f'    hosts => [{hosts_str}]',
        _kv("index", c.index),
        _kv("action", c.action),
    ]
    if c.document_id:
        lines.append(_kv("document_id", c.document_id))
    lines.append(_kv("manage_template", c.manage_template))
    lines.append(_kv("ilm_enabled", c.ilm_enabled))
    if c.user:
        lines.append(_kv("user", c.user))
    if c.password:
        lines.append(_kv("password", c.password))
    if c.ssl:
        lines.append(_kv("ssl", True))
        # `cacert` y `ssl_certificate_verification => false` son mutuamente
        # excluyentes: cacert valida contra un CA específico, mientras que el
        # flag false saltea TODA verificación (y hace que Logstash ignore el
        # cacert). docs/logstash-llm-context.md:73 lo confirma: el `false` es
        # el fallback "no tengo cert". Por eso:
        #   - cacert presente → emitimos cacert SOLO. Logstash queda en
        #     `ssl_certificate_verification => true` (default) y valida.
        #   - cacert vacío   → emitimos el flag que mandó el frontend
        #     (típicamente false para cluster con cert self-signed).
        if c.cacert:
            lines.append(_kv("cacert", c.cacert))
        else:
            lines.append(_kv("ssl_certificate_verification", c.ssl_certificate_verification))
    return _wrap("elasticsearch", lines)


def gen_output_s3(c: S3OutputConfig) -> str:
    lines = [
        _kv("access_key_id", c.access_key_id),
        _kv("secret_access_key", c.secret_access_key),
        _kv("bucket", c.bucket),
        _kv("region", c.region),
    ]
    if c.endpoint:
        lines.append(_kv("endpoint", c.endpoint))
    if c.prefix:
        lines.append(_kv("prefix", c.prefix))
    lines.append(f'    codec => {c.codec}')
    lines.append(_kv("encoding", c.encoding))
    lines.append(_kv("time_file", c.time_file))
    lines.append(_kv("restore", c.restore))
    return _wrap("s3", lines)


def gen_output_stdout(c: StdoutOutputConfig) -> str:
    return _wrap("stdout", [f'    codec => {c.codec}'])


def gen_output_kafka(c: KafkaOutputConfig) -> str:
    lines = [
        _kv("bootstrap_servers", c.bootstrap_servers),
        _kv("topic_id", c.topic_id),
        f'    codec => {c.codec}',
        _kv("compression_type", c.compression_type),
        _kv("acks", c.acks),
    ]
    if c.client_id:
        lines.append(_kv("client_id", c.client_id))
    return _wrap("kafka", lines)


def gen_output_mongodb(c: MongoDBOutputConfig) -> str:
    lines = [
        _kv("uri", c.uri),
        _kv("database", c.database),
        _kv("collection", c.collection),
        _kv("action", c.action),
    ]
    if c.generateId:
        lines.append(_kv("generateId", True))
    if c.isodate:
        lines.append(_kv("isodate", True))
    return _wrap("mongodb", lines)


def gen_output_postgresql(c: PostgreSQLOutputConfig) -> str:
    """logstash-output-jdbc apuntando a Postgres."""
    lines = [
        _kv("driver_jar_path", c.driver_jar_path),
        _kv("driver_class", c.driver_class),
        _kv("connection_string", c.connection_string),
        _kv("statement", [c.statement]),
        _kv("flush_size", c.flush_size),
        _kv("idle_flush_time", c.idle_flush_time),
    ]
    if c.user:
        lines.append(_kv("username", c.user))
    if c.password:
        lines.append(_kv("password", c.password))
    return _wrap("jdbc", lines)


# --- Dispatchers -----------------------------------------------------------
_INPUT_DISPATCH = {
    "s3":    (S3InputConfig,    gen_input_s3),
    "beats": (BeatsInputConfig, gen_input_beats),
    "file":  (FileInputConfig,  gen_input_file),
    "kafka": (KafkaInputConfig, gen_input_kafka),
    "http":  (HttpInputConfig,  gen_input_http),
    "jdbc":  (JdbcInputConfig,  gen_input_jdbc),
}

_OUTPUT_DISPATCH = {
    "elasticsearch": (ElasticsearchOutputConfig, gen_output_elasticsearch),
    "s3":            (S3OutputConfig,            gen_output_s3),
    "stdout":        (StdoutOutputConfig,        gen_output_stdout),
    "kafka":         (KafkaOutputConfig,         gen_output_kafka),
    "mongodb":       (MongoDBOutputConfig,       gen_output_mongodb),
    "postgresql":    (PostgreSQLOutputConfig,    gen_output_postgresql),
}

# Alias UI → nombre canónico del plugin de Logstash. El frontend usa nombres
# amigables al cliente (`opensearch` en vez de `elasticsearch`, `obs` en vez
# de `s3`) porque queda mejor en la demo, pero Logstash espera los nombres
# canónicos. Estos mapeos se aplican antes de buscar en _*_DISPATCH.
_INPUT_ALIASES  = {"obs": "s3"}
_OUTPUT_ALIASES = {"opensearch": "elasticsearch", "obs": "s3"}


def _canonical_input(name: str) -> str:
    return _INPUT_ALIASES.get(name, name)


def _canonical_output(name: str) -> str:
    return _OUTPUT_ALIASES.get(name, name)


def generate_input_block(raw: dict | None) -> str:
    """Genera el bloque `input { ... }`.

    Acepta dos formas en `raw`:
    - Nuevo: ``{"plugin_type": "kafka", "kafka": {...}}`` o
      ``{"plugin_type": "obs", "obs": {...}}`` (obs es alias UI de s3).
    - Legacy: dict con campos flat sin `plugin_type` => asume `s3`.
    """
    if not raw:
        return ""

    ui_name = raw.get("plugin_type", "s3")
    canonical = _canonical_input(ui_name)
    model_cls, gen_fn = _INPUT_DISPATCH.get(canonical, _INPUT_DISPATCH["s3"])

    # Sub-config: la clave anidada usa el nombre UI tal como vino del
    # frontend (`obs`, `kafka`, etc.). Fallback al shape flat legacy.
    if ui_name in raw and isinstance(raw[ui_name], dict):
        sub = raw[ui_name]
    else:
        sub = {k: v for k, v in raw.items() if k != "plugin_type"}
    cfg = model_cls(**(sub or {}))
    return f"input {{\n{gen_fn(cfg)}\n}}"


def generate_output_block(raw: dict | None) -> str:
    """Genera el bloque `output { ... }`.

    Soporta tres shapes (en este orden de preferencia):

    1. Multi-output (frontend actual)::

           {"plugins": ["opensearch", "obs"],
            "opensearch": {...}, "obs": {...}}

       Itera la lista y emite un bloque Logstash por cada plugin, todos
       envueltos en un solo ``output { ... }``.

    2. Single con `plugin_type`::

           {"plugin_type": "opensearch", "opensearch": {...}}

       Mapea el nombre UI a canónico (opensearch→elasticsearch, obs→s3) y
       genera un único bloque.

    3. Legacy dual: ``{"elasticsearch": {...}, "s3": {...}}`` sin
       ``plugin_type`` ni ``plugins`` — se mantiene por compatibilidad con
       payloads viejos.
    """
    if not raw:
        return ""

    plugin_blocks: list[str] = []

    # Forma 1: multi-output con lista `plugins`.
    if isinstance(raw.get("plugins"), list) and raw["plugins"]:
        for ui_name in raw["plugins"]:
            sub = raw.get(ui_name)
            if not isinstance(sub, dict):
                continue
            canonical = _canonical_output(ui_name)
            entry = _OUTPUT_DISPATCH.get(canonical)
            if not entry:
                continue
            model_cls, gen_fn = entry
            cfg = model_cls(**sub)
            plugin_blocks.append(gen_fn(cfg))
        if plugin_blocks:
            return "output {\n" + "\n".join(plugin_blocks) + "\n}"
        return ""

    # Forma 2: single con plugin_type.
    if "plugin_type" in raw:
        ui_name = raw["plugin_type"]
        canonical = _canonical_output(ui_name)
        model_cls, gen_fn = _OUTPUT_DISPATCH.get(
            canonical, _OUTPUT_DISPATCH["elasticsearch"]
        )
        if ui_name in raw and isinstance(raw[ui_name], dict):
            sub = raw[ui_name]
        else:
            sub = {k: v for k, v in raw.items() if k != "plugin_type"}
        cfg = model_cls(**(sub or {}))
        return f"output {{\n{gen_fn(cfg)}\n}}"

    # Forma 3: legacy dual elasticsearch + s3.
    blocks: list[str] = []
    if "elasticsearch" in raw and raw["elasticsearch"]:
        blocks.append(gen_output_elasticsearch(
            ElasticsearchOutputConfig(**raw["elasticsearch"])
        ))
    if "s3" in raw and raw["s3"]:
        blocks.append(gen_output_s3(S3OutputConfig(**raw["s3"])))
    if not blocks:
        return ""
    return "output {\n" + "\n".join(blocks) + "\n}"


# ===========================================================================
# Payloads del endpoint
# ===========================================================================
class GenerateFilterRequest(BaseModel):
    """Payload del endpoint que invoca al LLM para transformar el log."""

    raw_log: str = Field(
        ...,
        min_length=1,
        description="Log crudo de muestra (lo provee el nodo Input del canvas).",
    )
    namespace: str = Field(
        default="data",
        description="Parent bajo el cual viven los campos parseados (data.<campo>).",
    )
    ecs_overlay: bool = Field(
        default=False,
        description="Si True, agrega un overlay opcional de campos ECS estándar.",
    )
    feedback: str = Field(
        default="",
        description="Errores de la validación en sandbox (o notas del SA). Si viene, el LLM corrige el filter anterior en vez de generar de cero.",
    )
    previous_filter: str = Field(
        default="",
        description="El filter que falló la validación (acompaña a feedback).",
    )
    input_type: str = Field(
        default="",
        description="Tipo de input (obs, kafka, beats, jdbc). Para jdbc el LLM usa un prompt distinto (datos estructurados, sin grok/kv).",
    )


class FieldMapping(BaseModel):
    """Un campo detectado en el log por el LLM, con su mapeo a ECS.

    Lo consume el frontend en el step 2 (Visual Mapping) para mostrar la
    tabla "Detectamos la siguiente estructura en tus transacciones" — el
    cliente la valida con un click sin tener que mirar grok ni `mutate`.

    Los últimos tres campos (``is_ecs``, ``ecs_type_official``,
    ``normalized_path``) los completa el backend después de la respuesta del
    LLM cruzando el ``ecs_path`` contra la spec real (ECS 8.11). El LLM nunca
    los manda; son post-procesamiento.
    """

    raw_name: str = Field(..., description="Nombre del campo en el log original.")
    field_path: str = Field(
        default="",
        description="Path namespaced del campo (ej. data.trxl_resp). Es el path real en el evento.",
    )
    ecs_path: str = Field(
        default="",
        description="Back-compat: = field_path en modo namespaced; path ECS en formatos del catálogo.",
    )
    ecs_overlay_path: str | None = Field(
        default=None,
        description="Si el overlay ECS está activo y el campo mapea, su path ECS (ej. source.ip).",
    )
    type: str = Field(..., description="Tipo: string | integer | float | boolean | date | ip.")
    business_label: str = Field(..., description="Etiqueta amigable en español.")
    unit: str | None = Field(default=None, description="Unidad si aplica (ms, USD, ...) o null.")
    is_ecs: bool = Field(
        default=False,
        description="True si `ecs_path` existe en la spec oficial de ECS 8.11 (formatos del catálogo).",
    )
    ecs_type_official: str | None = Field(
        default=None,
        description="Tipo declarado por la spec ECS (puede diferir del que dijo el LLM).",
    )
    normalized_path: str = Field(
        default="",
        description="Path en forma dot (ej. transaction.id), normalizado por el backend.",
    )


class GenerateFilterResponse(BaseModel):
    """Respuesta del endpoint LLM: filter deployable + mapeo de campos."""

    filter_code: str
    fields: list[FieldMapping] = Field(default_factory=list)


class IndexTemplateRequest(BaseModel):
    """Payload para generar el index template de OpenSearch (fuente de verdad
    de tipos). Lo arma el frontend con los campos del step 2."""
    fields: list[dict] = Field(default_factory=list, description="Campos del step 2 (raw_name + type).")
    namespace: str = Field(default="data", description="Parent bajo el cual viven los campos.")
    opensearch_index: str = Field(default="logs-%{+YYYY.MM}", description="Índice del output (para derivar el pattern).")
    project_name: str = Field(default="log-analytics", description="Nombre del template (_index_template/<name>).")
    slug: str = Field(default="", description="Slug del caso (ej. fintech-transactions). Si tiene template curado, se previsualiza ese verbatim en vez del auto-generado. Vacío = derivar del índice.")


class IndexTemplateResponse(BaseModel):
    """El index template generado + el snippet listo para Kibana Dev Tools."""
    template_name: str
    index_pattern: str
    template: dict
    put_snippet: str


class OnboardingRequest(BaseModel):
    """Payload para armar el pipeline `.conf` completo.

    El flujo nuevo es de dos pasos: primero el frontend llama a
    `/generate-filter` con el `raw_log` del nodo Input y guarda el resultado.
    Después llama a este endpoint con ese `filter_code` ya generado más los
    configs de Input/Output. Sin embargo, por compatibilidad con clientes
    viejos seguimos aceptando `raw_log` y generando el filter on-the-fly si
    no llega `filter_code`.
    """

    raw_log: str | None = Field(
        default=None,
        description=(
            "Log crudo. Sólo necesario si no se manda `filter_code` "
            "(compatibilidad: el endpoint llamará al LLM en ese caso)."
        ),
    )
    filter_code: str | None = Field(
        default=None,
        description=(
            "Bloque filter {} ya generado por el LLM. Si viene, se usa "
            "directo y se omite la llamada al LLM."
        ),
    )
    # Aceptamos dict genérico para soportar tanto el formato nuevo (con
    # `plugin_type`) como el legacy. La normalización vive en los
    # generadores.
    input_config: dict | None = Field(
        default=None,
        description="Config del nodo input. Formato: {plugin_type, <plugin>: {...}}."
    )
    output_config: dict | None = Field(
        default=None,
        description="Config del nodo output. Formato: {plugin_type, <plugin>: {...}}."
    )


class PipelineResponse(BaseModel):
    """Respuesta exitosa: el pipeline generado."""

    status: str = Field(default="success")
    filter_code: str = Field(..., description="Bloque filter {} usado en el pipeline.")
    pipeline_code: str | None = Field(
        default=None,
        description="Pipeline completo (input + filter + output) si se proveyó config."
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
_VERTICALS_MARKER = "/*__VERTICALS_JSON__*/null"


@app.get("/", include_in_schema=False)
def index() -> Response:
    """Sirve la página de onboarding con los verticales inyectados.

    El front deriva LOG_EXAMPLES/EXAMPLE_DATA/etc. de `window.__VERTICALS__`.
    Lo inyectamos server-side (reemplazando `_VERTICALS_MARKER`) para que la
    data esté disponible SINCRÓNICAMENTE al parsear el script — sin reordenar
    el boot a un fetch async. `no-cache` fuerza revalidar siempre el index.html.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    payload = json.dumps(verticals.front_payload(), ensure_ascii=False)
    html = html.replace(_VERTICALS_MARKER, payload, 1)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/login", include_in_schema=False)
def login_page() -> Response:
    """Compat: el login ahora es un overlay dentro de la SPA (con la app blureada
    detrás). Redirigimos a `/`, que muestra ese overlay si no hay sesión."""
    return RedirectResponse(url="/", status_code=307)


@app.post("/auth/login", include_in_schema=False)
async def auth_login(request: Request) -> Response:
    """Body JSON ``{email, password}``. Valida contra allowlist + store de
    usuarios (alta en el primer login) y setea la cookie de sesión firmada."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    ip = request.client.host if request.client else None
    if not auth.AUTH_ENABLED:
        return JSONResponse({"ok": True, "auth": False})
    if not auth.check_login(email, password):
        audit.record("login_failed", email or "(vacío)", user=email or "-", ip=ip)
        return JSONResponse({"ok": False, "error": "Credenciales inválidas o email no autorizado."},
                            status_code=401)
    audit.record("login", email, user=email, ip=ip)
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session_token(email), max_age=auth.SESSION_TTL,
                    httponly=True, samesite="lax", secure=auth.SECURE_COOKIES, path="/")
    return resp


@app.post("/auth/logout", include_in_schema=False)
def auth_logout(request: Request) -> Response:
    email = auth.read_session_token(request.cookies.get(auth.COOKIE_NAME)) if auth.AUTH_ENABLED else None
    if email:
        audit.record("logout", email, user=email)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/auth/me", include_in_schema=False)
def auth_me(request: Request) -> dict:
    """Quién está logueado (para la UI). Lee la cookie directo (el endpoint es
    público, no liga contexto). En modo single-user devuelve auth:false."""
    if not auth.AUTH_ENABLED:
        return {"auth": False}
    email = auth.read_session_token(request.cookies.get(auth.COOKIE_NAME))
    return {"auth": True, "logged_in": bool(email), "email": email, "is_admin": auth.is_admin(email)}


# ── Panel de admin (solo hosteado, emails en SA_ADMINS) ──────────────────────
def _require_admin() -> str:
    """Devuelve el email del admin actual o corta con 403. Usa el contexto que
    liga el middleware (los /api/* están protegidos)."""
    ctx = auth.current_user_var.get()
    email = ctx.email if ctx is not None else None
    if not auth.is_admin(email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"stage": "admin", "message": "Requiere permisos de administrador."})
    return email


@app.get("/api/v1/admin/users", tags=["admin"], summary="Usuarios registrados (admin)")
def admin_list_users() -> dict:
    _require_admin()
    return {"users": auth.list_users(), "admins": sorted(auth._admins())}


@app.post("/api/v1/admin/users/reset", tags=["admin"], summary="Resetea la contraseña de un usuario (admin)")
def admin_reset_user(request: dict) -> dict:
    admin_email = _require_admin()
    target = str(request.get("email", "") or "").strip().lower()
    if not target:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"stage": "admin", "message": "Falta el email a resetear."})
    ok = auth.admin_reset_user(target)
    audit.record("admin_reset_user", f"{admin_email} → {target} ({'ok' if ok else 'no existía'})", user=admin_email)
    return {"ok": ok, "email": target}


@app.get("/api/v1/admin/audit", tags=["admin"], summary="Audit log (admin)")
def admin_audit(limit: int = 200) -> dict:
    _require_admin()
    return {"entries": audit.tail(max(1, min(1000, limit)))}


@app.get("/api/v1/verticals", tags=["verticals"], summary="Registro de verticales de demo")
def get_verticals() -> dict:
    """Payload declarativo de los verticales (grupos + specs de front). El mismo
    que se inyecta en `GET /`; expuesto para tests/tooling."""
    return verticals.front_payload()


@app.get("/health", tags=["infra"])
def health_check() -> dict:
    """Liveness probe para balanceadores / Kubernetes en Huawei Cloud."""
    return {"status": "ok"}


def _llm_filter(raw_log: str, namespace: str = "data", ecs_overlay: bool = False,
                feedback: str = "", previous_filter: str = "",
                input_type: str = "") -> dict:
    """Llama al generador y mapea sus errores a HTTPException.

    Devuelve ``{"filter_code": str, "fields": list[dict]}`` en modo namespaced
    (ECS opcional vía ``ecs_overlay``). Con ``feedback``, el LLM corrige el
    ``previous_filter`` en vez de generar de cero.
    """
    try:
        return generate_logstash_filter(
            raw_log, namespace=namespace, ecs_overlay=ecs_overlay,
            feedback=feedback, previous_filter=previous_filter,
            input_type=input_type,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        message = str(exc)
        is_config_error = "MAAS_API_KEY" in message
        raise HTTPException(
            status_code=(
                status.HTTP_500_INTERNAL_SERVER_ERROR
                if is_config_error
                else status.HTTP_502_BAD_GATEWAY
            ),
            detail=message,
        ) from exc


@app.post(
    "/api/v1/onboarding/generate-filter",
    response_model=GenerateFilterResponse,
    tags=["onboarding"],
    summary="Genera filter Logstash + tabla de campos (LLM glm-5.2)",
)
def generate_filter_endpoint(request: GenerateFilterRequest) -> GenerateFilterResponse:
    """Invoca al LLM para parsear el log y devolver tanto el filter como la
    tabla de mapeo de campos a ECS.

    El frontend usa esto al pasar de step 1 (Raw log) a step 2 (Mapping):
    el cliente ve los campos en su idioma y los confirma sin tocar Logstash.
    El `filter_code` queda guardado en el state del wizard para componerlo
    después con `/generate-pipeline`.

    Cada field que devuelve el LLM se enriquece acá con tres atributos
    derivados de la spec ECS 8.11 (``docs/fields.csv``):
      - ``is_ecs``: ``True`` si el path está en la spec oficial.
      - ``ecs_type_official``: tipo declarado por ECS (puede no coincidir
        con el que dijo el LLM — en ese caso el frontend puede marcarlo).
      - ``normalized_path``: el path en forma dot (``transaction.id``)
        canónica, para que el frontend muestre consistente.

    Códigos de error:
    - **400**: el LLM rechazó la entrada (ValueError).
    - **500**: error de configuración (p. ej. MAAS_API_KEY ausente).
    - **502**: la llamada al LLM falló o devolvió JSON inválido.
    """
    result = _llm_filter(
        request.raw_log,
        namespace=request.namespace,
        ecs_overlay=request.ecs_overlay,
        feedback=request.feedback,
        previous_filter=request.previous_filter,
        input_type=request.input_type,
    )
    # En modo namespaced los campos NO son ECS (is_ecs=False, field_path bajo el
    # namespace). Solo enriquecemos con classify_field los campos que vienen
    # de un detector del catálogo (Apache/Syslog/CEF) — esos sí traen ecs_path
    # ECS y `is_ecs=True` desde el generador.
    enriched_fields = []
    for f in result.get("fields", []):
        if f.get("is_ecs"):
            info = classify_field(f.get("ecs_path", ""))
            enriched_fields.append({
                **f,
                "is_ecs": info["is_ecs"],
                "ecs_type_official": info["ecs_type"],
                "normalized_path": info["normalized"],
            })
        else:
            enriched_fields.append(f)
    return GenerateFilterResponse(
        filter_code=result["filter_code"],
        fields=enriched_fields,
    )


@app.post(
    "/api/v1/onboarding/index-template",
    response_model=IndexTemplateResponse,
    tags=["onboarding"],
    summary="Genera el index template de OpenSearch (tipos) + snippet Dev Tools",
)
def index_template_endpoint(request: IndexTemplateRequest) -> IndexTemplateResponse:
    """Construye el index template a partir de los campos del step 2.

    El mapeo de TIPOS vive acá (template), no en Logstash: keyword-by-default
    vía dynamic template + propiedades explícitas para medidas/ip/date. El
    operador aplica el `put_snippet` en Kibana Dev Tools sobre el cluster
    vacío, ANTES de iniciar la ingesta (ver deploy en dos fases).

    Precedencia (misma que `_apply_index_templates`, para que el PREVIEW del
    paso 2 sea fiel a lo que realmente se aplica): si el `slug` (o el derivado
    del índice) tiene un template curado en ``templates/<slug>.json``, se
    previsualiza ese **verbatim**; si no, el auto-generado desde los campos.
    Esto además hace que un tipo curado (ej. fintech-transactions) se vea
    idéntico cargándolo solo o junto a otros — antes divergía porque el
    auto-generado dependía de los `fields` que llegaban (crudos vs. enriquecidos
    por el paso de mapping del wizard).
    """
    template_name = request.project_name or "log-analytics"
    slug = (request.slug or "").strip() or _slug_from_index(request.opensearch_index)
    bundled = _bundled_index_template(slug)
    if bundled is not None:
        template = bundled
        patterns = bundled.get("index_patterns") or [index_pattern_from_name(request.opensearch_index)]
        index_pattern = patterns[0]
    else:
        template = build_index_template(
            request.fields, request.namespace, request.opensearch_index
        )
        index_pattern = index_pattern_from_name(request.opensearch_index)
    return IndexTemplateResponse(
        template_name=template_name,
        index_pattern=index_pattern,
        template=template,
        put_snippet=put_snippet(template_name, template),
    )


@app.post(
    "/api/v1/onboarding/generate-pipeline",
    response_model=PipelineResponse,
    tags=["onboarding"],
    summary="Arma el pipeline .conf con input + filter + output",
)
def generate_pipeline(request: OnboardingRequest) -> PipelineResponse:
    """Compone el pipeline `.conf` final.

    - Si viene `filter_code`, lo usa directo (no llama al LLM).
    - Si no, llama al LLM con `raw_log` (compat con clientes viejos).
    """
    if request.filter_code:
        filter_code = request.filter_code
    elif request.raw_log:
        # _llm_filter ahora devuelve {filter_code, fields}; acá sólo usamos
        # el filter_code porque el endpoint de assemble no expone fields.
        filter_code = _llm_filter(request.raw_log)["filter_code"]
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Falta `filter_code` o `raw_log`.",
        )

    # Defensa en profundidad: el filter_code puede venir de tres orígenes
    # distintos — LLM (ya saneado), cached example hardcodeado en el
    # frontend, o textarea editado por el operador. Solo el primero pasa
    # por el strip de `generate_logstash_filter`. Aplicamos el strip acá
    # como catch-all para garantizar que el .conf final nunca tiene
    # comentarios sin importar el origen. Idempotente: ya saneado pasa OK.
    filter_code = strip_logstash_comments(filter_code)

    pipeline_code = None
    if request.input_config or request.output_config:
        parts: list[str] = []
        input_block = generate_input_block(request.input_config)
        if input_block:
            parts.append(input_block)
        parts.append(filter_code)
        output_block = generate_output_block(request.output_config)
        if output_block:
            parts.append(output_block)
        pipeline_code = "\n\n".join(parts)

    return PipelineResponse(
        status="success",
        filter_code=filter_code,
        pipeline_code=pipeline_code,
    )


# NOTE: el endpoint POST /api/v1/demo/deploy (con synthetic_logs +
# opensearch_client) fue removido. La vía actual del wizard es
# POST /api/v1/terraform/deploy (más abajo), que sube el log REAL del paso 1
# a OBS y deja que Logstash/CSS lo procesen end-to-end con terraform apply.


# NOTE: El copiloto (RAG de docs DeepSeek-3.2) fue removido de la plataforma: el chatbot
# ahora vive 100% en OpenSearch (agente conversacional provisionado por capabilities,
# se consulta desde Dev Tools). Se quitaron los endpoints /api/v1/chatbot/* y sus
# modelos. `chatbot.py`/`document_loader.py` quedan en el repo por si se reexpone.


# ── Industry matching: campos típicos por industria ──────────────────────────
# Industry matching: vocabulario por industria, del registro declarativo
# verticals/ (cada vertical aporta `industry_fields` + los sub-specs).
_INDUSTRY_FIELDS = verticals.industry_fields()


def _match_industry(detected_fields: list[str]) -> dict:
    """Matchea los campos detectados contra las industrias conocidas."""
    detected_set = {f.lower() for f in detected_fields}
    best_slug = ""
    best_score = 0.0
    for slug, industry_fields in _INDUSTRY_FIELDS.items():
        industry_set = {f.lower() for f in industry_fields}
        if not industry_set:
            continue
        overlap = len(detected_set & industry_set)
        score = overlap / len(industry_set)
        if score > best_score:
            best_score = score
            best_slug = slug
    return {"slug": best_slug, "score": best_score} if best_slug else {"slug": "", "score": 0.0}


@app.post(
    "/api/v1/obs/read-sample",
    tags=["onboarding"],
    summary="Lee una muestra del primer objeto bajo un prefijo OBS y detecta la industria",
)
def obs_read_sample(request: dict) -> dict:
    """Lee el primer objeto del bucket + prefijo, extrae la primera línea,
    detecta campos con el LLM, y matchea la industria.

    Body: ``{access_key, secret_key, endpoint, region, bucket, prefix}``
    Returns: ``{sample_line, total_objects, object_key, industry_match}``
    """
    from obs_client import OBSClient, OBSConfigError, OBSUploadError

    ak = request.get("access_key", "")
    sk = request.get("secret_key", "")
    endpoint = request.get("endpoint") or _default_obs_endpoint()
    region = request.get("region") or get_region()
    bucket = request.get("bucket", "")
    prefix = request.get("prefix", "")

    if not ak or not sk:
        raise HTTPException(status_code=400, detail="Faltan credenciales OBS (AK/SK).")
    if not bucket:
        raise HTTPException(status_code=400, detail="Falta el nombre del bucket.")

    try:
        client = OBSClient(
            access_key_id=ak,
            secret_access_key=sk,
            endpoint=endpoint,
            bucket=bucket,
        )
    except OBSConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        sample_line, total_objects, object_key = client.read_sample(prefix)
    except OBSUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        client.close()

    # Detectar campos con el LLM
    detected_fields: list[str] = []
    try:
        result = generate_logstash_filter(sample_line, namespace="data")
        detected_fields = [f.get("name", "") or f.get("field", "") for f in result.get("fields", [])]
    except Exception:
        pass

    # Industry matching
    industry_match = _match_industry(detected_fields)

    return {
        "sample_line": sample_line,
        "total_objects": total_objects,
        "object_key": object_key,
        "detected_fields": detected_fields,
        "industry_match": industry_match,
    }


@app.get(
    "/api/v1/settings/maas",
    tags=["settings"],
    summary="Estado de la API key de MaaS (configurada en la plataforma o del env)",
)
def get_maas_settings() -> dict:
    """Nunca devuelve la key entera — solo si está configurada, de dónde sale
    ('settings' = ⚙ Configuración, 'env' = MAAS_API_KEY) y los últimos 4 chars.
    `models` = qué modelo usa cada consumo real de LLM (para la vista
    Configuración). El copiloto quedó fuera: sus endpoints se removieron."""
    import capabilities as caps
    import maas_integrator as _mi

    key = _mi.get_maas_api_key()
    return {
        "configured": bool(key),
        "source": _mi.maas_key_source(),
        "masked": f"••••{key[-4:]}" if key else "",
        # Modelo que usa cada consumo (solo lectura — se configuran por env var).
        "models": {
            # Paso 2 del wizard: análisis del log + generación del filter.
            "pipeline": _mi.get_pipeline_model(),
            # Chatbot de OpenSearch (capabilities): razonador + generador PPL.
            "chatbot_llm": caps.maas_llm_model(),
            "chatbot_ppl": caps.maas_ppl_model(),
        },
    }


@app.post(
    "/api/v1/settings/maas",
    tags=["settings"],
    summary="Configura la API key de MaaS que usan TODOS los consumos de modelos",
)
def set_maas_settings(request: dict) -> dict:
    """Body: ``{api_key}``. Vacía → se borra la configurada y se vuelve a la del
    env (si hay). La usan: el análisis del log y los connectors del chatbot de
    OpenSearch (queda embebida en el cluster al provisionar)."""
    from maas_integrator import get_maas_api_key, maas_key_source, set_maas_api_key

    set_maas_api_key(str(request.get("api_key", "") or ""))
    key = get_maas_api_key()
    audit.record("settings_maas", "MaaS API key actualizada" if key else "MaaS API key borrada")
    return {"configured": bool(key), "source": maas_key_source()}


@app.get(
    "/api/v1/settings/huawei",
    tags=["settings"],
    summary="Cuenta Huawei Cloud del SA (project id, región, VPC, bucket de demos)",
)
def get_huawei_settings_endpoint() -> dict:
    """Valores guardados por UI (ninguno es secreto). `source` indica si el
    deploy va a usar estos settings ('settings') o el terraform.tfvars estático
    como fallback ('tfvars')."""
    import maas_integrator as _mi

    values = _mi.get_huawei_settings()
    infra_configured = all(
        values.get(f) for f in ("vpc_id", "subnet_id", "security_group_id"))
    return {
        "values": values,
        "source": "settings" if infra_configured else "tfvars",
        "effective_region": _mi.get_region(),
    }


@app.post(
    "/api/v1/settings/huawei",
    tags=["settings"],
    summary="Configura la cuenta Huawei Cloud que usan los deploys de Terraform",
)
def set_huawei_settings_endpoint(request: dict) -> dict:
    """Body: dict con project_id/region/vpc_id/subnet_id/security_group_id/
    availability_zone/demo_bucket (los vacíos se borran). Los IDs de infra
    pisan el terraform.tfvars al armar el deploy."""
    import maas_integrator as _mi

    _mi.set_huawei_settings({k: str(v or "") for k, v in dict(request).items()})
    audit.record("settings_huawei", "cuenta Huawei actualizada")
    return get_huawei_settings_endpoint()


@app.get(
    "/api/v1/settings/obs",
    tags=["settings"],
    summary="OBS AK/SK guardadas en la cuenta del usuario (por-usuario en modo hosteado)",
)
def get_obs_settings() -> dict:
    """Devuelve las AK/SK guardadas del usuario logueado (protegido por auth en modo
    hosteado). Permite prefilar la card y que el deploy funcione al entrar desde otro
    navegador / con el login del owner, sin re-tipear las claves."""
    import maas_integrator as _mi

    creds = _mi.get_obs_creds()
    return {
        "configured": bool(creds.get("ak") and creds.get("sk")),
        "ak": creds.get("ak", ""),
        "sk": creds.get("sk", ""),
    }


@app.post(
    "/api/v1/settings/obs",
    tags=["settings"],
    summary="Guarda las OBS AK/SK en la cuenta del usuario",
)
def set_obs_settings(request: dict) -> dict:
    """Body: ``{access_key, secret_key}`` (ambas vacías = borra). Se guardan por-usuario
    en el settings file del servidor."""
    import maas_integrator as _mi

    _mi.set_obs_creds(str(request.get("access_key", "") or ""), str(request.get("secret_key", "") or ""))
    creds = _mi.get_obs_creds()
    audit.record("settings_obs", "OBS AK/SK actualizadas" if creds.get("ak") else "OBS AK/SK borradas")
    return {"configured": bool(creds.get("ak") and creds.get("sk"))}


# ── Pre-carga de datasets de demo al bucket del SA ──────────────────────────
# Cada demo lee su dataset de `<slug>-logs/` en el bucket configurado. El mapa
# (slug → archivos de datasets/) sale del registro declarativo verticals/ y
# reemplaza la pre-carga manual con obsutil: el botón "Preparar bucket" de
# ⚙ Configuración sube lo que falte.
_DEMO_DATASET_FILES: dict[str, list[str]] = verticals.demo_dataset_files()


@app.post(
    "/api/v1/datasets/preload",
    tags=["settings"],
    summary="Sube los datasets de demo al bucket del SA (SSE con progreso)",
)
def preload_datasets(request: dict):
    """Body: ``{access_key, secret_key, bucket?, endpoint?, region?,
    only_missing?=true}``. Crea el bucket si no existe y sube cada dataset a
    su prefijo ``<slug>-logs/`` streameando el progreso por archivo (SSE,
    mismo formato que el deploy: eventos ``file`` y ``complete``)."""
    from obs_client import OBSClient, OBSConfigError, OBSUploadError

    ak = str(request.get("access_key", "") or "")
    sk = str(request.get("secret_key", "") or "")
    region = str(request.get("region", "") or "") or get_region()
    endpoint = str(request.get("endpoint", "") or "") or f"https://obs.{region}.myhuaweicloud.com"
    bucket = (str(request.get("bucket", "") or "")
              or get_huawei_settings().get("demo_bucket", ""))
    only_missing = bool(request.get("only_missing", True))
    if not ak or not sk:
        raise HTTPException(status_code=400, detail="Faltan credenciales OBS (AK/SK).")
    if not bucket:
        raise HTTPException(
            status_code=400,
            detail="Falta el bucket de demos (⚙ Configuración → Cuenta Huawei Cloud).")

    def _gen():
        try:
            client = OBSClient(access_key_id=ak, secret_access_key=sk,
                               endpoint=endpoint, bucket=bucket)
        except OBSConfigError as exc:
            yield _sse({"type": "error", "message": str(exc)})
            return
        try:
            try:
                created = client.ensure_bucket(region=region)
                if created:
                    yield _sse({"type": "bucket", "message": f"Bucket {bucket} creado en {region}"})
            except OBSConfigError as exc:
                yield _sse({"type": "error", "message": str(exc)})
                return
            uploaded = skipped = errors = 0
            for slug, files in _DEMO_DATASET_FILES.items():
                for fname in files:
                    key = f"{slug}-logs/{fname}"
                    src = _DATASETS_DIR / fname
                    if not src.is_file():
                        errors += 1
                        yield _sse({"type": "file", "slug": slug, "key": key,
                                    "state": "error", "detail": f"falta datasets/{fname} en el repo"})
                        continue
                    try:
                        if only_missing and client.object_exists(key):
                            skipped += 1
                            yield _sse({"type": "file", "slug": slug, "key": key, "state": "skipped"})
                            continue
                        yield _sse({"type": "file", "slug": slug, "key": key, "state": "uploading",
                                    "size_mb": round(src.stat().st_size / 1e6, 1)})
                        client.put_file(key, str(src))
                        uploaded += 1
                        yield _sse({"type": "file", "slug": slug, "key": key, "state": "done"})
                    except (OBSUploadError, OBSConfigError) as exc:
                        errors += 1
                        yield _sse({"type": "file", "slug": slug, "key": key,
                                    "state": "error", "detail": str(exc)})
            yield _sse({"type": "complete",
                        "uploaded": uploaded, "skipped": skipped, "errors": errors})
        finally:
            client.close()

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Terraform Deploy
# ---------------------------------------------------------------------------
import subprocess
import json
import re
import tempfile
import os


class PipelineCase(BaseModel):
    """Un caso de pipeline para deploy múltiple."""
    slug: str = Field(..., description="Identificador del caso (firewall, fintech-transactions, etc.)")
    raw_log: str = Field(default="", description="Log original del paso 1")
    filter_code: str = Field(default="", description="Bloque filter {} generado")
    fields: list[dict] = Field(default_factory=list, description="Campos del step 2")
    index_name: str = Field(default="logs-%{+YYYY.MM}", description="Índice del output")
    obs_prefix: str = Field(default="logs/", description="Prefijo OBS para este caso (el bucket es universal, viene del request)")
    read_existing_bucket: bool = Field(default=False, description="El caso lee datos reales ya presentes en su prefijo (ej. CTS → CloudTraces/); no subir sintéticos/dataset encima.")
    log_file_content: str = Field(default="", description="Contenido completo del archivo importado por el usuario (custom). Se sube tal cual a OBS, sin sintéticos.")
    document_id: str = Field(default="", description="document_id de dedup para read_existing (ej. CTS → %{trace_id}, fintech → %{[@metadata][generated_id]}). Vacío = ids auto de Logstash.")


class TerraformDeployRequest(BaseModel):
    """Request para deploy con Terraform."""
    project_name: str = Field(default="log-analytics")
    pipeline_conf: str = Field(..., description="Pipeline .conf generado")
    raw_log: str = Field(default="", description="Log original del paso 1")
    obs_access_key: str = Field(default="")
    obs_secret_key: str = Field(default="")
    obs_bucket: str = Field(default="")
    obs_endpoint: str = Field(default_factory=lambda: _default_obs_endpoint())
    obs_region: str = Field(default_factory=get_region)
    obs_prefix: str = Field(default="logs/")
    opensearch_password: str = Field(default="")
    opensearch_user: str = Field(default="admin")
    opensearch_index: str = Field(default="logs-%{+YYYY.MM}")
    pipeline_slug: str = Field(default="")
    read_existing_bucket: bool = Field(default=False)
    https_enabled: bool = Field(default=True, description="Habilitar HTTPS para OpenSearch")
    fields: list[dict] = Field(default_factory=list)
    namespace: str = Field(default="data", description="Namespace de los campos (para el index template).")
    industry_label: str = Field(default="", description="Etiqueta de la industria confirmada en el paso 1 (despliegue productivo). Se persiste en el registro y nombra la fuente en el chatbot.")
    log_file_content: str = Field(default="", description="Contenido del archivo importado (custom single-case). Se sube tal cual a OBS, sin sintéticos.")
    synthetic_count: int = Field(default=200, ge=0, le=5000)
    synthetic_window_hours: int = Field(default=24, ge=1, le=168)
    start_ingestion: bool = Field(default=False)
    cases: list[PipelineCase] = Field(default_factory=list, description="Casos múltiples para deploy en paralelo")
    fresh_deploy: bool = Field(default=False, description="Si True, limpia el registro de pipelines existentes antes de agregar los nuevos (deploy desde wizard). Si False, mergea con pipelines existentes (Nuevo pipeline).")
    existing_opensearch_endpoint: str = Field(default="", description="Endpoint de un cluster OpenSearch existente (ip:port). Si no está vacío, se saltea la creación del cluster OS y se usa este. Solo para demos con chatbot ya habilitado.")


class TerraformDeployResponse(BaseModel):
    """Response del deploy."""
    opensearch_endpoint: str
    logstash_endpoint: str
    dashboards_url: str
    status: str
    dashboards_imported: bool = False
    index_template_applied: bool = False


class ApplySchemaResponse(BaseModel):
    """Response del paso 2: aplicar index template + importar dashboards al cluster
    existente (sin terraform). Disparado por el operador desde el wizard."""
    status: str
    index_template_applied: bool = False
    dashboards_imported: bool = False
    capabilities: dict = Field(default_factory=dict, description="Capabilities provisionadas por slug (conversacional, anomalías, forecast, alerting).")
    message: str = ""


class ExportStarterKitRequest(BaseModel):
    """Payload del endpoint que arma el .zip del starter kit para el cliente."""
    pipeline_conf: str = Field(..., min_length=1, description="filter+input+output del wizard, lo que el cliente vio en la demo")
    project_name: str = Field(default="log-analytics", description="Nombre del proyecto, usado para el filename del .zip y el README")
    # Para incluir el index template en el kit (los tipos viven en OpenSearch).
    fields: list[dict] = Field(default_factory=list, description="Campos del step 2 (raw_name + type) para el index template.")
    namespace: str = Field(default="data", description="Namespace de los campos.")
    opensearch_index: str = Field(default="logs-%{+YYYY.MM}", description="Índice del output (para el pattern del template).")


class TerraformDestroyRequest(BaseModel):
    """Creds opcionales para el teardown. Si el deploy persistió las creds
    (`destroy.auto.tfvars.json`), no hace falta mandarlas. Para entornos
    legacy (creds ya borradas) el frontend las reenvía desde el form."""
    obs_access_key: str = ""
    obs_secret_key: str = ""
    opensearch_password: str = ""


class TerraformDestroyResponse(BaseModel):
    """Response del destroy. status=noop si no había entorno activo."""
    status: str
    message: str = ""


class TerraformStatusResponse(BaseModel):
    """Status del entorno (lectura idempotente del tfstate).

    El frontend lo consume al cargar la página para decidir si mostrar
    el banner global "Entorno activo" — así los botones Dashboards,
    Starter Kit y Destruir sobreviven a F5/refresh, en vez de depender
    solo de flags in-memory del browser.

    Los campos derivados de outputs (dashboards_url, pipeline_conf,
    project_name) quedan None si `terraform output -json` falla o si el
    state es de una versión previa que no exportaba esos outputs. El
    banner degrada: muestra solo los botones cuya pieza llegó.
    """
    active: bool
    deployed_at: str | None = None
    deployed_seconds_ago: int | None = None
    dashboards_url: str | None = None
    pipeline_conf: str | None = None
    project_name: str | None = None
    opensearch_endpoint: str | None = None
    opensearch_public_endpoint: str | None = None
    logstash_endpoint: str | None = None
    index_template_snippet: str | None = None
    index_template_name: str | None = None
    # Pipelines corriendo en paralelo en el cluster (cada una con su índice +
    # prefijo OBS). Lista de {slug, index, obs_prefix, active, dashboards_imported}.
    pipelines: list[dict] = Field(default_factory=list)
    # Indica si los dashboards baseline fueron importados al desplegar.
    dashboards_imported: bool = False
    # Capabilities provisionadas por slug (agent_id, detector_id, etc.). El
    # copiloto lo usa para habilitar el modo "preguntá a tus datos".
    capabilities: dict = Field(default_factory=dict)
    https_enabled: bool = False


def _generate_starter_kit_readme(project_name: str) -> str:
    """README del .zip con instrucciones paso-a-paso para que el cliente
    replique en su propia cuenta Huawei Cloud lo que vio en la demo."""
    return f"""# Starter Kit — pipeline Logstash + OpenSearch en Huawei Cloud

Proyecto: **{project_name}**

Este paquete contiene todo lo necesario para replicar, en tu propia cuenta de
Huawei Cloud, el pipeline que viste en la demo en vivo.

## Requisitos

- Terraform >= 1.5 (descarga en https://www.terraform.io/downloads).
- Credenciales de Huawei Cloud (AK/SK) con permisos sobre Cloud Search Service
  (CSS), Object Storage Service (OBS), y VPC.

## Pasos

1. Copiá `terraform/terraform.tfvars.example` a `terraform/terraform.tfvars` y
   completá tus credenciales:
   ```
   cp terraform/terraform.tfvars.example terraform/terraform.tfvars
   # editar el archivo y poner AK/SK, password de OpenSearch, etc.
   ```

2. Inicializá Terraform (descarga el provider Huawei, ~50 MB la primera vez):
   ```
   cd terraform
   terraform init
   ```

3. Aplicá la infraestructura. Tarda 5-15 min (creación de clusters CSS):
   ```
   terraform apply -auto-approve
   ```

4. Cuando termine, los endpoints del cluster están en `terraform output`:
   ```
   terraform output opensearch_endpoint
   terraform output dashboards_url
   ```

5. **IMPORTANTE — aplicá el index template ANTES de ingerir datos.** Los tipos
   de los campos se definen en `index-template.json` (no en Logstash). Si dejás
   que Logstash cree el índice primero, OpenSearch infiere los tipos del primer
   documento (dynamic mapping) y el template ya no aplica retroactivamente.
   Aplicalo sobre el cluster vacío:
   ```
   curl -X PUT "https://<opensearch_endpoint>:9200/_index_template/<proyecto>" \\
        -H "Content-Type: application/json" \\
        -u "admin:<password>" \\
        --data-binary @index-template.json -k
   ```
   (o pegá el contenido en Kibana → Dev Tools con `PUT _index_template/<proyecto>`).

6. **Opcional — importá los dashboards baseline.** El archivo `dashboards.ndjson`
   contiene visualizaciones pre-configuradas para el caso de uso (eventos en el
   tiempo, top fields, métricas). Importalo en OpenSearch Dashboards:
   - Abrí Dashboards → Management → Saved Objects → Import
   - Seleccioná `dashboards.ndjson` y confirmá
   - O vía API:
   ```
   curl -X POST "https://<opensearch_endpoint>:9200/_dashboards/api/saved_objects/_import?overwrite=true" \\
        -H "osd-xsrf: true" \\
        -u "admin:<password>" \\
        -F file=@dashboards.ndjson -k
   ```

7. Recién ahora conectá tu fuente de logs al bucket OBS creado (o apuntá al
   endpoint Logstash). Logstash crea el índice usando el template y los eventos
   se indexan con los tipos correctos.

## Estructura del paquete

```
starter-kit/
├── logstash.conf                     # Filter + input + output (solo PARSEA: envelope, kv, json, @timestamp)
├── index-template.json               # Mapping/tipos de OpenSearch (aplicar ANTES de ingerir)
├── dashboards.ndjson                 # Dashboards baseline (opcional, importar en Dashboards)
├── terraform/
│   ├── main.tf                       # Definición de OpenSearch + Logstash + OBS
│   └── terraform.tfvars.example      # Template de credenciales (completalo)
├── RUNBOOK-consola.md                # Los mismos pasos, pero por CONSOLA Huawei (PoC guiada con el SA)
└── README.md                         # Este archivo
```

> ¿Terraform o consola? Los dos caminos crean lo mismo. Si están haciendo la
> PoC junto al arquitecto de soluciones, lo habitual es seguir
> `RUNBOOK-consola.md` (consola, paso a paso). Este README documenta el
> camino Terraform (automatizado, reproducible).

## Notas técnicas

- **El tipado vive en `index-template.json`, no en Logstash.** El `logstash.conf`
  solo parsea/estructura (envelope, kv/json, `@timestamp`); el template de
  OpenSearch decide los tipos (keyword por default + numéricos/date/ip
  explícitos). Esto evita pérdida de datos (ej. códigos "000") y conflictos de
  mapping. Por eso `manage_template => false` en el output.
- El cluster CSS que crea el Terraform es de tamaño chico (apto para
  pruebas). Para producción, escalá los nodos via el campo `node_config`
  en `main.tf` — los valores típicos para volumen alto están comentados
  en el archivo.
- Si tu equipo prefiere otra región (la-south-2 es Santiago de Chile por
  default), cambialo en `terraform.tfvars` antes del apply.

## Soporte

Este paquete fue generado por una herramienta interna de preventa. Si
necesitás ayuda para integrarlo a tu pipeline existente, contactá al
equipo que hizo la demo.
"""


def _generate_console_runbook(project_name: str, index_name: str, region: str) -> str:
    """Runbook para el deploy CONJUNTO por consola Huawei (SA + cliente).

    En la práctica el cliente nunca aplica el Terraform solo: durante la PoC
    (cupón de 1 mes) el SA y el cliente crean los recursos juntos desde la
    consola. Este runbook deja por escrito exactamente qué crear y en qué
    orden, con los mismos artefactos del kit (conf, template, dashboards)."""
    return f"""# Runbook — deploy por consola Huawei Cloud (PoC guiada)

Proyecto: **{project_name}** · Región: **{region}** · Índice: **{index_name}**

Pasos para crear por **consola** lo mismo que despliega el Terraform del kit.
Pensado para hacerlo junto al arquitecto de soluciones durante la PoC.

## 1. Cluster OpenSearch (CSS)

Consola → Cloud Search Service → **Create Cluster**:

- Engine: **OpenSearch 3.4** · Región: {region}
- Nodos: 1 × `ess.spec-4u8g` (PoC) · Disco: HIGH 40 GB
- VPC / subnet / security group: los de tu red (anotá cuáles)
- **Security mode: ON** con password de admin (guardala)
- HTTPS según tu política (la demo usa HTTP + NAT privado)

## 2. Cluster Logstash (CSS)

Consola → CSS → **Create Cluster** → tipo **Logstash 7.10**:

- 1 × `ess.spec-4u8g`, misma VPC/subnet/SG que OpenSearch

## 3. Index template (ANTES de ingerir)

Los tipos de campos viven en `index-template.json`, NO en Logstash. Aplicalo
con el índice todavía inexistente — OpenSearch Dashboards → Dev Tools:

```
PUT _index_template/{project_name}
<contenido de index-template.json>
```

Si ya ingeriste sin template: borrá el índice y volvé a ingerir.

## 4. Pipeline de Logstash

Consola → cluster Logstash → **Configuration Center** → Create:

- Pegá el contenido de `logstash.conf`
- Revisá el input OBS: bucket, prefijo, AK/SK de la cuenta del cliente y
  `delete => false` (lectura read-only: NO borra los objetos)
- Guardar → **Start**. El índice `{index_name}` se crea con los tipos del template.

## 5. Dashboards

OpenSearch Dashboards → Management → Saved Objects → **Import** →
`dashboards.ndjson`. Quedan el dashboard del caso + sus visualizaciones.

## 6. Verificación

- Dev Tools: `GET _cat/indices?v` → el índice crece.
- `GET {index_name.replace("%{+YYYY.MM}", "*") if "%" in index_name else index_name}/_search?size=1` → los campos salen tipados (no todo `text`).
- Dashboard del caso con datos en el rango de tiempo correcto.

## Troubleshooting rápido

- **No ingesta**: revisá en el Configuration Center el estado de la pipeline y
  sus logs; el 90% es AK/SK o prefijo OBS mal escritos.
- **Campos como `text`/mal tipados**: el template se aplicó DESPUÉS de crear
  el índice. Borrá el índice, confirmá el template, re-ingerí.
- **_grokparsefailure**: el formato real difiere de la muestra usada en la
  PoC — pedile al SA regenerar el filter con una muestra más representativa.
"""


@app.post(
    "/api/v1/onboarding/export-starter-kit",
    tags=["onboarding"],
    summary="Empaqueta el pipeline + terraform + README en un .zip para entregar al cliente",
)
def export_starter_kit(request: ExportStarterKitRequest) -> Response:
    """Devuelve un .zip con todo lo necesario para que el cliente continúe el
    desarrollo en su propia cuenta Huawei Cloud después de la demo.

    Contenido:
      - `logstash.conf` (el filter + input + output que vio el cliente, con
        comentarios stripped via `strip_logstash_comments` para consistencia
        con el .conf que va al cluster real).
      - `terraform/main.tf` (copia del HCL actual).
      - `terraform/terraform.tfvars.example` (template de creds con placeholders).
      - `index-template.json` (tipos de campos para OpenSearch).
      - `dashboards.ndjson` (dashboards baseline importables en OpenSearch Dashboards).
      - `README.md` (instrucciones paso-a-paso interpoladas con project_name).

    Devuelve `application/zip` con filename `<project_name>-starter-kit.zip`.
    """
    import io
    import zipfile

    project = request.project_name or "log-analytics"
    terraform_dir = _active_terraform_dir()
    main_tf_path = terraform_dir / "main.tf"
    tfvars_example_path = terraform_dir / "terraform.tfvars.example"

    if not main_tf_path.exists() or not tfvars_example_path.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="terraform/main.tf o terraform.tfvars.example no encontrados — "
                   "el starter kit no puede armarse sin esos archivos base.",
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "starter-kit/logstash.conf",
            strip_logstash_comments(request.pipeline_conf),
        )
        z.writestr(
            "starter-kit/terraform/main.tf",
            main_tf_path.read_text(encoding="utf-8"),
        )
        z.writestr(
            "starter-kit/terraform/terraform.tfvars.example",
            tfvars_example_path.read_text(encoding="utf-8"),
        )
        template = build_index_template(
            request.fields, request.namespace, request.opensearch_index
        )
        z.writestr(
            "starter-kit/index-template.json",
            json.dumps(template, indent=2, ensure_ascii=False),
        )

        slug = _slug_from_index(request.opensearch_index)
        ndjson_content: str | None = None
        ndjson_file = Path(__file__).parent / "docs" / "dashboards" / f"{slug}.ndjson"
        if ndjson_file.exists():
            try:
                ndjson_content = ndjson_file.read_text(encoding="utf-8")
            except OSError:
                pass
        if ndjson_content is None:
            try:
                from dashboards import build_ndjson, get_available_slugs
                if slug in get_available_slugs():
                    ndjson_content = build_ndjson(slug)
            except Exception:
                pass
        if ndjson_content:
            z.writestr(
                "starter-kit/dashboards.ndjson",
                ndjson_content,
            )

        z.writestr(
            "starter-kit/README.md",
            _generate_starter_kit_readme(project),
        )
        z.writestr(
            "starter-kit/RUNBOOK-consola.md",
            _generate_console_runbook(project, request.opensearch_index, get_region()),
        )

    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in project)[:64]
    if not safe_name:
        safe_name = "log-analytics"

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}-starter-kit.zip"',
        },
    )


def _kit_capabilities_section(slug: str, index_pattern: str, fields: list[dict],
                              label: str) -> str:
    """Markdown con la secuencia Dev Tools del chatbot (OpenSearch Assistant) +
    forecasts, generada del schema del log del cliente. Con placeholders para la
    MaaS key y los ids que se encadenan entre pasos."""
    import capabilities as caps

    spec = caps.build_spec_from_fields(slug, index_pattern, fields, label=label)
    ops = spec.get("operations", [])
    prompt_fields = spec.get("fields", {})
    volume_field = spec.get("volume_field", "")
    ppl_prompt = caps.build_ppl_system_prompt(
        index_pattern, ops, prompt_fields, spec.get("success_code", ""), label)
    av = [{
        "tool_name": f"PPLTool-{slug}", "label": label, "index_pattern": index_pattern,
        "operations": ops, "fields": prompt_fields, "success_code": spec.get("success_code", ""),
        "ppl_system_prompt": ppl_prompt,
    }]
    instr = caps.build_agent_system_instruction(av)

    def j(x: Any) -> str:
        return json.dumps(x, ensure_ascii=False, indent=2)

    ep = caps.maas_connector_endpoint()
    parts = [
        "## 6. Chatbot — OpenSearch Assistant (NL → PPL → resultado)",
        "",
        "Habilita el asistente conversacional sobre tus datos. Corré cada bloque en "
        "**Dev Tools** de OpenSearch Dashboards, en orden. Reemplazá `<MAAS_API_KEY>` por tu "
        f"API key de MaaS y encadená los ids que devuelve cada paso. Endpoint MaaS: `{ep}`.",
        "",
        "### 6.1 Habilitar connectors remotos",
        "```", "PUT _cluster/settings", j(caps.build_cluster_settings()), "```",
        "### 6.2 Model group → guardá `model_group_id`",
        "```", "POST _plugins/_ml/model_groups/_register", j(caps.build_model_group()), "```",
        "### 6.3 Connector PPL (NL→PPL) → guardá `connector_id`",
        "```", "POST _plugins/_ml/connectors/_create",
        j(caps.build_ppl_connector("<MAAS_API_KEY>", ppl_prompt)), "```",
        "### 6.4 Connector LLM (razonador) → guardá `connector_id`",
        "```", "POST _plugins/_ml/connectors/_create",
        j(caps.build_llm_connector("<MAAS_API_KEY>")), "```",
        "### 6.5 Registrar los 2 modelos remotos → cada uno devuelve `task_id`",
        "```", "POST _plugins/_ml/models/_register",
        j(caps.build_remote_model("platform-ppl", "<PPL_CONNECTOR_ID>", "<MODEL_GROUP_ID>", "NL to PPL")),
        "", "POST _plugins/_ml/models/_register",
        j(caps.build_remote_model("platform-llm", "<LLM_CONNECTOR_ID>", "<MODEL_GROUP_ID>", "LLM frasea resultados")),
        "```",
        "### 6.6 Resolver `model_id` de cada task y desplegar",
        "```",
        "GET _plugins/_ml/tasks/<TASK_ID>            # tomá el model_id",
        "POST _plugins/_ml/models/<MODEL_ID>/_deploy   # uno por cada modelo (ppl y llm)",
        "GET _plugins/_ml/models/<MODEL_ID>          # confirmá model_state: DEPLOYED",
        "```",
        "### 6.7 Registrar el agente root → guardá `agent_id`",
        "```", "POST _plugins/_ml/agents/_register",
        j(caps.build_conversational_agent("<LLM_MODEL_ID>", "<PPL_MODEL_ID>", instr, av)), "```",
        "### 6.8 Apuntar el Assistant al agente root (+ reiniciar Dashboards)",
        "```", "PUT .plugins-ml-config/_doc/os_chat",
        j({"type": "os_chat_root_agent", "configuration": {"agent_id": "<AGENT_ID>"}}), "```",
        "### 6.9 Probar",
        "```", "POST _plugins/_ml/agents/<AGENT_ID>/_execute",
        j({"parameters": {"question": "¿Cuántos eventos hay en total?"}}), "```",
        "",
        "> `OPERATIONS` en el prompt PPL puede venir vacío: se completa solo cuando ya tenés datos "
        "ingeridos (se descubren los valores de las dimensiones). Editá el system prompt si querés "
        "fijarlos a mano.",
        "",
    ]

    forecasts = spec.get("forecasts", [])
    if forecasts and volume_field:
        parts += [
            "## 7. Forecasts",
            "",
            "Pronóstico de volumen sobre tu serie. **Importante**: el `window_delay` y el `history` "
            "hay que ajustarlos a las fechas reales de tus datos (la ventana de análisis del RCF "
            "tiene que caer sobre la serie, con ≥40 puntos poblados). Creá el forecaster y dispará "
            "un backtest con `_run_once`.", "",
        ]
        for fc in forecasts:
            body = caps.build_forecaster(
                index_pattern, volume_field, name=fc["name"], feature_name=fc["feature_name"],
                aggregation_query=fc.get("aggregation_query"),
                description=fc.get("description", "Forecast autogenerado"),
                history=fc.get("history", 4380))
            parts += ["```", "POST _plugins/_forecast/forecasters", j(body), "",
                      "POST _plugins/_forecast/forecasters/<FORECASTER_ID>/_run_once", "```"]
        parts.append("")
    return "\n".join(parts)


def _generate_kit_document(request: "ExportStarterKitRequest") -> str:
    """Arma el KIT completo en UN documento Markdown: config de Logstash, index
    template, dashboards, runbook de consola y los comandos del chatbot/forecasts,
    todo parametrizado con el índice y los campos detectados del log del cliente."""
    project = request.project_name or "log-analytics"
    index = request.opensearch_index or "logs-%{+YYYY.MM}"
    ip = index_pattern_from_name(index) if index else "logs-*"
    slug = _slug_from_index(index)
    fields = request.fields or []
    ns = request.namespace or "data"
    region = get_region()

    conf = strip_logstash_comments(request.pipeline_conf or "").strip()
    template_json = json.dumps(
        build_index_template(fields, ns, index), indent=2, ensure_ascii=False)

    ndjson = ""
    try:
        from dashboards import build_ndjson, build_ndjson_from_fields, get_available_slugs
        if slug in get_available_slugs():
            ndjson = build_ndjson(slug)
        elif fields:
            ndjson = build_ndjson_from_fields(slug, index, fields)
    except Exception as exc:  # noqa: BLE001
        print(f"[kit] dashboards no disponibles: {exc!r}")

    runbook = _generate_console_runbook(project, index, region)
    caps_section = _kit_capabilities_section(slug, ip, fields, project)

    field_rows = "\n".join(
        f"| `{f.get('field_path') or f.get('raw_name','')}` | {f.get('type','')} | "
        f"{f.get('business_label','')} |" for f in fields) or "| (sin campos) | | |"

    dashboards_block = (
        "```\n" + ndjson + "\n```\n\nImportá con **Saved Objects → Import** en OpenSearch "
        "Dashboards (o `POST _dashboards/api/saved_objects/_import?overwrite=true`)."
        if ndjson else
        "_(No se generaron dashboards automáticos para este log — se pueden armar a mano "
        "sobre el índice `" + ip + "`.)_")

    return f"""# Kit de pipeline — {project}

Generado por el **Builder**: pegaste unas líneas de log, GLM-5.2 armó el pipeline y este documento
reúne **todo** lo necesario para ponerlo en marcha en OpenSearch (por consola Huawei Cloud), sin
Terraform.

- **Índice destino:** `{index}`  (index-pattern `{ip}`)
- **Campos detectados:** {len(fields)}
- **Región:** {region}

## Campos detectados

| Campo | Tipo | Etiqueta |
|-------|------|----------|
{field_rows}

## 1. Configuration file (Logstash / CSS)

El pipeline completo (input elegido + filter generado + output). Pegalo en el **Configuration
Center** del cluster Logstash de CSS.

```
{conf}
```

## 2. Index template (aplicar ANTES de ingerir)

Los tipos de los campos viven en el index template, NO en Logstash. Aplicalo con el índice todavía
inexistente — Dev Tools:

```
PUT _index_template/{project}
{template_json}
```

## 3. Dashboards

{dashboards_block}

## 4. Runbook de consola (paso a paso)

{runbook}

{caps_section}
## Apéndice — deploy automatizado (opcional)

Si en vez de la consola preferís infra automatizada, el repositorio de la plataforma trae un
Terraform (`terraform/`) que crea los clusters CSS + NAT/DNAT. El camino recomendado para una PoC
guiada es el de la consola (este documento).
"""


@app.post(
    "/api/v1/onboarding/export-kit",
    tags=["onboarding"],
    summary="Genera el KIT completo (config + template + dashboards + runbook + chatbot) en UN documento Markdown",
)
def export_kit(request: ExportStarterKitRequest) -> Response:
    """Salida principal del **Builder**: un solo `.md` con todo lo necesario para
    armar el pipeline del cliente en OpenSearch, sin desplegar con Terraform."""
    project = request.project_name or "log-analytics"
    doc = _generate_kit_document(request)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in project)[:64] or "log-analytics"
    return Response(
        content=doc,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-kit.md"'},
    )


_DATASETS_DIR = Path(__file__).parent / "datasets"
_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _bundled_dataset(slug: str) -> str | None:
    """Devuelve el contenido del dataset bundleado para `slug`, o None.

    Para los tipos predefinidos (firewall, app-monitoring, ecommerce-search,
    fintech-transactions) hay un log real entero en ``datasets/<slug>.log`` que
    se sube tal cual a OBS en vez de generar variaciones sintéticas. Las líneas
    que empiezan con ``#`` son comentarios (placeholder/instrucciones) y se
    ignoran. Si el archivo no existe o queda sin líneas de datos, devuelve
    None y el caller cae al comportamiento anterior (synthetic / raw único).
    El tipo ``custom`` (slug ``logs``) no tiene archivo → siempre None.
    """
    if not slug:
        return None
    path = _DATASETS_DIR / f"{slug}.log"
    if not path.is_file():
        return None
    lines = [
        l for l in path.read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.lstrip().startswith("#")
    ]
    return "\n".join(lines) if lines else None


def _bundled_index_template(slug: str) -> dict[str, Any] | None:
    """Devuelve el index template hecho a mano para `slug`, o None.

    Algunos tipos predefinidos (ej. ``fintech-transactions``) traen un template
    curado en ``templates/<slug>.json`` con mappings que el auto-generador no
    produce (``nested``, ``index.sort``, dynamic_templates propios). Se aplica
    **verbatim** — tal cual el JSON — en `_apply_index_templates`, con
    prioridad sobre el template auto-generado desde los campos. Si no hay
    archivo (o no parsea), devuelve None y el caller cae al auto-generado.
    """
    if not slug:
        return None
    path = _TEMPLATES_DIR / f"{slug}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[index-template] no pude leer templates/{slug}.json: {exc!r}")
        return None


def _do_obs_upload(request: TerraformDeployRequest) -> None:
    """Sube el log (raw o batch sintético) al bucket OBS del operador.

    Ejecutado en un thread separado del terraform sequence — los dos
    workloads no tienen dependencia entre sí, así que corren en paralelo.

    El operador puede omitir AK/SK/bucket en el form; en ese caso skip
    silencioso (el cluster CSS se crea de todos modos pero arranca sin
    data, el operador puede subir manual después).
    """
    from obs_client import OBSClient, OBSConfigError, OBSUploadError
    from datetime import datetime

    if request.read_existing_bucket and not request.cases:
        print("[obs_upload] skip — read_existing_bucket (datos reales en el bucket)")
        return

    if not (request.obs_bucket and request.obs_access_key and request.obs_secret_key):
        print("[obs_upload] skip — operador no proveyó AK/SK/bucket")
        return

    obs_client: OBSClient | None = None
    try:
        obs_client = OBSClient(
            access_key_id=request.obs_access_key,
            secret_access_key=request.obs_secret_key,
            endpoint=request.obs_endpoint,
            bucket=request.obs_bucket,
        )
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if request.cases:
            for case in request.cases:
                if case.read_existing_bucket:
                    print(f"[obs_upload] [{case.slug}] skip — read_existing_bucket (datos reales en su prefijo)")
                    continue
                if not (case.raw_log or case.log_file_content):
                    continue
                # Prefijo TOP-LEVEL por tipo (`<slug>-logs`), NO anidado bajo un padre
                # compartido: el input OBS de Huawei no aísla sub-prefijos bajo `logs/`.
                prefix = case.obs_prefix.rstrip("/") if case.obs_prefix else f"{case.slug}-logs"

                # Limpiar el prefijo antes de subir: cada deploy arranca limpio por
                # prefijo. Evita que se acumulen datasets de corridas anteriores (o
                # data cruzada de otro tipo) que el pipeline volvería a ingerir.
                removed = obs_client.delete_prefix(f"{prefix}/")
                if removed:
                    print(f"[obs_upload] [{case.slug}] limpié {removed} objeto(s) viejos en {prefix}/")

                # Precedencia: dataset bundleado (predefinidos) → archivo importado
                # (custom, se sube tal cual) → fallback a la línea raw única. El
                # camino custom ya NO genera sintéticos.
                dataset = _bundled_dataset(case.slug)
                if dataset is not None:
                    object_key = f"{prefix}/dataset_{timestamp}.log"
                    print(f"[obs_upload] [{case.slug}] usando dataset bundleado ({len(dataset)} bytes)...")
                    obs_client.put_object(key=object_key, data=dataset)
                    print(f"[obs_upload] [{case.slug}] OK → {object_key}")
                    continue

                if case.log_file_content:
                    object_key = f"{prefix}/dataset_{timestamp}.log"
                    print(f"[obs_upload] [{case.slug}] usando archivo importado ({len(case.log_file_content)} bytes)...")
                    obs_client.put_object(key=object_key, data=case.log_file_content)
                    print(f"[obs_upload] [{case.slug}] OK → {object_key}")
                    continue

                batch = case.raw_log
                object_key = f"{prefix}/sample_log_{timestamp}.log"
                print(f"[obs_upload] [{case.slug}] uploading {object_key} ({len(batch)} bytes)...")
                obs_client.put_object(key=object_key, data=batch)
                print(f"[obs_upload] [{case.slug}] OK → {object_key}")
        else:
            if not (request.raw_log or request.log_file_content):
                print("[obs_upload] skip — no raw_log ni archivo importado")
                return
            prefix = request.obs_prefix.rstrip("/") if request.obs_prefix else "logs"

            # Precedencia: dataset bundleado (predefinidos) → archivo importado
            # (custom, tal cual) → fallback a la línea raw única. Sin sintéticos.
            dataset = _bundled_dataset(request.pipeline_slug)
            if dataset is not None:
                object_key = f"{prefix}/dataset_{timestamp}.log"
                print(f"[obs_upload] usando dataset bundleado para '{request.pipeline_slug}' ({len(dataset)} bytes)...")
                obs_client.put_object(key=object_key, data=dataset)
                print(f"[obs_upload] OK → {object_key}")
                return

            if request.log_file_content:
                object_key = f"{prefix}/dataset_{timestamp}.log"
                print(f"[obs_upload] usando archivo importado ({len(request.log_file_content)} bytes)...")
                obs_client.put_object(key=object_key, data=request.log_file_content)
                print(f"[obs_upload] OK → {object_key}")
                return

            batch = request.raw_log
            object_key = f"{prefix}/sample_log_{timestamp}.log"
            print(f"[obs_upload] uploading {object_key} ({len(batch)} bytes)...")
            obs_client.put_object(key=object_key, data=batch)
            print(f"[obs_upload] OK → {object_key}")
    except (OBSConfigError, OBSUploadError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"stage": "obs_upload", "message": str(exc)},
        ) from exc
    finally:
        if obs_client is not None:
            obs_client.close()


def _build_pipeline_conf_for_case(case: "PipelineCase", request: "TerraformDeployRequest") -> str:
    """Genera el pipeline .conf para un caso específico.
    
    Combina:
    - Input: OBS con el prefix del caso
    - Filter: el filter_code del caso
    - Output: OpenSearch con el index del caso
    """
    input_config = {
        "plugin_type": "s3",
        "s3": {
            "access_key_id": request.obs_access_key,
            "secret_access_key": request.obs_secret_key,
            "bucket": request.obs_bucket,
            "region": request.obs_region,
            "endpoint": request.obs_endpoint,
            "prefix": case.obs_prefix,
            "codec": "plain",
        }
    }
    
    output_config = {
        "plugin_type": "elasticsearch",
        "elasticsearch": {
            "hosts": [],
            "index": case.index_name,
            "user": request.opensearch_user,
            "password": request.opensearch_password,
            "ssl": request.https_enabled,
        }
    }

    # read_existing_bucket: el dataset ya está en OBS (pre-cargado por el operador).
    # Input read-only y one-shot — NO borra los objetos del bucket (delete=false) y
    # NO queda polleando indefinidamente (watch=false). Sin template/ILM manejados
    # (los tipos los pone el index template del paso 2). El dedup (document_id) es
    # POR TIPO y viene del caso: CTS usa %{trace_id}, fintech el fingerprint del
    # filtro; el resto no dedup (ids auto), y el clear de índice por corrida evita
    # duplicados. NO hardcodear trace_id acá: los tipos sin ese campo colapsarían
    # todos los docs a uno.
    if case.read_existing_bucket:
        input_config["s3"]["delete"] = False
        input_config["s3"]["watch_for_new_files"] = False
        output_config["elasticsearch"]["manage_template"] = False
        output_config["elasticsearch"]["ilm_enabled"] = False
        if case.document_id:
            output_config["elasticsearch"]["document_id"] = case.document_id

    input_block = generate_input_block(input_config)
    filter_block = case.filter_code
    output_block = generate_output_block(output_config)
    
    parts = []
    if input_block:
        parts.append(input_block)
    parts.append(filter_block)
    if output_block:
        parts.append(output_block)
    
    return "\n\n".join(parts)


def _do_terraform_sequence(
    request: TerraformDeployRequest, terraform_dir: Path,
    logstash_flavor: str | None = None, opensearch_flavor: str | None = None,
) -> dict:
    """Escribe pipeline.conf + tfvars + corre init (si hace falta) + apply +
    output. Devuelve el dict de outputs de terraform.

    Ejecutado en un thread separado del OBS upload. La limpieza del
    tfvars.json con secrets NO ocurre acá — la hace el handler principal
    en su `finally` después de que ambos futures completen, sino podría
    quedar colgado si esta función raisea.
    """
    # ── Escribir pipeline.conf en terraform/ ─────────────────────────
    pipeline_file = terraform_dir / "pipeline.conf"
    pipeline_file.write_text(request.pipeline_conf, encoding="utf-8")

    # ── Mergear pipelines en el registro ─────────────────────────────
    registry = {} if request.fresh_deploy else _read_pipelines_registry(terraform_dir)

    # `fields`/`label`: schema detectado en el paso 2. Habilitan las capabilities
    # (chatbot + forecasts) de un slug SIN spec curado — el despliegue productivo.
    # Para los verticales demo se persisten igual pero mandan sus specs
    # (`capabilities._CAPABILITY_SPECS`). Sin esto, los campos viven solo en el
    # browser y se pierden en un F5.
    if request.cases:
        for case in request.cases:
            case_conf = _build_pipeline_conf_for_case(case, request)
            registry[case.slug] = {
                "pipeline_conf": case_conf,
                "start_ingestion": request.start_ingestion,
                "index": case.index_name,
                "obs_prefix": case.obs_prefix,
                "fields": case.fields or [],
                "label": request.industry_label or "",
            }
    else:
        slug = (request.pipeline_slug or "").strip() or _slug_from_index(request.opensearch_index)
        registry[slug] = {
            "pipeline_conf": request.pipeline_conf,
            "start_ingestion": request.start_ingestion,
            "index": request.opensearch_index,
            "obs_prefix": request.obs_prefix,
            "fields": request.fields or [],
            "label": request.industry_label or "",
        }

    _write_pipelines_registry(terraform_dir, registry)
    pipelines_var = {
        k: {"pipeline_conf": v.get("pipeline_conf", ""),
            "start_ingestion": bool(v.get("start_ingestion", False))}
        for k, v in registry.items()
    }

    # ── Escribir deploy.auto.tfvars.json con todas las creds del form ─
    tfvars: dict[str, Any] = {
        "pipelines": pipelines_var,
        "project_name": _effective_project_name(request, terraform_dir),
        "https_enabled": request.https_enabled,
    }
    if logstash_flavor:
        tfvars["logstash_flavor"] = logstash_flavor
    if opensearch_flavor:
        tfvars["opensearch_flavor"] = opensearch_flavor
    if request.obs_access_key:
        tfvars["obs_access_key"] = request.obs_access_key
        tfvars["hwc_access_key"] = request.obs_access_key
    if request.obs_secret_key:
        tfvars["obs_secret_key"] = request.obs_secret_key
        tfvars["hwc_secret_key"] = request.obs_secret_key
    if request.opensearch_password:
        tfvars["opensearch_password"] = request.opensearch_password
    if request.existing_opensearch_endpoint:
        tfvars["existing_opensearch_endpoint"] = request.existing_opensearch_endpoint

    # Project ID (⚙ Configuración > .env): solo para armar el `dashboards_url`
    # con shape de consola Huawei. Si falta, el output cae al link interno VPC.
    huawei_project_id = get_huawei_project_id()
    if huawei_project_id:
        tfvars["huawei_project_id"] = huawei_project_id
    # Infra de la cuenta del SA (⚙ Configuración): si está configurada, pisa el
    # terraform.tfvars estático (que queda como fallback del setup original).
    tfvars.update(_huawei_infra_tfvars())

    tfvars_file = terraform_dir / "deploy.auto.tfvars.json"
    tfvars_file.write_text(
        json.dumps(tfvars, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── Persistir SOLO las credenciales para el teardown ─────────────
    # `deploy.auto.tfvars.json` se borra al terminar el deploy (tiene el
    # pipeline_conf y demás), pero `terraform destroy` necesita las creds del
    # provider. Las dejamos en un archivo aparte (auto-cargado por terraform)
    # que persiste hasta el destroy — sino el destroy se cuelga pidiendo las
    # variables por stdin. Es la cuenta del propio operador en su máquina.
    _write_destroy_creds(terraform_dir, request)

    # ── terraform init (si el cache de providers no existe) ──────────
    # `-input=false`: nunca pedir variables por stdin (en un server colgaría).
    providers_cache = terraform_dir / ".terraform" / "providers"
    if not providers_cache.exists():
        print("[terraform_sequence] cache de providers no existe, ejecutando init...")
        init_result = subprocess.run(
            ["terraform", "init", "-input=false"],
            cwd=terraform_dir,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if init_result.returncode != 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"stage": "terraform_init",
                        "message": init_result.stderr},
            )
        print("[terraform_sequence] init completado")

    # ── terraform apply ──────────────────────────────────────────────
    # Timeout 1800s (30 min): el provider HuaweiCloud típicamente termina
    # de provisionar CSS clusters en 10-20 min, pero hemos visto casos de
    # 25+ min con latencia de la API en Hong Kong/Santiago. Si nuestro
    # timeout pega antes que el del provider, Python mata terraform con
    # SIGKILL → el provider devuelve "context canceled" → quedan recursos
    # huérfanos creados a medias en Huawei sin que la `_logstash_configuration`
    # se haya aplicado. Mejor margen amplio y, en caso de re-deploy, terraform
    # es idempotente y completa los recursos faltantes desde el state.
    print("[terraform_sequence] apply iniciado (5-25 min creando clusters)...")
    apply_result = subprocess.run(
        ["terraform", "apply", "-auto-approve", "-input=false"],
        cwd=terraform_dir,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if apply_result.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"stage": "terraform_apply",
                    "message": apply_result.stderr or apply_result.stdout},
        )
    print("[terraform_sequence] apply completado")

    # ── Obtener outputs ──────────────────────────────────────────────
    output_result = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=terraform_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if output_result.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"stage": "terraform_output",
                    "message": output_result.stderr},
        )
    return json.loads(output_result.stdout)


@app.post(
    "/api/v1/terraform/deploy",
    response_model=TerraformDeployResponse,
    tags=["terraform"],
    summary="Deploya el pipeline en Huawei Cloud con Terraform",
)
def terraform_deploy(request: TerraformDeployRequest) -> TerraformDeployResponse:
    """Deploy con Terraform, protegido por el lock de deploy por-usuario."""
    with _deploy_guard():
        return _terraform_deploy_impl(request)


def _terraform_deploy_impl(request: TerraformDeployRequest) -> TerraformDeployResponse:
    """Deploy automático con Terraform.

    Ejecuta DOS workloads en paralelo (no tienen dependencia entre sí):
      A) OBS upload del log (raw o sintético) al bucket del operador.
      B) Terraform sequence: write pipeline.conf + tfvars → init → apply
         → output → endpoints.

    Espera ambos completados antes de responder. Si alguno falla, propaga
    el error correspondiente. La limpieza del tfvars.json con secrets se
    hace siempre en el `finally` (incluso si terraform crasheó por el medio).

    Ganancia vs ejecutarlos secuenciales: ~15-30s menos de latencia
    percibida. El total del deploy sigue dominado por terraform apply
    (5-15 min).
    """
    import concurrent.futures

    terraform_dir = _active_terraform_dir()
    if not terraform_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Directorio terraform/ no encontrado",
        )
    _check_unavailable_plugins(request)

    num_cases = len(request.cases) if request.cases else 1
    if num_cases > _MAX_PIPELINES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "stage": "pipeline_cap",
                "message": (
                    f"Máximo {_MAX_PIPELINES} pipelines por cluster en la demo. "
                    f"Solicitaste {num_cases}. Destruí pipelines o el entorno "
                    f"antes de agregar más."
                ),
            },
        )

    logstash_flavor, opensearch_flavor = _determine_flavor(num_cases)

    slug = (request.pipeline_slug or "").strip() or _slug_from_index(request.opensearch_index)
    registry = _read_pipelines_registry(terraform_dir)
    if slug not in registry and len(registry) >= _MAX_PIPELINES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "stage": "pipeline_cap",
                "message": (
                    f"Máximo {_MAX_PIPELINES} pipelines por cluster en la demo "
                    f"(el Logstash es 1 nodo). Destruí una pipeline o el entorno "
                    f"antes de agregar otra."
                ),
            },
        )

    tfvars_file = terraform_dir / "deploy.auto.tfvars.json"
    try:
        if request.start_ingestion:
            print("[terraform_deploy] FASE 2 (iniciar ingesta) — sin OBS upload")
            # Limpiar los índices de los casos ANTES de activar Logstash: cada
            # ingesta arranca limpia (sin docs de corridas anteriores). El index
            # template ya quedó del paso 2, así que el índice se recrea bien tipado.
            try:
                _clear_case_indices(request, _cluster_with_public_access(terraform_dir))
            except Exception as exc:  # noqa: BLE001
                print(f"[clear-index] fallo al limpiar índices (best-effort): {exc!r}")
            tf_outputs = _do_terraform_sequence(
                request, terraform_dir,
                logstash_flavor=logstash_flavor,
                opensearch_flavor=opensearch_flavor,
            )
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                obs_future = executor.submit(_do_obs_upload, request)
                tf_future = executor.submit(
                    _do_terraform_sequence, request, terraform_dir,
                    logstash_flavor, opensearch_flavor,
                )

                obs_exc: Exception | None = None
                tf_outputs = None
                tf_exc: Exception | None = None

                try:
                    obs_future.result()
                except Exception as exc:
                    obs_exc = exc
                try:
                    tf_outputs = tf_future.result()
                except Exception as exc:
                    tf_exc = exc

            # Priorizamos terraform: sin clusters no hay demo. OBS upload
            # fallado pero terraform OK = cluster vacío (recuperable).
            if tf_exc:
                if obs_exc:
                    print(
                        f"[terraform_deploy] ambos workloads fallaron — "
                        f"OBS: {obs_exc!r}, TF: {tf_exc!r}"
                    )
                raise tf_exc
            if obs_exc:
                raise obs_exc

        assert tf_outputs is not None  # garantizado si no hay tf_exc
        # Marcar este entorno como "desplegado por la plataforma" → habilita
        # que aparezca en "Mi Infraestructura". Sin esta marca, un tfstate
        # cualquiera (manual/leftover/stale) NO se reclama como propio.
        _write_platform_marker(terraform_dir, _effective_project_name(request, terraform_dir))
        # Persistir el index template para que "Mi Infraestructura" lo muestre
        # (hidratado del disco, sin pasar por el wizard).
        _write_index_template_artifact(terraform_dir, request)

        # dashboards_url se construye en Python (state + project_id del .env),
        # mismo criterio que /terraform/status — no dependemos del output de
        # Terraform que queda atado a las vars del apply.
        cluster = _read_opensearch_cluster_from_state(terraform_dir)
        # Con NAT/DNAT los endpoints públicos (EIP del NAT) vienen de los outputs,
        # no del state del cluster (ya sin public_access). Completar el cluster.
        _overlay_public_endpoints(cluster, tf_outputs or {})

        # Cluster Routes del CSS → salida a MaaS (para el agente/chatbot). Solo en
        # provisión (cluster nuevo); en la fase de ingesta ya están. Best-effort.
        if not request.start_ingestion:
            try:
                cluster_id = (tf_outputs.get("opensearch_cluster_id") or {}).get("value", "")
                _add_css_cluster_routes(
                    cluster_id, request.obs_access_key, request.obs_secret_key,
                    get_huawei_project_id(),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[css-routes] fallo agregando cluster routes (best-effort): {exc!r}")

        # NOTA: el index template + import de dashboards YA NO corren acá. Pasaron a
        # un paso explícito del wizard (POST /api/v1/onboarding/apply-schema), que el
        # operador dispara entre provisionar (este paso) y arrancar la ingesta. Así
        # se puede reintentar el schema/dashboards sin re-provisionar el cluster.

        # Security Analytics (Sigma rules + detector) para SIEM. Best-effort.
        target_slugs = {c.slug for c in request.cases} if request.cases else set()
        if not request.cases and request.pipeline_slug:
            target_slugs = {request.pipeline_slug}
        if "siem" in target_slugs and not request.start_ingestion:
            try:
                _provision_security_analytics(
                    cluster, request.opensearch_user or "admin",
                    request.opensearch_password, request.https_enabled,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[terraform_deploy] security-analytics falló (best-effort): {exc!r}")

        index_template_applied = False
        dashboards_imported = False

        return TerraformDeployResponse(
            opensearch_endpoint=tf_outputs.get("opensearch_endpoint", {}).get("value", ""),
            logstash_endpoint=tf_outputs.get("logstash_endpoint", {}).get("value", ""),
            dashboards_url=_build_dashboards_url(cluster, request.https_enabled),
            status="success",
            dashboards_imported=dashboards_imported,
            index_template_applied=index_template_applied,
        )
    finally:
        # Borrar el tfvars.json con secrets — best-effort, no rompemos
        # el flujo si falla el unlink (el archivo queda hasta el próximo
        # run pero con creds del último que igual tenían que estar acá).
        try:
            if tfvars_file.exists():
                tfvars_file.unlink()
        except OSError:
            pass


# ── Deploy con progreso streaming (SSE) ──────────────────────────────────────
# El endpoint /terraform/deploy bloquea 5-25 min sin feedback. Esta versión
# corre el mismo terraform apply pero con Popen + lectura línea por línea,
# emitiendo eventos SSE con % de progreso real (fase + mensaje) para que el
# frontend muestre una barra incremental.

_TF_PHASES: dict[str, tuple[str, float, float, float]] = {
    "huaweicloud_nat_gateway":               ("NAT gateway",            3,  5,  20),
    "huaweicloud_vpc_eip":                   ("EIP pública",            5,  7,  15),
    "huaweicloud_nat_dnat_rule":             ("DNAT (acceso público)",  7,  8,  10),
    "huaweicloud_nat_snat_rule":             ("SNAT (salida a MaaS)",   8,  9,  10),
    "huaweicloud_networking_secgroup_rule":  ("Security groups",        9, 10,  10),
    "huaweicloud_css_cluster":               ("OpenSearch cluster",    10, 55, 900),
    "huaweicloud_css_logstash_cluster":      ("Logstash cluster",      55, 85, 600),
    "huaweicloud_css_logstash_configuration":("Configurando pipeline", 85, 92,  30),
    "huaweicloud_css_logstash_pipeline":     ("Activando ingesta",     92, 95,  15),
}


def _parse_tf_line(line: str, completed: set[str]) -> dict | None:
    """Parsea una línea de stdout de `terraform apply` → evento de progreso.

    Terraform emite líneas como::
        huaweicloud_css_cluster.opensearch_cluster: Creating...
        huaweicloud_css_cluster.opensearch_cluster: Still creating... [30s elapsed]
        huaweicloud_css_cluster.opensearch_cluster: Creation complete after 5m30s [id=...]
        Apply complete! Resources: 12 added, 0 changed, 0 destroyed.

    Cada recurso se mapea a una fase con rango de % y duración estimada; el %
    avanza dentro del rango según el elapsed time reportado.
    """
    line = line.strip()
    if not line:
        return None
    if "Apply complete!" in line:
        return {"percent": 96, "phase": "Finalizando", "message": "Apply completo", "key": "apply", "done": True}
    for res_type, (label, start, end, est) in _TF_PHASES.items():
        if res_type not in line:
            continue
        if "Creation complete" in line or "Modifications complete" in line:
            completed.add(res_type)
            return {"percent": end, "phase": label, "message": f"{label} ✓", "key": res_type, "done": True}
        if "Still creating" in line or "Still modifying" in line:
            # El % avanza según el elapsed reportado, pero NO lo mostramos (evita "(0s)").
            m = re.search(r"\[(\d+)s elapsed\]", line)
            elapsed = int(m.group(1)) if m else 0
            frac = min(elapsed / est, 0.95) if est > 0 else 0
            pct = round(start + (end - start) * frac, 1)
            return {"percent": pct, "phase": label, "message": f"{label}…", "key": res_type, "done": False}
        if "Creating..." in line or "Modifying..." in line:
            return {"percent": start, "phase": label, "message": f"{label}…", "key": res_type, "done": False}
    return None


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _deploy_stream_gen(request: TerraformDeployRequest, terraform_dir: Path,
                       logstash_flavor: str | None, opensearch_flavor: str | None):
    """Generador que corre el deploy y emite eventos SSE con progreso real."""
    import concurrent.futures

    # ── Pre-flight: la infra Huawei (vpc/subnet/sg/az) tiene que venir de ⚙
    # Configuración o del terraform.tfvars estático. Si falta, cortamos con un
    # mensaje claro en vez de dejar que `terraform apply` explote con
    # "No value for required variable" (críptico). El tfvars estático NO está en
    # la imagen Docker (tiene secretos) → en el contenedor hay que usar ⚙. ──
    _hw = get_huawei_settings()
    _tfvars_static = (terraform_dir / "terraform.tfvars").exists()
    _missing_infra = [f for f in ("vpc_id", "subnet_id", "security_group_id", "availability_zone")
                      if not _hw.get(f)]
    if _missing_infra and not _tfvars_static:
        yield _sse({"type": "error", "message":
            "Falta configurar la cuenta Huawei en ⚙ Configuración: VPC, subnet, security group y "
            "availability zone. Sin esos IDs el deploy no puede crear los clusters CSS. "
            "Campos faltantes: " + ", ".join(_missing_infra) + "."})
        return

    yield _sse({"type": "progress", "percent": 1, "phase": "Preparando",
                "message": "Escribiendo configuración…"})

    # ── Setup: pipeline.conf + registry + tfvars (igual que _do_terraform_sequence) ──
    try:
        (terraform_dir / "pipeline.conf").write_text(request.pipeline_conf, encoding="utf-8")
        registry = {} if request.fresh_deploy else _read_pipelines_registry(terraform_dir)
        if request.cases:
            for case in request.cases:
                registry[case.slug] = {
                    "pipeline_conf": _build_pipeline_conf_for_case(case, request),
                    "start_ingestion": request.start_ingestion,
                    "index": case.index_name,
                    "obs_prefix": case.obs_prefix,
                    "fields": case.fields or [],
                    "label": request.industry_label or "",
                }
        else:
            slug = (request.pipeline_slug or "").strip() or _slug_from_index(request.opensearch_index)
            registry[slug] = {
                "pipeline_conf": request.pipeline_conf,
                "start_ingestion": request.start_ingestion,
                "index": request.opensearch_index,
                "obs_prefix": request.obs_prefix,
                "fields": request.fields or [],
                "label": request.industry_label or "",
            }
        _write_pipelines_registry(terraform_dir, registry)
        pipelines_var = {
            k: {"pipeline_conf": v.get("pipeline_conf", ""),
                "start_ingestion": bool(v.get("start_ingestion", False))}
            for k, v in registry.items()
        }
        tfvars: dict[str, Any] = {
            "pipelines": pipelines_var,
            "project_name": _effective_project_name(request, terraform_dir),
            "https_enabled": request.https_enabled,
        }
        if logstash_flavor:
            tfvars["logstash_flavor"] = logstash_flavor
        if opensearch_flavor:
            tfvars["opensearch_flavor"] = opensearch_flavor
        if request.obs_access_key:
            tfvars["obs_access_key"] = request.obs_access_key
            tfvars["hwc_access_key"] = request.obs_access_key
        if request.obs_secret_key:
            tfvars["obs_secret_key"] = request.obs_secret_key
            tfvars["hwc_secret_key"] = request.obs_secret_key
        if request.opensearch_password:
            tfvars["opensearch_password"] = request.opensearch_password
        if request.existing_opensearch_endpoint:
            tfvars["existing_opensearch_endpoint"] = request.existing_opensearch_endpoint
        huawei_project_id = get_huawei_project_id()
        if huawei_project_id:
            tfvars["huawei_project_id"] = huawei_project_id
        tfvars.update(_huawei_infra_tfvars())
        (terraform_dir / "deploy.auto.tfvars.json").write_text(
            json.dumps(tfvars, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_destroy_creds(terraform_dir, request)
    except Exception as exc:
        yield _sse({"type": "error", "message": f"Error en setup: {exc}"})
        return

    # ── FASE 2 (start_ingestion): limpiar índices; sin OBS upload ──────────
    obs_executor = None
    obs_future = None
    if request.start_ingestion:
        yield _sse({"type": "progress", "percent": 3, "phase": "Limpiando índices",
                    "message": "Limpiando índices previos…"})
        try:
            _clear_case_indices(request, _cluster_with_public_access(terraform_dir))
        except Exception as exc:
            print(f"[deploy-stream] clear-index falló (best-effort): {exc!r}")
    else:
        yield _sse({"type": "progress", "percent": 2, "phase": "Subiendo logs",
                    "message": "Subiendo datos a OBS…"})
        obs_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        obs_future = obs_executor.submit(_do_obs_upload, request)

    # ── terraform init (si hace falta) ────────────────────────────────────
    if not (terraform_dir / ".terraform" / "providers").exists():
        yield _sse({"type": "progress", "percent": 3, "phase": "Terraform init",
                    "message": "Descargando provider HuaweiCloud…"})
        init_result = subprocess.run(
            ["terraform", "init", "-input=false"],
            cwd=terraform_dir, capture_output=True, text=True, timeout=180,
        )
        if init_result.returncode != 0:
            err = init_result.stderr or init_result.stdout or "terraform init falló"
            print("[terraform init FALLÓ]\n" + err, flush=True)
            yield _sse({"type": "error", "message": "terraform init falló:\n" + err[-1500:]})
            return

    # ── terraform apply (streaming línea por línea) ──────────────────────
    yield _sse({"type": "progress", "percent": 5, "phase": "Terraform apply",
                "message": "Aplicando infraestructura…"})

    completed_resources: set[str] = set()
    tf_lines: list[str] = []
    process = subprocess.Popen(
        ["terraform", "apply", "-auto-approve", "-input=false"],
        cwd=terraform_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        for line in process.stdout:
            tf_lines.append(line)
            progress = _parse_tf_line(line, completed_resources)
            if progress:
                yield _sse({"type": "progress", **progress})
    except Exception as exc:
        yield _sse({"type": "error", "message": f"Error leyendo terraform: {exc}"})
        return
    finally:
        process.stdout.close()
        process.wait()

    apply_failed = process.returncode != 0
    if apply_failed:
        tail = "".join(tf_lines)[-3000:]
        print("[terraform apply FALLÓ]\n" + tail, flush=True)   # visible en `docker compose logs`

    # ── terraform output ─────────────────────────────────────────────────
    # Se lee INCLUSO si el apply falló: un apply parcial igual guarda estado, así
    # detectamos si el cluster se creó a pesar del error (típ. falló solo una regla
    # de SG que ya existía) y no descartamos un entorno que quedó levantado.
    yield _sse({"type": "progress", "percent": 97, "phase": "Outputs",
                "message": "Obteniendo endpoints…"})
    output_result = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=terraform_dir, capture_output=True, text=True, timeout=30,
    )
    if not apply_failed and output_result.returncode != 0:
        yield _sse({"type": "error", "message": output_result.stderr or "terraform output falló"})
        return
    try:
        tf_outputs = json.loads(output_result.stdout) if output_result.returncode == 0 else {}
    except Exception:
        tf_outputs = {}

    cluster_created = bool((tf_outputs.get("opensearch_cluster_id") or {}).get("value")
                           or (tf_outputs.get("opensearch_endpoint") or {}).get("value"))
    if apply_failed and not cluster_created:
        # Falla real (el cluster no llegó a crearse) → error.
        yield _sse({"type": "error", "message": "terraform apply falló:\n" + tail[-1500:]})
        return
    if apply_failed:
        # El cluster SÍ se creó: el apply falló en algo secundario (típicamente una
        # regla de security group que YA existía). Registramos el entorno y avisamos,
        # en vez de tirar todo abajo y dejar recursos huérfanos.
        yield _sse({"type": "progress", "percent": 97, "phase": "Aviso",
                    "message": "El entorno se creó, pero una regla de security group falló "
                               "(probablemente ya existía). Si no accedés a 9200/Kibana desde "
                               "afuera, revisá esa regla en tu SG."})

    # ── Verificar OBS upload (best-effort) ───────────────────────────────
    if obs_future is not None:
        try:
            obs_future.result()
        except Exception as exc:
            print(f"[deploy-stream] OBS upload falló (best-effort): {exc!r}")
        obs_executor.shutdown()

    # ── Post-deploy: marker + artifact ───────────────────────────────────
    yield _sse({"type": "progress", "percent": 99, "phase": "Finalizando",
                "message": "Guardando estado del deploy…"})
    _write_platform_marker(terraform_dir, _effective_project_name(request, terraform_dir))
    _write_index_template_artifact(terraform_dir, request)

    cluster = _read_opensearch_cluster_from_state(terraform_dir)
    _overlay_public_endpoints(cluster, tf_outputs or {})

    # Cluster Routes del CSS → salida a MaaS (para el agente/chatbot). Solo en
    # provisión (cluster nuevo). Best-effort. (Este es el path REAL del deploy —
    # el frontend usa /deploy-stream, no /terraform/deploy.)
    if not request.start_ingestion:
        try:
            cluster_id = (tf_outputs.get("opensearch_cluster_id") or {}).get("value", "")
            r = _add_css_cluster_routes(
                cluster_id, request.obs_access_key, request.obs_secret_key,
                get_huawei_project_id(),
            )
            print(f"[css-routes] resultado: {r}")
        except Exception as exc:  # noqa: BLE001
            print(f"[css-routes] fallo agregando cluster routes (best-effort): {exc!r}")

    # Security Analytics (Sigma rules + detector) para SIEM.
    # Best-effort: si el plugin no está o falla, no rompe el deploy.
    target_slugs = {c.slug for c in request.cases} if request.cases else set()
    if not request.cases and request.pipeline_slug:
        target_slugs = {request.pipeline_slug}
    if "siem" in target_slugs and not request.start_ingestion:
        yield _sse({"type": "progress", "percent": 98, "phase": "Security Analytics",
                    "message": "Provisionando Sigma rules + detector…"})
        try:
            sa_result = _provision_security_analytics(
                cluster, request.opensearch_user or "admin",
                request.opensearch_password, request.https_enabled,
            )
            n_rules = len(sa_result.get("rules", []))
            has_detector = bool(sa_result.get("detector"))
            print(f"[deploy-stream] security-analytics: {n_rules} rules, "
                  f"detector={'sí' if has_detector else 'no'}")
        except Exception as exc:  # noqa: BLE001
            print(f"[deploy-stream] security-analytics falló (best-effort): {exc!r}")

    yield _sse({"type": "complete", "result": {
        "opensearch_endpoint": tf_outputs.get("opensearch_endpoint", {}).get("value", ""),
        "logstash_endpoint": tf_outputs.get("logstash_endpoint", {}).get("value", ""),
        "dashboards_url": _build_dashboards_url(cluster, request.https_enabled),
        "status": "success",
        "dashboards_imported": False,
        "index_template_applied": False,
    }})


@app.post(
    "/api/v1/terraform/deploy-stream",
    tags=["terraform"],
    summary="Deploya con Terraform streameando progreso real (SSE)",
)
def terraform_deploy_stream(request: TerraformDeployRequest):
    """Versión streaming del deploy: emite eventos SSE con progreso real
    del ``terraform apply`` (porcentaje, fase, mensaje) en vez de bloquear
    5-25 min sin feedback.

    Formato de eventos (``text/event-stream``)::

        data: {"type": "progress", "percent": 25, "phase": "OpenSearch cluster", "message": "OpenSearch cluster (120s)"}
        data: {"type": "complete", "result": {…}}
        data: {"type": "error", "message": "…"}

    El frontend lee el stream con ``fetch`` + ``response.body.getReader()``.
    """
    terraform_dir = _active_terraform_dir()
    if not terraform_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Directorio terraform/ no encontrado",
        )
    num_cases = len(request.cases) if request.cases else 1
    if num_cases > _MAX_PIPELINES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"stage": "pipeline_cap",
                    "message": f"Máximo {_MAX_PIPELINES} pipelines por cluster."},
        )
    logstash_flavor, opensearch_flavor = _determine_flavor(num_cases)
    slug = (request.pipeline_slug or "").strip() or _slug_from_index(request.opensearch_index)
    registry = _read_pipelines_registry(terraform_dir)
    if slug not in registry and len(registry) >= _MAX_PIPELINES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"stage": "pipeline_cap",
                    "message": f"Máximo {_MAX_PIPELINES} pipelines por cluster."},
        )
    _check_unavailable_plugins(request)
    _check_demo_datasets_present(request)
    # Lock por-usuario: se adquiere ya (puede cortar con 409) y se libera cuando
    # el stream se agota/cierra.
    lock = _deploy_lock_for_current()
    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"stage": "deploy_lock",
                    "message": "Ya hay un despliegue o destroy en curso para tu usuario. "
                               "Esperá a que termine antes de lanzar otro."})

    audit.record("deploy", f"slug={slug} start_ingestion={request.start_ingestion} cases={num_cases}")

    def _guarded_stream():
        try:
            yield from _deploy_stream_gen(request, terraform_dir, logstash_flavor, opensearch_flavor)
        finally:
            lock.release()

    return StreamingResponse(_guarded_stream(), media_type="text/event-stream")


# La Logstash de CSS (7.10) NO trae todos los plugins bundled de la OSS: el
# cluster no tiene salida a internet para instalarlos y el deploy no los
# agrega. Confirmados ausentes hasta ahora; extender la lista al descubrir
# otros. El sandbox local NO detecta esto (su imagen sí los trae) — por eso
# el chequeo es estático, antes de gastar el apply.
_UNAVAILABLE_FILTER_PLUGINS = {"translate"}


def _check_unavailable_plugins(request: "TerraformDeployRequest") -> None:
    """Corta con 400 si algún .conf usa un plugin que CSS Logstash no tiene."""
    confs = [(request.pipeline_slug or "principal", request.pipeline_conf or "")]
    confs += [(c.slug, c.filter_code or "") for c in (request.cases or [])]
    offending: dict[str, list[str]] = {}
    for slug, conf in confs:
        found = sorted(p for p in _UNAVAILABLE_FILTER_PLUGINS
                       if re.search(rf"\b{p}\s*{{", conf))
        if found:
            offending[slug] = found
    if offending:
        detail = "; ".join(f"{slug}: {', '.join(ps)}" for slug, ps in offending.items())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "stage": "unavailable_plugins",
                "message": (
                    f"El pipeline usa plugins que la Logstash de CSS no tiene "
                    f"instalados ({detail}). Reemplazalos (ej. translate → "
                    "condicionales mutate) y volvé a intentar."
                ),
            },
        )


def _check_demo_datasets_present(request: "TerraformDeployRequest") -> None:
    """Guard pre-Terraform: los tipos de demo leen su dataset ya presente en
    ``<slug>-logs/`` del bucket. Si falta alguno, cortar ANTES de gastar 20 min
    de deploy, con mensaje accionable (⚙ Configuración → Preparar bucket).

    Solo aplica a slugs de demo conocidos (`_DEMO_DATASET_FILES`); el flujo
    productivo (prefijo del cliente) no se toca. Best-effort: si el chequeo en
    sí falla (red, permisos), no bloquea el deploy."""
    from obs_client import OBSClient, OBSConfigError, OBSUploadError

    cases = request.cases or []
    demo_cases = [c for c in cases
                  if c.read_existing_bucket and c.slug in _DEMO_DATASET_FILES]
    if not demo_cases or not request.obs_access_key or not request.obs_bucket:
        return
    missing: list[str] = []
    try:
        client = OBSClient(
            access_key_id=request.obs_access_key,
            secret_access_key=request.obs_secret_key,
            endpoint=request.obs_endpoint,
            bucket=request.obs_bucket,
        )
        try:
            for case in demo_cases:
                prefix = (case.obs_prefix or f"{case.slug}-logs/").rstrip("/") + "/"
                if not client.prefix_has_objects(prefix):
                    missing.append(case.slug)
        finally:
            client.close()
    except (OBSConfigError, OBSUploadError) as exc:
        print(f"[deploy] chequeo de datasets falló (best-effort, sigue): {exc!r}")
        return
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "stage": "datasets_missing",
                "message": (
                    f"Faltan los datasets de demo en el bucket "
                    f"`{request.obs_bucket}`: {', '.join(sorted(missing))}. "
                    "Andá a ⚙ Configuración → Preparar bucket de demos."
                ),
            },
        )


def _read_css_resource_from_state(terraform_dir: Path, resource_type: str) -> dict[str, str]:
    """Lee id + endpoint + name de un recurso CSS directo del tfstate.

    Parsear el state (en vez de depender de `terraform output`) nos
    desacopla del "apply-time coupling": el output `dashboards_url` que
    Terraform hornea queda fijado a las variables que había en el momento
    del apply. Si el `huawei_project_id` no estaba seteado entonces, el
    output queda mal hasta el próximo redeploy. Leyendo el cluster_id del
    state y combinándolo con el project_id del .env (siempre actual)
    construimos la URL correcta sin re-aplicar.

    `resource_type` distingue OpenSearch (`huaweicloud_css_cluster`) de
    Logstash (`huaweicloud_css_logstash_cluster`).
    """
    state_file = terraform_dir / "terraform.tfstate"
    if not state_file.exists():
        return {}
    try:
        st = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    for res in st.get("resources", []):
        if res.get("type") == resource_type:
            instances = res.get("instances") or []
            if instances:
                attrs = instances[0].get("attributes", {})
                # Endpoint público (EIP) si el cluster tiene public_access activo.
                # El bloque `public_access` es una lista; `public_ip` es la EIP
                # (computed). Lo exponemos como "<ip>:9200" para que el import de
                # dashboards lo alcance desde fuera de la VPC.
                public_endpoint = ""
                pa = attrs.get("public_access") or []
                if isinstance(pa, list) and pa:
                    public_ip = (pa[0] or {}).get("public_ip", "") or ""
                    if public_ip:
                        # El atributo ya viene como "IP:9200" (igual que `endpoint`).
                        # Solo agregar el puerto si no lo trae (evita ":9200:9200").
                        public_endpoint = public_ip if ":" in public_ip else f"{public_ip}:9200"
                # Endpoint público de Kibana/Dashboards (servicio aparte del 9200):
                # ahí vive la API de saved_objects para el import de dashboards.
                # `kibana_public_access[0].public_ip` viene como "IP:puerto".
                kibana_endpoint = ""
                kpa = attrs.get("kibana_public_access") or []
                if isinstance(kpa, list) and kpa:
                    kibana_ip = (kpa[0] or {}).get("public_ip", "") or ""
                    if kibana_ip:
                        kibana_endpoint = kibana_ip if ":" in kibana_ip else f"{kibana_ip}:5601"
                return {
                    "id": attrs.get("id", "") or "",
                    "endpoint": attrs.get("endpoint", "") or "",
                    "public_endpoint": public_endpoint,
                    "kibana_endpoint": kibana_endpoint,
                    "name": attrs.get("name", "") or "",
                }
    return {}


def _read_opensearch_cluster_from_state(terraform_dir: Path) -> dict[str, str]:
    """Cluster OpenSearch (id + endpoint + name) desde el tfstate."""
    return _read_css_resource_from_state(terraform_dir, "huaweicloud_css_cluster")


def _overlay_public_endpoints(cluster: dict[str, str], tf_outputs: dict[str, Any]) -> dict[str, str]:
    """Completa `public_endpoint`/`kibana_endpoint` del cluster desde los outputs
    de Terraform cuando el state no los trae.

    Con el acceso vía NAT gateway + DNAT, las direcciones públicas (EIP del NAT)
    salen de los outputs `opensearch_public_endpoint` / `dashboards_public_endpoint`,
    no del recurso css_cluster (que ya no tiene bloques public_access). Si el
    cluster ya los trae (setup viejo con EIP de CSS en el state), se respetan.
    """
    if not cluster.get("public_endpoint"):
        v = (tf_outputs.get("opensearch_public_endpoint") or {}).get("value", "") or ""
        if v:
            cluster["public_endpoint"] = v
    if not cluster.get("kibana_endpoint"):
        v = (tf_outputs.get("dashboards_public_endpoint") or {}).get("value", "") or ""
        if v:
            cluster["kibana_endpoint"] = v
    return cluster


def _read_nat_eip_from_state(terraform_dir: Path) -> str:
    """Address de la EIP del NAT gateway desde el tfstate (huaweicloud_vpc_eip)."""
    state_file = terraform_dir / "terraform.tfstate"
    if not state_file.exists():
        return ""
    try:
        st = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    for res in st.get("resources", []):
        if res.get("type") == "huaweicloud_vpc_eip":
            inst = res.get("instances") or []
            if inst:
                return (inst[0].get("attributes", {}) or {}).get("address", "") or ""
    return ""


def _cluster_with_public_access(terraform_dir: Path) -> dict[str, str]:
    """Cluster del state + endpoint público derivado de la EIP del NAT (todo desde
    el state). Para endpoints que NO corren terraform (no tienen tf_outputs), ej.
    /apply-schema."""
    cluster = _read_opensearch_cluster_from_state(terraform_dir)
    if not cluster.get("public_endpoint"):
        eip = _read_nat_eip_from_state(terraform_dir)
        if eip:
            cluster["public_endpoint"] = f"{eip}:9200"
    return cluster


# Marcador de "entorno desplegado por la plataforma". El wizard lo escribe al
# terminar un deploy exitoso; el destroy lo borra. "Mi Infraestructura" SOLO
# considera activo un entorno si este marcador existe — así no reclama como
# propio un tfstate que quedó de un `terraform apply` manual, de un leftover,
# o (como pasó) de un cluster que el operador borró a mano pero cuyo state
# quedó stale. La fuente de verdad de "lo levanté yo con la app" es ESTE
# archivo, no la mera presencia de recursos en el state.
_PLATFORM_MARKER_NAME = ".platform_deploy.json"


def _read_https_enabled_from_state(terraform_dir: Path) -> bool:
    """Lee https_enabled del tfstate (huaweicloud_css_cluster). Default False."""
    state_file = terraform_dir / "terraform.tfstate"
    if not state_file.exists():
        return False
    try:
        st = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    for res in st.get("resources", []):
        if res.get("type") == "huaweicloud_css_cluster":
            instances = res.get("instances") or []
            if instances:
                return bool(instances[0].get("attributes", {}).get("https_enabled", False))
    return False


def _write_platform_marker(terraform_dir: Path, project_name: str) -> None:
    """Escribe el marcador de deploy. Best-effort: si falla, no rompe el
    deploy (el entorno igual quedó levantado); solo perdemos el tracking
    en la UI hasta el próximo deploy."""
    marker = terraform_dir / _PLATFORM_MARKER_NAME
    try:
        marker.write_text(
            json.dumps({
                "deployed_at": datetime.now(timezone.utc).isoformat(),
                "project_name": project_name or "log-analytics",
            }),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[deploy] no se pudo escribir el marcador de plataforma: {exc!r}")


def _read_platform_marker(terraform_dir: Path) -> dict[str, Any] | None:
    """Lee el marcador de deploy, o None si no existe / está corrupto."""
    marker = terraform_dir / _PLATFORM_MARKER_NAME
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _effective_project_name(request, terraform_dir: Path) -> str:
    """project_name a usar en el deploy. En reuse ("Nuevo pipeline", not fresh_deploy)
    lo FIJAMOS al del entorno existente (marcador): el cluster Logstash se llama
    "${project_name}-logstash", así que un nombre distinto haría que Terraform lo
    RECREE (un Logstash nuevo) en vez de agregar la pipeline al que ya está."""
    requested = request.project_name or "log-analytics"
    if not getattr(request, "fresh_deploy", False):
        existing = (_read_platform_marker(terraform_dir) or {}).get("project_name")
        if existing:
            return existing
    return requested


def _remove_platform_marker(terraform_dir: Path) -> None:
    """Borra el marcador (best-effort). Lo llama el destroy al terminar."""
    marker = terraform_dir / _PLATFORM_MARKER_NAME
    try:
        if marker.exists():
            marker.unlink()
    except OSError:
        pass


# ── Guardrail de costo: reaper de TTL (auto-destroy de entornos demo viejos) ──
# Opt-in con ENV_TTL_HOURS>0. Cada ENV_TTL_CHECK_SECONDS recorre los workspaces
# por-usuario y destruye los entornos cuyo marker supera el TTL, reusando el
# destroy.auto.tfvars.json que persistió el deploy (con las creds). Off por
# default: destruir infra es una acción fuerte.
def _reap_expired_envs(ttl_hours: float) -> None:
    import subprocess as _sp
    from datetime import timedelta as _timedelta

    cutoff = datetime.now(timezone.utc) - _timedelta(hours=ttl_hours)
    users_root = auth.DATA_ROOT / "users"
    if not users_root.is_dir():
        return
    for udir in users_root.iterdir():
        try:
            tdir = udir / "terraform"
            marker = tdir / _PLATFORM_MARKER_NAME
            state = tdir / "terraform.tfstate"
            if not marker.is_file() or not state.is_file() or state.stat().st_size < 200:
                continue
            data = json.loads(marker.read_text(encoding="utf-8"))
            ts = data.get("deployed_at")
            try:
                when = datetime.fromisoformat(ts) if ts else datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.utc)
            except ValueError:
                when = datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.utc)
            if when > cutoff:
                continue
            print(f"[ttl-reaper] destruyendo entorno vencido de {udir.name} (deployed {ts})", flush=True)
            r = _sp.run(["terraform", "destroy", "-auto-approve", "-input=false"],
                        cwd=tdir, capture_output=True, text=True, timeout=1800)
            ok = r.returncode == 0
            audit.record("ttl_autodestroy", f"user_dir={udir.name} ok={ok}", user="(ttl-reaper)")
            if ok:
                try:
                    marker.unlink()
                except OSError:
                    pass
            else:
                print(f"[ttl-reaper] destroy falló para {udir.name}: {(r.stderr or r.stdout)[-400:]}", flush=True)
        except Exception as exc:
            print(f"[ttl-reaper] error con {udir.name}: {exc!r}", flush=True)


def _start_ttl_reaper() -> None:
    try:
        ttl = float(os.environ.get("ENV_TTL_HOURS", "0") or "0")
    except ValueError:
        ttl = 0.0
    if ttl <= 0:
        return
    interval = max(300, int(os.environ.get("ENV_TTL_CHECK_SECONDS", "1800") or "1800"))

    def _loop():
        import time as _t
        while True:
            try:
                _reap_expired_envs(ttl)
            except Exception:
                pass
            _t.sleep(interval)

    _threading.Thread(target=_loop, name="ttl-reaper", daemon=True).start()
    print(f"[ttl-reaper] activo: destruye entornos > {ttl}h (chequeo cada {interval}s)", flush=True)


_start_ttl_reaper()


# Registro de pipelines activas en el cluster. Cada "Nuevo pipeline" agrega una
# entrada (slug → conf + flags + índice/prefijo) que corre EN PARALELO con las
# demás. Es la fuente de verdad para reconstruir la var `pipelines` de Terraform
# en cada deploy (sin esto, un apply con el mapa vacío destruiría las otras).
# `deploy.auto.tfvars.json` se borra tras cada deploy; este registro persiste.
_PIPELINES_REGISTRY_NAME = ".pipelines.json"
# Tope de pipelines concurrentes: el Logstash es 1 nodo ess.spec-4u8g (4 vCPU);
# con workers=2 por pipeline, ~4 conviven sin sobre-suscribir el nodo. Configurable
# por env (guardrail de costo) — default 5.
try:
    _MAX_PIPELINES = max(1, int(os.environ.get("MAX_PIPELINES_PER_USER", "5")))
except ValueError:
    _MAX_PIPELINES = 5


def _determine_flavor(num_pipelines: int) -> tuple[str, str]:
    """Determina los flavors de Logstash y OpenSearch según la cantidad de pipelines.
    
    1 pipeline → ess.spec-4u8g (4 vCPU, 8 GB) para ambos
    2-5 pipelines → ess.spec-8u16g (8 vCPU, 16 GB) para ambos
    
    Returns:
        (logstash_flavor, opensearch_flavor)
    """
    if num_pipelines <= 1:
        return ("ess.spec-4u8g", "ess.spec-4u8g")
    return ("ess.spec-8u16g", "ess.spec-8u16g")


def _read_pipelines_registry(terraform_dir: Path) -> dict[str, dict]:
    """Lee el registro de pipelines, o {} si no existe / está corrupto."""
    f = terraform_dir / _PIPELINES_REGISTRY_NAME
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _huawei_infra_tfvars() -> dict[str, str]:
    """IDs de infra de ⚙ Configuración para el deploy.auto.tfvars.json.

    Solo si el SA configuró la terna vpc/subnet/security group (todo o nada:
    mezclar la VPC de una cuenta con la subnet del tfvars estático no puede
    funcionar). AZ y región acompañan si están; si no, quedan los defaults."""
    values = get_huawei_settings()
    if not all(values.get(f) for f in ("vpc_id", "subnet_id", "security_group_id")):
        return {}
    infra = {
        "vpc_id": values["vpc_id"],
        "subnet_id": values["subnet_id"],
        "security_group_id": values["security_group_id"],
    }
    if values.get("availability_zone"):
        infra["availability_zone"] = values["availability_zone"]
    if values.get("region"):
        infra["region"] = values["region"]
    return infra


def _write_pipelines_registry(terraform_dir: Path, registry: dict[str, dict]) -> None:
    """Persiste el registro de pipelines (best-effort)."""
    try:
        (terraform_dir / _PIPELINES_REGISTRY_NAME).write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"[deploy] no se pudo persistir el registro de pipelines: {exc!r}")


def _remove_pipelines_registry(terraform_dir: Path) -> None:
    """Borra el registro (best-effort). Lo llama el destroy al terminar."""
    try:
        (terraform_dir / _PIPELINES_REGISTRY_NAME).unlink(missing_ok=True)
    except OSError:
        pass


# ── Registro de capabilities provisionadas (ml-commons / AD / forecast / alert) ──
# Sibling de `.pipelines.json`: por slug guarda los IDs de los artefactos creados
# en el cluster (agent, models, connectors, detector, forecaster, monitor) para
# que el copiloto los use (agent_id) y el destroy los limpie.
_CAPABILITIES_REGISTRY_NAME = ".capabilities.json"


def _read_capabilities(terraform_dir: Path) -> dict[str, dict]:
    f = terraform_dir / _CAPABILITIES_REGISTRY_NAME
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_capabilities(terraform_dir: Path, registry: dict[str, dict]) -> None:
    try:
        (terraform_dir / _CAPABILITIES_REGISTRY_NAME).write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"[capabilities] no se pudo persistir el registro: {exc!r}")


def _remove_capabilities(terraform_dir: Path) -> None:
    try:
        (terraform_dir / _CAPABILITIES_REGISTRY_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def _cluster_admin_password(terraform_dir: Path) -> str:
    """Password admin de OpenSearch para llamadas REST fuera del wizard (ej. el
    copiloto en modo datos). Orden: creds persistidas → env → default de plataforma."""
    for name in ("destroy.auto.tfvars.json", "deploy.auto.tfvars.json"):
        f = terraform_dir / name
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("opensearch_password"):
                    return str(d["opensearch_password"])
            except (json.JSONDecodeError, OSError):
                pass
    return os.getenv("OPENSEARCH_PASSWORD", "Huawei1234")


def _cluster_hwc_creds(terraform_dir: Path) -> "tuple[str, str]":
    """AK/SK de Huawei persistidos en los tfvars — para llamar a la API del CSS
    (Cluster Routes) sobre un cluster ya desplegado, sin re-pedir creds."""
    for name in ("destroy.auto.tfvars.json", "deploy.auto.tfvars.json"):
        f = terraform_dir / name
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                ak = d.get("hwc_access_key") or d.get("obs_access_key") or ""
                sk = d.get("hwc_secret_key") or d.get("obs_secret_key") or ""
                if ak and sk:
                    return str(ak), str(sk)
            except (json.JSONDecodeError, OSError):
                pass
    return os.getenv("HWC_ACCESS_KEY", ""), os.getenv("HWC_SECRET_KEY", "")


def _slug_from_index(index_name: str) -> str:
    """Deriva un slug estable del nombre de índice del output.

    El slug es la clave del pipeline en el mapa de Terraform y parte del nombre
    de la configuración (`${project}-${slug}`). Tomamos el prefijo antes del
    date-math y lo saneamos a ``[a-z0-9-]``. Como el frontend garantiza un
    índice distinto por pipeline en reuse (``logs``, ``logs-ej2``…), los slugs
    salen únicos sin trackear estado extra.

      - ``"logs-%{+YYYY.MM}"``  → ``"logs"``
      - ``"logs-ej2-%{+YYYY.MM}"`` → ``"logs-ej2"``
    """
    base = (index_name or "").split("%{", 1)[0].strip().rstrip("-._/ ")
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug or "pipeline"


# Artifact del index template, persistido en el deploy para que "Mi
# Infraestructura" lo muestre (hidratado del disco, sobrevive F5) sin tener
# que pasar por el wizard. Se arma de los `fields` (tipos) del step 2.
_INDEX_TEMPLATE_ARTIFACT_NAME = ".index_template.json"


def _write_index_template_artifact(terraform_dir: Path, request: "TerraformDeployRequest") -> None:
    """Construye el index template de los fields del deploy y lo persiste.
    Multi-deploy: un template POR caso (cada tipo tiene su índice + sus fields),
    concatenados en el snippet. Best-effort: si no hay fields o falla, no rompe."""
    try:
        base_name = request.project_name or "log-analytics"
        if request.cases:
            cases = [c for c in request.cases if c.fields]
            if not cases:
                return
            snippets, patterns, first_tpl = [], [], None
            for case in cases:
                # Los tipos predefinidos parsean a top-level (namespace "").
                tpl = build_index_template(case.fields, "", case.index_name)
                snippets.append(put_snippet(f"{base_name}-{case.slug}", tpl))
                patterns.append(index_pattern_from_name(case.index_name))
                if first_tpl is None:
                    first_tpl = tpl
            artifact = {
                "template_name": base_name,
                "index_pattern": ", ".join(patterns),
                "template": first_tpl,
                "put_snippet": "\n\n".join(snippets),
            }
        else:
            if not request.fields:
                return
            template = build_index_template(
                request.fields, request.namespace, request.opensearch_index
            )
            artifact = {
                "template_name": base_name,
                "index_pattern": index_pattern_from_name(request.opensearch_index),
                "template": template,
                "put_snippet": put_snippet(base_name, template),
            }
        (terraform_dir / _INDEX_TEMPLATE_ARTIFACT_NAME).write_text(
            json.dumps(artifact, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 — best-effort, no rompemos el deploy
        print(f"[deploy] no se pudo persistir el index template: {exc!r}")


def _read_index_template_artifact(terraform_dir: Path) -> dict[str, Any] | None:
    """Lee el artifact del index template, o None si no existe / corrupto."""
    f = terraform_dir / _INDEX_TEMPLATE_ARTIFACT_NAME
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# Credenciales persistidas para el teardown. `terraform destroy` necesita los
# mismos AK/SK/password del provider que el apply; sin ellos, terraform los
# pide por stdin y el subprocess se cuelga hasta el timeout. Las guardamos en
# un `*.auto.tfvars.json` (auto-cargado por terraform) que vive hasta el
# destroy. El destroy lo borra al terminar.
_DESTROY_CREDS_NAME = "destroy.auto.tfvars.json"


def _write_destroy_creds(terraform_dir: Path, request: "TerraformDeployRequest") -> None:
    """Persiste solo las credenciales del provider para el teardown."""
    creds: dict[str, Any] = {}
    if request.obs_access_key:
        creds["hwc_access_key"] = request.obs_access_key
        creds["obs_access_key"] = request.obs_access_key
    if request.obs_secret_key:
        creds["hwc_secret_key"] = request.obs_secret_key
        creds["obs_secret_key"] = request.obs_secret_key
    if request.opensearch_password:
        creds["opensearch_password"] = request.opensearch_password
    if not creds:
        return
    try:
        (terraform_dir / _DESTROY_CREDS_NAME).write_text(
            json.dumps(creds, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        print(f"[deploy] no se pudo persistir creds de teardown: {exc!r}")


def _build_dashboards_url(cluster: dict[str, str], https_enabled: bool = True) -> str:
    """Construye la URL de OpenSearch Dashboards.

    Prioriza la consola Huawei (accesible desde internet) si hay
    project_id (del .env) + cluster_id (del state). Si falta alguno, cae al
    link interno por VPC (solo accesible desde una VM de la misma VPC).
    """
    cluster_id = cluster.get("id", "")
    # Para el link directo preferimos el endpoint público (EIP) sobre el privado.
    endpoint = cluster.get("public_endpoint", "") or cluster.get("endpoint", "")
    project_id = get_huawei_project_id()
    region = get_region()
    if project_id and cluster_id:
        return (
            f"https://{region}-console.huaweicloud.com/elasticsearch/kibana/"
            f"{region}/{project_id}/{cluster_id}/app/login"
        )
    if endpoint:
        proto = "https" if https_enabled else "http"
        return f"{proto}://{endpoint}/_dashboards"
    return ""


_INDEX_TEMPLATE_APPLY_RETRIES = 3
_INDEX_TEMPLATE_APPLY_RETRY_DELAY = 5.0


def _apply_index_templates(
    request: "TerraformDeployRequest",
    cluster: dict[str, str],
) -> bool:
    """Aplica el/los index template(s) al cluster con un PUT directo a
    `_index_template/<name>`, reemplazando el paso manual de Dev Tools.

    Alcanza el cluster por el endpoint PÚBLICO (EIP) igual que el import de
    dashboards. Construye el template con `build_index_template` (mismos fields
    que `_write_index_template_artifact`). Best-effort con reintentos: si falla,
    retorna False sin romper el deploy (queda el snippet manual como fallback).
    """
    import requests

    endpoint = cluster.get("public_endpoint", "") or cluster.get("endpoint", "")
    if not endpoint:
        print("[index-template] no hay endpoint del cluster — skip")
        return False
    if not request.opensearch_password:
        print("[index-template] no hay password — skip")
        return False

    base_name = request.project_name or "log-analytics"
    templates: list[tuple[str, dict[str, Any]]] = []
    if request.cases:
        for case in request.cases:
            # Precedencia: template curado (verbatim, ej. fintech-transactions con
            # nested/geo_point/index.sort) → si no, el auto-generado de los campos.
            bundled = _bundled_index_template(case.slug)
            if bundled is not None:
                templates.append((f"{base_name}-{case.slug}", bundled))
                continue
            if not case.fields:
                continue
            # Predefinidos: parsean a top-level (namespace "").
            templates.append((
                f"{base_name}-{case.slug}",
                build_index_template(case.fields, "", case.index_name),
            ))
    else:
        bundled = _bundled_index_template(request.pipeline_slug)
        if bundled is not None:
            templates.append((base_name, bundled))
        elif request.fields:
            templates.append((
                base_name,
                build_index_template(request.fields, request.namespace, request.opensearch_index),
            ))

    if not templates:
        print("[index-template] no hay fields — nada para aplicar")
        return False

    proto = "https" if request.https_enabled else "http"
    user = request.opensearch_user or "admin"
    if cluster.get("public_endpoint"):
        print(f"[index-template] usando endpoint público {endpoint}")

    all_ok = True
    for name, tpl in templates:
        url = f"{proto}://{endpoint}/_index_template/{name}"
        applied = False
        for attempt in range(1, _INDEX_TEMPLATE_APPLY_RETRIES + 1):
            try:
                resp = requests.put(
                    url,
                    json=tpl,
                    auth=(user, request.opensearch_password),
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                    verify=False,
                )
                if resp.status_code in (200, 201):
                    print(f"[index-template] aplicado '{name}' (attempt {attempt})")
                    applied = True
                    break
                print(f"[index-template] '{name}' status {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:  # noqa: BLE001 — best-effort
                print(f"[index-template] error aplicando '{name}' (attempt {attempt}): {exc!r}")
            if attempt < _INDEX_TEMPLATE_APPLY_RETRIES:
                time.sleep(_INDEX_TEMPLATE_APPLY_RETRY_DELAY)
        if not applied:
            print(f"[index-template] no se pudo aplicar '{name}' tras {_INDEX_TEMPLATE_APPLY_RETRIES} intentos")
        all_ok = all_ok and applied
    return all_ok


def _delete_indices(endpoint: str, pattern: str, password: str,
                    https_enabled: bool, user: str = "admin") -> int:
    """Borra los índices concretos que matchean `pattern` (ej. `firewall-*`).

    Lista primero (`_cat/indices`) y borra por NOMBRE exacto — así no choca con
    `action.destructive_requires_name`, que bloquea el delete por wildcard.
    Best-effort: loguea y sigue.
    """
    import requests

    proto = "https" if https_enabled else "http"
    try:
        r = requests.get(
            f"{proto}://{endpoint}/_cat/indices/{pattern}?h=index&format=json",
            auth=(user, password), timeout=30, verify=False,
        )
        if r.status_code != 200:
            return 0
        names = [row.get("index") for row in r.json() if row.get("index")]
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"[clear-index] no pude listar '{pattern}': {exc!r}")
        return 0
    deleted = 0
    for name in names:
        try:
            d = requests.delete(f"{proto}://{endpoint}/{name}", auth=(user, password),
                                timeout=30, verify=False)
            if d.status_code in (200, 404):
                deleted += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[clear-index] error borrando '{name}': {exc!r}")
    if deleted:
        print(f"[clear-index] borré {deleted} índice(s) de '{pattern}'")
    return deleted


def _clear_case_indices(request: "TerraformDeployRequest", cluster: dict[str, str]) -> None:
    """Borra los índices de los casos del request (ANTES de re-ingestar), para que
    cada corrida arranque con un índice limpio (sin docs acumulados de deploys
    anteriores). El index template ya se aplicó en el paso 2, así que el índice
    nuevo se recrea bien tipado. Best-effort."""
    endpoint = cluster.get("public_endpoint", "") or cluster.get("endpoint", "")
    if not endpoint or not request.opensearch_password:
        return
    user = request.opensearch_user or "admin"
    if request.cases:
        patterns = [index_pattern_from_name(c.index_name) for c in request.cases]
    else:
        patterns = [index_pattern_from_name(request.opensearch_index)]
    for pat in patterns:
        _delete_indices(endpoint, pat, request.opensearch_password, request.https_enabled, user)


_DASHBOARDS_IMPORT_RETRIES = 3
_DASHBOARDS_IMPORT_RETRY_DELAY = 5.0


def _saved_objects_to_kibana_bulk(ndjson_content: str) -> str:
    """Transforma el NDJSON de saved objects al body `_bulk` del índice `.kibana`.

    Cada objeto `{attributes, id, type, references}` → documento
    `{type, <type>: attributes, references, updated_at}` con `_id = "<type>:<id>"`,
    que es el formato interno con que OpenSearch Dashboards guarda los saved objects.
    Las líneas sin `type`/`id` (ej. `{"exportedCount": N}`) se saltean.
    """
    import json as _json

    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []
    for raw in ndjson_content.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = _json.loads(raw)
        except _json.JSONDecodeError:
            continue
        t = obj.get("type")
        _id = obj.get("id")
        if not t or not _id:
            continue
        doc = {
            "type": t,
            t: obj.get("attributes", {}),
            "references": obj.get("references", []),
            "updated_at": now,
        }
        lines.append(_json.dumps({"index": {"_id": f"{t}:{_id}"}}))
        lines.append(_json.dumps(doc))
    return ("\n".join(lines) + "\n") if lines else ""


def _import_dashboards_via_opensearch(
    ndjson_content: str, slug: str, endpoint: str, password: str,
    https_enabled: bool, user: str = "admin",
) -> bool:
    """Back-door: escribe los saved objects directo en el índice `.kibana` vía el
    motor OpenSearch (9200).

    En Huawei CSS, OpenSearch Dashboards (Kibana) NO está expuesto en la NIC del
    nodo de datos (la API `saved_objects/_import` no es alcanzable ni por NAT). Pero
    los saved objects SON documentos del índice `.kibana`, y a OpenSearch sí llegamos
    por el 9200. Así que los escribimos con un `_bulk` a `.kibana`.
    """
    import requests

    bulk = _saved_objects_to_kibana_bulk(ndjson_content)
    if not bulk:
        return False
    proto = "https" if https_enabled else "http"
    url = f"{proto}://{endpoint}/.kibana/_bulk?refresh=wait_for"
    try:
        resp = requests.post(
            url, data=bulk.encode("utf-8"), auth=(user, password),
            headers={"Content-Type": "application/x-ndjson"},
            timeout=30, verify=False,
        )
        if resp.status_code in (200, 201):
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {}
            if body.get("errors"):
                first = next(
                    (it for it in body.get("items", [])
                     if (list(it.values())[0] or {}).get("error")), None)
                print(f"[dashboards] back-door .kibana con errores para '{slug}': {str(first)[:300]}")
                return False
            print(f"[dashboards] import OK para '{slug}' vía back-door .kibana ({endpoint})")
            return True
        print(f"[dashboards] back-door .kibana status {resp.status_code} para '{slug}': {resp.text[:200]}")
    except requests.exceptions.RequestException as exc:
        print(f"[dashboards] error back-door .kibana '{slug}': {exc!r}")
    return False


def _import_dashboards(
    slug: str,
    cluster: dict[str, str],
    password: str,
    https_enabled: bool = True,
    terraform_dir: Path | None = None,
    fields: list[dict[str, Any]] | None = None,
    index_name: str | None = None,
    time_bounds: "tuple[float, float, int] | None" = None,
) -> bool:
    """Importa dashboards baseline al cluster OpenSearch Dashboards.

    El NDJSON sale, en orden: docs/dashboards/<slug>.ndjson (disco) → spec hecho a
    mano (`build_ndjson(slug)`) → **genérico de los campos detectados**
    (`build_ndjson_from_fields`) cuando el slug no tiene spec pero vienen `fields`
    (caso custom / "Tu log específico"). Best-effort con reintentos.

    Args:
        slug: Identificador del caso (firewall, fintech-transactions, logs custom, …)
        cluster: Dict con endpoint del cluster OpenSearch
        password: Password del usuario admin
        https_enabled / terraform_dir: como antes.
        fields / index_name: campos detectados + nombre de índice, para auto-generar
            el dashboard cuando no hay spec (custom).

    Returns:
        True si la importación fue exitosa, False si falló.
    """
    import requests
    from dashboards import build_ndjson, build_ndjson_from_fields, get_available_slugs

    if not password:
        print("[dashboards] no hay password de OpenSearch — skip import")
        return False

    # NDJSON: disco → spec hecho a mano → genérico de los campos detectados.
    ndjson_content: str | None = None
    if terraform_dir:
        ndjson_file = terraform_dir.parent / "docs" / "dashboards" / f"{slug}.ndjson"
        if ndjson_file.exists():
            try:
                ndjson_content = ndjson_file.read_text(encoding="utf-8")
                print(f"[dashboards] usando NDJSON de disco: {ndjson_file}")
            except OSError as exc:
                print(f"[dashboards] error leyendo {ndjson_file}: {exc!r}")
    if ndjson_content is None and slug in get_available_slugs():
        try:
            ndjson_content = build_ndjson(slug)
            print(f"[dashboards] NDJSON generado (spec) para '{slug}'")
        except Exception as exc:  # noqa: BLE001
            print(f"[dashboards] error generando NDJSON de spec: {exc!r}")
    if ndjson_content is None and fields:
        try:
            ndjson_content = build_ndjson_from_fields(slug, index_name or f"{slug}-*", fields)
            print(f"[dashboards] NDJSON auto-generado de {len(fields)} campos para '{slug}'")
        except Exception as exc:  # noqa: BLE001
            print(f"[dashboards] error generando NDJSON de campos: {exc!r}")
    if ndjson_content is None:
        print(f"[dashboards] '{slug}' sin spec, sin disco y sin campos — skip import")
        return False

    user = "admin"
    proto = "https" if https_enabled else "http"
    os_endpoint = cluster.get("public_endpoint", "") or cluster.get("endpoint", "")
    kibana = cluster.get("kibana_endpoint", "")
    if not os_endpoint and not kibana:
        print("[dashboards] no hay endpoint del cluster — skip import")
        return False

    # Ajustar el rango temporal del dashboard al de los datos REALES del índice
    # (`time_bounds` = min/max de @timestamp, que calcula el caller): abre mostrando
    # su serie sin tocar el time picker — fraud (2017-2018) abre en 2017, no en la
    # ventana fija 2025-2026. Si no vienen bounds, queda el default del NDJSON.
    if time_bounds:
        try:
            bmin, bmax, _ = time_bounds
            pad = max(3600000.0, (bmax - bmin) * 0.01)
            ndjson_content = _patch_dashboard_time_range(
                ndjson_content, _epoch_ms_to_iso(bmin - pad), _epoch_ms_to_iso(bmax + pad))
            print(f"[dashboards] '{slug}' timeRange ajustado a los datos: "
                  f"{_epoch_ms_to_iso(bmin)} .. {_epoch_ms_to_iso(bmax)}")
        except Exception as exc:  # noqa: BLE001
            print(f"[dashboards] no se pudo ajustar el timeRange de '{slug}' (best-effort): {exc!r}")

    for attempt in range(1, _DASHBOARDS_IMPORT_RETRIES + 1):
        # 1) Back-door por OpenSearch: escribir los saved objects en `.kibana` por
        #    el 9200. Es lo que funciona en Huawei CSS (Kibana no es alcanzable
        #    directo en la NIC del nodo, ni por NAT).
        if os_endpoint and _import_dashboards_via_opensearch(
            ndjson_content, slug, os_endpoint, password, https_enabled, user
        ):
            return True
        # 2) Fallback: API saved_objects de Kibana (si hay un endpoint Kibana
        #    alcanzable, ej. kibana_public_access). Raíz y basePath /_dashboards.
        if kibana:
            for url in (
                f"{proto}://{kibana}/api/saved_objects/_import?overwrite=true",
                f"{proto}://{kibana}/_dashboards/api/saved_objects/_import?overwrite=true",
            ):
                try:
                    resp = requests.post(
                        url, headers={"osd-xsrf": "true"}, auth=(user, password),
                        files={"file": ("import.ndjson", ndjson_content, "application/json")},
                        timeout=30, verify=False,
                    )
                    if resp.status_code == 200:
                        print(f"[dashboards] import exitoso para '{slug}' via {url}")
                        return True
                    print(f"[dashboards] '{slug}' status {resp.status_code} via {url}: {resp.text[:200]}")
                except requests.exceptions.RequestException as exc:
                    print(f"[dashboards] error import '{slug}' via {url}: {exc!r}")
        if attempt < _DASHBOARDS_IMPORT_RETRIES:
            time.sleep(_DASHBOARDS_IMPORT_RETRY_DELAY)

    print(f"[dashboards] import falló tras {_DASHBOARDS_IMPORT_RETRIES} intentos para '{slug}'")
    return False


# ── Capability provisioner (ml-commons / anomaly detection / forecast / alerting) ──
# Provisiona por REST, sobre el mismo endpoint público + auth admin que usa
# apply-schema. Best-effort: cada capability se envuelve; un fallo no rompe el
# resto ni el deploy. El conversacional (ml-commons) es el que puede no estar
# disponible en un cluster fresco → un preflight lo detecta y lo saltea con
# mensaje claro.
_ML_TASK_POLL_RETRIES = 20
_ML_TASK_POLL_DELAY = 3.0


def _os_base(cluster: dict[str, str], https_enabled: bool) -> str:
    endpoint = cluster.get("public_endpoint", "") or cluster.get("endpoint", "")
    proto = "https" if https_enabled else "http"
    return f"{proto}://{endpoint}"


# ── Cluster Routes del CSS (salida a MaaS) ───────────────────────────────────
# El cluster CSS necesita alcanzar el endpoint MaaS (connector del agente). Eso NO
# se resuelve con rutas de VPC/NAT sino con las "Cluster Routes" del CSS, que el
# provider terraform no expone → se agregan por la API del CSS con el SDK oficial
# (firma AK/SK). Default: las 2 IPs a las que resuelve api-*.modelarts-maas.com;
# override por env MAAS_ROUTE_IPS ("ip1,ip2") si cambian.
_MAAS_ROUTE_IPS_DEFAULT = ["119.8.35.218", "183.87.47.249"]


def _maas_route_ips() -> list[str]:
    raw = os.getenv("MAAS_ROUTE_IPS", "").strip()
    if raw:
        return [ip.strip() for ip in raw.split(",") if ip.strip()]
    return list(_MAAS_ROUTE_IPS_DEFAULT)


def _css_client(ak: str, sk: str, project_id: str):
    """Wrapper del SDK CSS (firma AK/SK) con `list_route_ips`/`add_route`. Import
    lazy: si el SDK no está instalado, devuelve None (best-effort). Encapsular el SDK
    acá deja `_add_css_cluster_routes` testeable sin el SDK (mockeando `_css_client`)."""
    try:
        from huaweicloudsdkcore.auth.credentials import BasicCredentials
        from huaweicloudsdkcore.region.region import Region
        from huaweicloudsdkcss.v1 import (
            CssClient, ListRoutesRequest, UpdateRouteRequest, UpdateRouteRequestBody,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[css-routes] SDK huaweicloudsdkcss no disponible: {exc!r}")
        return None
    region_id = get_region()
    region = Region(region_id, f"https://css.{region_id}.myhuaweicloud.com")
    client = CssClient.new_builder().with_credentials(BasicCredentials(ak, sk, project_id)).with_region(region).build()

    class _Wrapper:
        def list_route_ips(self, cluster_id: str) -> set:
            req = ListRoutesRequest()
            req.cluster_id = cluster_id
            resp = client.list_routes(req)
            return {getattr(r, "ip_address", None)
                    for r in (getattr(resp, "route_resps", None) or [])
                    if getattr(r, "ip_address", None)}

        def add_route(self, cluster_id: str, ip: str) -> None:
            req = UpdateRouteRequest()
            req.cluster_id = cluster_id
            req.body = UpdateRouteRequestBody(configtype="add_ip", configkey=ip, configvalue="255.255.255.255")
            client.update_route(req)

    return _Wrapper()


def _add_css_cluster_routes(cluster_id: str, ak: str, sk: str, project_id: str,
                            ips: "list[str] | None" = None) -> dict:
    """Agrega (idempotente) las Cluster Routes del CSS para que el cluster salga a
    MaaS. Best-effort: cualquier fallo se loguea y NO rompe el deploy. Devuelve
    ``{"added": [...], "skipped": [...], "error": str|None}``."""
    ips = ips or _maas_route_ips()
    result: dict = {"added": [], "skipped": [], "error": None}
    if not (cluster_id and ak and sk and project_id):
        result["error"] = "faltan cluster_id/AK/SK/project_id"
        print(f"[css-routes] skip: {result['error']}")
        return result
    client = _css_client(ak, sk, project_id)
    if client is None:
        result["error"] = "SDK CSS no disponible"
        return result

    existing: set = set()
    try:
        existing = client.list_route_ips(cluster_id)
    except Exception as exc:  # noqa: BLE001 — si el GET falla, add_ip es idempotente igual
        print(f"[css-routes] no pude listar rutas existentes (sigo igual): {exc!r}")

    for ip in ips:
        if ip in existing:
            result["skipped"].append(ip)
            continue
        try:
            client.add_route(cluster_id, ip)
            result["added"].append(ip)
            print(f"[css-routes] add_ip {ip} OK")
        except Exception as exc:  # noqa: BLE001 — 409/'already exists' → OK
            msg = str(exc)
            if "409" in msg or "exist" in msg.lower():
                result["skipped"].append(ip)
                print(f"[css-routes] {ip} ya existía")
            else:
                result["error"] = msg[:200]
                print(f"[css-routes] add_ip {ip} falló: {msg[:200]}")
    return result


def _os_req(method: str, url: str, user: str, password: str,
            json_body: "dict | None" = None, timeout: int = 30) -> "Any":
    """Request REST al cluster con el idiom de la plataforma (auth admin,
    verify=False). Devuelve el `requests.Response` o None si hubo excepción."""
    import requests
    try:
        return requests.request(
            method, url, auth=(user, password), json=json_body,
            headers={"Content-Type": "application/json"}, timeout=timeout, verify=False,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"[capabilities] {method} {url} error: {exc!r}")
        return None


def _ml_commons_available(base: str, user: str, password: str) -> bool:
    """Preflight: ¿está ml-commons en este cluster? (sin ticket).

    Sondea `GET /_plugins/_ml/stats` — una ruta GET REGISTRADA de ml-commons que
    devuelve 200 con el plugin presente. NO usar `GET /_plugins/_ml/models` (no es
    ruta válida — la colección se consulta con `_search` — y da "no handler found"
    exista o no el plugin → falso negativo)."""
    r = _os_req("GET", f"{base}/_plugins/_ml/stats", user, password, timeout=15)
    if r is not None and r.status_code == 200:
        return True
    print(f"[capabilities] preflight ml-commons no disponible "
          f"(status {getattr(r, 'status_code', None)}): {(getattr(r, 'text', '') or '')[:160]}")
    return False


def _index_ready_for_capabilities(base: str, user: str, password: str,
                                  index_pattern: str, category_field: str,
                                  need_docs: bool = False) -> "tuple[bool, str]":
    """Preflight: ¿el índice existe y (opcionalmente) tiene documentos?

    AD necesita que el `category_field` exista en el mapping del índice concreto
    (el template solo materializa al crearse el índice → sin ingesta no hay field).
    Forecast además necesita documentos históricos. Devuelve ``(ready, reason)``.
    """
    r = _os_req("GET", f"{base}/{index_pattern}/_field_caps?fields={category_field}",
                user, password, timeout=15)
    if r is None:
        return False, "índice no alcanzable"
    if r.status_code == 404 or category_field not in (r.text or ""):
        return False, "índice sin documentos aún (corre la ingesta y reejecuta)"
    if r.status_code not in (200, 201):
        return False, f"field_caps status {r.status_code}"
    if not need_docs:
        return True, ""
    r2 = _os_req("GET", f"{base}/{index_pattern}/_count", user, password, timeout=15)
    if r2 is None or r2.status_code not in (200, 201):
        return False, f"_count status {getattr(r2, 'status_code', None)}"
    try:
        count = int(r2.json().get("count", 0))
    except (ValueError, TypeError):
        count = 0
    if count == 0:
        return False, "índice vacío (sin historia para forecast)"
    return True, ""


def _discover_enums(base: str, user: str, password: str, index_pattern: str,
                    fields: "list[dict]", max_terms: int = 12,
                    max_cardinality: int = 15) -> "dict[str, list[str]]":
    """Descubre los VALORES reales de los campos dimensionales del índice.

    El chatbot (PPLTool) necesita saber que `status` vale COMPLETED/CANCELLED para
    generar PPL correcto — en los verticales de demo eso está curado a mano
    (`_CAPABILITY_SPECS[...]["operations"]`); para un log productivo se descubre acá.
    Se corre al provisionar capabilities, cuando los datos YA están ingestados, con
    una agregación `terms` nativa (sin LLM).

    Solo devuelve los campos con cardinalidad BAJA (<= `max_cardinality`): esos son
    enums reales. Un campo con un valor por documento (ids) no aporta al prompt y lo
    ensuciaría. Devuelve ``{field_path: [valores]}`` ordenado de menor a mayor
    cardinalidad (el primero es la dimensión principal → `operations`).
    """
    # Para terms agg, los campos text necesitan su subcampo .keyword (sin
    # doc_values → terms falla sobre el campo text). Los string caen al dynamic
    # template (keyword) → terms directo.
    def _agg_field(f: dict) -> str:
        path = (f.get("field_path") or "").strip()
        if (f.get("type") or "").strip() == "text":
            return f"{path}.keyword"
        return path

    dims = [(f, _agg_field(f)) for f in (fields or [])
            if f.get("dimension") and (f.get("field_path") or "").strip()]
    if not dims:
        return {}
    # Un solo request: un `terms` por dimensión, pidiendo uno más que el tope para
    # detectar (y descartar) los de alta cardinalidad.
    aggs = {f"f{i}": {"terms": {"field": agg_p, "size": max_cardinality + 1}}
            for i, (_f, agg_p) in enumerate(dims)}
    r = _os_req("POST", f"{base}/{index_pattern}/_search", user, password,
                json_body={"size": 0, "aggs": aggs}, timeout=30)
    if r is None or r.status_code not in (200, 201):
        print(f"[capabilities] _discover_enums: search status {getattr(r, 'status_code', None)} — sigo sin enums")
        return {}
    try:
        buckets_by_agg = (r.json().get("aggregations") or {})
    except (ValueError, TypeError):
        return {}
    found: list[tuple[int, str, list[str]]] = []
    for i, (f, agg_p) in enumerate(dims):
        buckets = (buckets_by_agg.get(f"f{i}") or {}).get("buckets") or []
        if not buckets or len(buckets) > max_cardinality:
            continue   # sin datos, o alta cardinalidad → no es un enum
        values = [str(b.get("key")) for b in buckets[:max_terms] if b.get("key") is not None]
        if values:
            # Devolver el field_path original (sin .keyword) para que matchee
            # con lo que el PPLTool usa en sus queries.
            found.append((len(values), f.get("field_path", ""), values))
    found.sort(key=lambda t: t[0])   # menor cardinalidad primero
    return {path: values for _n, path, values in found}


def _index_time_bounds(base: str, user: str, password: str, index_pattern: str,
                       time_field: str = "@timestamp") -> "tuple[float, float, int] | None":
    """Rango real de la serie: ``(min_epoch_ms, max_epoch_ms, doc_count)`` de
    ``@timestamp`` del índice, o ``None`` si está vacío / no responde.

    Lo usa el forecaster para caer su ventana de análisis SOBRE los datos
    (ver `_forecaster_window`) — el fallo de "INIT vacío" es no encontrar serie.
    """
    r = _os_req("POST", f"{base}/{index_pattern}/_search", user, password,
                json_body={"size": 0, "track_total_hits": True, "aggs": {
                    "tmin": {"min": {"field": time_field}},
                    "tmax": {"max": {"field": time_field}},
                }}, timeout=30)
    if r is None or r.status_code not in (200, 201):
        return None
    try:
        body = r.json()
        aggs = body.get("aggregations") or {}
        min_ms = (aggs.get("tmin") or {}).get("value")
        max_ms = (aggs.get("tmax") or {}).get("value")
        total = (body.get("hits") or {}).get("total") or {}
        count = total.get("value") if isinstance(total, dict) else total
    except (ValueError, TypeError, AttributeError):
        return None
    if min_ms is None or max_ms is None or max_ms <= min_ms:
        return None
    return (float(min_ms), float(max_ms), int(count or 0))


def _forecaster_window(min_ms: float, max_ms: float, now_ms: float,
                       doc_count: int) -> "tuple[int, int, int]":
    """Deriva ``(interval_minutes, window_delay_minutes, history)`` del rango real
    de datos, para que la ventana de análisis del RCF
    ``[now − window_delay − history×interval, now − window_delay]`` caiga sobre la
    serie (≥40 puntos poblados; requisito de OpenSearch Forecasting).

    - ``window_delay``: minutos desde el FIN de los datos hasta ahora → la ventana
      termina donde terminan los datos (por eso no es fijo: fraud 2017 necesita
      años de delay; un log reciente, minutos).
    - ``interval``: apunta a un nº de buckets ligado al volumen (~1 cada 10
      eventos), acotado [40, 2000] y sin superar el span → buckets poblados, no
      vacíos.
    - ``history``: intervalos que cubren el span, tope 10000.
    """
    span_min = max(1, int((max_ms - min_ms) // 60000))
    window_delay = 1
    buckets = min(2000, max(40, int(doc_count) // 10))
    buckets = max(1, min(buckets, span_min))
    interval = max(1, span_min // buckets)
    history = min(10000, max(1, span_min // interval + 1))
    return interval, window_delay, history


# Estados del _profile de un forecaster tras run_once. TEST_COMPLETE = el backtest
# corrió con datos (lo que queremos). Los de "esperando datos"/"init" son
# transitorios; el resto no-OK es error terminal.
_FORECAST_STATE_OK = {"TEST_COMPLETE"}
_FORECAST_STATE_PENDING = {"INIT_TEST", "INITIALIZING_TEST", "AWAITING_DATA_TO_INIT",
                           "AWAITING_DATA_TO_RESTART", "INIT", "INITIALIZING_FORECAST"}


def _forecast_test_state(base: str, user: str, password: str, fc_id: str,
                         tries: int = 8, delay: float = 5.0) -> "tuple[bool, str]":
    """Pollea `GET /_plugins/_forecast/forecasters/<id>/_profile` tras `_run_once`
    y devuelve ``(ok, state)``. ``ok`` = el backtest completó con datos
    (``TEST_COMPLETE``). Así dejamos de reportar "ok" a ciegas: si el forecaster
    queda en INIT vacío, el estado real llega al front."""
    last = "UNKNOWN"
    for _ in range(tries):
        r = _os_req("GET", f"{base}/_plugins/_forecast/forecasters/{fc_id}/_profile",
                    user, password, timeout=20)
        if r is not None and r.status_code == 200:
            try:
                body = r.json() or {}
            except (ValueError, TypeError):
                body = {}
            state = str(body.get("forecaster_state") or body.get("state") or "").upper()
            if state:
                last = state
                if state in _FORECAST_STATE_OK:
                    return True, state
                if state not in _FORECAST_STATE_PENDING and "INIT" not in state and "AWAIT" not in state:
                    return False, state   # terminal no-OK (error / failure)
        time.sleep(delay)
    return False, last


def _epoch_ms_to_iso(ms: float) -> str:
    """Epoch millis → ISO8601 UTC con milisegundos (formato del time picker de OS Dashboards)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _patch_dashboard_time_range(ndjson: str, from_iso: str, to_iso: str) -> str:
    """Reescribe el `timeFrom`/`timeTo` (+ `timeRestore`) del objeto dashboard del
    NDJSON, para que abra en el rango real de sus datos. Deja el resto intacto."""
    out: list[str] = []
    for line in ndjson.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except ValueError:
            out.append(line)
            continue
        if obj.get("type") == "dashboard":
            attrs = obj.setdefault("attributes", {})
            attrs["timeRestore"] = True
            attrs["timeFrom"] = from_iso
            attrs["timeTo"] = to_iso
            out.append(json.dumps(obj))
        else:
            out.append(line)
    return "\n".join(out)


def _ml_wait_model(base: str, user: str, password: str, task_id: str) -> "str | None":
    """Pollea `_plugins/_ml/tasks/<id>` hasta COMPLETED y devuelve el model_id."""
    for _ in range(_ML_TASK_POLL_RETRIES):
        r = _os_req("GET", f"{base}/_plugins/_ml/tasks/{task_id}", user, password, timeout=15)
        if r is not None and r.status_code == 200:
            body = r.json()
            state = body.get("state")
            if state == "COMPLETED":
                return body.get("model_id")
            if state in ("FAILED", "COMPLETED_WITH_ERROR"):
                print(f"[capabilities] task {task_id} state {state}: {body.get('error')}")
                return None
        time.sleep(_ML_TASK_POLL_DELAY)
    print(f"[capabilities] task {task_id} no completó a tiempo")
    return None


def _ml_wait_deployed(base: str, user: str, password: str, model_id: str) -> "tuple[bool, str]":
    """Pollea `GET /_plugins/_ml/models/<id>` hasta `model_state=DEPLOYED`.

    El `_deploy` es asíncrono: crea un task y devuelve al toque, con el modelo en
    DEPLOYING. Si el agente se arma antes de que el modelo termine de desplegar (o
    si el deploy FALLA), el `_execute` rompe (el modelo remoto no está listo → la
    llamada a MaaS puede propagar 401/500). Verificamos el estado real antes de
    entregar el agente. Devuelve ``(ok, state)``.
    """
    for _ in range(_ML_TASK_POLL_RETRIES):
        r = _os_req("GET", f"{base}/_plugins/_ml/models/{model_id}", user, password, timeout=15)
        if r is not None and r.status_code == 200:
            state = (r.json() or {}).get("model_state")
            if state == "DEPLOYED":
                return True, state
            if state in ("DEPLOY_FAILED", "UNDEPLOYED"):
                print(f"[capabilities] model {model_id} state {state}")
                return False, state
        time.sleep(_ML_TASK_POLL_DELAY)
    print(f"[capabilities] model {model_id} no llegó a DEPLOYED a tiempo")
    return False, "TIMEOUT"


def _ml_create(base: str, user: str, password: str, path: str, body: dict, id_key: str) -> "str | None":
    """POST genérico a ml-commons; devuelve el id (`id_key`) de la respuesta."""
    r = _os_req("POST", f"{base}{path}", user, password, json_body=body, timeout=60)
    if r is None or r.status_code not in (200, 201):
        print(f"[capabilities] POST {path} status {getattr(r,'status_code',None)}: {(getattr(r,'text','') or '')[:200]}")
        return None
    data = r.json()
    return data.get(id_key)


def _set_os_chat_root_agent(base: str, user: str, password: str, agent_id: str) -> bool:
    """Apunta el OpenSearch Assistant (chat de Dashboards) al agente root vía el
    config `os_chat` de ml-commons — así el chat lo usa sin el PUT manual.

    Best-effort: `.plugins-ml-config` es un system index; si el Assistant no está
    habilitado o faltan permisos, se loguea y se sigue (el config queda seteado y
    toma efecto cuando lo habiliten + reinicien Dashboards)."""
    r = _os_req("PUT", f"{base}/.plugins-ml-config/_doc/os_chat", user, password,
                json_body={"type": "os_chat_root_agent",
                           "configuration": {"agent_id": agent_id}}, timeout=20)
    ok = r is not None and getattr(r, "status_code", 0) in (200, 201)
    print(f"[capabilities] os_chat root agent {'seteado' if ok else 'no se pudo setear (best-effort)'}: {agent_id}")
    return ok


def _ml_register_model_group(base: str, user: str, password: str, body: dict) -> "str | None":
    """Registra el model_group; su nombre es ÚNICO en el cluster. Si ya existe (p. ej.
    un intento previo lo creó pero falló después, sin persistir su ID → el force
    teardown no lo pudo borrar), OpenSearch responde 400 con el ID existente en el
    mensaje → lo reusamos en vez de fallar."""
    r = _os_req("POST", f"{base}/_plugins/_ml/model_groups/_register", user, password, json_body=body, timeout=60)
    if r is not None and r.status_code in (200, 201):
        return r.json().get("model_group_id")
    txt = getattr(r, "text", "") or ""
    m = re.search(r"model group with ID:\s*([A-Za-z0-9_\-]+)", txt)
    if m:
        print(f"[capabilities] model_group ya existía — reuso {m.group(1)}")
        return m.group(1)
    print(f"[capabilities] model_groups/_register status {getattr(r,'status_code',None)}: {txt[:200]}")
    return None


def _ml_predict(base: str, user: str, password: str, model_id: str,
                params: dict, timeout: int = 60) -> "str | None":
    """`POST /_plugins/_ml/models/<id>/_predict` con `{"parameters": params}` y extrae
    el texto de la respuesta OpenAI-compat (`choices[0].message.content`). Devuelve el
    string o None si falló. Lo usa el chatbot PPL orquestado en el backend."""
    r = _os_req("POST", f"{base}/_plugins/_ml/models/{model_id}/_predict", user, password,
                json_body={"parameters": params}, timeout=timeout)
    if r is None or r.status_code not in (200, 201):
        print(f"[ppl-chat] _predict {model_id} status {getattr(r,'status_code',None)}: {(getattr(r,'text','') or '')[:200]}")
        return None
    try:
        out = r.json()["inference_results"][0]["output"][0]
        data = out.get("dataAsMap")
        # Con response_filter el connector deja el texto plano en dataAsMap.
        if isinstance(data, str):
            return data.strip() or None
        data = data or {}
        # Sin response_filter: estructura OpenAI completa.
        content = data.get("choices", [{}])[0].get("message", {}).get("content")
        # Con response_filter ($.choices[0].message.content): ya viene extraído.
        if not isinstance(content, str):
            for k in ("response", "content", "output", "result", "text"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    content = v
                    break
        if not isinstance(content, str):
            print(f"[ppl-chat] _predict {model_id} sin content; dataAsMap={str(data)[:400]}")
            return None
        return content.strip() or None
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        print(f"[ppl-chat] _predict {model_id} respuesta inesperada: {exc!r}")
        return None


def _teardown_slug_caps(base: str, user: str, password: str, ids: dict) -> None:
    """Borra los artefactos de capabilities de un slug (best-effort). Orden inverso."""
    if ids.get("monitor_id"):
        _os_req("DELETE", f"{base}/_plugins/_alerting/monitors/{ids['monitor_id']}", user, password, timeout=20)
    if ids.get("forecaster_ids"):
        for fid in ids["forecaster_ids"]:
            if fid:
                _os_req("POST", f"{base}/_plugins/_forecast/forecasters/{fid}/_stop", user, password, timeout=20)
                _os_req("DELETE", f"{base}/_plugins/_forecast/forecasters/{fid}", user, password, timeout=20)
    elif ids.get("forecaster_id"):
        _os_req("POST", f"{base}/_plugins/_forecast/forecasters/{ids['forecaster_id']}/_stop", user, password, timeout=20)
        _os_req("DELETE", f"{base}/_plugins/_forecast/forecasters/{ids['forecaster_id']}", user, password, timeout=20)
    if ids.get("detector_id"):
        _os_req("POST", f"{base}/_plugins/_anomaly_detection/detectors/{ids['detector_id']}/_stop", user, password, timeout=20)
        _os_req("DELETE", f"{base}/_plugins/_anomaly_detection/detectors/{ids['detector_id']}", user, password, timeout=20)
    if ids.get("agent_id"):
        _os_req("DELETE", f"{base}/_plugins/_ml/agents/{ids['agent_id']}", user, password, timeout=20)
    for mid in ids.get("model_ids", []) or []:
        if mid:
            _os_req("POST", f"{base}/_plugins/_ml/models/{mid}/_undeploy", user, password, timeout=20)
            _os_req("DELETE", f"{base}/_plugins/_ml/models/{mid}", user, password, timeout=20)
    if ids.get("model_group_id"):
        _os_req("DELETE", f"{base}/_plugins/_ml/model_groups/{ids['model_group_id']}", user, password, timeout=20)
    for cid in ids.get("connector_ids", []) or []:
        if cid:
            _os_req("DELETE", f"{base}/_plugins/_ml/connectors/{cid}", user, password, timeout=20)


def _search_ids(base: str, user: str, password: str, search_path: str,
                name: str, name_field: str = "name.keyword") -> "list[str]":
    """Devuelve los `_id` de los docs cuyo `name_field` == `name` (para limpiar
    huérfanos por nombre en el force teardown)."""
    body = {"query": {"term": {name_field: name}}, "size": 20, "_source": False}
    r = _os_req("POST", f"{base}{search_path}", user, password, json_body=body, timeout=20)
    if r is None or r.status_code != 200:
        return []
    try:
        return [h["_id"] for h in r.json().get("hits", {}).get("hits", [])]
    except (KeyError, TypeError, ValueError):
        return []


def _teardown_orphans_by_name(base: str, user: str, password: str) -> None:
    """Borra por NOMBRE los artefactos de nombre fijo que puedan haber quedado
    huérfanos (creados en runs previos que fallaron antes de persistir su ID → el
    teardown por ID no los alcanza). Idempotente, best-effort. Los nombres son los
    de los builders/spec de `capabilities.py`."""
    import capabilities as caps

    # Forecasters: hay que _stop antes de _delete. Los nombres salen de los specs
    # de TODOS los verticales (cada uno define sus 3 forecasts).
    fc_names = [fc["name"] for s in caps.get_capability_slugs()
                for fc in (caps.get_capability_spec(s) or {}).get("forecasts", [])]
    for fc_name in fc_names:
        for fid in _search_ids(base, user, password, "/_plugins/_forecast/forecasters/_search", fc_name):
            _os_req("POST", f"{base}/_plugins/_forecast/forecasters/{fid}/_stop", user, password, timeout=20)
            _os_req("DELETE", f"{base}/_plugins/_forecast/forecasters/{fid}", user, password, timeout=20)
    # Agente ANTES de los modelos (referencia al llm/ppl model).
    for aid in _search_ids(base, user, password, "/_plugins/_ml/agents/_search", "Platform Conversational Root"):
        _os_req("DELETE", f"{base}/_plugins/_ml/agents/{aid}", user, password, timeout=20)
    # Modelos ANTES del model_group (no se puede borrar un group con modelos).
    for name in ("platform-ppl", "platform-llm"):
        for mid in _search_ids(base, user, password, "/_plugins/_ml/models/_search", name):
            _os_req("POST", f"{base}/_plugins/_ml/models/{mid}/_undeploy", user, password, timeout=20)
            _os_req("DELETE", f"{base}/_plugins/_ml/models/{mid}", user, password, timeout=20)
    for gid in _search_ids(base, user, password, "/_plugins/_ml/model_groups/_search", "platform-maas-deepseek"):
        _os_req("DELETE", f"{base}/_plugins/_ml/model_groups/{gid}", user, password, timeout=20)
    for name in ("MaaS DeepSeek PPLTool (platform)", "MaaS LLM (platform)"):
        for cid in _search_ids(base, user, password, "/_plugins/_ml/connectors/_search", name):
            _os_req("DELETE", f"{base}/_plugins/_ml/connectors/{cid}", user, password, timeout=20)
    print("[capabilities] teardown de huérfanos por nombre OK")


def _provision_capabilities(cluster: dict[str, str], slug: str, user: str,
                            password: str, https_enabled: bool,
                            force: bool = False) -> dict:
    """Provisiona el bundle de capabilities del `slug` (si tiene spec) y persiste
    los IDs en `.capabilities.json`. Devuelve un dict de estado por capability.

    Si ``force=True``, tear down de artifacts existentes y recrea (para
    re-provisionar con modelo nuevo)."""
    import capabilities as caps

    base = _os_base(cluster, https_enabled)
    if not base.rsplit("//", 1)[-1]:
        return {"error": "no endpoint"}

    terraform_dir = _active_terraform_dir()

    spec = caps.get_capability_spec(slug)
    if not spec:
        # Sin spec curado → ¿es un slug productivo con fields persistidos en el
        # registry de pipelines? Se arma un spec con la MISMA forma a partir de
        # los campos detectados (paso 2) + los enums descubiertos del índice.
        pipe_reg = _read_pipelines_registry(terraform_dir)
        entry = pipe_reg.get(slug, {})
        prod_fields = entry.get("fields") or []
        if not prod_fields:
            return {}
        from index_template import index_pattern_from_name
        prod_index = entry.get("index", "")
        prod_ip = index_pattern_from_name(prod_index) if prod_index else f"{slug}*"
        prod_label = entry.get("label") or "Tus logs"
        # Descubrir enums del índice ya ingestado (sin LLM, terms agg nativa).
        prod_enums = _discover_enums(base, user, password, prod_ip, prod_fields) if base else {}
        spec = caps.build_spec_from_fields(slug, prod_ip, prod_fields, prod_label, prod_enums)
        print(f"[capabilities] spec productivo construido para '{slug}': {len(spec.get('fields',{}))} fields, {len(spec.get('forecasts',[]))} forecasts, enums={list(prod_enums.keys())}")

    registry = _read_capabilities(terraform_dir)
    ids: dict = dict(registry.get(slug, {}))
    result: dict = {}
    ip = spec["index_pattern"]

    # ── Cluster Routes del CSS → salida a MaaS (prerequisito del agente) ──────
    # El agente/chatbot llama a MaaS por el connector; sin las Cluster Routes del
    # CSS el cluster no lo alcanza ("Connection timed out ...:443"). Las agrega el
    # deploy, pero si el cluster se creó sin ellas (deploy viejo), las aseguramos
    # acá sobre el cluster existente — sin re-deploy. Best-effort.
    try:
        ak, sk = _cluster_hwc_creds(terraform_dir)
        cluster_id = cluster.get("id", "")
        pid = get_huawei_project_id()
        if cluster_id and ak and sk and pid:
            print(f"[css-routes] (provision) {_add_css_cluster_routes(cluster_id, ak, sk, pid)}")
        else:
            print(f"[css-routes] (provision) skip — cluster_id={bool(cluster_id)} creds={bool(ak and sk)} project_id={bool(pid)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[css-routes] (provision) fallo: {exc!r}")

    # ── Force: tear down y recrear desde cero ──────
    # Limpia por ID persistido Y por NOMBRE (los huérfanos de runs que fallaron sin
    # persistir su ID quedaban colgados y hacían fallar el re-create por nombre —
    # detector/model_group/etc). Corre aunque `ids` esté vacío.
    if force:
        print(f"[capabilities] force=True — tear down para '{slug}'")
        if ids:
            _teardown_slug_caps(base, user, password, ids)
        ids = {}

    # ── Conversacional (ml-commons) ──────────────────────────────────────────
    # Key de MaaS: la configurada en ⚙ Configuración (prioridad) o la del env.
    # Queda embebida en los connectors del cluster → el agente consume la key
    # del cliente, no los recursos del operador.
    from maas_integrator import get_maas_api_key
    api_key = get_maas_api_key()
    if not api_key:
        result["conversational"] = {"ok": False, "reason": "API Key de MaaS no configurada (⚙ Configuración o MAAS_API_KEY)"}
    elif not _ml_commons_available(base, user, password):
        result["conversational"] = {"ok": False, "reason": "ml-commons no disponible en el cluster"}
    elif ids.get("agent_id"):
        result["conversational"] = {"ok": True, "agent_id": ids["agent_id"],
                                    "ppl_model_id": ids.get("ppl_model_id"),
                                    "llm_model_id": ids.get("llm_model_id"), "reason": "ya provisionado"}
    else:
        try:
            _os_req("PUT", f"{base}/_cluster/settings", user, password,
                    json_body=caps.build_cluster_settings(), timeout=30)
            # Modelos/connectors GLOBALES (un solo platform-ppl/platform-llm para
            # todos los verticales): si otro slug ya los dejó DEPLOYED, se reusan
            # en vez de duplicarlos — solo se re-registra el agente con la fuente
            # nueva incluida.
            ppl_model = llm_model = None
            group_id = ppl_conn = llm_conn = None
            for o_slug, o_ids in registry.items():
                if o_slug == slug or not (o_ids.get("ppl_model_id") and o_ids.get("llm_model_id")):
                    continue
                deployed = []
                for mid in (o_ids["ppl_model_id"], o_ids["llm_model_id"]):
                    r = _os_req("GET", f"{base}/_plugins/_ml/models/{mid}", user, password, timeout=20)
                    deployed.append(r is not None and r.status_code == 200
                                    and (r.json() or {}).get("model_state") == "DEPLOYED")
                if all(deployed):
                    ppl_model, llm_model = o_ids["ppl_model_id"], o_ids["llm_model_id"]
                    group_id = o_ids.get("model_group_id")
                    conn_ids = o_ids.get("connector_ids") or [None, None]
                    ppl_conn, llm_conn = (conn_ids + [None, None])[:2]
                    print(f"[capabilities] reuso modelos globales de '{o_slug}' (platform-ppl/llm DEPLOYED)")
                    break
            if not (ppl_model and llm_model):
                group_id = _ml_register_model_group(base, user, password, caps.build_model_group())
                # Agente conversacional ml-commons con PPLTool, en OpenSearch (se consulta
                # por Dev Tools con `_execute`). `platform-ppl` (usado por los PPLTools)
                # traduce NL→PPL y la ejecuta; `platform-llm` es el LLM que razona en el
                # agente. El prompt del vertical actual queda como default del connector;
                # cada PPLTool pasa el suyo por parámetro.
                ppl_prompt = caps.build_ppl_system_prompt(ip, spec["operations"], spec["fields"], spec.get("success_code", ""), spec.get("label", slug))
                ppl_conn = _ml_create(base, user, password, "/_plugins/_ml/connectors/_create",
                                      caps.build_ppl_connector(api_key, ppl_prompt), "connector_id")
                llm_conn = _ml_create(base, user, password, "/_plugins/_ml/connectors/_create",
                                      caps.build_llm_connector(api_key), "connector_id")
            # Persistir group + connectors YA (antes del register de modelos, que puede
            # fallar): así un force teardown posterior los limpia y no quedan huérfanos
            # (fue lo que dejó el model_group colgado y rompía el re-register por nombre).
            ids["model_group_id"] = group_id
            ids["connector_ids"] = [c for c in (ppl_conn, llm_conn) if c]
            registry[slug] = ids
            _write_capabilities(terraform_dir, registry)
            if not (ppl_model and llm_model) and group_id and ppl_conn and llm_conn:
                ppl_task = _ml_create(base, user, password, "/_plugins/_ml/models/_register",
                                     caps.build_remote_model("platform-ppl", ppl_conn, group_id, "NL to PPL"), "task_id")
                llm_task = _ml_create(base, user, password, "/_plugins/_ml/models/_register",
                                     caps.build_remote_model("platform-llm", llm_conn, group_id, "LLM frasea resultados"), "task_id")
                ppl_model = _ml_wait_model(base, user, password, ppl_task) if ppl_task else None
                llm_model = _ml_wait_model(base, user, password, llm_task) if llm_task else None
                for mid in (ppl_model, llm_model):
                    if mid:
                        _os_req("POST", f"{base}/_plugins/_ml/models/{mid}/_deploy", user, password, timeout=60)
            # Verificar que AMBOS queden DEPLOYED (un modelo caído haría fallar el chatbot).
            deploy_err = None
            for label, mid in (("PPL", ppl_model), ("LLM", llm_model)):
                if not mid:
                    deploy_err = f"el modelo {label} no se registró"
                    break
                ok_dep, state = _ml_wait_deployed(base, user, password, mid)
                if not ok_dep:
                    deploy_err = f"el modelo {label} no quedó DEPLOYED (state={state})"
                    break
            if ppl_model and llm_model and not deploy_err:
                ids.update({"model_group_id": group_id, "connector_ids": [ppl_conn, llm_conn],
                            "model_ids": [ppl_model, llm_model],
                            "ppl_model_id": ppl_model, "llm_model_id": llm_model})
                registry[slug] = ids
                _write_capabilities(terraform_dir, registry)
                # UN solo agente para TODAS las fuentes: un PPLTool por vertical con
                # datos. El vertical actual entra siempre (recién ingerido); los demás
                # si su índice ya tiene docs (provisionings previos). Cada PPLTool lleva
                # el system prompt de SU vertical (el connector lo recibe por
                # ${parameters.system_prompt}); el LLM elige la fuente según la pregunta.
                agent_verticals = []
                # Fuentes curadas (demo) + productivas (registry con fields).
                pipe_reg = _read_pipelines_registry(terraform_dir)
                seen_slugs: set[str] = set()
                # 1) Specs curados.
                for slug2 in caps.get_capability_slugs():
                    seen_slugs.add(slug2)
                    spec2 = caps.get_capability_spec(slug2) or {}
                    if not spec2.get("fields"):
                        continue   # spec placeholder sin schema aún
                    ip2 = spec2["index_pattern"]
                    if slug2 != slug:
                        ok2, _ = _index_ready_for_capabilities(base, user, password, ip2,
                                                               spec2.get("volume_field", ""), need_docs=True)
                        if not ok2:
                            continue
                    agent_verticals.append({
                        "tool_name": f"PPLTool-{slug2}",
                        "label": spec2.get("label", slug2),
                        "index_pattern": ip2,
                        "operations": spec2["operations"],
                        "fields": spec2["fields"],
                        "success_code": spec2.get("success_code", ""),
                        "ppl_system_prompt": caps.build_ppl_system_prompt(
                            ip2, spec2["operations"], spec2["fields"],
                            spec2.get("success_code", ""), spec2.get("label", slug2)),
                    })
                # 2) Slugs productivos del registry (fields persistidos, sin spec curado).
                for slug2, entry2 in pipe_reg.items():
                    if slug2 in seen_slugs:
                        continue
                    prod_fields2 = entry2.get("fields") or []
                    if not prod_fields2:
                        continue
                    from index_template import index_pattern_from_name
                    prod_index2 = entry2.get("index", "")
                    ip2 = index_pattern_from_name(prod_index2) if prod_index2 else f"{slug2}*"
                    if slug2 != slug:
                        ok2, _ = _index_ready_for_capabilities(base, user, password, ip2,
                                                               prod_fields2[0].get("field_path", ""), need_docs=True)
                        if not ok2:
                            continue
                    prod_label2 = entry2.get("label") or "Tus logs"
                    prod_enums2 = _discover_enums(base, user, password, ip2, prod_fields2)
                    spec2 = caps.build_spec_from_fields(slug2, ip2, prod_fields2, prod_label2, prod_enums2)
                    if not spec2.get("fields"):
                        continue
                    agent_verticals.append({
                        "tool_name": f"PPLTool-{slug2}",
                        "label": spec2.get("label", slug2),
                        "index_pattern": ip2,
                        "operations": spec2["operations"],
                        "fields": spec2["fields"],
                        "success_code": spec2.get("success_code", ""),
                        "ppl_system_prompt": caps.build_ppl_system_prompt(
                            ip2, spec2["operations"], spec2["fields"],
                            spec2.get("success_code", ""), spec2.get("label", slug2)),
                    })
                instr = caps.build_agent_system_instruction(agent_verticals)
                # ml-commons NO exige nombres únicos de agente: sin borrar el anterior
                # quedarían duplicados. Se borra por nombre y se re-registra con la
                # lista completa de fuentes.
                for aid in _search_ids(base, user, password, "/_plugins/_ml/agents/_search",
                                       "Platform Conversational Root"):
                    _os_req("DELETE", f"{base}/_plugins/_ml/agents/{aid}", user, password, timeout=20)
                agent_id = _ml_create(base, user, password, "/_plugins/_ml/agents/_register",
                                      caps.build_conversational_agent(llm_model, ppl_model, instr, agent_verticals), "agent_id")
                if agent_id:
                    ids["agent_id"] = agent_id
                    # Apuntar el OpenSearch Assistant (chat de Dashboards) a este
                    # agente root, sin pasos manuales. Best-effort: si el Assistant
                    # todavía no está habilitado, el config queda igual y toma efecto
                    # cuando lo prendan + reinicien Dashboards.
                    _set_os_chat_root_agent(base, user, password, agent_id)
                    # El agente es GLOBAL (multi-fuente): el re-register borró el
                    # anterior, así que actualizar el id guardado por los otros slugs
                    # para que sus chips/teardown no apunten a un agente inexistente.
                    for other_ids in registry.values():
                        if other_ids is not ids and other_ids.get("agent_id"):
                            other_ids["agent_id"] = agent_id
                    result["conversational"] = {"ok": True, "agent_id": agent_id,
                                                "ppl_model_id": ppl_model, "llm_model_id": llm_model}
                else:
                    result["conversational"] = {"ok": False,
                                                "reason": "los modelos quedaron listos pero no se pudo registrar el agente"}
            else:
                result["conversational"] = {"ok": False,
                                            "reason": deploy_err or "no se pudieron provisionar los modelos (ver logs)"}
        except Exception as exc:  # noqa: BLE001
            result["conversational"] = {"ok": False, "reason": repr(exc)}

    # ── Anomaly detection — deshabilitado ───────────────────────────────────
    # AD y alerting eliminados por decisión de diseño. Solo conversational + forecast.

    # ── Forecasting ──────────────────────────────────────────────────────────
    forecast_specs = spec.get("forecasts")
    if not forecast_specs:
        result["forecast"] = {"ok": False, "reason": "no hay forecasts definidos para este vertical (placeholder)"}
    else:
        forecaster_ids = ids.get("forecaster_ids") or []
        if forecaster_ids and len(forecaster_ids) >= len(forecast_specs):
            result["forecast"] = {"ok": True, "forecaster_ids": forecaster_ids, "reason": "ya provisionado"}
        else:
            ready, reason = _index_ready_for_capabilities(base, user, password, ip, spec.get("volume_field", ""), need_docs=True)
            bounds = _index_time_bounds(base, user, password, ip) if ready else None
            if not ready:
                result["forecast"] = {"ok": False, "reason": reason}
            elif not bounds:
                # Sin rango de @timestamp utilizable (log sin fecha de evento real):
                # el forecaster quedaría en INIT vacío. Se omite con motivo claro.
                result["forecast"] = {"ok": False,
                                      "reason": "el índice no tiene un rango de @timestamp utilizable (sin fecha de evento real) — forecast omitido"}
            else:
                min_ms, max_ms, doc_count = bounds
                now_ms = datetime.now(timezone.utc).timestamp() * 1000
                interval_m, window_delay_m, hist = _forecaster_window(min_ms, max_ms, now_ms, doc_count)
                print(f"[capabilities] forecaster window para '{slug}': interval={interval_m}min "
                      f"window_delay={window_delay_m}min history={hist} (span={int((max_ms-min_ms)//86400000)}d, docs={doc_count})")
                forecaster_ids = []
                states: list[str] = []
                for fc_spec in forecast_specs:
                    # history: el derivado de los datos, respetando el tope del spec
                    # (cardinality usa 2000 por el circuit breaker).
                    fc_history = min(hist, int(fc_spec.get("history", 10000)))
                    fc_id = _ml_create(base, user, password, "/_plugins/_forecast/forecasters",
                                      caps.build_forecaster(ip, spec["volume_field"],
                                                            interval_minutes=interval_m,
                                                            horizon=spec.get("forecast_horizon", 8),
                                                            name=fc_spec["name"],
                                                            feature_name=fc_spec["feature_name"],
                                                            aggregation_query=fc_spec.get("aggregation_query"),
                                                            description=fc_spec.get("description", "Forecast autogenerado"),
                                                            history=fc_history,
                                                            window_delay_minutes=window_delay_m), "_id")
                    if fc_id:
                        _os_req("POST", f"{base}/_plugins/_forecast/forecasters/{fc_id}/_run_once", user, password, timeout=30)
                        _ok_fc, state = _forecast_test_state(base, user, password, fc_id)
                        states.append(f"{fc_spec['name']}={state}")
                        forecaster_ids.append(fc_id)
                ids["forecaster_ids"] = forecaster_ids
                ids["forecaster_id"] = forecaster_ids[0] if forecaster_ids else None
                n_ok = sum(1 for s in states if s.endswith("TEST_COMPLETE"))
                if not forecaster_ids:
                    result["forecast"] = {"ok": False, "reason": "no se pudo crear ningún forecaster"}
                else:
                    fc_result = {
                        "ok": n_ok > 0,
                        "forecaster_ids": forecaster_ids,
                        "states": states,
                        "window": {"interval_min": interval_m, "window_delay_min": window_delay_m, "history": hist},
                    }
                    if n_ok:
                        fc_result["note"] = f"backtest OK ({n_ok}/{len(states)})"
                    else:
                        fc_result["reason"] = (
                            "los forecasters quedaron en INIT / sin datos en la ventana "
                            f"(estados: {', '.join(states)})")
                    result["forecast"] = fc_result

    # ── Alerting — deshabilitado ─────────────────────────────────────────────
    # AD y alerting eliminados por decisión de diseño.

    # Persistir IDs acumulados.
    registry[slug] = ids
    _write_capabilities(terraform_dir, registry)
    return result


def _teardown_capabilities(cluster: dict[str, str], user: str, password: str,
                           https_enabled: bool) -> None:
    """Borra los artefactos de capabilities de TODOS los slugs (best-effort),
    antes del terraform destroy (el cluster aún vive). Orden inverso al create."""
    terraform_dir = _active_terraform_dir()
    registry = _read_capabilities(terraform_dir)
    if not registry:
        return
    base = _os_base(cluster, https_enabled)
    # Preflight rápido de alcanzabilidad: si el cluster NO responde (p. ej. ya lo
    # destruyeron a mano), no tiene sentido intentar borrar cada artefacto — cada
    # DELETE colgaría su timeout completo (~20s × N artefactos). El terraform destroy
    # que corre después se lleva el cluster y TODO lo suyo igual. Un solo GET con
    # timeout corto decide: sin respuesta → skip del teardown REST.
    health = _os_req("GET", f"{base}/", user, password, timeout=5)
    if health is None:
        print(f"[capabilities] cluster no alcanzable ({base}) — se saltea el teardown REST "
              f"(el terraform destroy limpia el cluster y sus artefactos)")
        return
    for slug, ids in registry.items():
        if ids.get("monitor_id"):
            _os_req("DELETE", f"{base}/_plugins/_alerting/monitors/{ids['monitor_id']}", user, password, timeout=20)
        if ids.get("forecaster_id"):
            _os_req("POST", f"{base}/_plugins/_forecast/forecasters/{ids['forecaster_id']}/_stop", user, password, timeout=20)
            _os_req("DELETE", f"{base}/_plugins/_forecast/forecasters/{ids['forecaster_id']}", user, password, timeout=20)
        if ids.get("detector_id"):
            _os_req("POST", f"{base}/_plugins/_anomaly_detection/detectors/{ids['detector_id']}/_stop", user, password, timeout=20)
            _os_req("DELETE", f"{base}/_plugins/_anomaly_detection/detectors/{ids['detector_id']}", user, password, timeout=20)
        if ids.get("agent_id"):
            _os_req("DELETE", f"{base}/_plugins/_ml/agents/{ids['agent_id']}", user, password, timeout=20)
        for mid in ids.get("model_ids", []) or []:
            if mid:
                _os_req("POST", f"{base}/_plugins/_ml/models/{mid}/_undeploy", user, password, timeout=20)
                _os_req("DELETE", f"{base}/_plugins/_ml/models/{mid}", user, password, timeout=20)
        if ids.get("model_group_id"):
            _os_req("DELETE", f"{base}/_plugins/_ml/model_groups/{ids['model_group_id']}", user, password, timeout=20)
        for cid in ids.get("connector_ids", []) or []:
            if cid:
                _os_req("DELETE", f"{base}/_plugins/_ml/connectors/{cid}", user, password, timeout=20)
        print(f"[capabilities] teardown de '{slug}' OK")


# ── Security Analytics (Sigma rules + detectors + correlation) ───────────────

_SA_BASE = "/_plugins/_security_analytics"

_SA_SIGMA_RULES = [
    {
        "name": "siem_ssh_bruteforce",
        "category": "network",
        "title": "SSH Brute Force Detection",
        "description": "Multiple failed SSH login attempts from same source IP",
        "level": "high",
        "query": """title: SSH Brute Force
id: f2c3a1d0-1e2b-4f5a-9c8d-7a6b5c4d3e2f
status: experimental
description: Multiple failed SSH login attempts
author: SIEM Platform
date: 2025/07/01
logsource:
  product: linux
  service: ssh
detection:
  selection:
    event.action: ssh_login
    event.outcome: failure
  condition: selection
level: high""",
    },
    {
        "name": "siem_sqli_waf",
        "category": "network",
        "title": "SQL Injection Attack (WAF)",
        "description": "WAF detected SQL injection attempt",
        "level": "high",
        "query": """title: SQL Injection via WAF
id: a1b2c3d4-e5f6-4a5b-8c7d-9e0f1a2b3c4d
status: experimental
description: WAF blocked SQL injection attempt
author: SIEM Platform
date: 2025/07/01
logsource:
  product: web
  service: waf
detection:
  selection:
    event.dataset: waf
    rule.name: sqli
  condition: selection
level: high""",
    },
    {
        "name": "siem_webshell",
        "category": "network",
        "title": "Webshell Upload/Access",
        "description": "WAF detected webshell activity",
        "level": "critical",
        "query": """title: Webshell Detection
id: b2c3d4e5-f6a7-4b6c-9d8e-0f1a2b3c4d5e
status: experimental
description: WAF detected webshell upload or access
author: SIEM Platform
date: 2025/07/01
logsource:
  product: web
  service: waf
detection:
  selection:
    event.dataset: waf
    rule.name: webshell
  condition: selection
level: critical""",
    },
    {
        "name": "siem_ips_exploit",
        "category": "network",
        "title": "IPS Exploit Detection",
        "description": "FortiGate IPS detected exploit attempt",
        "level": "critical",
        "query": """title: IPS Exploit
id: c3d4e5f6-a7b8-4c7d-9e0f-1a2b3c4d5e6f
status: experimental
description: FortiGate IPS triggered on exploit
author: SIEM Platform
date: 2025/07/01
logsource:
  product: firewall
  service: ips
detection:
  selection:
    event.dataset: fortigate
    event.category: intrusion_detection
    event.outcome: failure
  condition: selection
level: critical""",
    },
    {
        "name": "siem_cloud_key_create",
        "category": "network",
        "title": "Cloud Access Key Creation",
        "description": "New cloud access key created (potential persistence)",
        "level": "medium",
        "query": """title: Cloud Access Key Creation
id: d4e5f6a7-b8c9-4d8e-9f0a-2b3c4d5e6f7a
status: experimental
description: IAM access key creation event
author: SIEM Platform
date: 2025/07/01
logsource:
  product: cloud
  service: iam
detection:
  selection:
    event.dataset: cloudaudit
    event.action: createAccessKey
  condition: selection
level: medium""",
    },
    {
        "name": "siem_cloud_tracker_delete",
        "category": "network",
        "title": "Cloud Audit Tracker Deleted",
        "description": "CTS tracker deleted (defense evasion)",
        "level": "high",
        "query": """title: CTS Tracker Deletion
id: e5f6a7b8-c9d0-4e9f-8a1b-3c4d5e6f7a8b
status: experimental
description: Cloud trace service tracker deleted
author: SIEM Platform
date: 2025/07/01
logsource:
  product: cloud
  service: audit
detection:
  selection:
    event.dataset: cloudaudit
    event.action: deleteTracker
  condition: selection
level: high""",
    },
    {
        "name": "siem_sudo_shadow",
        "category": "network",
        "title": "Shadow File Access via Sudo",
        "description": "User accessed /etc/shadow via sudo (credential dumping)",
        "level": "critical",
        "query": """title: Shadow File Access
id: f6a7b8c9-d0e1-4f0a-9b2c-4d5e6f7a8b9c
status: experimental
description: Credential dumping via sudo
author: SIEM Platform
date: 2025/07/01
logsource:
  product: linux
  service: sudo
detection:
  selection:
    event.action: sudo
    event.technique: T1003
  condition: selection
level: critical""",
    },
    {
        "name": "siem_threat_intel_hit",
        "category": "network",
        "title": "Threat Intel Match",
        "description": "Source IP matched known malicious IP feed",
        "level": "high",
        "query": """title: Threat Intel Match
id: a7b8c9d0-e1f2-4a1b-8c3d-5e6f7a8b9c0d
status: experimental
description: Source IP in threat intel feed
author: SIEM Platform
date: 2025/07/01
logsource:
  product: network
  service: firewall
detection:
  selection:
    threat.matched: true
  condition: selection
level: high""",
    },
]

_SA_CORRELATIONS = [
    {
        "name": "siem_corr_cmp001_web_app_compromise",
        "campaign": "CMP-001",
        "description": "Web App Compromise: scan → sqli → webshell → key creation",
    },
    {
        "name": "siem_corr_cmp002_credential_theft",
        "campaign": "CMP-002",
        "description": "Credential Theft: cloud recon → SSH brute → shadow → tracker delete",
    },
    {
        "name": "siem_corr_cmp003_lateral_movement",
        "campaign": "CMP-003",
        "description": "Lateral Movement: scan → SSH brute → sudo → server creation",
    },
]


def _provision_security_analytics(
    cluster: dict[str, str], user: str, password: str, https_enabled: bool,
) -> dict:
    """Provisiona Security Analytics (Sigma rules + detector + correlations)
    para el índice siem-*. Best-effort: si el plugin no está disponible, retorna
    ``{"available": False}`` sin error."""
    import requests as _requests

    base = _os_base(cluster, https_enabled)
    auth = (user, password)

    # Preflight: ¿está el plugin? (la API de rules requiere POST _search, no GET)
    r = _os_req("POST", f"{base}{_SA_BASE}/rules/_search",
                user, password, json_body={"size": 1, "query": {"match_all": {}}}, timeout=15)
    if r is None or r.status_code not in (200, 404):
        print(f"[security-analytics] plugin no disponible "
              f"(status {getattr(r, 'status_code', None)}): "
              f"{(getattr(r, 'text', '') or '')[:160]}")
        return {"available": False}

    print("[security-analytics] plugin detectado, provisionando...")
    result: dict = {"available": True, "rules": [], "detector": None, "correlations": []}

    # 1) Buscar custom rules existentes (idempotencia por título)
    existing_rules: dict[str, str] = {}
    sr = _os_req("POST", f"{base}{_SA_BASE}/rules/_search?pre_packaged=false",
                 user, password, json_body={"size": 100, "query": {"match_all": {}}}, timeout=15)
    if sr and sr.status_code == 200:
        try:
            for hit in (sr.json().get("hits", {}).get("hits", []) or []):
                src = hit.get("_source", {})
                title = src.get("title", "")
                rid = hit.get("_id", "")
                if title:
                    existing_rules[title] = rid
        except (ValueError, KeyError):
            pass

    # Crear Sigma rules — la API recibe YAML crudo + category como query param
    rule_ids: list[str] = []
    for rule in _SA_SIGMA_RULES:
        if rule["title"] in existing_rules:
            rid = existing_rules[rule["title"]]
            rule_ids.append(rid)
            print(f"[security-analytics] rule '{rule['name']}' ya existe (id={rid}), salteando")
            result["rules"].append({"name": rule["name"], "id": rid, "created": False})
            continue

        url = f"{base}{_SA_BASE}/rules?category={rule['category']}"
        try:
            rr = _requests.post(url, auth=auth, data=rule["query"],
                                headers={"Content-Type": "application/json"},
                                timeout=30, verify=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[security-analytics] rule '{rule['name']}' error: {exc!r}")
            rr = None
        if rr and rr.status_code in (200, 201):
            try:
                rid = rr.json().get("_id", "")
            except (ValueError, KeyError):
                rid = ""
            rule_ids.append(rid)
            result["rules"].append({"name": rule["name"], "id": rid, "created": True})
            print(f"[security-analytics] rule '{rule['name']}' creada (id={rid})")
        else:
            print(f"[security-analytics] rule '{rule['name']}' falló: "
                  f"status {getattr(rr, 'status_code', None)}: "
                  f"{(getattr(rr, 'text', '') or '')[:200]}")

    if not rule_ids:
        print("[security-analytics] no se crearon rules, saltando detector")
        return result

    # 2) Crear detector (idempotente: busca por nombre via _search)
    detector_name = "siem-unified-detector"
    existing_detector_id = ""
    dr_search = _os_req("POST", f"{base}{_SA_BASE}/detectors/_search",
                        user, password, json_body={"size": 50, "query": {"match_all": {}}}, timeout=15)
    if dr_search and dr_search.status_code == 200:
        try:
            for hit in (dr_search.json().get("hits", {}).get("hits", []) or []):
                src = hit.get("_source", hit.get("detector", {}))
                if src.get("name") == detector_name:
                    existing_detector_id = hit.get("_id", "")
                    break
        except (ValueError, KeyError):
            pass

    if existing_detector_id:
        result["detector"] = {"name": detector_name, "id": existing_detector_id, "created": False}
        print(f"[security-analytics] detector '{detector_name}' ya existe (id={existing_detector_id})")
    else:
        detector_body = {
            "enabled": True,
            "name": detector_name,
            "detector_type": "network",
            "schedule": {"period": {"interval": 5, "unit": "MINUTES"}},
            "inputs": [{
                "detector_input": {
                    "description": "SIEM unified (FortiGate + Auth + CTS + WAF)",
                    "custom_rules": [{"id": rid} for rid in rule_ids],
                    "indices": ["siem-all"],
                }
            }],
            "triggers": [
                {"id": "siem-trig-critical", "name": "Critical alerts", "severity": "1",
                 "ids": rule_ids, "sev_levels": ["critical"], "tags": [], "actions": []},
                {"id": "siem-trig-high", "name": "High alerts", "severity": "2",
                 "ids": rule_ids, "sev_levels": ["high"], "tags": [], "actions": []},
            ],
        }
        dr = _os_req("POST", f"{base}{_SA_BASE}/detectors", user, password,
                     json_body=detector_body, timeout=20)
        if dr and dr.status_code in (200, 201):
            try:
                did = dr.json().get("_id", "")
            except (ValueError, KeyError):
                did = ""
            result["detector"] = {"name": detector_name, "id": did, "created": True}
            print(f"[security-analytics] detector '{detector_name}' creado (id={did})")
        else:
            print(f"[security-analytics] detector falló: "
                  f"status {getattr(dr, 'status_code', None)}: "
                  f"{(getattr(dr, 'text', '') or '')[:200]}")

    # 3) Crear correlation rules para campañas multi-stage
    # Correlation API: cada regla necesita ≥2 queries (index + log type + field + value)
    for corr in _SA_CORRELATIONS:
        corr_name = corr["name"]
        # Idempotente: no hay _search para correlations, se crean directo
        # (si ya existe, la API devuelve el mismo _id)
        already_exists = False
        if already_exists:
            print(f"[security-analytics] correlation '{corr_name}' ya existe")
            continue

        # Correlation API: POST /correlation/rules con body {"correlate": [...]}
        # Cada item: index + query (Lucene) + category (log type)
        # Idempotente: buscar si ya existe (GET /correlations lista por time window)
        corr_body = {
            "correlate": [
                {"index": "siem-all", "query": f"event.campaign:{corr['campaign']}", "category": "network"},
                {"index": "siem-all", "query": f"event.campaign:{corr['campaign']}", "category": "network"},
            ]
        }
        rcr = _os_req("POST", f"{base}{_SA_BASE}/correlation/rules", user, password,
                      json_body=corr_body, timeout=30)
        if rcr and rcr.status_code in (200, 201):
            result["correlations"].append({"name": corr_name, "created": True})
            print(f"[security-analytics] correlation '{corr_name}' creada")
        else:
            print(f"[security-analytics] correlation '{corr_name}' falló: "
                  f"status {getattr(rcr, 'status_code', None)}")

    n_rules = sum(1 for r in result["rules"] if r.get("created"))
    n_corr = sum(1 for c in result["correlations"] if c.get("created"))
    print(f"[security-analytics] OK: {n_rules} rules nuevas, "
          f"detector={'nuevo' if (result.get('detector') or {}).get('created') else 'existente'}, "
          f"{n_corr} correlations nuevas")
    return result


@app.post(
    "/api/v1/onboarding/apply-schema",
    response_model=ApplySchemaResponse,
    tags=["onboarding"],
    summary="Paso 2: aplica el index template e importa los dashboards al cluster existente",
)
def apply_schema(request: TerraformDeployRequest) -> ApplySchemaResponse:
    """Aplica el/los index template(s) + importa los dashboards al cluster YA
    provisionado, SIN correr terraform. Es el paso 2 del wizard (lo dispara el
    operador entre provisionar y arrancar la ingesta), y es **idempotente**: se
    puede reintentar sin re-provisionar.

    Lee el cluster + la EIP del NAT desde el tfstate. Best-effort: si el index
    template falla, `index_template_applied=False` y el operador puede reintentar.
    """
    terraform_dir = _active_terraform_dir()
    cluster = _cluster_with_public_access(terraform_dir)
    if not cluster.get("public_endpoint") and not cluster.get("endpoint"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "stage": "apply_schema",
                "message": "No hay un cluster alcanzable. ¿Provisionaste el entorno (paso 1)?",
            },
        )

    index_template_applied = False
    try:
        index_template_applied = _apply_index_templates(request, cluster)
    except Exception as exc:  # noqa: BLE001
        print(f"[apply-schema] index template falló (best-effort): {exc!r}")

    # (slug, fields, index_name) por caso. Predefinidos usan su spec; custom (sin
    # spec) auto-genera el dashboard de sus campos detectados.
    if request.cases:
        targets = [(c.slug, c.fields, c.index_name) for c in request.cases]
    else:
        slug = request.pipeline_slug or _slug_from_index(request.opensearch_index)
        targets = [(slug, request.fields, request.opensearch_index)]

    os_base = _os_base(cluster, request.https_enabled)
    os_user = request.opensearch_user or "admin"

    dashboards_imported = False
    gmin = gmax = None
    for s, flds, idx in targets:
        # Rango real de @timestamp del índice: adapta el timeRange del dashboard
        # (fraud 2017 abre en 2017) y alimenta la unión del time picker global.
        bounds = None
        try:
            bounds = _index_time_bounds(os_base, os_user, request.opensearch_password,
                                        index_pattern_from_name(idx) if idx else f"{s}-*")
        except Exception as exc:  # noqa: BLE001
            print(f"[apply-schema] bounds de '{s}' no disponibles (best-effort): {exc!r}")
        if bounds:
            gmin = bounds[0] if gmin is None else min(gmin, bounds[0])
            gmax = bounds[1] if gmax is None else max(gmax, bounds[1])
        try:
            if _import_dashboards(
                slug=s, cluster=cluster, password=request.opensearch_password,
                https_enabled=request.https_enabled, terraform_dir=terraform_dir,
                fields=flds, index_name=idx, time_bounds=bounds,
            ):
                dashboards_imported = True
                registry = _read_pipelines_registry(terraform_dir)
                if s in registry:
                    registry[s]["dashboards_imported"] = True
                    _write_pipelines_registry(terraform_dir, registry)
        except Exception as exc:  # noqa: BLE001
            print(f"[apply-schema] import de dashboards '{s}' falló (best-effort): {exc!r}")

    # Time picker global de Dashboards = UNIÓN de los rangos reales (min de mins, max
    # de maxes), para que Discover y dashboards nuevos arranquen sobre la serie aunque
    # los verticales tengan fechas distintas (fraud 2017 + otros 2025-2026). Fallback
    # a la ventana histórica.
    if dashboards_imported:
        try:
            if gmin is not None and gmax is not None:
                t_from, t_to = _epoch_ms_to_iso(gmin), _epoch_ms_to_iso(gmax)
            else:
                t_from, t_to = "2025-07-01T00:00:00.000Z", "2026-07-02T00:00:00.000Z"
            # Buscar el ID del config doc de Dashboards (config:<version>).
            cfg = _os_req("GET", f"{os_base}/.kibana/_search?q=type:config+AND+config.buildNum:*&size=1",
                          os_user, request.opensearch_password, timeout=15)
            cfg_id = ""
            if cfg is not None and cfg.status_code == 200:
                hits = ((cfg.json() or {}).get("hits", {}) or {}).get("hits", [])
                cfg_id = hits[0].get("_id", "") if hits else ""
            if cfg_id:
                _os_req("POST", f"{os_base}/.kibana/_update/{cfg_id}", os_user,
                        request.opensearch_password,
                        json_body={"doc": {"config": {"timepicker:timeDefaults":
                            f'{{"from":"{t_from}","to":"{t_to}","mode":"absolute"}}'}}},
                        timeout=15)
                print(f"[apply-schema] timepicker global → {t_from} .. {t_to}")
        except Exception as exc:  # noqa: BLE001
            print(f"[apply-schema] timepicker global no se pudo setear (best-effort): {exc!r}")

    # Capabilities (ml-commons / anomalías / forecast / alerting) se provisionan
    # en un paso separado (POST /api/v1/onboarding/provision-capabilities) DESPUÉS
    # de que Logstash ingiere datos: AD/forecast necesitan documentos en el índice.

    # Security Analytics (Sigma rules + detector + correlations) para SIEM.
    # Best-effort: si el plugin no está, se saltea sin error.
    sa_result: dict = {}
    target_slugs = {s for s, _, _ in targets}
    if "siem" in target_slugs:
        try:
            sa_result = _provision_security_analytics(
                cluster, request.opensearch_user or "admin",
                request.opensearch_password, request.https_enabled,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[apply-schema] security-analytics falló (best-effort): {exc!r}")

    msg = "Index template aplicado" if index_template_applied else "El index template no se pudo aplicar"
    if dashboards_imported:
        msg += " · dashboards importados"
    if sa_result.get("available"):
        n_rules = len(sa_result.get("rules", []))
        msg += f" · security analytics ({n_rules} rules)"
    return ApplySchemaResponse(
        status="success" if index_template_applied else "partial",
        index_template_applied=index_template_applied,
        dashboards_imported=dashboards_imported,
        capabilities={},
        message=msg,
    )


class ProvisionCapabilitiesRequest(BaseModel):
    """Request del paso 3: provisionar capabilities (ml-commons / AD / forecast /
    alerting) sobre el cluster YA con datos ingeridos."""
    opensearch_password: str = Field(default="")
    opensearch_user: str = Field(default="admin")
    https_enabled: bool = Field(default=True, description="Habilitar HTTPS para OpenSearch")
    slugs: list[str] = Field(default_factory=list, description="Slugs a provisionar (vacío = todos los que tienen bundle).")
    project_name: str = Field(default="log-analytics")
    force: bool = Field(default=False, description="Si True, tear down de artifacts existentes y recrea (para re-provisionar con modelo nuevo).")


class ProvisionCapabilitiesResponse(BaseModel):
    """Response del paso 3."""
    status: str
    capabilities: dict = Field(default_factory=dict, description="Capabilities provisionadas por slug.")
    message: str = ""


@app.post(
    "/api/v1/onboarding/provision-capabilities",
    response_model=ProvisionCapabilitiesResponse,
    tags=["onboarding"],
    summary="Paso 3: provisiona capabilities (ml-commons / anomalías / forecast / alerting) tras la ingesta",
)
def provision_capabilities(request: ProvisionCapabilitiesRequest) -> ProvisionCapabilitiesResponse:
    """Provisiona el bundle de capabilities de los `slugs` indicados sobre el
    cluster YA provisionado y con datos ingeridos. Es el paso 3 del wizard (lo
    dispara el operador después de que Logstash corre), y es **idempotente**:
    detecta los IDs ya provisionados y los saltea.

    AD/forecast tienen un preflight de readiness del índice (campo presente +
    documentos); si el índice aún no tiene datos, se saltean con un `reason`
    claro en vez de fallar.
    """
    import capabilities as _caps

    audit.record("provision_capabilities", f"slugs={','.join(request.slugs or [])}")
    terraform_dir = _active_terraform_dir()
    cluster = _cluster_with_public_access(terraform_dir)
    if not cluster.get("public_endpoint") and not cluster.get("endpoint"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "stage": "provision_capabilities",
                "message": "No hay un cluster alcanzable. ¿Provisionaste el entorno (paso 1)?",
            },
        )

    # Si el operador no envió password (ej. recargó la página y perdió el state
    # in-memory), leerla de las creds persistidas por el deploy.
    password = request.opensearch_password or _cluster_admin_password(terraform_dir)
    user = request.opensearch_user or "admin"

    slugs = request.slugs or _caps.get_capability_slugs()
    # Incluir también slugs productivos del registry (fields persistidos, sin spec
    # curado) — si el frontend los manda, se provisionan.
    pipe_reg = _read_pipelines_registry(terraform_dir)
    caps_result: dict = {}
    any_ok = False
    if request.force:
        _base = _os_base(cluster, request.https_enabled)
        if _base.rsplit("//", 1)[-1]:
            _teardown_orphans_by_name(_base, user, password)
    for slug in slugs:
        has_spec = _caps.get_capability_spec(slug) is not None
        has_fields = bool((pipe_reg.get(slug, {}) or {}).get("fields"))
        if not has_spec and not has_fields:
            continue
        try:
            caps_result[slug] = _provision_capabilities(
                cluster, slug, user, password, request.https_enabled,
                force=request.force,
            )
            if caps_result[slug]:
                any_ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"[provision-capabilities] '{slug}' falló (best-effort): {exc!r}")
            caps_result[slug] = {"error": repr(exc)}

    msg = "Capabilities provisionadas" if any_ok else "No se provisionó ninguna capability"
    return ProvisionCapabilitiesResponse(
        status="success" if any_ok else "partial",
        capabilities=caps_result,
        message=msg,
    )


class PplChatRequest(BaseModel):
    """Request del chatbot PPL: pregunta en lenguaje natural sobre un tipo (slug)."""
    question: str = Field(..., min_length=1)
    slug: str = Field(default="transacciones-billetera")
    opensearch_password: str = Field(default="")
    opensearch_user: str = Field(default="admin")
    # None → se deriva del state (robusto tras un reload que perdió el flag en el front).
    https_enabled: bool | None = Field(default=None)


class PplChatResponse(BaseModel):
    answer: str
    ppl: str = ""
    result: dict = Field(default_factory=dict)


def _index_time_window(base: str, user: str, password: str, index_pattern: str):
    """(min, max) de @timestamp del índice como strings, o None. Best-effort: se usa
    para que el generador de PPL sepa de qué AÑO consultar cuando el usuario menciona
    un mes/período sin año (evita rangos vacíos fuera de la ventana del dataset)."""
    try:
        r = _os_req("POST", f"{base}/_plugins/_ppl", user, password,
                    json_body={"query": f"source={index_pattern} | stats min(@timestamp) as mn, max(@timestamp) as mx"},
                    timeout=15)
        if r is None or r.status_code != 200:
            return None
        rows = (r.json() or {}).get("datarows") or []
        if not rows or len(rows[0]) < 2 or rows[0][0] is None or rows[0][1] is None:
            return None

        def _fmt(v):
            s = str(v)
            if s.isdigit() and len(s) >= 12:   # epoch millis → fecha legible (UTC)
                try:
                    import datetime as _dt
                    return _dt.datetime.utcfromtimestamp(int(s) / 1000).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    return s
            return s

        return (_fmt(rows[0][0]), _fmt(rows[0][1]))
    except Exception:
        return None


@app.post("/api/v1/capabilities/ppl-chat", response_model=PplChatResponse, tags=["capabilities"])
def ppl_chat(request: PplChatRequest) -> PplChatResponse:
    """Chatbot PPL orquestado en el BACKEND (no un agente ml-commons: las tools nativas
    rompen con estos docs). 3 pasos, todos ya probados:
      1. modelo `platform-ppl` (`_predict`) traduce la pregunta a PPL,
      2. `POST /_plugins/_ppl` ejecuta el PPL → resultado REAL,
      3. modelo `platform-llm` frasea el resultado en lenguaje natural.
    Nunca inventa: si el PPL no ejecuta, devuelve el error + el PPL generado.
    """
    import json as _json

    terraform_dir = _active_terraform_dir()
    cluster = _cluster_with_public_access(terraform_dir)
    if not cluster.get("public_endpoint") and not cluster.get("endpoint"):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"stage": "ppl_chat", "message": "No hay un cluster alcanzable."})
    password = request.opensearch_password or _cluster_admin_password(terraform_dir)
    user = request.opensearch_user or "admin"
    https = (request.https_enabled if request.https_enabled is not None
             else _read_https_enabled_from_state(terraform_dir))
    base = _os_base(cluster, https)

    ids = _read_capabilities(terraform_dir).get(request.slug, {})
    ppl_model = ids.get("ppl_model_id")
    llm_model = ids.get("llm_model_id")
    if not ppl_model or not llm_model:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"stage": "ppl_chat",
                                    "message": "El chatbot no está provisionado para este tipo. Corré 'Provisionar capabilities'."})

    # 1) NL → PPL. Pasamos el system_prompt del SLUG elegido (índice + campos + reglas)
    # para que el modelo apunte al vertical correcto. Sin esto, el _predict directo usa
    # el default del connector (que quedó con OTRO vertical) y responde sobre el índice
    # equivocado. El agente/DevTools no tiene el problema: cada PPLTool pasa su prompt.
    import capabilities as caps
    predict_params: dict = {"prompt": request.question}
    _spec = getattr(caps, "_CAPABILITY_SPECS", {}).get(request.slug)
    if _spec:
        index_pattern = _spec.get("index_pattern", f"{request.slug}*")
        _sp = caps.build_ppl_system_prompt(
            index_pattern,
            _spec.get("operations", []),
            _spec.get("fields", {}),
            _spec.get("success_code", ""),
            _spec.get("label", request.slug),
        )
        # Ventana temporal real del índice → el modelo elige el AÑO correcto cuando el
        # usuario menciona un mes/período sin año (evita rangos vacíos).
        tw = _index_time_window(base, user, password, index_pattern)
        if tw:
            _sp += (
                f"\n\nDATA TIME WINDOW: @timestamp ranges from {tw[0]} to {tw[1]}. "
                "If the user names a month or period without a year, choose the year that falls within this window."
            )
        predict_params["system_prompt"] = _sp.replace("\n", "\\n")
    ppl = _ml_predict(base, user, password, ppl_model, predict_params)
    if ppl:
        ppl = ppl.replace("```ppl", "").replace("```", "").strip()
    if not ppl or not ppl.lower().startswith("source="):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail={"stage": "ppl_chat",
                                    "message": f"No pude generar una consulta PPL válida a partir de la pregunta.",
                                    "ppl": ppl or ""})

    # 2) Ejecutar el PPL. Si falla, devolver el error + el PPL (honesto, no inventar).
    r = _os_req("POST", f"{base}/_plugins/_ppl", user, password, json_body={"query": ppl}, timeout=30)
    if r is None or r.status_code != 200:
        detail = (getattr(r, "text", "") or "")[:300]
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail={"stage": "ppl_chat", "message": f"El PPL no se pudo ejecutar: {detail}", "ppl": ppl})
    body = r.json()
    result = {"schema": body.get("schema", []), "datarows": body.get("datarows", [])}

    # 3) Frasear el resultado. Fallback: devolver los datarows crudos si el LLM falla.
    phrase_prompt = (
        f"Pregunta del usuario: {request.question}\n"
        f"Resultado de la consulta (JSON): {_json.dumps(result, ensure_ascii=False)}\n\n"
        "Respondé la pregunta en el MISMO idioma que el usuario, en UNA frase clara, con separador de "
        "miles en los números. No muestres JSON ni la query, solo la respuesta."
    )
    answer = _ml_predict(base, user, password, llm_model, {"prompt": phrase_prompt})
    if not answer:
        answer = f"Resultado: {result['datarows']}"
    return PplChatResponse(answer=answer, ppl=ppl, result=result)


class DatasetPreviewResponse(BaseModel):
    slug: str
    lines: list[str] = Field(default_factory=list)


def _dataset_head(path: "Path", n: int) -> "list[str]":
    """Primeras N líneas de datos (no comentadas), truncadas a 1200 chars."""
    out: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            s = raw.rstrip("\n")
            if not s.strip() or s.lstrip().startswith("#"):
                continue
            out.append(s[:1200])
            if len(out) >= n:
                break
    return out


@app.get("/api/v1/datasets/{slug}/preview", response_model=DatasetPreviewResponse, tags=["datasets"])
def dataset_preview(slug: str, lines: int = 6) -> DatasetPreviewResponse:
    """Primeras N líneas de datos (no comentadas) del dataset bundleado del `slug` —
    para el panel 'qué va a hacer Logstash' durante el deploy. Resuelve la variante
    guión/guión_bajo del nombre (fintech es `fintech_transactions.log`). Si el tipo
    tiene VARIOS archivos (ej. el SIEM: `siem-fortigate.log`, `siem-cloudaudit.log`,
    …), hace glob de `<slug>-*.log` e INTERCALA líneas de cada uno, para que el preview
    refleje que ingiere varias fuentes. 404 si no hay ningún dataset para el tipo."""
    n = max(1, min(int(lines or 6), 20))
    # Resolver dataset_files desde el registro de verticales (source of truth).
    v = verticals.get_vertical(slug)
    ds_files = v.get("dataset_files", []) if v else []
    if ds_files and len(ds_files) == 1:
        single = _DATASETS_DIR / ds_files[0]
        if single.is_file():
            return DatasetPreviewResponse(slug=slug, lines=_dataset_head(single, n))
    if ds_files and len(ds_files) > 1:
        files = [(_DATASETS_DIR / f) for f in ds_files if (_DATASETS_DIR / f).is_file()]
        per_file = [_dataset_head(f, 3) for f in files]
        out: list[str] = []
        i = 0
        while len(out) < n and any(i < len(pf) for pf in per_file):
            for pf in per_file:
                if i < len(pf) and len(out) < n:
                    out.append(pf[i])
            i += 1
        return DatasetPreviewResponse(slug=slug, lines=out)
    # Fallback: resolver por slug (compat con verticales sin dataset_files).
    single = next(
        ((_DATASETS_DIR / f"{c}.log") for c in (slug, slug.replace("-", "_"))
         if (_DATASETS_DIR / f"{c}.log").is_file()),
        None,
    )
    if single is not None:
        return DatasetPreviewResponse(slug=slug, lines=_dataset_head(single, n))
    # Multi-archivo bajo el mismo tipo (ej. SIEM): glob e intercalar.
    files = sorted(_DATASETS_DIR.glob(f"{slug}-*.log")) or \
        sorted(_DATASETS_DIR.glob(f"{slug.replace('-', '_')}-*.log"))
    if not files:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"message": "sin dataset bundleado para este tipo"})
    per_file = [_dataset_head(f, 3) for f in files]   # hasta 3 por fuente
    out: list[str] = []
    i = 0
    while len(out) < n and any(i < len(pf) for pf in per_file):
        for pf in per_file:
            if i < len(pf) and len(out) < n:
                out.append(pf[i])
        i += 1
    return DatasetPreviewResponse(slug=slug, lines=out)


@app.get(
    "/api/v1/terraform/status",
    response_model=TerraformStatusResponse,
    tags=["terraform"],
    summary="Indica si hay un entorno CSS activo (último terraform apply)",
)
def terraform_status() -> TerraformStatusResponse:
    """Indica si hay un entorno levantado POR LA PLATAFORMA.

    Dos condiciones, AMBAS necesarias:
      1. El marcador `.platform_deploy.json` existe (lo escribe el wizard al
         deployar). Sin marcador, NO reclamamos el entorno como propio —
         evita mostrar como "activo" un tfstate manual, leftover, o stale
         (cluster borrado a mano pero state sin refrescar).
      2. El tfstate existe y pesa ≥ 200 bytes (hay recursos reales).

    El frontend lo llama al cargar para renderizar "Mi Infraestructura".
    Si dice active=true, /destroy opera sobre un entorno real.
    """
    terraform_dir = _active_terraform_dir()
    state_file = terraform_dir / "terraform.tfstate"

    marker = _read_platform_marker(terraform_dir)
    has_state = state_file.exists() and state_file.stat().st_size >= 200
    if marker is None or not has_state:
        # Sin marcador (entorno no desplegado desde la app) o sin state real
        # → empty state. El operador solo ve lo que levantó con el wizard.
        return TerraformStatusResponse(active=False)

    # Timestamp del deploy: preferimos el del marcador (momento exacto del
    # deploy por la plataforma); si no es parseable, caemos al mtime del state.
    deployed_at_str = marker.get("deployed_at")
    try:
        deployed_at = datetime.fromisoformat(deployed_at_str) if deployed_at_str else None
    except (TypeError, ValueError):
        deployed_at = None
    if deployed_at is None:
        mtime = state_file.stat().st_mtime
        deployed_at = datetime.fromtimestamp(mtime, tz=timezone.utc)
    seconds_ago = max(0, int(time.time() - deployed_at.timestamp()))

    # `terraform output -json` para reconstruir los botones del banner
    # (Dashboards/Starter Kit) sin depender del state in-memory del browser.
    # Si falla (terraform CLI ausente, state corrupto, etc.) degradamos:
    # active=True con outputs None → banner muestra solo Destruir.
    outputs: dict[str, Any] = {}
    try:
        proc = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=terraform_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            outputs = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        print(f"[terraform_status] output -json falló ({exc!r}) — degradando a active=True sin outputs")

    def _pick(key: str) -> str | None:
        raw = outputs.get(key, {})
        if isinstance(raw, dict):
            val = raw.get("value")
            if isinstance(val, str) and val:
                return val
        return None

    # La URL de Dashboards se construye en Python desde el state + el
    # project_id del .env — NO se usa el output `dashboards_url` de
    # Terraform, que queda fijado a las vars del último apply (si el
    # project_id no estaba entonces, el output sale mal hasta redeploy).
    cluster = _cluster_with_public_access(terraform_dir)
    dashboards_url = _build_dashboards_url(cluster) or None

    # project_name: preferimos el del marcador (lo que el operador eligió al
    # deployar); luego el output de TF; y por último lo derivamos del nombre
    # del cluster ("${project_name}-opensearch").
    project_name = marker.get("project_name") or _pick("project_name")
    if not project_name:
        cluster_name = cluster.get("name", "")
        if cluster_name.endswith("-opensearch"):
            project_name = cluster_name[: -len("-opensearch")] or None
        elif cluster_name:
            project_name = cluster_name

    logstash = _read_css_resource_from_state(
        terraform_dir, "huaweicloud_css_logstash_cluster"
    )

    # Index template persistido en el deploy (lo muestra "Mi Infraestructura").
    artifact = _read_index_template_artifact(terraform_dir)

    # Pipelines corriendo en paralelo (registro persistido en el deploy).
    registry = _read_pipelines_registry(terraform_dir)
    import capabilities as _caps
    _curated_slugs = set(_caps.get_capability_slugs())
    pipelines = [
        {
            "slug": slug,
            "index": entry.get("index", ""),
            "obs_prefix": entry.get("obs_prefix", ""),
            "active": bool(entry.get("start_ingestion", False)),
            "dashboards_imported": bool(entry.get("dashboards_imported", False)),
            # has_capabilities: el slug tiene spec curado (demo) O fields persistidos
            # (productivo) → el backend es la única fuente de verdad para el gate del
            # frontend (no más CAPABILITY_SLUGS hardcodeado).
            "has_capabilities": slug in _curated_slugs or bool(entry.get("fields")),
        }
        for slug, entry in registry.items()
    ]
    # `pipeline_conf` (para el Starter Kit) = la última pipeline registrada.
    last_conf = None
    if registry:
        last_conf = list(registry.values())[-1].get("pipeline_conf") or None

    # dashboards_imported global = True si al menos una pipeline tiene dashboards.
    any_dashboards_imported = any(p.get("dashboards_imported", False) for p in pipelines)

    return TerraformStatusResponse(
        active=True,
        deployed_at=deployed_at.isoformat(),
        deployed_seconds_ago=seconds_ago,
        dashboards_url=dashboards_url,
        pipeline_conf=last_conf,
        project_name=project_name,
        opensearch_endpoint=cluster.get("endpoint") or None,
        opensearch_public_endpoint=cluster.get("public_endpoint") or None,
        logstash_endpoint=logstash.get("endpoint") or None,
        index_template_snippet=(artifact or {}).get("put_snippet"),
        index_template_name=(artifact or {}).get("template_name"),
        pipelines=pipelines,
        dashboards_imported=any_dashboards_imported,
        capabilities=_read_capabilities(terraform_dir),
        https_enabled=_read_https_enabled_from_state(terraform_dir),
    )


@app.post(
    "/api/v1/terraform/destroy",
    response_model=TerraformDestroyResponse,
    tags=["terraform"],
    summary="Destruye los recursos del último deploy (FinOps: control de costos de demo)",
)
def terraform_destroy(request: TerraformDestroyRequest = Body(default_factory=TerraformDestroyRequest)) -> TerraformDestroyResponse:
    """Destroy con Terraform, protegido por el lock de deploy por-usuario."""
    with _deploy_guard():
        audit.record("destroy", "terraform destroy")
        return _terraform_destroy_impl(request)


def _terraform_destroy_impl(request: TerraformDestroyRequest) -> TerraformDestroyResponse:
    """Corre `terraform destroy -auto-approve -input=false` en terraform/.

    El provider necesita credenciales (AK/SK/password). Orden de resolución:
      1. Si el request trae creds → se escriben a `destroy.auto.tfvars.json`.
      2. Si no, se usa el `destroy.auto.tfvars.json` que persistió el deploy.
    `-input=false` evita que terraform se cuelgue pidiendo variables por stdin
    (causa del timeout de 600s): si faltan creds, falla rápido y claro.

    Si NO hay state significativo, devuelve status="noop" sin invocar terraform.
    """
    terraform_dir = _active_terraform_dir()
    if not terraform_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Directorio terraform/ no encontrado",
        )

    # Guard noop: si el tfstate está vacío o tiene <200 bytes (struct base
    # de Terraform sin recursos), no hay nada que destruir.
    state_file = terraform_dir / "terraform.tfstate"
    if not state_file.exists() or state_file.stat().st_size < 200:
        print("[terraform_destroy] state vacío → noop, no se invoca terraform")
        return TerraformDestroyResponse(
            status="noop",
            message="No hay entorno activo para destruir.",
        )

    # Si el frontend reenvió creds (entorno legacy sin creds persistidas),
    # las escribimos. Reusa el writer del deploy mapeando obs→hwc.
    if request.obs_access_key or request.obs_secret_key or request.opensearch_password:
        _write_destroy_creds(terraform_dir, request)

    creds_file = terraform_dir / _DESTROY_CREDS_NAME
    if not creds_file.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "stage": "terraform_destroy",
                "message": (
                    "No hay credenciales para el teardown. El entorno se creó "
                    "antes de que se persistieran. Reingresá AK/SK + password "
                    "en el paso 3-4 del wizard y destruí desde ahí, o corré "
                    "`terraform destroy` a mano con las creds."
                ),
            },
        )

    # Antes de destruir el cluster: borrar los artefactos de capabilities por REST
    # (el cluster aún vive). Best-effort — si falla, el destroy del cluster los
    # elimina igual. Password del request (o vacío → los DELETE fallan sin romper).
    try:
        cluster = _cluster_with_public_access(terraform_dir)
        if cluster.get("public_endpoint") or cluster.get("endpoint"):
            pw = request.opensearch_password or _cluster_admin_password(terraform_dir)
            _teardown_capabilities(cluster, "admin", pw, https_enabled=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[terraform_destroy] teardown de capabilities falló (best-effort): {exc!r}")

    print("[terraform_destroy] ejecutando terraform destroy -auto-approve -input=false...")
    result = subprocess.run(
        ["terraform", "destroy", "-auto-approve", "-input=false"],
        cwd=terraform_dir,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "stage": "terraform_destroy",
                "message": result.stderr or result.stdout,
            },
        )
    # Borrar marcador + artifact del template + creds de teardown →
    # "Mi Infraestructura" vuelve a empty state y no quedan secrets en disco.
    _remove_platform_marker(terraform_dir)
    _remove_pipelines_registry(terraform_dir)
    _remove_capabilities(terraform_dir)
    for tmp in (_INDEX_TEMPLATE_ARTIFACT_NAME, _DESTROY_CREDS_NAME, "deploy.auto.tfvars.json"):
        try:
            (terraform_dir / tmp).unlink(missing_ok=True)
        except OSError:
            pass
    print("[terraform_destroy] OK — entorno destruido")
    return TerraformDestroyResponse(
        status="success",
        message="Entorno destruido correctamente.",
    )