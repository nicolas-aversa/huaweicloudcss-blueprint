"""¿Este CSS corre un modelo de embeddings adentro? MaaS no tiene uno: la
búsqueda semántica depende de eso. La prueba registra un modelo preentrenado,
lo despliega, lo usa y lo borra, y dice en qué paso falló."""
import json

import pytest

import main
import plugins_vista as pv


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


def _cluster(monkeypatch, registro="COMPLETED", deploy="COMPLETED", vector=(0.1, 0.2, 0.3)):
    pedidos = []

    def req(m, url, user, password, json_body=None, timeout=30):
        pedidos.append((m, url, json_body))
        if url.endswith("/_plugins/_ml/models/_register"):
            return _R(200, {"task_id": "T1"})
        if url.endswith("/_deploy"):
            return _R(200, {"task_id": "T2"})
        if url.endswith("/tasks/T1"):
            return _R(200, {"state": registro, "model_id": "M1", "error": "Connection refused: artifacts.opensearch.org"})
        if url.endswith("/tasks/T2"):
            return _R(200, {"state": deploy, "error": "Native Memory Circuit Breaker is open"})
        if "/_predict/text_embedding/" in url:
            return _R(200, {"inference_results": [{"output": [{"data": list(vector)}]}]})
        return _R(200)

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    return pedidos


def test_si_anda_lo_dice_y_no_deja_nada(monkeypatch):
    pedidos = _cluster(monkeypatch)
    r = main._probar_embeddings("http://x", "a", "p")
    assert r == {"ok": True, "paso": "", "dimensiones": 3,
                 "reason": "el cluster corre embeddings (3 dimensiones): se puede hacer búsqueda semántica"}
    assert pedidos[0] == ("POST", "http://x/_plugins/_ml/models/_register", main.MODELO_DE_EMBEDDINGS)
    assert "multilingual" in main.MODELO_DE_EMBEDDINGS["name"], "las demos están en castellano"
    assert ("POST", "http://x/_plugins/_ml/models/M1/_undeploy", None) in pedidos
    assert ("DELETE", "http://x/_plugins/_ml/models/M1", None) in pedidos, "el modelo ocupa memoria: se borra"


@pytest.mark.parametrize("kw, paso, motivo", [
    ({"registro": "FAILED"}, "registrar", "¿tiene salida a artifacts.opensearch.org?"),
    ({"deploy": "FAILED"}, "desplegar", "Native Memory Circuit Breaker is open"),
    ({"vector": ()}, "predecir", "no devolvió embeddings"),
])
def test_si_no_anda_dice_en_que_paso(monkeypatch, kw, paso, motivo):
    pedidos = _cluster(monkeypatch, **kw)
    r = main._probar_embeddings("http://x", "a", "p")
    assert not r["ok"] and r["paso"] == paso and motivo in r["reason"]
    if paso != "registrar":
        assert ("DELETE", "http://x/_plugins/_ml/models/M1", None) in pedidos, "aunque falle, se limpia"


def test_el_endpoint_guarda_el_resultado(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_cluster_del_entorno", lambda stage: ("http://x", "a", "p"))
    monkeypatch.setattr(main, "_probar_embeddings", lambda b, u, p: {"ok": False, "paso": "registrar", "reason": "sin salida"})
    r = TestClient(main.app).post("/api/v1/plugins/probar-embeddings")
    assert r.json()["paso"] == "registrar"
    assert main._read_estados(tmp_path)["_cluster"]["embeddings"] == {"ok": False, "motivo": "sin salida",
                                                                     "paso": "registrar", "dimensiones": 0}


def test_la_tarjeta_antes_y_despues_de_probar():
    sin = {t["plugin"]: t for t in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="")}["embeddings"]
    assert sin["estado"] == pv.SIN_PROBAR and sin["accion"] == {"id": "probar_embeddings", "texto": "Probarlo ahora"}
    ok = {t["plugin"]: t for t in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="",
                                                          embeddings={"ok": True, "dimensiones": 384})}["embeddings"]
    assert ok["estado"] == pv.OK and "384 dimensiones" in ok["que"] and ok["accion"]["texto"] == "Probar de nuevo"
    mal = {t["plugin"]: t for t in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="",
                                                           embeddings={"ok": False, "motivo": "sin salida"})}["embeddings"]
    assert (mal["estado"], mal["motivo"]) == (pv.FALLA, "sin salida")


def test_el_boton_en_la_tarjeta():
    import pathlib
    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "sin_probar: ['Sin probar', 'sev--low']" in html
    assert 'class="btn btn-secondary btn-sm plug__accion" data-accion="${escapeHtml(t.accion.id)}"' in html
    assert "if (accion) return probarEmbeddings(accion);" in html
    assert "fetch('/api/v1/plugins/probar-embeddings', { method: 'POST' })" in html
