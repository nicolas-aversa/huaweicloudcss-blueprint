"""deepseek-v4-flash se retira de MaaS el 2026-10-08: el modelo por defecto pasa
a deepseek-v4.1-flash, y un entorno ya desplegado se migra al volver a
provisionar. El cluster falso reproduce lo medido en CSS 3.4: no deja cambiar un
connector con su modelo desplegado, y si se lo actualiza sin la credencial MaaS
deja de autorizar."""
import json

import capabilities as caps
import main


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


class _Cluster:
    def __init__(self, connectors):
        # id → {"name", "parameters", "credencial"}; modelos: id → {"connector", "estado"}
        self.connectors = connectors
        self.modelos = {}
        self.pedidos = []

    def req(self, method, url, user, password, json_body=None, timeout=30):
        ruta = url.split(":9200", 1)[1]
        self.pedidos.append((method, ruta))
        if ruta == "/_plugins/_ml/connectors/_search":
            return _R(200, {"hits": {"hits": [{"_id": i, "_source": {"name": c["name"], "parameters": c["parameters"]}}
                                              for i, c in self.connectors.items()]}})
        if ruta == "/_plugins/_ml/models/_search":
            cid = json_body["query"]["term"]["connector_id"]
            return _R(200, {"hits": {"hits": [{"_id": m} for m, d in self.modelos.items() if d["connector"] == cid]}})
        if ruta.endswith("/_undeploy"):
            # Como en CSS: asíncrono, tarda un par de consultas en quedar replegado.
            self.modelos[ruta.split("/")[-2]]["estado"] = "DEPLOYED"
            self.modelos[ruta.split("/")[-2]]["repliegue"] = 2
            return _R(200)
        if ruta.endswith("/_deploy"):
            self.modelos[ruta.split("/")[-2]]["estado"] = "DEPLOYED"
            return _R(200)
        if method == "PUT" and ruta.startswith("/_plugins/_ml/connectors/"):
            cid = ruta.rsplit("/", 1)[1]
            if any(d["connector"] == cid and d["estado"] == "DEPLOYED" for d in self.modelos.values()):
                return _R(400, {"error": {"reason": "1 models are still using this connector, please undeploy the models first"}})
            c = self.connectors[cid]
            c["parameters"] = json_body["parameters"]
            c["credencial"] = (json_body.get("credential") or {}).get("maas_key")
            return _R(200, {"result": "updated"})
        if method == "GET" and ruta.startswith("/_plugins/_ml/models/"):
            m = self.modelos[ruta.rsplit("/", 1)[1]]
            if m.get("repliegue"):
                m["repliegue"] -= 1
                if not m["repliegue"]:
                    m["estado"] = "UNDEPLOYED"
            return _R(200, {"model_state": m["estado"]})
        return _R(404)


def _cluster(monkeypatch):
    c = _Cluster({
        "C-LLM": {"name": "MaaS LLM (platform)", "parameters": {"endpoint": "e", "model": "deepseek-v4-flash"}, "credencial": "K"},
        "C-PPL": {"name": "MaaS DeepSeek PPLTool (platform)", "credencial": "K",
                  "parameters": {"endpoint": "e", "model": "deepseek-v4-flash", "system_prompt": "PPL…", "response_filter": "$.x"}},
        "C-OK": {"name": "otro", "parameters": {"endpoint": "e", "model": "glm-5.2"}, "credencial": "K"},
    })
    c.modelos = {"M-LLM": {"connector": "C-LLM", "estado": "DEPLOYED"}, "M-PPL": {"connector": "C-PPL", "estado": "DEPLOYED"},
                 "M-OK": {"connector": "C-OK", "estado": "DEPLOYED"}}
    monkeypatch.setattr(main, "_os_req", c.req)
    monkeypatch.setattr(main, "_ML_TASK_POLL_DELAY", 0, raising=False)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    return c


def test_el_modelo_por_defecto_ya_no_es_el_retirado():
    assert caps.DEFAULT_MAAS_LLM_MODEL == caps.DEFAULT_MAAS_PPL_MODEL == "deepseek-v4.1-flash"
    assert caps.DEFAULT_MAAS_LLM_MODEL not in caps.MODELOS_RETIRADOS
    assert "deepseek-v4-flash" in caps.MODELOS_RETIRADOS


def test_migra_los_connectors_con_el_modelo_retirado(monkeypatch):
    c = _cluster(monkeypatch)
    pasos = main._migrar_modelos_retirados("http://x:9200", "a", "p", "CLAVE")
    assert [p["nombre"] for p in pasos] == ["Modelo de MaaS · MaaS LLM (platform)", "Modelo de MaaS · MaaS DeepSeek PPLTool (platform)"]
    assert all(p["ok"] for p in pasos) and "deepseek-v4-flash → deepseek-v4.1-flash" in pasos[0]["motivo"]
    for cid in ("C-LLM", "C-PPL"):
        assert c.connectors[cid]["parameters"]["model"] == "deepseek-v4.1-flash"
        assert c.connectors[cid]["credencial"] == "CLAVE", "sin la credencial MaaS deja de autorizar"
    # El resto de los parámetros (el prompt del PPL) no se pierde.
    assert c.connectors["C-PPL"]["parameters"]["system_prompt"] == "PPL…"
    assert all(m["estado"] == "DEPLOYED" for m in c.modelos.values()), "vuelven a quedar desplegados"


def test_el_orden_replegar_actualizar_desplegar(monkeypatch):
    c = _cluster(monkeypatch)
    main._migrar_modelos_retirados("http://x:9200", "a", "p", "CLAVE")
    p = c.pedidos
    assert p.index(("POST", "/_plugins/_ml/models/M-LLM/_undeploy")) < p.index(("PUT", "/_plugins/_ml/connectors/C-LLM")) \
        < p.index(("POST", "/_plugins/_ml/models/M-LLM/_deploy"))


def test_lo_que_no_usa_un_modelo_retirado_no_se_toca(monkeypatch):
    c = _cluster(monkeypatch)
    main._migrar_modelos_retirados("http://x:9200", "a", "p", "CLAVE")
    assert c.connectors["C-OK"]["parameters"]["model"] == "glm-5.2"
    assert not [x for x in c.pedidos if "M-OK" in x[1] or x == ("PUT", "/_plugins/_ml/connectors/C-OK")]
    # Una segunda pasada no hace nada.
    c.pedidos.clear()
    assert main._migrar_modelos_retirados("http://x:9200", "a", "p", "CLAVE") == []
    assert all(m != "PUT" for m, _ in c.pedidos)


def test_un_fallo_se_dice(monkeypatch):
    c = _cluster(monkeypatch)
    real = c.req

    def falla_put(method, url, *a, **k):
        if method == "PUT":
            return _R(500, {"error": "boom"})
        return real(method, url, *a, **k)

    monkeypatch.setattr(main, "_os_req", falla_put)
    pasos = main._migrar_modelos_retirados("http://x:9200", "a", "p", "CLAVE")
    assert pasos and not pasos[0]["ok"] and "connector: status 500" in pasos[0]["motivo"]


def test_el_paso_3_migra_antes_de_provisionar():
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    assert cuerpo.index("_migrar_modelos_retirados(") < cuerpo.index("for slug in slugs:")
    assert "runs.step(run, p[\"nombre\"], p[\"ok\"], p[\"motivo\"][:300])" in cuerpo
