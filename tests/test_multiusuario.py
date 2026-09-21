"""Dos SAs en la misma instancia: qué comparten y qué no.

La app hostea a varios SAs y le da a cada uno su propio workspace
(`data/users/<uid>/`) y su propia ⚙ Configuración. Nada de eso tenía un solo
test: no había ninguno que levantara la app con `APP_SECRET_KEY`, logueara a dos
usuarios distintos y mirara si de verdad están aislados.

Se escribió porque alguien reportó que "la pantalla de puesta en marcha se ve
distinta" en otra cuenta y la pregunta era si eso lo causaba el rol de admin. No:
en toda la SPA el rol solo decide si aparece el botón "Panel de control". Lo que
cambia es el workspace y la configuración, y eso es lo que fijan estos tests.
"""
import json

import pytest
from fastapi.testclient import TestClient

import auth
import main


@pytest.fixture
def instancia(tmp_path, monkeypatch):
    """App con auth prendida, dos usuarios y un template de terraform mínimo."""
    monkeypatch.setattr(auth, "_SECRET", b"secreto-de-test")
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "SECURE_COOKIES", False)
    raiz = tmp_path / "data"
    raiz.mkdir(exist_ok=True)
    monkeypatch.setattr(auth, "DATA_ROOT", raiz)
    monkeypatch.setattr(auth, "_ADMINS_FILE", raiz / "admins.json")
    monkeypatch.setattr(auth, "_USERS_FILE", raiz / "users.json", raising=False)
    plantilla = tmp_path / "plantilla"
    plantilla.mkdir()
    (plantilla / "main.tf").write_text("# vacío", encoding="utf-8")
    monkeypatch.setattr(auth, "TERRAFORM_TEMPLATE", plantilla)
    monkeypatch.setenv("SA_ADMINS", "jefa@acme.com")     # admin explícito

    auth.create_user("jefa@acme.com", "hunter2")
    auth.create_user("nuevo@acme.com", "hunter2")

    def como(email):
        c = TestClient(main.app)
        c.cookies.set(auth.COOKIE_NAME, auth.make_session_token(email))
        return c

    return como


def test_la_configuracion_de_la_cuenta_no_se_comparte(instancia):
    """Las AK/SK, el bucket y los IDs de infra son POR USUARIO: un SA nuevo no
    hereda los del admin. Es la causa más probable de que la app "se vea
    distinta" en otra cuenta."""
    jefa, nuevo = instancia("jefa@acme.com"), instancia("nuevo@acme.com")

    jefa.post("/api/v1/settings/huawei", json={"demo_bucket": "demoscss", "vpc_id": "vpc-1"})

    assert jefa.get("/api/v1/settings/huawei").json()["values"]["demo_bucket"] == "demoscss"
    assert nuevo.get("/api/v1/settings/huawei").json()["values"].get("demo_bucket") in (None, "")


def test_el_entorno_de_uno_no_aparece_en_el_otro(instancia, monkeypatch):
    """Cada usuario tiene su propio `terraform/`, con su propio state y su propio
    marcador de deploy."""
    jefa, nuevo = instancia("jefa@acme.com"), instancia("nuevo@acme.com")
    ws = auth.build_user_ctx("jefa@acme.com").terraform_dir
    (ws / main._PLATFORM_MARKER_NAME).write_text(
        json.dumps({"deployed_at": "2026-09-18T17:35:27+00:00", "project_name": "log-analytics"}))
    (ws / "terraform.tfstate").write_text(json.dumps({
        "version": 4, "resources": [{"mode": "managed", "type": "huaweicloud_css_cluster",
                                     "name": "c", "instances": [{"attributes": {"id": "x"}}]}]}))
    monkeypatch.setattr(main.subprocess, "run",
                        lambda *a, **kw: type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})())

    assert jefa.get("/api/v1/terraform/status").json()["active"] is True
    assert nuevo.get("/api/v1/terraform/status").json()["active"] is False


def test_solo_el_admin_entra_al_panel_de_control(instancia):
    jefa, nuevo = instancia("jefa@acme.com"), instancia("nuevo@acme.com")

    assert jefa.get("/api/v1/admin/users").status_code == 200
    assert nuevo.get("/api/v1/admin/users").status_code == 403
    assert jefa.get("/auth/me").json()["is_admin"] is True
    assert nuevo.get("/auth/me").json()["is_admin"] is False


def test_ningun_endpoint_del_wizard_pide_admin(instancia):
    """Desplegar, configurar y operar el entorno NO son cosa de admins: si algún
    día alguien agrega un `_require_admin` ahí, medio equipo queda afuera."""
    nuevo = instancia("nuevo@acme.com")

    for ruta in ("/api/v1/terraform/status", "/api/v1/settings/huawei",
                 "/api/v1/verticals", "/api/v1/pipelines/health"):
        assert nuevo.get(ruta).status_code != 403, ruta


def test_la_vista_de_infra_no_mira_el_rol():
    """La afirmación que se le dio al usuario: la pantalla de puesta en marcha no
    cambia por ser admin. Lo único que el rol decide en toda la SPA es si
    aparece el botón "Panel de control" en el nav."""
    import pathlib
    import re

    html = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    i = html.index("function renderInfraView(")
    vista = html[i:html.index("async function hydrateActiveEnv(", i)]

    assert not re.search(r"is_admin|isAdmin", vista), "la vista de infra empezó a mirar el rol"


def test_una_cuenta_a_medio_configurar_se_entera_de_que_le_falta(instancia):
    """Un SA nuevo veía la misma pantalla vacía que uno que ya desplegó y
    destruyó, sin una palabra sobre lo que le falta."""
    nuevo = instancia("nuevo@acme.com")

    faltan = nuevo.get("/api/v1/terraform/status").json()["missing_settings"]

    assert "Access Key / Secret Key de OBS" in faltan
    assert "Bucket de demos y tfstate" in faltan
    assert any("VPC" in f for f in faltan)


def test_un_state_remoto_ilegible_no_se_reporta_como_sin_entorno(instancia, monkeypatch):
    """El modo de falla más engañoso: con las AK/SK del bucket rotadas, el state
    en OBS no se puede leer y la app decía "No tenés ningún entorno levantado"
    mientras los clusters seguían vivos y facturando."""
    import tfstate

    jefa = instancia("jefa@acme.com")
    ws = auth.build_user_ctx("jefa@acme.com").terraform_dir
    (ws / main._PLATFORM_MARKER_NAME).write_text(json.dumps({"project_name": "log-analytics"}))
    monkeypatch.setattr(tfstate, "has_resources", lambda _d: False)
    monkeypatch.setattr(tfstate, "error_remoto",
                        lambda _d: "error configuring S3 Backend: InvalidAccessKeyId")

    body = jefa.get("/api/v1/terraform/status").json()

    assert body["active"] is False
    assert "InvalidAccessKeyId" in body["state_error"]


def test_sin_marcador_no_se_inventa_un_error_de_state(instancia, monkeypatch):
    """Una cuenta que nunca desplegó no tiene nada que leer: ahí "no tenés
    entorno" es la respuesta correcta y no hay que alarmar."""
    import tfstate

    nuevo = instancia("nuevo@acme.com")
    monkeypatch.setattr(tfstate, "error_remoto", lambda _d: "lo que sea")

    assert nuevo.get("/api/v1/terraform/status").json()["state_error"] is None
