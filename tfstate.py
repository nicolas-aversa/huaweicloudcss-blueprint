"""Lectura del estado de Terraform, sin importar dónde viva.

El `terraform.tfstate` es lo ÚNICO que sabe qué recursos existen en Huawei y
cómo destruirlos. Hasta ahora vivía siempre como archivo local en el workspace
del usuario, y siete lugares del código lo abrían y parseaban a mano. Eso ataba
la plataforma al disco de una máquina: perderlo significa clusters CSS
facturando sin forma de darlos de baja desde la app.

Este módulo hace dos cosas:

1. **Centraliza la lectura.** Los llamadores piden "el state" y no saben si sale
   de un archivo o de un bucket. Es lo que permite mover el backend sin volver a
   tocar esos siete lugares.
2. **Prepara el backend remoto en OBS.** OBS habla S3, así que se usa el backend
   `s3` de Terraform apuntado al endpoint de Huawei, con los `skip_*` que hacen
   falta porque OBS no expone las APIs de AWS que Terraform valida por default.

El backend remoto es **opt-in por usuario**: se activa cargando un bucket en
⚙ Configuración. Sin bucket, todo sigue exactamente como antes (state local), y
por eso el camino local sigue siendo el rápido — un `read_state` no paga una ida
a la red salvo que el workspace esté efectivamente en remoto.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

# El archivo que marca que este workspace usa backend remoto. Terraform toma
# cualquier .tf del directorio, así que escribirlo/borrarlo es lo que cambia el
# backend — no hay forma de hacerlo con variables: los bloques `backend` no
# admiten interpolación.
BACKEND_FILE = "backend.tf"
STATE_FILE = "terraform.tfstate"

# Registro que Terraform escribe tras un `init` con la config del backend en uso.
# Comparar lo que dice contra lo que queremos es lo que detecta que hay que
# migrar el state (en cualquiera de las dos direcciones).
_BACKEND_RECORD = Path(".terraform") / "terraform.tfstate"

# Un state recién inicializado, sin recursos, pesa ~180 bytes. El umbral existe
# para los casos en que el archivo no parsea y hay que decidir igual.
_MIN_STATE_BYTES = 200

# `/terraform/status` se poleaba desde el front. Con state local leer el archivo
# es gratis, pero con backend remoto cada lectura es un `terraform state pull`
# contra OBS. Este cache colapsa la ráfaga de polls sin ocultar un deploy en
# curso (5 s es menos que el intervalo con el que el front refresca).
_CACHE_TTL = 5.0
_cache: dict[str, tuple[float, dict]] = {}
_cache_guard = threading.Lock()

_PULL_TIMEOUT = 60


def uses_remote_backend(terraform_dir: Path | str) -> bool:
    """True si este workspace tiene el backend remoto configurado."""
    return (Path(terraform_dir) / BACKEND_FILE).is_file()


def read_state(terraform_dir: Path | str) -> dict:
    """El state parseado (shape v4 de Terraform). `{}` si no hay o no se puede leer.

    Nunca levanta: los llamadores son endpoints de lectura y guards de destroy,
    y ninguno puede romperse porque el state no esté disponible.
    """
    terraform_dir = Path(terraform_dir)
    if not uses_remote_backend(terraform_dir):
        return _read_local(terraform_dir)

    clave = str(terraform_dir)
    ahora = time.time()
    with _cache_guard:
        hit = _cache.get(clave)
        if hit and ahora - hit[0] < _CACHE_TTL:
            return hit[1]

    estado = _pull_remote(terraform_dir)
    with _cache_guard:
        _cache[clave] = (ahora, estado)
    return estado


def tf_env() -> dict[str, str]:
    """El entorno con el que corre TODO `terraform`.

    Desde Terraform 1.11 el SDK de AWS que trae el backend s3 calcula un
    checksum CRC por defecto en cada PutObject y lo manda con codificación
    `aws-chunked` (el cambio de "default integrity protections" de enero
    2025). OBS no entiende ese encoding, calcula el SHA-256 sobre el cuerpo
    crudo y rechaza con `XAmzContentSHA256Mismatch`. `skip_s3_checksum` en el
    HCL alcanzaba para Terraform 1.9 (el de la imagen Docker) y NO alcanza
    para 1.13 (el de una máquina de desarrollo): con 1.13.3 el apply creó los
    clusters y no pudo guardar el state. Estas dos variables son la perilla
    oficial del SDK para volver a calcular checksums solo cuando la API los
    exige; con ellas puestas, `state push` contra OBS funcionó.

    Van en cada invocación —init, apply, destroy, output, state pull/push—
    porque cualquiera de ellas puede escribir o leer el state.
    """
    env = dict(os.environ)
    env.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    env.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")
    return env


def invalidate(terraform_dir: Path | str) -> None:
    """Descarta el cache de ese workspace. Se llama tras un apply o un destroy:
    sin esto, el status podría mostrar hasta 5 s el mundo anterior."""
    with _cache_guard:
        _cache.pop(str(Path(terraform_dir)), None)


def _read_local(terraform_dir: Path) -> dict:
    archivo = terraform_dir / STATE_FILE
    try:
        if not archivo.is_file():
            return {}
        return json.loads(archivo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


# Por qué no se pudo leer el state remoto la última vez, por workspace. Un
# state remoto ilegible y uno vacío se ven igual desde afuera —los dos devuelven
# `{}`— y la app los pintaba igual: "No tenés ningún entorno levantado". Con las
# AK/SK rotadas, eso significa una pantalla vacía mientras el cluster factura.
_ultimo_error: dict[str, str] = {}


def error_remoto(terraform_dir: Path | str) -> str:
    """Por qué falló el último `state pull` de ese workspace, o "" si anduvo."""
    return _ultimo_error.get(str(Path(terraform_dir)), "")


def _pull_remote(terraform_dir: Path) -> dict:
    """`terraform state pull` devuelve el state crudo, con el MISMO shape que el
    archivo local — por eso los parsers de arriba no cambian."""
    clave = str(terraform_dir)
    try:
        res = subprocess.run(
            ["terraform", "state", "pull"], cwd=str(terraform_dir), env=tf_env(),
            capture_output=True, text=True, timeout=_PULL_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[tfstate] no se pudo leer el state remoto: {exc}", flush=True)
        _ultimo_error[clave] = str(exc)[:300]
        return {}
    if res.returncode != 0:
        print(f"[tfstate] state pull falló: {(res.stderr or '')[:300]}", flush=True)
        _ultimo_error[clave] = (res.stderr or "").strip()[:300] or "terraform state pull falló"
        return {}
    try:
        estado = json.loads(res.stdout or "{}")
    except (json.JSONDecodeError, ValueError) as exc:
        _ultimo_error[clave] = f"el state remoto no es JSON válido: {exc}"
        return {}
    _ultimo_error.pop(clave, None)
    return estado


def has_resources(terraform_dir: Path | str) -> bool:
    """True si el state tiene recursos reales (hay algo que destruir).

    Reemplaza al chequeo de "el archivo pesa ≥200 bytes", que era una
    aproximación a esto mismo. Ante un state ilegible se responde **True** a
    propósito: equivocarse hacia "no hay nada" haría que el destroy sea un no-op
    y dejaría clusters CSS facturando; equivocarse hacia "hay algo" solo hace que
    Terraform corra y no encuentre nada.
    """
    terraform_dir = Path(terraform_dir)
    estado = read_state(terraform_dir)
    if estado:
        return bool(estado.get("resources"))
    if uses_remote_backend(terraform_dir):
        return False        # remoto vacío o inalcanzable: no inventamos recursos
    archivo = terraform_dir / STATE_FILE
    try:
        return archivo.is_file() and archivo.stat().st_size >= _MIN_STATE_BYTES
    except OSError:
        return False


def resource_attributes(state: dict, resource_type: str) -> dict:
    """Atributos de la primera instancia de `resource_type`. `{}` si no está."""
    for res in (state or {}).get("resources", []) or []:
        if res.get("type") == resource_type:
            instancias = res.get("instances") or []
            if instancias:
                return instancias[0].get("attributes") or {}
    return {}


# ── Backend remoto en OBS ────────────────────────────────────────────────────
# Prefijo bajo el que vive el estado dentro del bucket de demos. Convive con los
# `<slug>-logs/` de los datasets sin tocarse: el input s3 de Logstash lista por
# prefijo, así que nunca ve `tfstate/`. La excepción —un input SIN prefijo, que
# listaría el bucket entero -y con `delete => true` lo borraría- la ataja el
# guard de `generate_input_block`.
STATE_PREFIX = "tfstate"


def state_bucket() -> str:
    """El bucket donde vive el estado: el mismo de los datasets de demo.

    Sin bucket configurado devuelve '' y el estado se queda local — que es lo que
    pasa en una instalación recién levantada, antes de que el SA cargue su cuenta.
    """
    import maas_integrator as _mi

    return (_mi.get_huawei_settings().get("demo_bucket") or "").strip()


class BackendIncompleto(RuntimeError):
    """Hay bucket de state pero no credenciales para llegar a él."""


def backend_settings() -> dict:
    """Config del backend para el usuario actual, o `{}` si no hay bucket.

    No hay nada que activar: si el SA tiene bucket de demos y credenciales, su
    estado va a OBS. Todo sale de lo que la cuenta ya tiene.

    Bucket sin AK/SK levanta `BackendIncompleto` en vez de devolver `{}`. Antes
    devolvía `{}` y `prepare` lo tomaba como "quiere local": borraba el
    `backend.tf` y **migraba el state de OBS al disco** con `-force-copy`, sin
    que nadie lo pidiera. Un guardado parcial de ⚙ Configuración alcanzaba para
    que el state cambiara de lugar, y de ahí que el init fallara "a veces".
    """
    import maas_integrator as _mi

    bucket = state_bucket()
    if not bucket:
        return {}
    ak, sk = _mi.resolve_obs_creds()
    if not (ak and sk):
        raise BackendIncompleto(
            f"El bucket de state `{bucket}` está configurado pero faltan las AK/SK de "
            "OBS: cargalas en ⚙ Configuración → Credenciales de la cuenta.")
    region = _mi.get_region()
    return {"bucket": bucket, "region": region,
            "endpoint": f"https://obs.{region}.myhuaweicloud.com",
            "access_key": ak, "secret_key": sk}


def _backend_config(cfg: dict, key: str) -> dict:
    """La config del backend, con las mismas claves y valores que Terraform
    registra en `.terraform/terraform.tfstate` → `backend.config` tras el init.
    Es lo que se escribe al HCL y lo que se compara para decidir si hace falta
    re-inicializar: una sola fuente para las dos cosas."""
    return {
        "bucket": cfg["bucket"],
        "key": key,
        "region": cfg["region"],
        "endpoints": {"s3": cfg["endpoint"]},
        "access_key": cfg["access_key"],
        "secret_key": cfg["secret_key"],
        "skip_credentials_validation": True,
        "skip_region_validation": True,
        "skip_metadata_api_check": True,
        "skip_requesting_account_id": True,
        "use_path_style": False,
        "skip_s3_checksum": True,
    }


def _backend_hcl(cfg: dict, key: str) -> str:
    """El bloque `backend "s3"` apuntado a OBS.

    Los `skip_*` no son opcionales: OBS no tiene STS, ni el endpoint de metadata
    de EC2, ni las regiones de AWS, así que sin ellos Terraform aborta validando
    cosas que no existen.

    `use_path_style = false`: el bucket va como subdominio
    (`<bucket>.obs.<region>.myhuaweicloud.com`), que es como lo pide OBS. Estuvo
    en `true` con la creencia de que OBS no servía el bucket como subdominio, y
    era al revés: el primer `init` contra un bucket real murió con
    `403 VirtualHostDomainRequired: Virtual host domain is required while
    accessing a specific bucket`. El SDK de OBS con el que la app sube los
    datasets usa virtual-host desde siempre — por eso ese camino andaba y este no.

    `endpoints.s3` (anidado) es la forma desde Terraform 1.6.3; el `endpoint`
    plano que aparece en tutoriales viejos ya no se acepta. Acá se pinea 1.9.8.
    """
    c = _backend_config(cfg, key)
    tf = lambda v: "true" if v else "false"   # noqa: E731
    return f'''# Generado por la plataforma — NO editar a mano.
# El estado de Terraform vive en OBS, no en el disco de esta máquina: es lo único
# capaz de destruir los clusters CSS, y un disco perdido significaba clusters
# facturando sin forma de darlos de baja.
terraform {{
  backend "s3" {{
    bucket = "{c['bucket']}"
    key    = "{c['key']}"
    region = "{c['region']}"

    endpoints = {{
      s3 = "{c['endpoints']['s3']}"
    }}

    access_key = "{c['access_key']}"
    secret_key = "{c['secret_key']}"

    # OBS es compatible con S3 pero no trae las APIs auxiliares de AWS.
    skip_credentials_validation = {tf(c['skip_credentials_validation'])}
    skip_region_validation      = {tf(c['skip_region_validation'])}
    skip_metadata_api_check     = {tf(c['skip_metadata_api_check'])}
    skip_requesting_account_id  = {tf(c['skip_requesting_account_id'])}
    use_path_style              = {tf(c['use_path_style'])}
    # El SDK de AWS firma el PutObject por chunks (`x-amz-content-sha256:
    # STREAMING-…`) y OBS lo calcula sobre el cuerpo crudo: no coinciden y OBS
    # rechaza la escritura con `XAmzContentSHA256Mismatch`. Sin esto el apply
    # crea los clusters y después NO PUEDE guardar el state: queda solo en
    # `errored.tfstate`, y el siguiente apply crea un segundo par de clusters.
    skip_s3_checksum            = {tf(c['skip_s3_checksum'])}
  }}
}}
'''


def _recorded(terraform_dir: Path) -> dict:
    """El bloque `backend` que registró el último `init` ({} si nunca corrió)."""
    try:
        datos = json.loads((terraform_dir / _BACKEND_RECORD).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return datos.get("backend") or {}


def _recorded_backend(terraform_dir: Path) -> str:
    """Qué backend registró el último `init`: 's3', 'local', o '' si nunca corrió."""
    if not (terraform_dir / _BACKEND_RECORD).is_file():
        return ""
    return (_recorded(terraform_dir).get("type") or "local").strip()


def _config_cambio(reg: dict, cfg: dict, key: str) -> bool:
    """True si la config registrada por el último init difiere en ALGO de la que
    se acaba de escribir al HCL.

    Terraform compara la config entera —credenciales incluidas, que guarda en
    claro en el registro— y si algo cambió aborta el apply con "Backend
    configuration changed"; acá se detecta antes para mandar `-reconfigure`, que
    re-lee sin migrar nada. La versión anterior comparaba tres claves: rotar las
    AK/SK, que es lo que más se cambia, no disparaba ningún init.
    """
    grabada = reg.get("config") or {}
    for k, v in _backend_config(cfg, key).items():
        tiene = grabada.get(k)
        if isinstance(v, bool):
            if bool(tiene) != v:
                return True
        elif isinstance(v, dict):
            # Terraform registra el bloque `endpoints` con TODAS sus claves
            # (`dynamodb: null, iam: null, s3: ..., sso: null, sts: null`):
            # comparar el dict entero decía "cambió" en cada deploy y mandaba un
            # `-reconfigure` de más. Solo importan las que nosotros fijamos.
            if any((tiene or {}).get(sk) != sv for sk, sv in v.items()):
                return True
        elif (tiene or "") != v:
            return True
    return False


def _local_state_has_resources(terraform_dir: Path) -> bool:
    """¿El `terraform.tfstate` del disco tiene recursos? Mira SOLO el archivo
    local: `has_resources` leería el remoto si ya hay `backend.tf` escrito, y acá
    se llama justo después de escribirlo."""
    estado = _read_local(terraform_dir)
    if estado:
        return bool(estado.get("resources"))
    archivo = terraform_dir / STATE_FILE
    try:
        return archivo.is_file() and archivo.stat().st_size >= _MIN_STATE_BYTES
    except OSError:
        return False


def prepare(terraform_dir: Path | str, state_key: str) -> tuple[bool, list[str]]:
    """Deja el workspace apuntando al backend que corresponde.

    Devuelve `(hace_falta_init, flags_extra)`.

    Reconcilia en las **dos** direcciones. Activar el bucket migra el state local
    a OBS; vaciarlo lo trae de vuelta al disco. Dejar una sin la otra sería la
    forma más fácil de perder el state: Terraform arrancaría con un backend
    vacío, no vería recursos, y un apply intentaría crear todo de nuevo.

    `-migrate-state -force-copy` es lo que copia el state existente al backend
    nuevo sin preguntar por stdin (acá no hay nadie para contestar).

    El flag de init es su propio valor y no se deduce de los flags extra: un
    workspace recién sembrado desde el template YA trae los providers cacheados
    pero nunca corrió `init`. Si solo se mirara "faltan providers", se escribiría
    el `backend.tf` sin inicializarlo y el apply moriría con "Backend
    initialization required".
    """
    terraform_dir = Path(terraform_dir)
    archivo = terraform_dir / BACKEND_FILE
    cfg = backend_settings()
    quiere_remoto = bool(cfg)
    tenia = _recorded_backend(terraform_dir)

    if quiere_remoto:
        archivo.write_text(_backend_hcl(cfg, state_key), encoding="utf-8")
    elif archivo.exists():
        archivo.unlink()

    invalidate(terraform_dir)

    if not tenia:
        # Sin registro de backend. Ojo: Terraform NO escribe registro para el
        # `local` implícito, así que "sin registro" también es un workspace que
        # desplegó en el disco y ahora estrena bucket. Si hay state local con
        # recursos, hay que migrarlo; un `init` pelado con `-input=false` muere
        # con "Can't ask approval for state migration when interactive input is
        # disabled" — y pasó.
        if quiere_remoto and _local_state_has_resources(terraform_dir):
            return True, ["-migrate-state", "-force-copy"]
        return True, []
    destino = "s3" if quiere_remoto else "local"
    if tenia != destino:
        return True, ["-migrate-state", "-force-copy"]
    if quiere_remoto:
        reg = _recorded(terraform_dir)
        grabada = reg.get("config") or {}
        # Mismo backend pero otro bucket u otra key: es una mudanza del state.
        if grabada.get("bucket") != cfg["bucket"] or grabada.get("key") != state_key:
            return True, ["-migrate-state", "-force-copy"]
        # Mismo lugar, otra config (credenciales rotadas, otra región, otro
        # flag): re-leer sin migrar.
        if _config_cambio(reg, cfg, state_key):
            return True, ["-reconfigure"]
    return False, []


# ── errored.tfstate ──────────────────────────────────────────────────────────
# Cuando Terraform no puede escribir el state al backend, lo deja en este
# archivo y avisa que otro apply "crearía un state bifurcado". Bifurcado quiere
# decir: los clusters que ya se crearon no están en ningún state, y el siguiente
# apply crea OTRO par — facturando los dos, y sin forma de destruir el primero
# desde la app. Pasó: el PutObject a OBS falló por el checksum y los clusters
# quedaron solo acá.
ERRORED_FILE = "errored.tfstate"


def push_errored_state(terraform_dir: Path | str) -> tuple[bool, str]:
    """Si quedó un `errored.tfstate`, lo sube al backend ANTES de operar.

    Devuelve `(ok, detalle)`. `ok` también cuando no había nada que subir. Si el
    push falla, el que llama tiene que PARAR: seguir es bifurcar el state.
    No usa `-force`: si Terraform rechaza el push por linaje o serial, es que el
    backend tiene un state más nuevo y pisarlo a ciegas es peor.
    """
    terraform_dir = Path(terraform_dir)
    archivo = terraform_dir / ERRORED_FILE
    if not archivo.is_file():
        return True, ""
    try:
        res = subprocess.run(
            ["terraform", "state", "push", "-no-color", ERRORED_FILE],
            cwd=str(terraform_dir), env=tf_env(), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"no se pudo correr `terraform state push`: {exc}"
    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "terraform state push falló").strip()
    archivo.unlink()
    invalidate(terraform_dir)
    return True, "state recuperado de errored.tfstate y subido al backend"


def state_key_for(user_id: str) -> str:
    """Ruta del state dentro del bucket. Un archivo por SA: sus entornos son
    independientes y nunca deben compartir state."""
    return f"{STATE_PREFIX}/{user_id or 'default'}/terraform.tfstate"
