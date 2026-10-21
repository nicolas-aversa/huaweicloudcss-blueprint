"""Casos de demo creados desde la plataforma (no hardcodeados en `verticals/`).

Los 8 casos built-in viven en `verticals/<slug>.py` y solo se agregan tocando
código + rebuild. Este módulo permite crear casos **en runtime** desde el
Builder: el SA sube su `.log`, GLM-5.2 detecta el filter + los campos, y el caso
queda guardado como uno más del flujo de demo.

Alcance: **compartido por instancia** (no por usuario). Todos los usuarios de esa
ECS ven los casos creados; borrar queda limitado al creador o a un admin.

Layout (en el volumen `appdata`, que ya persiste):

    $APP_DATA_DIR/cases/
      <slug>.json           -> metadata con el MISMO shape que un `VERTICAL`
      <slug>/<archivo>      -> el dataset, con el nombre y el contenido con que
                               se subió (así llega a OBS: `<slug>-logs/<archivo>`)
      <slug>.log            -> (casos anteriores) el dataset, renombrado

El archivo se guarda byte a byte: ni se le sacan las líneas `#`, ni se
renombra a `.log`. Lo que el SA subió es lo que aparece en el bucket.

El JSON respeta las convenciones de `verticals/__init__.py` (slug, label,
full_label, group, icon, index_base, description, sample, filter_code, fields,
suggested_questions, dataset_files) para que el merge con el registro built-in
sea directo. **No** lleva `capability` ni `dashboard`: esas dos piezas se
auto-derivan en runtime desde `fields` (`capabilities.build_spec_from_fields` y
`dashboards.build_ndjson_from_fields`), que es el camino que ya usaba el
despliegue productivo.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import unicodedata
from pathlib import Path

import auth as _auth
import maas_integrator as _mi
import preguntas as _preguntas
import verticals as _verticals

# Grupo propio en el grid del paso 1. Se agrega al payload solo si hay ≥1 caso.
GROUP = {"id": "mis-casos", "label": "Mis casos", "icon": "plus"}

# Iconos ofrecidos en la UI: sprites que YA existen en el <svg> de index.html.
ICONS = ["shield", "activity", "shopping-cart", "play", "droplet", "mountain", "heart",
         "truck", "database", "cloud", "code", "box"]

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SLUG_MIN, _SLUG_MAX = 3, 40
# `custom` es la card UI-only del Builder; `logs` es el index_base por defecto de
# ese camino. Un caso guardado no puede pisarlos.
_RESERVED = {"custom", "logs", "mis-casos"}
_MAX_LOG_BYTES = 50 * 1024 * 1024  # mismo cap que la drop-zone del front


# Plugins de entrada que el backend sabe emitir (`main.generate_input_block`).
INPUT_PLUGINS = ("obs", "s3", "kafka", "beats", "jdbc", "http", "file")

# Campos del `input_config` que son SECRETOS: se guardan cifrados en el JSON del
# caso (Fernet, misma clave derivada de APP_SECRET_KEY que usa el settings file).
# Se cifran campo a campo y no el archivo entero: así el resto del caso sigue
# siendo legible/diffeable, y solo lo sensible queda opaco.
_SECRET_FIELDS = frozenset({
    "secret_access_key", "access_key_id",
    "sasl_password", "ssl_truststore_password",
    "jdbc_password",
})
_ENC_PREFIX = "enc:"


class CaseError(ValueError):
    """Validación fallida al guardar un caso (se mapea a HTTP 400)."""


def cases_dir() -> Path:
    """Directorio del store. Se resuelve en cada llamada (no al importar) para
    que los tests puedan mover `auth.DATA_ROOT`."""
    return _auth.DATA_ROOT / "cases"


# Qué formato lee el pipeline, como lo diría el SA. Sale del filtro (lo arma el
# Builder para cualquier archivo), así que vale también para los casos guardados
# antes de que la card lo mostrara.
_FORMATOS = [(r"\bcsv\s*\{", "CSV"), (r"\bjson\s*\{", "JSON"), (r"\bxml\s*\{", "XML"),
             (r"\bkv\s*\{", "clave=valor"), (r"SYSLOG|syslog_pri", "Syslog"), (r"CEF:", "CEF")]


def formato_del_filtro(filter_code: str) -> str:
    hallados = [nombre for patron, nombre in _FORMATOS if re.search(patron, filter_code or "")]
    if not hallados and re.search(r"\b(grok|dissect)\s*\{", filter_code or ""):
        hallados = ["Texto"]
    return ", ".join(hallados)


_ENTRADAS = {"obs": "OBS", "s3": "OBS", "kafka": "Kafka", "beats": "Beats", "jdbc": "JDBC"}


def entrada_y_formato(case: dict) -> tuple[str, str]:
    """Por dónde llega el dato y en qué, para la card. Un dataset llega por OBS y
    se dice con la extensión de su archivo; una fuente en vivo, con su plugin y
    el formato que lee el filtro (no hay archivo)."""
    if case.get("case_type", "dataset") == "live":
        plugin = str((case.get("input_config") or {}).get("plugin_type", "") or "")
        return _ENTRADAS.get(plugin, plugin.capitalize()), formato_del_filtro(case.get("filter_code", ""))
    exts = sorted({Path(f).suffix.lower() for f in case.get("dataset_files") or [] if Path(f).suffix})
    return "OBS", ", ".join(exts) or formato_del_filtro(case.get("filter_code", ""))


# ── Normalización ────────────────────────────────────────────────────────────
def slugify(text: str) -> str:
    """`'Logs de mi Firewall'` -> `'logs-de-mi-firewall'`."""
    norm = unicodedata.normalize("NFKD", str(text or ""))
    ascii_only = norm.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", ascii_only)).strip("-")


def _clean_fields(fields: list) -> list[dict]:
    """Deja solo las claves que consume el resto de la app (index template,
    dashboards, capabilities), descartando basura del browser."""
    # `date_format` viaja con el campo hasta el index template: sin él, una fecha
    # como `2026-09-18 11:04:12` se indexa con el formato por defecto y, si no
    # entra, se descarta en silencio. `frecuentes` son los valores más comunes
    # de una dimensión: las preguntas de ejemplo los nombran entre paréntesis.
    keep = ("raw_name", "field_path", "ecs_path", "ecs_overlay_path", "type",
            "business_label", "unit", "dimension", "role", "is_ecs",
            "ecs_type_official", "normalized_path", "date_format", "frecuentes",
            # Las marcas del paso 2: sin ellas, un caso guardado perdía su
            # perfil (Entidad) y su analista enmascarado (Sensible).
            "entity", "sensitive", "principal", "derivado_de", "falla_si")
    out = []
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        entry = {k: f[k] for k in keep if k in f and f[k] not in (None, "")}
        if entry.get("field_path") or entry.get("raw_name"):
            out.append(entry)
    return out


def _encrypt(value: str) -> str:
    """Cifra un secreto para guardarlo. Sin `APP_SECRET_KEY` (single-user) no hay
    clave: se deja en claro, igual que hace el settings file."""
    f = _mi._fernet()
    if f is None or not value:
        return value
    return _ENC_PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(_ENC_PREFIX):
        return value  # plano (legacy o sin clave)
    f = _mi._fernet()
    if f is None:
        return ""
    try:
        return f.decrypt(value[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def _walk_secrets(cfg: dict, fn) -> dict:
    """Aplica `fn` a los campos secretos de un `input_config` (1 nivel de anidado:
    `{plugin_type, <plugin>: {...}}`)."""
    out: dict = {}
    for k, v in (cfg or {}).items():
        if isinstance(v, dict):
            out[k] = {kk: (fn(vv) if kk in _SECRET_FIELDS and isinstance(vv, str) else vv)
                      for kk, vv in v.items()}
        else:
            out[k] = fn(v) if k in _SECRET_FIELDS and isinstance(v, str) else v
    return out


def _clean_input_config(cfg: dict | None) -> dict:
    """Normaliza el `input_config` del caso: solo `plugin_type` + la sub-config de
    ESE plugin (descarta las de los otros que el front pueda haber mandado)."""
    if not isinstance(cfg, dict):
        return {}
    plugin = str(cfg.get("plugin_type", "") or "").strip().lower()
    if plugin not in INPUT_PLUGINS:
        return {}
    sub = cfg.get(plugin)
    out: dict = {"plugin_type": plugin}
    if isinstance(sub, dict):
        out[plugin] = {k: v for k, v in sub.items() if v not in (None, "")}
    return out


def _data_lines(text: str) -> list[str]:
    """Líneas con datos: sin vacías y sin comentarios `#` (mismo criterio que
    `main._bundled_dataset` y `datasets/README.md`). Solo para CONTAR y para
    sacar la muestra; el archivo se guarda entero."""
    return [l for l in (text or "").splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(nombre: str, slug: str) -> str:
    """El nombre con que se subió, apto para disco y para una key de OBS.

    Solo el basename (un `../x` no sale de la carpeta del caso), caracteres
    seguros, sin puntos al inicio, máximo 120. Si no queda nada usable, cae a
    `<slug>.log`, que es lo que se usaba siempre."""
    base = str(nombre or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _FILENAME_RE.sub("_", base).lstrip(".")[:120]
    return base or f"{slug}.log"


# ── Lectura ──────────────────────────────────────────────────────────────────
def list_cases() -> list[dict]:
    """Todos los casos guardados, ordenados por fecha de creación."""
    d = cases_dir()
    if not d.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(d.glob("*.json")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(case, dict) and case.get("slug"):
            out.append(case)
    out.sort(key=lambda c: (c.get("created_at", 0), c.get("slug", "")))
    return out


def get_case(slug: str) -> dict | None:
    if not slug:
        return None
    path = cases_dir() / f"{slug}.json"
    if not path.is_file():
        return None
    try:
        case = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return case if isinstance(case, dict) else None


def dataset_path(slug: str) -> Path | None:
    """Ruta del dataset del caso, o None si no existe.

    Primero `<slug>/<archivo>` (el nombre original, registrado en
    `dataset_files`); si no, el `<slug>.log` plano de los casos anteriores."""
    if not slug:
        return None
    d = cases_dir()
    case = get_case(slug) or {}
    for nombre in case.get("dataset_files") or []:
        path = d / slug / nombre
        if path.is_file():
            return path
    path = d / f"{slug}.log"
    return path if path.is_file() else None


def dataset_files() -> dict[str, list[str]]:
    """`slug -> [archivo]`, para mergear con `verticals.demo_dataset_files()`.
    El nombre es el del archivo en disco: es lo que va a la key de OBS."""
    out: dict[str, list[str]] = {}
    for case in list_cases():
        slug = case["slug"]
        path = dataset_path(slug)
        if path is not None:
            out[slug] = [path.name]
    return out


def label_for(slug: str) -> str:
    case = get_case(slug)
    return str(case.get("label", "")) if case else ""


def front_entries() -> list[dict]:
    """Entradas para `front_payload()['verticals']` (mismo shape que
    `verticals.front_payload`, + `custom`/`createdBy` para la UI de borrado)."""
    out = []
    for c in list_cases():
        out.append({
            "slug": c["slug"],
            "label": c.get("label", ""),
            "fullLabel": c.get("full_label", "") or c.get("label", ""),
            "group": c.get("group", "") or GROUP["id"],
            "icon": c.get("icon", "box"),
            "indexBase": c.get("index_base", "") or c["slug"],
            "description": c.get("description", ""),
            "entrada": entrada_y_formato(c)[0],
            "formato": entrada_y_formato(c)[1],
            "origen": c.get("origen", ""),
            "dedupId": "",
            "hidden": False,
            "hasCapability": bool(c.get("fields")),  # el asistente sale de sus campos
            "hasSecurity": bool(c.get("seguridad")) and "security_analytics" not in (c.get("excluir") or []),
            "familia": c.get("familia", ""),
            "familiaLabel": c.get("familia_label", ""),
            "sample": c.get("sample", ""),
            "filterCode": c.get("filter_code", ""),
            "fields": c.get("fields", []),
            # Los casos guardados antes de que el modal mandara las preguntas
            # tienen `[]`: se arman de sus campos, así el chat de cada caso
            # muestra las suyas y no las mismas genéricas para todos.
            "questions": (c.get("suggested_questions")
                          or _preguntas.armar(c.get("fields", []))),
            "custom": True,
            "createdBy": c.get("created_by", ""),
            "caseType": c.get("case_type", "dataset"),
            # Solo el plugin, NUNCA la sub-config: ahí viven las credenciales y
            # este payload se inyecta en el HTML que ve el browser.
            "inputPlugin": (c.get("input_config") or {}).get("plugin_type", ""),
        })
    return out


# ── Escritura ────────────────────────────────────────────────────────────────
def _volumen(valor) -> dict:
    """`{eventos_por_dia, alta_disponibilidad}` del paso 2, o vacío."""
    if not isinstance(valor, dict):
        return {}
    try:
        eventos = max(0, min(int(valor.get("eventos_por_dia") or 0), 1_000_000_000_000))
    except (TypeError, ValueError):
        eventos = 0
    return {"eventos_por_dia": eventos, "alta_disponibilidad": bool(valor.get("alta_disponibilidad", True))} if eventos else {}


def _retencion(valor) -> int:
    """Los días de retención del paso 2 (0 = el valor por defecto)."""
    try:
        dias = int(valor or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(dias, 3650))


def save_case(meta: dict, log_text: str, created_by: str = "", filename: str = "") -> dict:
    """Valida, normaliza y persiste un caso nuevo. Devuelve el caso guardado.

    `filename` es el nombre con que el SA subió el archivo: el dataset se guarda
    con ese nombre y con el contenido exacto, y así llega al bucket.

    Levanta `CaseError` con un mensaje para el usuario si algo no cierra.
    """
    label = str(meta.get("label", "") or "").strip()
    if not label:
        raise CaseError("Falta el nombre del caso.")
    if len(label) > 60:
        raise CaseError("El nombre no puede superar los 60 caracteres.")

    slug = str(meta.get("slug", "") or "").strip().lower() or slugify(label)
    if not (_SLUG_MIN <= len(slug) <= _SLUG_MAX) or not _SLUG_RE.match(slug):
        raise CaseError(
            f"El identificador '{slug}' no es válido: usá {_SLUG_MIN}-{_SLUG_MAX} "
            "caracteres en minúscula, números y guiones.")
    if slug in _RESERVED:
        raise CaseError(f"'{slug}' es un identificador reservado. Elegí otro nombre.")
    if _verticals.get_vertical(slug) is not None:
        raise CaseError(f"Ya existe un caso de demo built-in con el identificador '{slug}'. "
                        "Elegí otro nombre.")
    if get_case(slug) is not None:
        raise CaseError(f"Ya existe un caso guardado con el identificador '{slug}'. "
                        "Elegí otro nombre o borrá el anterior.")

    fields = _clean_fields(meta.get("fields") or [])
    if not fields:
        raise CaseError("No hay campos detectados. Analizá el log en el paso 1 antes de guardar.")

    filter_code = str(meta.get("filter_code", "") or "").strip()
    if not filter_code:
        raise CaseError("Falta el filter de Logstash. Analizá el log en el paso 1 antes de guardar.")

    input_config = _clean_input_config(meta.get("input_config"))
    # El tipo se DERIVA del archivo: con dataset el caso es repetible (se sube a
    # OBS y se replayea); sin dataset los datos tienen que llegar de la fuente
    # real del cliente, así que el caso es `live` y exige un input_config.
    if len((log_text or "").encode("utf-8")) > _MAX_LOG_BYTES:
        raise CaseError("El dataset supera los 50 MB.")
    lines = _data_lines(log_text)
    case_type = "dataset" if lines else "live"
    if case_type == "live":
        if not input_config:
            raise CaseError(
                "Sin archivo de datos hay que decir de dónde salen: elegí la fuente "
                "(Kafka, Beats o JDBC) en el paso 3, o importá un .log.")
        # Un caso que lee de un bucket ajeno ya no se puede crear: solo lo podía
        # desplegar quien tuviera acceso a ESE bucket, y copiarle los datos al
        # cliente para guardarlos como dataset es justo lo que no queremos hacer.
        # Si el dato es un archivo, se sube como archivo.
        if input_config["plugin_type"] in ("obs", "s3"):
            raise CaseError(
                "Un caso no puede leer directo de un bucket: si los datos son un "
                "archivo, subilo en el paso 1 y queda guardado como dataset.")

    sample = str(meta.get("sample", "") or "").strip() or (lines[0] if lines else "")
    if not sample:
        raise CaseError("Falta la muestra del log. Pegá unas líneas o importá el archivo.")
    icon = str(meta.get("icon", "") or "").strip() or "box"
    if icon not in ICONS:
        icon = "box"
    group = str(meta.get("group", "") or "").strip() or GROUP["id"]
    known_groups = {g["id"] for g in _verticals.GROUPS} | {GROUP["id"]}
    if group not in known_groups:
        group = GROUP["id"]
    questions = [str(q).strip() for q in (meta.get("suggested_questions") or []) if str(q).strip()]

    case = {
        "slug": slug,
        "label": label,
        "full_label": str(meta.get("full_label", "") or "").strip() or label,
        "group": group,
        "icon": icon,
        "index_base": slug,
        "description": str(meta.get("description", "") or "").strip()[:200],
        # La línea chica de la card: la entrada y el formato salen solos (ver
        # entrada_y_formato); de qué sistema sale el dato lo dice el SA.
        "origen": str(meta.get("origen", "") or "").strip()[:40],
        "sample": sample,
        "filter_code": filter_code,
        "fields": fields,
        "suggested_questions": questions[:10],
        # Lo que se decidió en el paso 2 y que el deploy usa aunque el body se
        # rearme sin ello: las reglas de Security Analytics que se propusieron,
        # los plugins apagados y la familia (las fuentes que se subieron juntas).
        "seguridad": meta.get("seguridad") if isinstance(meta.get("seguridad"), dict) else None,
        "excluir": [str(x) for x in (meta.get("excluir") or []) if isinstance(x, str)][:10],
        "retencion_dias": _retencion(meta.get("retencion_dias")),
        # Lo que dijo el cliente para dimensionar el cluster de producción.
        "volumen": _volumen(meta.get("volumen")),
        "familia": slugify(str(meta.get("familia") or "")) if meta.get("familia") else "",
        "familia_label": str(meta.get("familia_label") or "").strip()[:60],
        # Solo los casos con dataset se pre-cargan en OBS; los `live` leen de la
        # fuente del cliente y no tienen archivo que subir. El nombre es el del
        # archivo subido: `<slug>-logs/<ese nombre>` en el bucket.
        "dataset_files": [safe_filename(filename, slug)] if case_type == "dataset" else [],
        "case_type": case_type,
        # La fuente SOLO se guarda para los casos `live`. Un caso con dataset lee
        # de `<slug>-logs/` en el bucket de QUIEN LO DESPLIEGA — guardar acá el
        # bucket y las AK/SK del creador haría que los demás usuarios de la
        # instancia leyeran de la cuenta ajena, rompiendo el aislamiento.
        "input_config": _walk_secrets(input_config, _encrypt) if case_type == "live" else {},
        "created_by": (created_by or "").lower(),
        "created_at": int(time.time()),
        "lines": len(lines),
    }

    d = cases_dir()
    d.mkdir(parents=True, exist_ok=True)
    if case_type == "dataset":
        # Byte a byte, con su nombre: lo que el SA subió es lo que aparece en el
        # bucket. Antes se renombraba a `<slug>.log` y se le sacaban las líneas
        # `#`; el archivo que llegaba a OBS no era el que había subido.
        # `write_bytes`, no `write_text`: en Windows este último traduce los \n.
        (d / slug).mkdir(exist_ok=True)
        (d / slug / case["dataset_files"][0]).write_bytes((log_text or "").encode("utf-8"))
    (d / f"{slug}.json").write_text(json.dumps(case, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    return case


def input_config_for(slug: str) -> dict:
    """`input_config` del caso con los secretos **descifrados**, listo para
    `generate_input_block`. {} si el caso no existe o no tiene fuente propia."""
    case = get_case(slug)
    if not case:
        return {}
    return _walk_secrets(case.get("input_config") or {}, _decrypt)


def source_label(slug: str) -> str:
    """De dónde lee un caso `live`, para mostrar: `kafka (topic)`,
    `beats (puerto)`, `jdbc`. Sin secretos: solo el plugin y lo que identifica
    la fuente."""
    cfg = (get_case(slug) or {}).get("input_config") or {}
    plugin = str(cfg.get("plugin_type", "") or "")
    sub = cfg.get(plugin) or {}
    if plugin == "kafka":
        topics = sub.get("topics") or sub.get("topic") or "?"
        if isinstance(topics, list):
            topics = ", ".join(str(t) for t in topics)
        return f"kafka ({topics})"
    if plugin == "beats":
        return f"beats (puerto {sub.get('port', '?')})"
    if plugin == "jdbc":
        return "jdbc"
    return plugin or "su fuente"


def case_type_for(slug: str) -> str:
    """`dataset` | `live` | '' (no es un caso creado desde la plataforma)."""
    case = get_case(slug)
    return str(case.get("case_type", "dataset")) if case else ""


# ── Reemplazar el dataset (misma estructura, otros valores) ─────────────────
# Un caso se arma una vez (campos, roles, nombres, plugins) y los SAs le guardan
# dashboards encima. Para mejorarle los datos sin rehacerlo —rehacerlo vuelve a
# pasar por la IA y los nombres pueden cambiar, rompiendo lo guardado— se le
# cambia SOLO el archivo, si tiene la misma estructura que el actual. La
# definición no se toca: todo lo que sale de ella sigue igual.
_MUESTRA_PARA_COMPARAR = 2000
# Un valor nuevo que el .conf del caso igual procesa bien: la columna era texto
# (acepta cualquier cosa) o decimal (acepta enteros).
_ACEPTA = {"string": None, "text": None, "float": {"float", "integer"}}
_TIPO_EN_PALABRAS = {"string": "texto", "text": "texto", "integer": "número entero", "float": "número decimal",
                     "date": "fecha", "boolean": "sí/no", "ip": "IP"}


def _perfil_para_comparar(texto: str):
    """El perfil del archivo con una muestra repartida en todo el archivo (no
    solo el principio): una clave de JSON que aparece recién al final también
    cuenta. Un delimitado se lee desde el principio (el header manda y un
    registro con saltos de línea no se puede cortar al medio)."""
    import perfilador

    lineas = (texto or "").splitlines()
    n = _MUESTRA_PARA_COMPARAR
    perfil = perfilador.perfilar(lineas[:n])
    if perfil.formato in ("json", "kv") and len(lineas) > n:
        paso = len(lineas) / (n - 1)
        perfil = perfilador.perfilar([lineas[min(len(lineas) - 1, int(i * paso))] for i in range(n)])
    return perfil


def _tipo(col) -> str:
    return col.tipo + (f" ({col.formato_fecha})" if col.tipo == "date" else "")


def comparar_estructura(actual: str, nuevo: str) -> dict:
    """¿El archivo nuevo tiene la estructura del actual? `{verificable,
    compatible, formato, faltan, sobran, cambian: [{campo, antes, ahora}],
    vacias}`. `verificable` es False si el formato no se puede perfilar (un log
    que parsea el grok del caso): ahí no se puede afirmar nada."""
    pa, pn = _perfil_para_comparar(actual), _perfil_para_comparar(nuevo)
    base = {"verificable": bool(pa.formato), "formato": pa.formato, "faltan": [], "sobran": [],
            "cambian": [], "vacias": []}
    if not pa.formato:
        return {**base, "compatible": False}
    if pn.formato != pa.formato or (pa.formato == "delimitado" and pn.separador != pa.separador):
        return {**base, "compatible": False, "formato_nuevo": pn.formato or "sin estructura"}
    antes = {c.nombre: c for c in pa.columnas}
    ahora = {c.nombre: c for c in pn.columnas}
    base["faltan"] = sorted(set(antes) - set(ahora))
    base["sobran"] = sorted(set(ahora) - set(antes))
    for nombre in sorted(set(antes) & set(ahora)):
        a, n = antes[nombre], ahora[nombre]
        if n.vacios >= len(n.valores):
            base["vacias"].append(nombre)          # sin un valor en la muestra: no hay tipo que comparar
            continue
        # Sigue siendo número, pero ahora trae símbolos, miles o coma decimal
        # ("55%", "1.234,5") que el filtro del caso no limpia: no se indexaría.
        if (a.tipo in ("integer", "float") and n.tipo in ("integer", "float") and n.limpiar_numero
                and (not a.limpiar_numero or n.decimal != a.decimal)):
            base["cambian"].append({"campo": nombre, "antes": _tipo(a), "ahora": "número con símbolos o separadores"})
            continue
        if a.tipo in _ACEPTA and (_ACEPTA[a.tipo] is None or n.tipo in _ACEPTA[a.tipo]):
            continue
        if _tipo(a) != _tipo(n):
            base["cambian"].append({"campo": nombre, "antes": _tipo(a), "ahora": _tipo(n)})
    return {**base, "compatible": not (base["faltan"] or base["sobran"] or base["cambian"])}


def explicar_diferencias(comp: dict) -> str:
    """La comparación en una frase para el SA."""
    if not comp.get("verificable"):
        return "No se puede verificar automáticamente la estructura de este formato."
    if comp.get("formato_nuevo"):
        return (f"El archivo nuevo no tiene el formato del caso ({comp['formato']}, y el nuevo es "
                f"{comp['formato_nuevo']}): eso es un caso nuevo, no otros datos para este.")
    partes = []
    if comp["faltan"]:
        partes.append("le faltan " + ", ".join(comp["faltan"]))
    if comp["sobran"]:
        partes.append("trae campos que el caso no tiene: " + ", ".join(comp["sobran"]))
    for c in comp["cambian"]:
        palabra = lambda t: _TIPO_EN_PALABRAS.get(t.split(" ")[0], t) + (" " + t.split(" ", 1)[1] if " " in t else "")  # noqa: E731
        partes.append(f"{c['campo']} era {palabra(c['antes'])} y ahora es {palabra(c['ahora'])}")
    return ("El archivo nuevo no tiene la misma estructura: " + "; ".join(partes) + ".") if partes else ""


def replace_dataset(slug: str, log_text: str, replaced_by: str = "") -> dict:
    """Le cambia el archivo al caso (mismo nombre, así en el bucket queda uno
    solo) sin tocar su definición. La estructura la verifica quien llama."""
    case = get_case(slug)
    if case is None:
        raise CaseError(f"No existe el caso '{slug}'.")
    if case.get("case_type", "dataset") != "dataset":
        raise CaseError("Este caso no tiene dataset: lee los datos de su fuente en cada deploy.")
    if len((log_text or "").encode("utf-8")) > _MAX_LOG_BYTES:
        raise CaseError("El dataset supera los 50 MB.")
    lineas = _data_lines(log_text)
    if not lineas:
        raise CaseError("El archivo no tiene datos.")
    d = cases_dir()
    destino = dataset_path(slug)
    if destino is None:
        nombre = (case.get("dataset_files") or [f"{slug}.log"])[0]
        (d / slug).mkdir(parents=True, exist_ok=True)
        destino = d / slug / nombre
    destino.write_bytes(log_text.encode("utf-8"))      # bytes: en Windows write_text traduce los \n
    case["dataset_reemplazado"] = {"por": (replaced_by or "").lower(), "en": int(time.time()),
                                   "lineas_antes": case.get("lines", 0)}
    case["lines"] = len(lineas)
    (d / f"{slug}.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    return case


def delete_case(slug: str) -> bool:
    """Borra el caso y su dataset. True si existía."""
    case = get_case(slug)
    if case is None:
        return False
    d = cases_dir()
    for path in (d / f"{slug}.json", d / f"{slug}.log"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    shutil.rmtree(d / slug, ignore_errors=True)
    return True


def can_delete(slug: str, email: str | None) -> bool:
    """Solo el creador o un admin.

    En single-user (sin `APP_SECRET_KEY` → sin login) no hay a quién atribuirle
    nada: el único que usa la app es el operador, así que puede borrar. Sin este
    caso los casos quedarían imborrables en dev/local y en el modo nativo.
    """
    case = get_case(slug)
    if case is None:
        return False
    if not _auth.AUTH_ENABLED:
        return True
    email = (email or "").lower()
    if email and case.get("created_by", "").lower() == email:
        return True
    return _auth.is_admin(email)
