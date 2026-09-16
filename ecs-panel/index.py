# -*- coding:utf-8 -*-
"""Prende y apaga la ECS que hostea la plataforma, manejando sus puertos.

Reemplaza a la función que sólo hacía start/stop por timer. Agrega:

  1. Apertura/cierre de los puertos 80 y 443 en el security group, atado al
     encendido y al apagado.
  2. Consulta de estado, para que el frente pueda mostrar si está prendida.
  3. Una invocación directa (`{"action": ...}`), por donde entra el frente web.

**El contrato del timer se mantiene**: un evento con `user_event` =
`"<id>,<startup|shutdown>"` hace lo mismo que antes, así que los timers ya
configurados siguen funcionando sin tocarlos — pero ahora también manejan los
puertos.

Acá NO hay página ni login. FunctionGraph no ofrece ninguna puerta HTTP gratis
(el APIG compartido está dado de baja y APIC es sólo AP-Singapore), así que el
frente vive afuera —ver `worker.js`— y es él quien autentica al usuario. Lo que
protege a esta función es la credencial IAM que hace falta para invocarla.

Se sube pegándolo en la consola: un archivo, sin dependencias fuera de la stdlib.

**Red: public access, NO VPC access.** Sólo habla con las APIs de control de
Huawei, que son públicas; adentro de una VPC quedaría sin salida a internet y
todas las llamadas darían timeout.

Configuración, en Environment/User Data de la función:

    project_id   (ya existía)
    region       (ya existía)
    ecs_id       instancia a manejar
    sg_id        security group donde se abren/cierran los puertos
    app_url      opcional — URL de la plataforma, para el link del frente
    ports        opcional, default "80,443"
    source_cidr  opcional, default "0.0.0.0/0"
    rule_marker  opcional, default "ecs-panel:auto"

La agency de la función necesita permisos de **ECS** (start/stop/query) y de
**VPC** (crear, listar y borrar reglas de security group).
"""
import json
import urllib.error
import urllib.parse
import urllib.request

# Generoso para una API de control, pero acotado: un `start` encadena hasta
# cuatro llamadas y el timeout de la función es de 60 s.
HTTP_TIMEOUT = 15

# Estados que cuentan como "prendida" / "apagada".
#
# Ojo con `status` a secas: en la API de ECS **no cambia durante la transición**.
# Un `os-start` deja la instancia reportando SHUTOFF con
# `OS-EXT-STS:task_state: "powering-on"` hasta que termina, y un `os-stop` la deja
# en ACTIVE con `powering-off`. El panel leía solo `status`, así que 1,5 s después
# de apretar el botón veía el estado viejo, lo daba por definitivo y **cortaba el
# polling** — se quedaba mostrando "Apagada" para siempre. `task_state` es el único
# campo que distingue "apagada" de "arrancando".
ON, OFF, TRANSICION = "ACTIVE", "SHUTOFF", "TRANSICION"


class PanelError(Exception):
    """Error con mensaje mostrable al usuario."""


# ── Configuración ────────────────────────────────────────────────────────────
def _cfg(context):
    """Lee la config del User Data. Falla temprano si falta algo obligatorio."""
    get = context.getUserData
    cfg = {
        "project_id": get("project_id"),
        "region": get("region"),
        "ecs_id": get("ecs_id"),
        "sg_id": get("sg_id"),
        "ports": get("ports") or "80,443",
        "cidr": get("source_cidr") or "0.0.0.0/0",
        "marker": get("rule_marker") or "ecs-panel:auto",
        # El frente se sirve desde otro dominio, no desde la ECS: allá el link a
        # la plataforma no se puede derivar de `location`, hay que declararlo acá.
        "app_url": get("app_url") or "",
    }
    faltan = [k for k in ("project_id", "region", "ecs_id", "sg_id") if not cfg[k]]
    if faltan:
        raise PanelError("Falta configurar en User Data: " + ", ".join(faltan))
    return cfg


# ── Llamadas a las APIs de Huawei ────────────────────────────────────────────
def _api(method, url, token, body=None):
    """Request autenticado con el token IAM que da la agency. Devuelve dict."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "X-Auth-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            raw = res.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detalle = exc.read().decode("utf-8", "replace")[:300]
        raise PanelError("La API de Huawei respondió %d: %s" % (exc.code, detalle))
    except Exception as exc:
        raise PanelError("No se pudo llamar a la API: %s" % exc)


def _ecs_url(cfg, sufijo=""):
    return "https://ecs.%s.myhuaweicloud.com/v1/%s/cloudservers%s" % (
        cfg["region"], cfg["project_id"], sufijo)


def _vpc_url(cfg, sufijo=""):
    return "https://vpc.%s.myhuaweicloud.com/v3/%s/vpc/security-group-rules%s" % (
        cfg["region"], cfg["project_id"], sufijo)


def ecs_status(cfg, token):
    """Estado efectivo de la instancia: ON, OFF, TRANSICION o el crudo de la API.

    Combina `status` con `OS-EXT-STS:task_state`. Si hay task_state, hay una
    operación en curso y eso gana sobre el `status`, que todavía informa el estado
    de partida — que es exactamente lo que hacía que el panel se congelara.
    """
    servidor = (_api("GET", _ecs_url(cfg, "/" + cfg["ecs_id"]), token)
                .get("server") or {})
    tarea = (servidor.get("OS-EXT-STS:task_state") or "").strip()
    if tarea:
        return TRANSICION
    estado = servidor.get("status", "UNKNOWN")
    # BUILD/REBOOT/HARD_REBOOT tampoco son estados estables: el frente tiene que
    # seguir poleando igual que con un task_state.
    if estado in ("BUILD", "REBOOT", "HARD_REBOOT", "RESIZE", "MIGRATING"):
        return TRANSICION
    return estado


def ecs_action(cfg, token, arrancar, ecs_ids=None):
    """Arranca o detiene. Asíncrono: devuelve job_id, no espera a que termine.

    El apagado va en SOFT (ACPI ordenado), NO en HARD como hacía la versión
    anterior. Un HARD es desenchufar la máquina, y ahí adentro viven el
    `users.json`, los settings cifrados de cada SA, los casos creados y el
    registro de pipelines. Cortarla en medio de una escritura los puede dejar
    corruptos.
    """
    ids = ecs_ids or [cfg["ecs_id"]]
    servers = [{"id": i} for i in ids]
    payload = ({"os-start": {"servers": servers}} if arrancar
               else {"os-stop": {"type": "SOFT", "servers": servers}})
    res = _api("POST", _ecs_url(cfg, "/action"), token, payload)
    return res.get("job_id", "")


# ── Reglas del security group ────────────────────────────────────────────────
# El SG es el de la ECS: tiene la regla de SSH y lo que haya puesto Terraform.
# Por eso el panel SOLO puede borrar reglas que él mismo creó, y las reconoce por
# el `description` marcador. Nunca borra por puerto ni por posición: un match por
# puerto se llevaría puesta una regla puesta a mano, y uno por posición es una
# bomba de tiempo.
def _own_rules(cfg, token):
    """Reglas del SG creadas por el panel (las marcadas)."""
    url = _vpc_url(cfg, "?" + urllib.parse.urlencode(
        {"security_group_id": cfg["sg_id"], "limit": 2000}))
    reglas = _api("GET", url, token).get("security_group_rules") or []
    return [r for r in reglas if (r.get("description") or "") == cfg["marker"]]


def ports_open(cfg, token):
    """True si el panel tiene su regla puesta."""
    return bool(_own_rules(cfg, token))


def open_ports(cfg, token):
    """Abre los puertos. Idempotente: si ya están, no duplica la regla."""
    if _own_rules(cfg, token):
        return False
    _api("POST", _vpc_url(cfg), token, {"security_group_rule": {
        "security_group_id": cfg["sg_id"],
        "direction": "ingress",
        "ethertype": "IPv4",
        "protocol": "tcp",
        "multiport": cfg["ports"],
        "remote_ip_prefix": cfg["cidr"],
        "description": cfg["marker"],
    }})
    return True


def close_ports(cfg, token):
    """Borra las reglas del panel. Idempotente: sin reglas propias, no hace nada."""
    borradas = 0
    for regla in _own_rules(cfg, token):
        _api("DELETE", _vpc_url(cfg, "/" + regla["id"]), token)
        borradas += 1
    return borradas


# ── Las dos operaciones, con el orden que importa ────────────────────────────
def do_start(cfg, token, ecs_ids=None):
    """Abrir puertos PRIMERO, después arrancar.

    Caddy pide/renueva el certificado de Let's Encrypt al bootear y necesita el
    80/443 alcanzable: si arranca con los puertos cerrados, el challenge ACME
    falla y la plataforma queda sin HTTPS.
    """
    open_ports(cfg, token)
    ecs_action(cfg, token, True, ecs_ids)
    return "Encendiendo. Tarda ~40-60 s en estar disponible."


def do_stop(cfg, token, ecs_ids=None):
    """Detener PRIMERO, después cerrar puertos.

    Al revés, si el stop falla quedaría una máquina encendida e inalcanzable.
    """
    ecs_action(cfg, token, False, ecs_ids)
    close_ports(cfg, token)
    return "Apagando y cerrando los puertos."


def run_action(accion, cfg, token, logger):
    """Las tres acciones que entiende el frente."""
    if accion == "status":
        return {"ecs": ecs_status(cfg, token), "ports": ports_open(cfg, token),
                "app_url": cfg["app_url"]}

    estado = ecs_status(cfg, token)

    # Mientras hay una operación en curso no se manda otra: un segundo os-start
    # sobre una instancia que ya está arrancando devuelve error de la API.
    if estado == TRANSICION:
        return {"ok": True, "message": "Hay una operación en curso, esperá.",
                "ecs": estado}

    # Guarda contra el doble toque, pero SIN saltarse los puertos.
    #
    # Antes esto devolvía temprano y no tocaba el security group. El agujero: si
    # `do_start` abría los puertos y después fallaba el arranque, quedabas en
    # "apagada + puertos abiertos" y **no había forma de cerrarlos desde el panel**
    # — apretar Apagar respondía "ya estaba apagada" y no hacía nada. Lo mismo si
    # alguien apagaba la ECS desde la consola de Huawei. Ahora la acción siempre
    # reconcilia el SG con el estado deseado, aunque la ECS ya esté donde toca.
    if accion == "start" and estado == ON:
        abrio = open_ports(cfg, token)
        return {"ok": True, "ecs": estado, "message": (
            "Ya estaba encendida; abrí los puertos." if abrio else "Ya estaba encendida.")}
    if accion == "stop" and estado == OFF:
        cerradas = close_ports(cfg, token)
        return {"ok": True, "ecs": estado, "message": (
            "Ya estaba apagada; cerré los puertos." if cerradas else "Ya estaba apagada.")}

    logger.info("acción %s (estado actual %s)", accion, estado)
    mensaje = do_start(cfg, token) if accion == "start" else do_stop(cfg, token)
    # `ecs` va como TRANSICION, no como el estado previo: la acción ya se disparó,
    # así que informar el estado de partida es decirle al frente algo que dejó de
    # ser cierto en el momento mismo de responder.
    return {"ok": True, "message": mensaje, "ecs": TRANSICION}


def _as_dict(event):
    """El evento como dict, venga como venga.

    Defensivo, no correctivo: la documentación dice que el body llega parseado y
    en la práctica así fue. Pero `event.get(...)` sobre cualquier otra cosa tira
    un AttributeError que la consola muestra como stacktrace, y este handler
    atiende tres orígenes distintos (timer, invocación directa, y un gateway si
    algún día lo hay). Normalizar cuesta cuatro líneas.
    """
    if isinstance(event, (str, bytes)):
        try:
            event = json.loads(event)
        except (ValueError, TypeError):
            return {}
    return event if isinstance(event, dict) else {}


# ── Handler ──────────────────────────────────────────────────────────────────
def handler(event, context):
    logger = context.getLogger()
    # El evento crudo, como llega. Vale el ruido: sin esto, una URN mal apuntada
    # se manifestó como un "parameters invalid." —el string que devolvía la
    # función VIEJA— y costó varias hipótesis equivocadas darse cuenta de que lo
    # que respondía no era este código.
    logger.info("event (%s): %r", type(event).__name__, event)
    event = _as_dict(event)

    # Dos formas de llegar, y cada una espera una respuesta distinta:
    #   action      → invocación directa desde el frente: dict JSON.
    #   user_event  → timer: string, como la función original.
    directa = bool(event.get("action"))

    try:
        cfg = _cfg(context)
    except PanelError as exc:
        logger.error("configuración inválida: %s", exc)
        return {"error": str(exc)} if directa else "parameters invalid."

    token = context.getToken()
    if not token:
        logger.error("sin token IAM: ¿la función tiene una agency asignada?")
        msg = "No hay token IAM. Asigná una agency con permisos de ECS y VPC."
        return {"error": msg} if directa else "authentication failed"

    if not directa:
        return _handle_timer(event, cfg, token, logger)

    accion = (event.get("action") or "").strip().lower()
    if accion not in ("status", "start", "stop"):
        return {"error": "Acción desconocida: %r. Usá status, start o stop." % accion}
    try:
        return run_action(accion, cfg, token, logger)
    except PanelError as exc:
        logger.error("error del panel: %s", exc)
        return {"error": str(exc)}
    except Exception as exc:
        logger.exception("error inesperado: %s", exc)
        return {"error": "Error inesperado: %s" % exc}


def _handle_timer(event, cfg, token, logger):
    """Encendido/apagado programado. Mismo `user_event` que la función anterior,
    pero ahora también abre y cierra los puertos."""
    try:
        ecs_ids, operacion = parse_user_event(event.get("user_event", ""))
    except ValueError as exc:
        logger.error("user_event inválido: %s", exc)
        return "parameters invalid."
    if not ecs_ids:
        logger.warning("user_event sin IDs de ECS")
        return "nothing to do"

    logger.info("timer: %s sobre %s", operacion, ecs_ids)
    try:
        if operacion == "startup":
            do_start(cfg, token, ecs_ids)
        elif operacion == "shutdown":
            do_stop(cfg, token, ecs_ids)
        else:
            logger.warning("operación no soportada: %s", operacion)
            return "nothing to do"
    except PanelError as exc:
        logger.error("%s falló: %s", operacion, exc)
        return "%s request failed" % operacion
    return "%s ecs servers %s initiated." % (operacion, ecs_ids)


def parse_user_event(payload):
    """'id1,id2,shutdown' → (['id1','id2'], 'shutdown'). Igual que antes."""
    if not payload:
        raise ValueError("user_event está vacío")
    servidores, sep, operacion = payload.rpartition(",")
    if not sep:
        raise ValueError("user_event debe terminar con ',<operation>'")
    ids = [i.strip() for i in servidores.split(",") if i.strip()]
    operacion = operacion.strip().lower()
    if not operacion:
        raise ValueError("operación no especificada")
    return ids, operacion
