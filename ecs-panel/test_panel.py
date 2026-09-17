# -*- coding:utf-8 -*-
"""Tests de la función de FunctionGraph.

Se concentran en lo que puede romper en silencio y salir caro:

  - el estado en vivo: que una ECS arrancando NO se reporte como apagada (es lo
    que congelaba el panel),
  - que no se dispare una acción con otra en curso, ni se re-apague/re-arranque,
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
    "app_url": "",
}


class FakeAPI:
    """Doble de `_api`: registra las llamadas y responde lo que se le configure."""

    def __init__(self, status="SHUTOFF"):
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
        if url.endswith("/action"):
            return {"job_id": "job-1"}
        if self.secuencia:
            self.status, self.task_state = (self.secuencia.pop(0) if len(self.secuencia) > 1
                                            else self.secuencia[0])
        return {"server": {"status": self.status,
                           "OS-EXT-STS:task_state": self.task_state}}

    @property
    def acciones(self):
        """Los payloads de los POST a /action, en orden."""
        return [b for m, u, b in self.calls if u.endswith("/action")]

    @property
    def urls(self):
        return [u for _, u, _ in self.calls]


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(panel, "_api", fake)
    return fake


class FakeContext:
    def __init__(self, token="tok"):
        self._d = {
            "project_id": CFG["project_id"], "region": CFG["region"],
            "ecs_id": CFG["ecs_id"], "app_url": CFG["app_url"],
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


# ── Las dos acciones ─────────────────────────────────────────────────────────
def test_stop_usa_soft_no_hard(api):
    """La versión anterior usaba HARD, que es desenchufar la máquina."""
    panel.do_stop(CFG, "tok")

    [payload] = api.acciones
    assert payload["os-stop"]["type"] == "SOFT"
    assert "HARD" not in str(payload)


def test_start_manda_os_start_con_el_id(api):
    panel.do_start(CFG, "tok")

    assert api.acciones == [{"os-start": {"servers": [{"id": CFG["ecs_id"]}]}}]


def test_la_funcion_solo_habla_con_ecs(api):
    """Una versión anterior también abría y cerraba puertos en el security group.
    Se fue: una ECS apagada no responde a nada, con los puertos como estén. Si
    esto vuelve a tocar VPC, la agency vuelve a necesitar esos permisos y cada
    consulta de estado vuelve a costar dos llamadas."""
    panel.handler({"action": "status"}, FakeContext())
    panel.handler({"action": "start"}, FakeContext())
    api.status = "ACTIVE"
    panel.handler({"action": "stop"}, FakeContext())

    assert api.urls, "no llamó a nada"
    assert all(u.startswith("https://ecs.") for u in api.urls), api.urls


# ── Contrato del timer (no romper los triggers ya configurados) ──────────────
def test_parse_user_event_igual_que_antes():
    assert panel.parse_user_event("id1,id2,shutdown") == (["id1", "id2"], "shutdown")
    assert panel.parse_user_event("%s,startup" % CFG["ecs_id"]) == (
        [CFG["ecs_id"]], "startup")


@pytest.mark.parametrize("malo", ["", "sin-coma"])
def test_parse_user_event_invalido(malo):
    with pytest.raises(ValueError):
        panel.parse_user_event(malo)


def test_timer_shutdown_apaga(api):
    r = panel.handler({"user_event": "%s,shutdown" % CFG["ecs_id"]}, FakeContext())

    assert isinstance(r, str) and "initiated" in r
    assert [list(p)[0] for p in api.acciones] == ["os-stop"]


def test_timer_startup_enciende(api):
    r = panel.handler({"user_event": "%s,startup" % CFG["ecs_id"]}, FakeContext())

    assert isinstance(r, str)
    assert [list(p)[0] for p in api.acciones] == ["os-start"]


def test_timer_con_varios_ids_los_manda_todos(api):
    """El timer viejo aceptaba `id1,id2,shutdown`; se mantiene."""
    panel.handler({"user_event": "id-a,id-b,shutdown"}, FakeContext())

    [payload] = api.acciones
    assert payload["os-stop"]["servers"] == [{"id": "id-a"}, {"id": "id-b"}]


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

    assert panel.handler({"action": "status"}, FakeContext()) == {
        "ecs": "ACTIVE", "app_url": ""}


def test_status_es_una_sola_llamada(api):
    """El frente poletea cada 5 s: cada llamada de más se paga en latencia. Con el
    manejo de puertos eran dos (ECS + VPC)."""
    panel.handler({"action": "status"}, FakeContext())

    assert len(api.calls) == 1


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
    assert [list(p)[0] for p in api.acciones] == ["os-start"]


def test_directa_stop(api):
    api.status = "ACTIVE"

    r = panel.handler({"action": "stop"}, FakeContext())

    assert r["ok"] is True
    assert [list(p)[0] for p in api.acciones] == ["os-stop"]


def test_start_sobre_una_maquina_encendida_no_la_rearranca(api):
    api.status = "ACTIVE"

    r = panel.handler({"action": "start"}, FakeContext())

    assert r["ok"] is True and "Ya estaba" in r["message"]
    assert api.acciones == []


def test_stop_sobre_una_maquina_apagada_no_la_reapaga(api):
    api.status = "SHUTOFF"

    r = panel.handler({"action": "stop"}, FakeContext())

    assert r["ok"] is True and "Ya estaba" in r["message"]
    assert api.acciones == []


def test_no_se_dispara_una_accion_con_otra_en_curso(api):
    """Un segundo os-start sobre una instancia que ya está arrancando es un error
    de la API. El panel rebotaba el botón durante el powering-on, así que pasaba."""
    api.status = "SHUTOFF"
    api.task_state = "powering-on"

    r = panel.handler({"action": "start"}, FakeContext())

    assert r["ecs"] == panel.TRANSICION
    assert "en curso" in r["message"]
    assert api.acciones == []


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
    ctx._d["region"] = None

    assert "region" in panel.handler({"action": "status"}, ctx)["error"]
    assert api.calls == []


def test_la_config_del_sg_ya_no_es_obligatoria(api):
    """Una función configurada con `sg_id` y compañía sigue andando (se ignoran),
    y una nueva no tiene por qué declararlos."""
    ctx = FakeContext()
    ctx._d.update({"sg_id": "sg-viejo", "ports": "80,443", "rule_marker": "x"})
    assert "error" not in panel.handler({"action": "status"}, ctx)

    assert "sg_id" not in FakeContext()._d
    assert "error" not in panel.handler({"action": "status"}, FakeContext())


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
    assert [list(p)[0] for p in api.acciones] == ["os-start"]


@pytest.mark.parametrize("basura", [None, [], 42, "no es json", '"solo un string"'])
def test_eventos_sin_forma_no_rompen(basura, api):
    """Cualquier cosa rara cae a la rama del timer y devuelve su string, sin
    levantar una excepción que la consola muestre como un stacktrace."""
    assert isinstance(panel.handler(basura, FakeContext()), str)
    assert api.calls == []
