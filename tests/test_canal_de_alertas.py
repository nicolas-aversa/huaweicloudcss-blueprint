"""Que las alertas le lleguen a alguien: el canal de Notifications (Slack,
Teams o un webhook). Antes el monitor de anomalías alertaba en Alerting y no
avisaba a nadie: en una demo, "te llega el aviso" no se podía mostrar."""
import json
import pathlib

import pytest

import capabilities as caps
import exportar
import main
import maas_integrator as mi
import plugins_vista as pv


# ── El canal, en ⚙ Configuración ────────────────────────────────────────────
def test_se_guarda_y_la_url_no_vuelve():
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    assert c.get("/api/v1/settings/alertas").json() == {"configurado": False, "tipo": "", "host": ""}
    r = c.post("/api/v1/settings/alertas", json={"tipo": "slack", "url": "https://hooks.slack.com/services/T/B/SECRETO"})
    assert r.json() == {"configurado": True, "tipo": "slack", "host": "hooks.slack.com"}
    assert "SECRETO" not in r.text, "el token del webhook no vuelve al navegador"
    assert mi.get_canal_de_alertas() == {"tipo": "slack", "url": "https://hooks.slack.com/services/T/B/SECRETO"}
    assert c.post("/api/v1/settings/alertas", json={"tipo": "slack", "url": ""}).json()["configurado"] is False


@pytest.mark.parametrize("tipo, url", [("slack", "http://hooks.slack.com/x"), ("sms", "https://x"), ("webhook", "https://a b")])
def test_lo_que_no_sirve_se_rechaza(tipo, url):
    from fastapi.testclient import TestClient
    r = TestClient(main.app).post("/api/v1/settings/alertas", json={"tipo": tipo, "url": url})
    assert r.status_code == 400


# ── Builders ────────────────────────────────────────────────────────────────
def test_el_canal_por_tipo():
    s = caps.build_canal_de_alertas("slack", "https://h/x")
    assert s["config_id"] == "platform-alertas"
    assert s["config"]["config_type"] == "slack" and s["config"]["slack"] == {"url": "https://h/x"}
    w = caps.build_canal_de_alertas("webhook", "https://h/x")["config"]["webhook"]
    assert w == {"url": "https://h/x", "method": "POST", "header_params": {"Content-Type": "application/json"}}
    assert caps.build_canal_de_alertas("microsoft_teams", "https://h/x")["config"]["microsoft_teams"] == {"url": "https://h/x"}


def test_el_monitor_avisa_por_el_canal_una_vez_por_hora():
    sin = caps.build_monitor_de_anomalias("pozos", "D")
    assert sin["triggers"][0]["query_level_trigger"]["actions"] == []
    con = caps.build_monitor_de_anomalias("pozos", "D", canal_id="platform-alertas")
    (accion,) = con["triggers"][0]["query_level_trigger"]["actions"]
    assert accion["destination_id"] == "platform-alertas"
    assert accion["throttle_enabled"] and accion["throttle"] == {"value": 60, "unit": "MINUTES"}
    assert "{{ctx.results.0.hits.total.value}}" in accion["message_template"]["source"]


# ── Provisionar ─────────────────────────────────────────────────────────────
class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


def _cluster(monkeypatch, tmp_path, existe=False, prueba=200, crear=200):
    mi.set_canal_de_alertas("slack", "https://hooks.slack.com/services/T/B/X")
    pedidos, rutas, pasos = [], [], []

    def req(m, url, user, password, json_body=None, timeout=30):
        pedidos.append((m, url, json_body))
        if "/feature/test/" in url:
            return _R(200, {"event_source": {}, "status_list": [{"delivery_status": {
                "status_code": str(prueba), "status_text": "ok" if prueba == 200 else "invalid_token"}}]})
        if m == "GET" and url.endswith("/configs/platform-alertas"):
            return _R(200 if existe else 404)
        return _R(crear)

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_cluster_hwc_creds", lambda td: ("AK", "SK"))
    monkeypatch.setattr(main, "get_huawei_project_id", lambda: "P")
    monkeypatch.setattr(main, "_add_css_cluster_routes", lambda cid, ak, sk, pid, ips: rutas.append((cid, ips)) or {})
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "52.1.2.3")
    monkeypatch.setattr(main.runs, "step", lambda run, n, ok, r="": pasos.append((n, ok, r)))
    return pedidos, rutas, pasos


def test_el_canal_se_crea_con_su_ruta_y_se_prueba(monkeypatch, tmp_path):
    pedidos, rutas, pasos = _cluster(monkeypatch, tmp_path)
    main._provisionar_canal({"public_endpoint": "x:9200", "id": "C1"}, "a", "p", False, tmp_path, {})
    assert rutas == [("C1", ["52.1.2.3"])], "sin la ruta, el CSS no sale al webhook"
    assert ("POST", "http://x:9200/_plugins/_notifications/configs",
            caps.build_canal_de_alertas("slack", "https://hooks.slack.com/services/T/B/X")) in pedidos
    assert pasos == [("Canal de alertas", True, "se mandó un mensaje de prueba por Slack")]
    assert main._read_estados(tmp_path)["_cluster"]["canal"] == {
        "ok": True, "motivo": "se mandó un mensaje de prueba por Slack", "tipo": "slack", "host": "hooks.slack.com"}
    assert main._canal_listo(tmp_path) == "platform-alertas"


def test_si_ya_estaba_se_actualiza(monkeypatch, tmp_path):
    pedidos, _, _ = _cluster(monkeypatch, tmp_path, existe=True)
    main._provisionar_canal({"public_endpoint": "x:9200", "id": "C1"}, "a", "p", False, tmp_path, {})
    assert any(m == "PUT" and u.endswith("/configs/platform-alertas") for m, u, b in pedidos)


def test_si_la_prueba_no_llega_se_dice_y_el_monitor_no_lo_usa(monkeypatch, tmp_path):
    _, _, pasos = _cluster(monkeypatch, tmp_path, prueba=403)
    main._provisionar_canal({"public_endpoint": "x:9200", "id": "C1"}, "a", "p", False, tmp_path, {})
    assert pasos == [("Canal de alertas", False, "el canal se creó pero la prueba no llegó: invalid_token")]
    assert main._canal_listo(tmp_path) == "", "un canal que no anda no se engancha a los monitores"


def test_sin_canal_configurado_no_se_toca_nada(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: pytest.fail("no debería llamar al cluster"))
    main._provisionar_canal({"public_endpoint": "x:9200"}, "a", "p", False, tmp_path, {})


def test_un_monitor_de_antes_pasa_a_avisar(monkeypatch):
    pedidos = []
    monitor = {"_seq_no": 3, "_primary_term": 1, "monitor": {"triggers": [{"query_level_trigger": {"actions": []}}]}}

    def req(m, url, user, password, json_body=None, timeout=30):
        pedidos.append((m, url, json_body))
        return _R(200, monitor)

    monkeypatch.setattr(main, "_os_req", req)
    assert main._asegurar_aviso("http://x", "a", "p", "pozos", "M", "D", "platform-alertas")
    (m, url, cuerpo), = [p for p in pedidos if p[0] == "PUT"]
    assert url.endswith("/_plugins/_alerting/monitors/M?if_seq_no=3&if_primary_term=1")
    assert cuerpo == caps.build_monitor_de_anomalias("pozos", "D", canal_id="platform-alertas")
    # Si ya avisaba, no se reescribe.
    monitor["monitor"]["triggers"][0]["query_level_trigger"]["actions"] = [{"destination_id": "platform-alertas"}]
    pedidos.clear()
    assert main._asegurar_aviso("http://x", "a", "p", "pozos", "M", "D", "platform-alertas")
    assert [p for p in pedidos if p[0] == "PUT"] == []


def test_la_provision_lo_hace_antes_de_los_casos():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    cuerpo = src[i:src.index("\n    msg = ", i)]
    assert cuerpo.index("_provisionar_canal(cluster, user, password, request.https_enabled, terraform_dir, run)") \
        < cuerpo.index("for slug in slugs:")
    j = src.index("def _provision_capabilities(")
    caso = src[j:src.index("\ndef ", j + 10)]
    assert "json_body=caps.build_monitor_de_anomalias(slug, ids[\"detector_id\"], canal_id=canal))" in caso
    assert "_asegurar_aviso(base, user, password, slug, ids[\"monitor_id\"], ids[\"detector_id\"], canal)" in caso


# ── La vista y el export ────────────────────────────────────────────────────
def test_las_tarjetas_dicen_por_donde_avisa():
    canal = {"ok": True, "tipo": "slack", "host": "hooks.slack.com"}
    t = {x["plugin"]: x for x in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="https://d", canal=canal)}
    assert t["canal"]["titulo"] == "Avisos por Slack (Notifications)" and t["canal"]["estado"] == pv.OK
    assert t["canal"]["que"] == "Las alertas de anomalías avisan por Slack (hooks.slack.com)."
    assert t["canal"]["links"][0]["url"] == "https://d/app/notifications-dashboards#/channels"
    mal = {x["plugin"]: x for x in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="",
                                                           canal={"ok": False, "tipo": "slack", "motivo": "no llegó"})}
    assert (mal["canal"]["estado"], mal["canal"]["motivo"]) == (pv.FALLA, "no llegó")
    assert pv._alertas({"monitor_id": "M"}, {}, "", canal)["que"].startswith("Avisa por Slack cuando")
    assert "Sin un canal configurado" in pv._alertas({"monitor_id": "M"}, {}, "", None)["que"]


def test_el_export_lo_lleva():
    campos = [{"field_path": "@timestamp", "type": "date", "role": "timestamp"},
              {"field_path": "monto", "type": "double", "role": "measure"}]
    texto = exportar.devtools("ventas", campos)
    assert "POST _plugins/_notifications/configs" in texto and "<URL_DEL_WEBHOOK>" in texto
    assert texto.index("POST _plugins/_notifications/configs") < texto.index("POST _plugins/_alerting/monitors")
    assert '"destination_id": "platform-alertas"' in texto


def test_la_tarjeta_de_configuracion():
    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="settings-canal-url" autocomplete="off"' in html and 'type="password" class="form-input" id="settings-canal-url"' in html
    assert "fetch('/api/v1/settings/alertas', {" in html
    assert "document.getElementById('settings-canal-url').value = '';" in html, "la URL no queda en la pantalla"
