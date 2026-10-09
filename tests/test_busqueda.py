"""La búsqueda híbrida (por palabras y por significado) sobre el texto libre
de un caso: qué campo, el índice de textos distintos con su vector, el
Transform que lo mantiene, las dos búsquedas lado a lado."""
import json

import pytest

import busqueda
import exportar
import main
import plan_de_cluster
import plugins_vista as pv


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


F = lambda path, tipo="keyword", **kw: {"field_path": path, "type": tipo, **kw}  # noqa: E731


# ── Qué campo ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("campos, esperado", [
    ([F("status"), F("comentario", "text")], "comentario"),                  # el text, primero
    ([F("event.action"), F("rule.name")], "rule.name"),                       # SIEM
    ([F("logdesc"), F("msg")], "logdesc"),                                    # FortiAnalyzer
    ([F("reject_reason"), F("title")], "title"),                              # un nombre antes que un motivo
    ([F("cancel_reason"), F("order_id")], "cancel_reason"),
    ([F("transaction.response_code"), F("customer_id")], ""),                 # códigos e ids: no
    ([F("message_id"), F("msg_code")], ""),
    ([F("desc", role="entity_id")], ""),
    ([F("monto", "float")], ""),
    ([], ""),
])
def test_el_campo_de_texto_libre(campos, esperado):
    assert busqueda.campo_de_texto_libre(campos) == esperado


def test_las_piezas():
    t = busqueda.transform("ventas", "ventas-*", "comentario", "text")["transform"]
    assert t["continuous"] and t["target_index"] == "busqueda-ventas" and t["source_index"] == "ventas-*"
    assert t["groups"] == [{"terms": {"source_field": "comentario.keyword", "target_field": "texto"}}]
    assert busqueda.transform("x", "x-*", "msg", "keyword")["transform"]["groups"][0]["terms"]["source_field"] == "msg"
    # Cada hora (cada pasada vuelve a vectorizar lo que apareció de nuevo) y solo
    # los eventos con el campo (si no, una fila `texto: null` sin vector).
    assert t["schedule"]["interval"]["period"] == 60 and t["schedule"]["interval"]["unit"] == "Minutes"
    assert t["data_selection_query"] == {"exists": {"field": "comentario.keyword"}}
    m = busqueda.mapping_del_indice()
    assert m["settings"]["index.default_pipeline"] == busqueda.PIPELINE_DE_INGESTA and m["settings"]["index.knn"]
    assert m["mappings"]["properties"]["embedding"]["dimension"] == busqueda.DIMENSION == 384
    assert busqueda.pipeline_de_ingesta("M")["processors"][0]["text_embedding"] == \
        {"model_id": "M", "field_map": {"texto": "embedding"}}
    norm = busqueda.pipeline_de_busqueda()["phase_results_processors"][0]["normalization-processor"]
    assert norm["combination"]["parameters"]["weights"] == [0.3, 0.7]
    h = busqueda.consulta_hibrida("cobro rechazado", "M")["query"]["hybrid"]["queries"]
    assert h[0]["multi_match"]["query"] == "cobro rechazado" and h[1]["neural"]["embedding"]["model_id"] == "M"
    assert busqueda.resultados({"hits": {"hits": [{"_score": 2.0, "_source": {"texto": "a", "cuenta": 3}}]}}) == \
        [{"texto": "a", "cuenta": 3, "puntaje": 2.0}]


# ── Provisionar ─────────────────────────────────────────────────────────────
def _cluster(monkeypatch, distintos=120, transform_existe=False, fallado=None):
    pedidos = []

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        pedidos.append((m, url.replace("http://x", ""), json_body))
        if url.endswith("/_search") and json_body and "aggs" in json_body:
            return _R(200, {"aggregations": {"n": {"value": distintos}}})
        if m == "GET" and "/_transform/" in url:
            return _R(200, {"_id": "t"}) if transform_existe else _R(404)
        return _R(200, {"acknowledged": True})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_motivo_si_fallo", lambda ruta, u, p: fallado)
    return pedidos


def test_arma_el_indice_y_el_transform(monkeypatch):
    pedidos = _cluster(monkeypatch)
    campos = [F("monto", "float"), F("comentario", "text")]
    r = main._provisionar_busqueda("http://x", "a", "p", "ventas", "ventas-*", campos, "M1", force=False)
    assert r["ok"] and r["campo"] == "comentario" and r["textos"] == 120
    rutas = [(m, u) for m, u, _ in pedidos]
    for esperado in (("PUT", "/_ingest/pipeline/plataforma-embeddings"), ("PUT", "/_search/pipeline/plataforma-hibrida"),
                     ("PUT", "/busqueda-ventas"), ("PUT", "/_plugins/_transform/ventas-busqueda"),
                     ("POST", "/_plugins/_transform/ventas-busqueda/_start")):
        assert esperado in rutas, esperado
    # El índice existe antes que el Transform (con el pipeline por defecto).
    assert rutas.index(("PUT", "/busqueda-ventas")) < rutas.index(("PUT", "/_plugins/_transform/ventas-busqueda"))
    cardinalidad = next(b for m, u, b in pedidos if "aggs" in (b or {}))
    assert cardinalidad["aggs"]["n"]["cardinality"]["field"] == "comentario.keyword"


@pytest.mark.parametrize("distintos, dice", [
    (3, "es una categoría"), (busqueda.MAX_TEXTOS + 1, "tardaría demasiado"),
])
def test_si_no_es_texto_libre_no_aplica(monkeypatch, distintos, dice):
    pedidos = _cluster(monkeypatch, distintos=distintos)
    r = main._provisionar_busqueda("http://x", "a", "p", "s", "s-*", [F("msg")], "M1", force=False)
    assert r["no_aplica"] and dice in r["reason"]
    assert not [u for m, u, _ in pedidos if m == "PUT"], "no crea nada"


def test_si_ya_estaba_lo_deja_y_si_fallo_lo_rehace(monkeypatch):
    pedidos = _cluster(monkeypatch, transform_existe=True)
    r = main._provisionar_busqueda("http://x", "a", "p", "s", "s-*", [F("msg")], "M1", force=False)
    assert r["reason"] == "ya estaba" and not [u for m, u, _ in pedidos if m == "DELETE"]
    pedidos = _cluster(monkeypatch, transform_existe=True, fallado="rejected execution")
    r = main._provisionar_busqueda("http://x", "a", "p", "s", "s-*", [F("msg")], "M1", force=False)
    assert r["ok"] and ("DELETE", "/_plugins/_transform/s-busqueda", None) in pedidos


def test_el_run_prepara_el_modelo_una_vez_y_cada_caso(monkeypatch, tmp_path):
    estados = {}
    monkeypatch.setattr(main, "_guardar_estados", lambda td, slug, nuevo: estados.setdefault(slug, {}).update(nuevo))
    monkeypatch.setattr(main, "_asegurar_modelo_de_embeddings", lambda *a: ("M1", ""))
    monkeypatch.setattr(main, "_provisionar_busqueda", lambda b, u, p, slug, ip, fields, m, force:
                        {"ok": True, "campo": "msg", "textos": 10, "reason": "ok"} if slug == "a"
                        else {"ok": False, "no_aplica": True, "campo": "msg", "reason": "es una categoría"})
    pipe = {"a": {"fields": [F("msg")]}, "b": {"fields": [F("msg")]}, "c": {"fields": [F("monto", "float")]},
            "d": {"fields": [F("msg")], "excluir": ["busqueda"]}}
    main._busqueda_original({"public_endpoint": "x:9200"}, "a", "p", False, tmp_path, ["a", "b", "c", "d"], pipe, False, main.runs.start("capabilities"))
    assert estados["_cluster"]["embeddings"] == {"ok": True, "motivo": "", "model_id": "M1", "dimensiones": 384}
    assert estados["a"]["busqueda"]["ok"] and estados["a"]["busqueda"]["textos"] == 10
    assert estados["b"]["busqueda"]["no_aplica"] and estados["b"]["busqueda"]["motivo"] == "es una categoría"
    assert "c" not in estados and "d" not in estados, "sin texto libre, o apagada en el paso 2: nada"
    # Sin el modelo, cada caso lo dice.
    estados.clear()
    monkeypatch.setattr(main, "_asegurar_modelo_de_embeddings", lambda *a: ("", "sin salida a OBS"))
    main._busqueda_original({"public_endpoint": "x:9200"}, "a", "p", False, tmp_path, ["a"], pipe, False, main.runs.start("capabilities"))
    assert estados["a"]["busqueda"] == {"ok": False, "motivo": "sin el modelo de embeddings: sin salida a OBS", "ya": False,
                                        "campo": "msg", "textos": None, "no_aplica": False}


def test_el_endpoint_busca_de_las_dos_formas(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    c = TestClient(main.app)
    assert c.get("/api/v1/plugins/buscar?slug=ventas&q=x").status_code == 409, "sin armar"
    monkeypatch.setattr(main, "_read_estados", lambda td: {"_cluster": {"embeddings": {"model_id": "M1"}},
                                                          "ventas": {"busqueda": {"ok": True, "campo": "comentario"}}})
    monkeypatch.setattr(main, "_cluster_del_entorno", lambda stage: ("http://x", "a", "p"))
    pedidos = []

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        pedidos.append(url)
        texto = "pago denegado" if "search_pipeline" in url else "cobro rechazado"
        return _R(200, {"hits": {"hits": [{"_score": 1.0, "_source": {"texto": texto, "cuenta": 7}}]}})

    monkeypatch.setattr(main, "_os_req", req)
    d = c.get("/api/v1/plugins/buscar", params={"slug": "ventas", "q": "cobro rechazado"}).json()
    assert d["campo"] == "comentario" and d["lexica"][0]["texto"] == "cobro rechazado" and d["hibrida"][0]["texto"] == "pago denegado"
    assert "http://x/busqueda-ventas/_search?search_pipeline=plataforma-hibrida" in pedidos


# ── El plan, el export y la tarjeta ─────────────────────────────────────────
def test_el_plan_lo_dice():
    con = {i["plugin"]: i for i in plan_de_cluster.plan("s", [F("@timestamp", "date"), F("comentario", "text")])}["busqueda"]
    assert con["aplica"] and con["opcional"] and con["config"] == {"campo": "comentario", "tipo": "text"}
    sin = {i["plugin"]: i for i in plan_de_cluster.plan("s", [F("@timestamp", "date"), F("codigo")])}["busqueda"]
    assert not sin["aplica"] and "no hay un campo de texto libre" in sin["motivo"]


def test_el_export_lleva_la_busqueda():
    campos = [F("@timestamp", "date", role="timestamp"), F("comentario", "text"), F("monto", "float", role="measure")]
    texto = exportar.devtools("hotel", campos, label="Hotel")
    for pedazo in ("PUT _plugins/_transform/hotel-busqueda", "PUT _search/pipeline/plataforma-hibrida",
                   "PUT busqueda-hotel", '"url": "<LINK_FIRMADO_DE_OBS>"', busqueda.MODELO["archivo"]):
        assert pedazo in texto, pedazo
    assert "hotel-busqueda" not in exportar.devtools("hotel", campos, label="Hotel", excluir=["busqueda"])


def test_la_tarjeta():
    ok = pv._busqueda({"ok": True, "campo": "comentario"})
    assert ok["estado"] == pv.OK and "«comentario»" in ok["que"] and ok["numero"] == "busqueda"
    assert ok["accion"] == {"id": "probar_busqueda", "texto": "Probar una búsqueda"}
    assert pv._busqueda({"ok": False, "no_aplica": True, "motivo": "es una categoría"})["estado"] == pv.EXCLUIDO
    mal = pv._busqueda({"ok": False, "motivo": "sin el modelo"})
    assert mal["estado"] == pv.FALLA and "accion" not in mal
    caso = pv.tarjetas_del_caso("s", entry={"excluir": ["busqueda"]}, ids={}, spec={}, perfil=None, enmascarados=[],
                                seguridad_reg={}, seguridad_spec={}, estados={}, analista_creado=False, base="")
    assert any(t["plugin"] == "busqueda" and t["estado"] == pv.EXCLUIDO for t in caso)


def test_si_el_modelo_falla_lo_que_andaba_sigue(monkeypatch, tmp_path):
    """Un timeout preparando el modelo dejaba el id en "" y todas las búsquedas
    en falla, aunque el modelo y los índices seguían andando."""
    estados = {"_cluster": {"embeddings": {"ok": True, "model_id": "M_OK"}}, "a": {"busqueda": {"ok": True, "campo": "msg"}}}
    monkeypatch.setattr(main, "_read_estados", lambda td: estados)
    monkeypatch.setattr(main, "_guardar_estados", lambda td, slug, nuevo: estados.setdefault(slug, {}).update(nuevo))
    monkeypatch.setattr(main, "_asegurar_modelo_de_embeddings", lambda *a: ("", "no se pudo leer el estado del modelo"))
    pipe = {"a": {"fields": [F("msg")]}, "b": {"fields": [F("msg")]}}
    main._busqueda_original({"public_endpoint": "x:9200"}, "a", "p", False, tmp_path, ["a", "b"], pipe, False,
                            main.runs.start("capabilities"))
    assert estados["_cluster"]["embeddings"]["model_id"] == "M_OK" and not estados["_cluster"]["embeddings"]["ok"]
    assert estados["a"]["busqueda"] == {"ok": True, "campo": "msg"}, "la que andaba no se pisa"
    assert not estados["b"]["busqueda"]["ok"]


def test_el_cuidador_frena_el_que_crece_y_vectoriza_lo_que_falta(monkeypatch, tmp_path):
    pedidos, estados = [], {"s": {"busqueda": {"ok": True, "campo": "msg"}}}
    cuenta = {"textos": busqueda.MAX_TEXTOS + 5, "faltan": 0}
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "x:9200"})
    monkeypatch.setattr(main, "_cluster_admin_password", lambda td: "pw")
    monkeypatch.setattr(main, "_read_https_enabled_from_state", lambda td: False)
    monkeypatch.setattr(main, "_cola_de_busquedas", lambda *a: 0)
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda td: {"s": {}})
    monkeypatch.setattr(main, "_perfil_de", lambda slug, entry: None)
    monkeypatch.setattr(main, "_read_estados", lambda td: estados)
    monkeypatch.setattr(main, "_guardar_estados", lambda td, slug, nuevo: estados.setdefault(slug, {}).update(nuevo))
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {})

    def req(m, url, *a, json_body=None, **k):
        pedidos.append((m, url, json_body))
        if url.endswith("/_count"):
            return _R(200, {"count": cuenta["faltan"] if json_body["query"] == busqueda.SIN_VECTOR else cuenta["textos"]})
        if url.endswith("_explain"):
            return _R(200, {"s-busqueda": {"transform_metadata": {"status": "started"}}})
        return _R(200, {})

    monkeypatch.setattr(main, "_os_req", req)
    assert main._cuidar_plugins(tmp_path) == ["tope de textos · s"]
    assert ("POST", "http://x:9200/_plugins/_transform/s-busqueda/_stop", None) in pedidos
    assert estados["s"]["busqueda"]["no_aplica"] and "se paró" in estados["s"]["busqueda"]["motivo"]
    # Debajo del tope y con textos sin vector: se vectorizan (solo los que tienen texto).
    main._cuidados.clear()
    estados["s"]["busqueda"] = {"ok": True, "campo": "msg"}
    cuenta.update(textos=100, faltan=3)
    pedidos.clear()
    assert main._cuidar_plugins(tmp_path) == ["vectores · s"]
    ubq = next(b for m, u, b in pedidos if "_update_by_query" in u)
    assert ubq == {"query": busqueda.SIN_VECTOR}
    main._cuidados.clear()
