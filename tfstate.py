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


def _pull_remote(terraform_dir: Path) -> dict:
    """`terraform state pull` devuelve el state crudo, con el MISMO shape que el
    archivo local — por eso los parsers de arriba no cambian."""
    try:
        res = subprocess.run(
            ["terraform", "state", "pull"], cwd=str(terraform_dir),
            capture_output=True, text=True, timeout=_PULL_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[tfstate] no se pudo leer el state remoto: {exc}", flush=True)
        return {}
    if res.returncode != 0:
        print(f"[tfstate] state pull falló: {(res.stderr or '')[:300]}", flush=True)
        return {}
    try:
        return json.loads(res.stdout or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}


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


def backend_settings() -> dict:
    """Config del backend para el usuario actual, o `{}` si no hay con qué.

    No hay nada que activar: si el SA tiene bucket de demos y credenciales, su
    estado va a OBS. Todo sale de lo que la cuenta ya tiene.
    """
    import maas_integrator as _mi

    bucket = state_bucket()
    if not bucket:
        return {}
    ak, sk = _mi.resolve_obs_creds()
    if not (ak and sk):
        return {}
    region = _mi.get_region()
    return {"bucket": bucket, "region": region,
            "endpoint": f"https://obs.{region}.myhuaweicloud.com",
            "access_key": ak, "secret_key": sk}


def _backend_hcl(cfg: dict, key: str) -> str:
    """El bloque `backend "s3"` apuntado a OBS.

    Los `skip_*` no son opcionales: OBS no tiene STS, ni el endpoint de metadata
    de EC2, ni las regiones de AWS, así que sin ellos Terraform aborta validando
    cosas que no existen. `use_path_style` porque OBS no sirve el bucket como
    subdominio para este endpoint.

    `endpoints.s3` (anidado) es la forma desde Terraform 1.6.3; el `endpoint`
    plano que aparece en tutoriales viejos ya no se acepta. Acá se pinea 1.9.8.
    """
    return f'''# Generado por la plataforma — NO editar a mano.
# El estado de Terraform vive en OBS, no en el disco de esta máquina: es lo único
# capaz de destruir los clusters CSS, y un disco perdido significaba clusters
# facturando sin forma de darlos de baja.
terraform {{
  backend "s3" {{
    bucket = "{cfg['bucket']}"
    key    = "{key}"
    region = "{cfg['region']}"

    endpoints = {{
      s3 = "{cfg['endpoint']}"
    }}

    access_key = "{cfg['access_key']}"
    secret_key = "{cfg['secret_key']}"

    # OBS es compatible con S3 pero no trae las APIs auxiliares de AWS.
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true
    use_path_style              = true
  }}
}}
'''


def _recorded_backend(terraform_dir: Path) -> str:
    """Qué backend registró el último `init`: 's3', 'local', o '' si nunca corrió."""
    try:
        datos = json.loads((terraform_dir / _BACKEND_RECORD).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    return ((datos.get("backend") or {}).get("type") or "local").strip()


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
        # Nunca se inicializó: init limpio, sin nada que migrar.
        return True, []
    destino = "s3" if quiere_remoto else "local"
    if tenia != destino:
        return True, ["-migrate-state", "-force-copy"]
    return False, []


def state_key_for(user_id: str) -> str:
    """Ruta del state dentro del bucket. Un archivo por SA: sus entornos son
    independientes y nunca deben compartir state."""
    return f"{STATE_PREFIX}/{user_id or 'default'}/terraform.tfstate"
