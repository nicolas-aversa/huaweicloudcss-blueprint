# -*- coding:utf-8 -*-
"""Tests de la función de FunctionGraph.

Se concentran en lo que puede romper en silencio y salir caro:

  - que el cierre de puertos NO toque reglas ajenas (si borra la de SSH, te
    quedás afuera de la máquina),
  - el orden de las operaciones (puertos antes de encender, apagar antes de
    cerrar),
  - que el contrato del timer siga andando igual que en la función anterior.

No hablan con Huawei: `_api` se reemplaza por un doble que registra las llamadas.
"""
import importlib.util
import pathlib

import pytest

# El módulo se llama index.py (nombre que espera FunctionGraph), no importable
# por nombre: se carga por path.
_spec = importlib.util.spec_from_file_location(
    "ecs_panel", pathlib.Path(__file__).with_name("index.py"))
panel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(panel)


CFG = {
    "project_id": "proj-1", "region": "la-south-2",
    "ecs_id": "a0b8d54a-9153-482a-9ff3-52ed6e03c88e",
    "sg_id": "e7fbf08c-7864-479d-98fe-d01aa4a2077a",
    "ports": "80,443", "cidr": "0.0.0.0/0", "marker": "ecs-panel:auto",
    "app_url": "",
}


class FakeAPI:
    """Doble de `_api`: registra las llamadas y responde lo que se le configure."""

    def __init__(self, rules=None, status="SHUTOFF"):
        self.rules = list(rules or [])
        self.status = status
        # `OS-EXT-STS:task_state`. Es el campo que distingue "apagada" de
        # "arrancando": durante un os-start el `status` sigue diciendo SHUTOFF.
        # El doble no lo tenía, así que la transición era intesteable y el bug de
        # "el panel se congela" no lo podía atrapar ningún test.
        self.task_state = ""
        # Estados sucesivos que devuelve el GET, para simular una ECS que tarda:
        # cada llamada consume uno; cuando se acaban, queda el último.
        self.secuencia = None
        self.calls = []          # [(method, url, body), ...]

    def __call__(self, method, url, token, body=None):
        self.calls.append((method, url, body))
        if "security-group-rules" in url:
            if method == "GET":
                return {"security_group_rules": self.rules}
            if method == "POST":
                nueva = dict(body["security_group_rule"], id="nueva-1")
                self.rules.append(nueva)
                return {"security_group_rule": nueva}
            if method == "DELETE":
                rid = url.rsplit("/", 1)[-1]
                self.rules = [r for r in self.rules if r["id"] != rid]
                return {}
        if url.endswith("/action"):
            return {"job_id": "job-1"}
        if self.secuencia:
            self.status, self.task_state = (self.secuencia.pop(0) if len(self.secuencia) > 1
                                            else self.secuencia[0])
        return {"server": {"status": self.status,
                           "OS-EXT-STS:task_state": self.task_state}}

    @property
    def verbos(self):
        """Secuencia de (método, recurso) para chequear el ORDEN."""
        out = []
        for metodo, url, _ in self.calls:
            recurso = "sg" if "security-group-rules" in url else (
                "ecs-action" if url.endswith("/action") else "ecs-get")
            out.append((metodo, recurso))
        return out


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(panel, "_api", fake)
    return fake


class FakeContext:
    def __init__(self, token="tok"):
        self._d = {
            "project_id": CFG["project_id"], "region": CFG["region"],
            "ecs_id": CFG["ecs_id"], "sg_id": CFG["sg_id"],
            "ports": CFG["ports"], "source_cidr": CFG["cidr"],
            "rule_marker": CFG["marker"], "app_url": CFG["app_url"],
        }
        self._token = token

    def getUserData(self, k):
        return self._d.get(k)

    def getToken(self):
        return self._token

    def getLogger(self):
        class _L:
            def __getattr__(self, _):
                return lambda *a, **k: None
        return _L()


# ── Lo crítico: no borrar reglas ajenas ──────────────────────────────────────
# Un SG real tiene la regla de SSH, las que puso Terraform para Beats/CSS, y
# posiblemente una 443 que alguien agregó a mano. Si el panel las borra, rompe la
# infra o te deja sin acceso.
AJENAS = [
    {"id": "ssh", "multiport": "22", "description": "acceso SSH"},
    {"id": "beats", "multiport": "5044", "description": "terraform: beats"},
    # La trampa: MISMOS puertos que el panel, pero puesta a mano.
    {"id": "manual", "multiport": "80,443", "description": "puesta a mano"},
    # Otra trampa: sin description (la API devuelve "" o None).
    {"id": "sin-desc", "multiport": "443", "description": None},
]
PROPIA = {"id": "del-panel", "multiport": "80,443", "description": "ecs-panel:auto"}


def test_close_ports_solo_borra_las_propias(api):
    api.rules = AJENAS + [PROPIA]

    borradas = panel.close_ports(CFG, "tok")

    assert borradas == 1
    assert {r["id"] for r in api.rules} == {"ssh", "beats", "manual", "sin-desc"}
    # Y que el DELETE fue exactamente al id propio, no a otro.
    deletes = [u for m, u, _ in api.calls if m == "DELETE"]
    assert len(deletes) == 1 and deletes[0].endswith("/del-panel")


def test_close_ports_sin_reglas_propias_no_borra_nada(api):
    """Apagar dos veces seguidas, o apagar algo que nunca encendió el panel."""
    api.rules = list(AJENAS)

    assert panel.close_ports(CFG, "tok") == 0
    assert not [m for m, _, _ in api.calls if m == "DELETE"]
    assert len(api.rules) == 4


def test_marker_configurable_no_pisa_el_default(api):
    """Con otro marcador, una regla del marcador default es ajena."""
    api.rules = [PROPIA, {"id": "otro", "description": "mi-marcador"}]

    assert panel.close_ports(dict(CFG, marker="mi-marcador"), "tok") == 1
    assert {r["id"] for r in api.rules} == {"del-panel"}


# ── Idempotencia al abrir ────────────────────────────────────────────────────
def test_open_ports_no_duplica(api):
    assert panel.open_ports(CFG, "tok") is True
    assert panel.open_ports(CFG, "tok") is False
    assert len([r for r in api.rules if r["description"] == CFG["marker"]]) == 1


def test_open_ports_manda_la_regla_correcta(api):
    panel.open_ports(CFG, "tok")

    regla = [b for m, _, b in api.calls if m == "POST"][0]["security_group_rule"]
    assert regla == {
        "security_group_id": CFG["sg_id"], "direction": "ingress",
        "ethertype": "IPv4", "protocol": "tcp", "multiport": "80,443",
        "remote_ip_prefix": "0.0.0.0/0", "description": "ecs-panel:auto"}


def test_open_ports_ignora_una_regla_ajena_con_los_mismos_puertos(api):
    """Que alguien haya abierto el 80/443 a mano no exime al panel de poner la
    suya: si no, al apagar no tendría qué borrar y quedaría abierto."""
    api.rules = [{"id": "manual", "multiport": "80,443", "description": "a mano"}]

    assert panel.open_ports(CFG, "tok") is True


# ── El orden de las operaciones ──────────────────────────────────────────────
def test_start_abre_puertos_antes_de_encender(api):
    """Caddy pide el cert de Let's Encrypt al bootear: con el 80/443 cerrado el
    challenge ACME falla y la plataforma queda sin HTTPS."""
    panel.do_start(CFG, "tok")

    assert api.verbos == [("GET", "sg"), ("POST", "sg"), ("POST", "ecs-action")]


def test_stop_apaga_antes_de_cerrar_puertos(api):
    """Al revés, si el stop falla queda una máquina encendida e inalcanzable."""
    api.rules = [PROPIA]

    panel.do_stop(CFG, "tok")

    assert api.verbos == [("POST", "ecs-action"), ("GET", "sg"), ("DELETE", "sg")]


def test_stop_usa_soft_no_hard(api):
    """La versión anterior usaba HARD, que es desenchufar la máquina."""
    panel.do_stop(CFG, "tok")

    payload = [b for m, u, b in api.calls if u.endswith("/action")][0]
    assert payload["os-stop"]["type"] == "SOFT"
    assert "HARD" not in str(payload)


def test_start_manda_os_start_con_el_id(api):
    panel.do_start(CFG, "tok")

    payload = [b for m, u, b in api.calls if u.endswith("/action")][0]
    assert payload == {"os-start": {"servers": [{"id": CFG["ecs_id"]}]}}


# ── Contrato del timer (no romper los triggers ya configurados) ──────────────
def test_parse_user_event_igual_que_antes():
    assert panel.parse_user_event("id1,id2,shutdown") == (["id1", "id2"], "shutdown")
    assert panel.parse_user_event("%s,startup" % CFG["ecs_id"]) == (
        [CFG["ecs_id"]], "startup")


@pytest.mark.parametrize("malo", ["", "sin-coma"])
def test_parse_user_event_invalido(malo):
    with pytest.raises(ValueError):
        panel.parse_user_event(malo)


def test_timer_shutdown_apaga_y_cierra(api):
    api.rules = [PROPIA]

    r = panel.handler({"user_event": "%s,shutdown" % CFG["ecs_id"]}, FakeContext())

    assert isinstance(r, str) and "initiated" in r
    assert api.verbos == [("POST", "ecs-action"), ("GET", "sg"), ("DELETE", "sg")]


def test_timer_startup_abre_y_enciende(api):
    r = panel.handler({"user_event": "%s,startup" % CFG["ecs_id"]}, FakeContext())

    assert isinstance(r, str)
    assert api.verbos == [("GET", "sg"), ("POST", "sg"), ("POST", "ecs-action")]


def test_timer_con_payload_invalido_devuelve_string(api):
    assert panel.handler({"user_event": ""}, FakeContext()) == "parameters invalid."
    assert api.calls == []


def test_timer_con_operacion_desconocida_no_hace_nada(api):
    assert panel.handler({"user_event": "id1,reiniciar"},
                         FakeContext()) == "nothing to do"
    assert api.calls == []


def test_el_timer_reporta_los_errores_como_string(api):
    """El timer no puede devolver un dict: rompería el contrato anterior."""
    assert panel.handler({"user_event": "id1,startup"},
                         FakeContext(token=None)) == "authentication failed"

    ctx = FakeContext()
    ctx._d["ecs_id"] = None
    assert panel.handler({"user_event": "id1,startup"}, ctx) == "parameters invalid."


# ── Invocación directa (por donde entra el frente) ───────────────────────────
def test_directa_status(api):
    api.status = "ACTIVE"
    api.rules = [PROPIA]

    assert panel.handler({"action": "status"}, FakeContext()) == {
        "ecs": "ACTIVE", "ports": True, "app_url": ""}


# ── El estado en vivo ────────────────────────────────────────────────────────
# Estos tests no existían, y por eso el panel se podía congelar sin que nada se
# pusiera rojo: el doble tenía un `status` fijo, así que era estructuralmente
# imposible representar una ECS que tarda en arrancar.
def test_arrancando_no_se_reporta_como_apagada(api):
    """El bug exacto: durante el `powering-on` la API sigue diciendo SHUTOFF.
    Si eso llega al frente como "apagada", el frente lo toma por definitivo y
    corta el polling — se queda mostrando "Apagada" para siempre."""
    api.status = "SHUTOFF"
    api.task_state = "powering-on"

    assert panel.handler({"action": "status"}, FakeContext())["ecs"] == panel.TRANSICION


def test_apagando_no_se_reporta_como_encendida(api):
    """Simétrico: el os-stop es SOFT (ACPI) y tarda; el status sigue en ACTIVE."""
    api.status = "ACTIVE"
    api.task_state = "powering-off"

    assert panel.handler({"action": "status"}, FakeContext())["ecs"] == panel.TRANSICION


def test_los_estados_inestables_tambien_son_transicion(api):
    for estado in ("BUILD", "REBOOT", "HARD_REBOOT"):
        api.status, api.task_state = estado, ""
        assert panel.handler({"action": "status"}, FakeContext())["ecs"] == panel.TRANSICION


def test_start_reporta_transicion_no_el_estado_previo(api):
    """`start` devolvía el estado leído ANTES de disparar la acción — o sea que
    nacía mentiroso: para cuando el frente lo recibía, ya no era cierto."""
    api.status = "SHUTOFF"

    r = panel.handler({"action": "start"}, FakeContext())

    assert r["ecs"] == panel.TRANSICION, "no puede decir SHUTOFF justo después de arrancar"


def test_el_arranque_se_ve_completo_recien_cuando_lo_esta(api):
    """Recorrido real: apagada → arrancando → encendida. El estado solo se
    estabiliza en el último paso."""
    api.secuencia = [("SHUTOFF", ""), ("SHUTOFF", "powering-on"),
                     ("ACTIVE", "powering-on"), ("ACTIVE", "")]

    vistos = [panel.handler({"action": "status"}, FakeContext())["ecs"] for _ in range(4)]

    assert vistos == [panel.OFF, panel.TRANSICION, panel.TRANSICION, panel.ON]


def test_directa_expone_el_app_url_configurado(api):
    """El link a la plataforma no se puede derivar de `location` en el frente:
    ese vive en otro dominio."""
    ctx = FakeContext()
    ctx._d["app_url"] = "https://159.138.118.73.sslip.io"

    assert panel.handler({"action": "status"}, ctx)["app_url"] == \
        "https://159.138.118.73.sslip.io"


def test_directa_start(api):
    api.status = "SHUTOFF"

    r = panel.handler({"action": "start"}, FakeContext())

    assert r["ok"] is True
    assert api.verbos == [("GET", "ecs-get"), ("GET", "sg"), ("POST", "sg"),
                          ("POST", "ecs-action")]


def test_directa_stop(api):
    api.status = "ACTIVE"
    api.rules = [PROPIA]

    assert panel.handler({"action": "stop"}, FakeContext())["ok"] is True
    assert ("DELETE", "sg") in api.verbos


def test_start_sobre_una_maquina_encendida_no_la_rearranca(api):
    api.status = "ACTIVE"

    r = panel.handler({"action": "start"}, FakeContext())

    assert r["ok"] is True and "Ya estaba" in r["message"]
    assert not [c for c in api.calls if c[1].endswith("/action")]


def test_start_sobre_una_maquina_encendida_igual_abre_los_puertos(api):
    """Encendida pero inalcanzable (alguien la prendió por consola, o el cierre
    quedó a medias): el botón tiene que servir para eso."""
    api.status = "ACTIVE"
    api.rules = []

    r = panel.handler({"action": "start"}, FakeContext())

    assert not [c for c in api.calls if c[1].endswith("/action")]
    assert len(api.rules) == 1 and "abrí los puertos" in r["message"]


def test_stop_sobre_una_maquina_apagada_cierra_los_puertos(api):
    """**La expectativa cambió a propósito.** Este test afirmaba que un `stop`
    sobre una máquina ya apagada tampoco tocaba el SG, y eso dejaba un estado del
    que no se podía salir: si `do_start` abría los puertos y después fallaba el
    arranque, quedaba "apagada + puertos abiertos" y el botón Apagar respondía "ya
    estaba apagada" sin cerrar nada. La acción ahora reconcilia el SG siempre."""
    api.status = "SHUTOFF"
    api.rules = [PROPIA]

    r = panel.handler({"action": "stop"}, FakeContext())

    assert not [c for c in api.calls if c[1].endswith("/action")], "no re-apaga"
    assert api.rules == [], "los puertos SÍ se cierran"
    assert "cerré los puertos" in r["message"]


def test_no_se_dispara_una_accion_con_otra_en_curso(api):
    """Un segundo os-start sobre una instancia que ya está arrancando es un error
    de la API. El panel rebotaba el botón durante el powering-on, así que pasaba."""
    api.status = "SHUTOFF"
    api.task_state = "powering-on"

    r = panel.handler({"action": "start"}, FakeContext())

    assert r["ecs"] == panel.TRANSICION
    assert "en curso" in r["message"]
    assert not [c for c in api.calls if c[1].endswith("/action")]


def test_directa_devuelve_json_pelado(api):
    """El invoker recibe el return crudo: envolverlo en {statusCode, body} lo
    obligaría a desanidar y a parsear un JSON adentro de otro."""
    r = panel.handler({"action": "status"}, FakeContext())

    assert "statusCode" not in r and "body" not in r


def test_directa_accion_desconocida(api):
    r = panel.handler({"action": "reiniciar"}, FakeContext())

    assert "error" in r and "reiniciar" in r["error"]
    assert api.calls == []


def test_directa_normaliza_la_accion(api):
    api.status = "SHUTOFF"
    assert panel.handler({"action": "  START  "}, FakeContext())["ok"] is True


# ── Errores de configuración y de entorno ────────────────────────────────────
def test_falta_config_avisa_cual(api):
    ctx = FakeContext()
    ctx._d["sg_id"] = None

    assert "sg_id" in panel.handler({"action": "status"}, ctx)["error"]
    assert api.calls == []


def test_sin_agency_avisa_en_vez_de_fallar_feo(api):
    r = panel.handler({"action": "status"}, FakeContext(token=None))

    assert "agency" in r["error"]
    assert api.calls == []


def test_error_de_la_api_llega_como_error_json(monkeypatch):
    def explota(*a, **k):
        raise panel.PanelError("La API de Huawei respondió 403: denegado")
    monkeypatch.setattr(panel, "_api", explota)

    r = panel.handler({"action": "status"}, FakeContext())

    assert r == {"error": "La API de Huawei respondió 403: denegado"}


def test_un_error_inesperado_no_tumba_la_funcion(monkeypatch):
    def explota(*a, **k):
        raise RuntimeError("algo raro")
    monkeypatch.setattr(panel, "_api", explota)

    assert "algo raro" in panel.handler({"action": "status"}, FakeContext())["error"]


# ── El evento puede llegar como string ───────────────────────────────────────
# FunctionGraph entrega el body ya parseado o como el JSON crudo según por dónde
# entre. Con un string, `event.get(...)` no existe y la invocación directa se iba
# por la rama del timer, devolviendo "parameters invalid." sin más pistas.
def test_evento_como_string_json_se_parsea(api):
    api.status = "ACTIVE"

    r = panel.handler('{"action": "status"}', FakeContext())

    assert r["ecs"] == "ACTIVE"


def test_evento_como_bytes_tambien(api):
    api.status = "ACTIVE"

    assert panel.handler(b'{"action": "status"}', FakeContext())["ecs"] == "ACTIVE"


def test_el_timer_tambien_tolera_el_string(api):
    r = panel.handler('{"user_event": "%s,startup"}' % CFG["ecs_id"], FakeContext())

    assert "initiated" in r
    assert ("POST", "ecs-action") in api.verbos


@pytest.mark.parametrize("basura", [None, [], 42, "no es json", '"solo un string"'])
def test_eventos_sin_forma_no_rompen(basura, api):
    """Cualquier cosa rara cae a la rama del timer y devuelve su string, sin
    levantar una excepción que la consola muestre como un stacktrace."""
    assert isinstance(panel.handler(basura, FakeContext()), str)
    assert api.calls == []
