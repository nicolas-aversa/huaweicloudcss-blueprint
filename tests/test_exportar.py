"""Llevarse el cluster: la configuración de un dataset como requests de Dev
Tools, armada desde el mismo plan que el provisioning, sin secretos."""
import json
import re

import pytest
from fastapi.testclient import TestClient

import exportar
import main
from test_plan_de_cluster import HOTEL

REGLAS = {"descripcion": "Firewall", "reglas": [
    {"titulo": "IPS crítico", "descripcion": "d", "nivel": "critical", "tags": ["attack.t1190"],
     "seleccion": {"canal": "web"}},
    {"titulo": "Login admin", "descripcion": "d", "nivel": "low", "tags": [],
     "seleccion": {"comentario|contains": "admin"}}]}


def _requests(texto: str) -> list[tuple[str, str, object]]:
    """(método, ruta, cuerpo) de cada request, como los lee Dev Tools."""
    fuera = []
    bloques = [b for b in texto.split("\n\n") if b.strip()]
    for b in bloques:
        lineas = [l for l in b.splitlines() if not l.startswith("#")]
        if not lineas:
            continue
        m = re.match(r"^(GET|PUT|POST|DELETE) (\S+)$", lineas[0])
        assert m, b[:120]
        cuerpo = "\n".join(lineas[1:]).strip()
        fuera.append((m.group(1), m.group(2), json.loads(cuerpo) if cuerpo else None))
    return fuera


def _texto(**kw):
    datos = {"label": "Hotel", "index_name": "hotel-%{+YYYY.MM}", "seguridad_propuesta": REGLAS,
             "filter_code": "filter {\n  csv {}\n}", **kw}
    return exportar.devtools("hotel", HOTEL, **datos)


def test_cada_plugin_que_aplica_tiene_su_request_y_todo_es_json_valido():
    reqs = _requests(_texto())
    rutas = [r for _, r, _ in reqs]
    for esperada in ("_index_template/hotel", "_plugins/_ml/agents/_register", "_plugins/_forecast/forecasters",
                     "_plugins/_anomaly_detection/detectors", "_plugins/_alerting/monitors",
                     "_plugins/_transform/hotel-perfil", "_plugins/_security/api/roles/hotel-analista",
                     "_plugins/_security/api/internalusers/analista-hotel",
                     "_plugins/_security_analytics/logtype", "_plugins/_security_analytics/detectors"):
        assert esperada in rutas, esperada
    assert rutas.count("_plugins/_forecast/forecasters") == 4
    # Los mismos builders que el provisioning.
    detector = next(c for _, r, c in reqs if r == "_plugins/_security_analytics/detectors")
    assert detector["inputs"][0]["detector_input"]["indices"] == ["hotel-seguridad"]
    assert [x["id"] for x in detector["inputs"][0]["detector_input"]["custom_rules"]] == ["<RULE_ID_1>", "<RULE_ID_2>"]
    template = next(c for _, r, c in reqs if r == "_index_template/hotel")
    assert "hotel-seguridad" in template["template"]["aliases"]


def test_las_reglas_van_como_curl_con_su_yaml():
    texto = _texto()
    assert "# curl -k -u admin:<CONTRASEÑA_ADMIN> -X POST" in texto
    assert "rules?category=hotel" in texto and '#     comentario|contains: "admin"' in texto
    assert "# El filter del dataset" in texto and "#   csv {}" in texto


def test_lo_que_no_aplica_o_se_apago_no_se_exporta():
    rutas = [r for _, r, _ in _requests(_texto(excluir=["perfil", "anomalias", "security_analytics"],
                                               seguridad_propuesta=None))]
    assert not any("_transform/hotel-perfil" in r or "anomaly_detection" in r or "_alerting" in r
                   or "security_analytics" in r for r in rutas)
    assert "_plugins/_forecast/forecasters" in rutas


def test_sin_secretos():
    """La API key y las contraseñas van como placeholders; ni los patrones de
    secretos conocidos del repo."""
    texto = _texto()
    assert "<MAAS_API_KEY>" in texto and "<CONTRASEÑA_DEL_ANALISTA>" in texto
    assert not re.search(r"HPUA|vJfk|VTd4|Huawei1234|k72q30|sk-[A-Za-z0-9]{20}", texto)
    connectors = [c for _, r, c in _requests(texto) if r == "_plugins/_ml/connectors/_create"]
    assert len(connectors) == 2 and all(c["credential"] == {"maas_key": "<MAAS_API_KEY>"} for c in connectors)


def test_los_endpoints(monkeypatch, tmp_path):
    import custom_cases

    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    caso = {"label": "Hotel", "fields": HOTEL, "index_base": "hotel", "seguridad": REGLAS,
            "excluir": ["perfil"], "filter_code": "filter { }"}
    monkeypatch.setattr(custom_cases, "get_case", lambda slug: caso if slug == "hotel" else None)
    c = TestClient(main.app)
    r = c.get("/api/v1/cases/hotel/export")
    assert r.status_code == 200 and "hotel-devtools.txt" in r.headers["content-disposition"]
    assert "_plugins/_security_analytics/detectors" in r.text and "_transform/hotel-perfil" not in r.text, "lo apagado no va"
    d = c.get("/api/v1/cases/hotel/export/dashboards")
    assert d.status_code == 200 and d.headers["content-type"].startswith("application/x-ndjson")
    assert all(json.loads(l) for l in d.text.splitlines() if l.strip())
    tipos = [json.loads(l)["type"] for l in d.text.splitlines() if l.strip()]
    assert "search" in tipos, "las búsquedas de Discover, como al desplegar"
    assert not any(json.loads(l)["id"] == "perfil-hotel" for l in d.text.splitlines() if l.strip()), \
        "el perfil apagado en el paso 2 no lleva su tabla"
    assert c.get("/api/v1/cases/no-existe/export").status_code == 404
    # Desplegado: el índice y lo apagado salen del registro (el .conf no: lleva credenciales).
    (tmp_path / main._PIPELINES_REGISTRY_NAME).write_text(json.dumps({"hotel": {
        "index": "hotel-%{+YYYY_MM}", "excluir": [], "fields": HOTEL, "pipeline_conf": "password => 'secreta'"}}),
        encoding="utf-8")
    r = c.get("/api/v1/cases/hotel/export")
    assert "_plugins/_transform/hotel-perfil" in r.text and "secreta" not in r.text


def test_la_tarjeta_del_caso_ofrece_llevarselo():
    import pathlib

    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "const base = `/api/v1/cases/${encodeURIComponent(slug)}`;" in html
    assert '<a href="${base}/export" download>' in html
    assert '<a href="${base}/export/dashboards" download>' in html
