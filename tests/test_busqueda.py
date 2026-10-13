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
    # La primera pasada, al minuto (con start_time: 1 era la próxima hora en punto).
    pr = busqueda.transform("x", "x-*", "msg", "keyword", ahora_ms=10_000_000)["transform"]["schedule"]["interval"]
    assert pr["start_time"] == 10_000_000 - 59 * 60_000, "start + 1·período = ahora + 1 minuto"
    assert t["data_selection_query"] == {"exists": {"field": "comentario.keyword"}}
    m = busqueda.mapping_del_indice()
    assert m["settings"]["index.default_pipeline"] == busqueda.PIPELINE_DE_INGESTA and m["settings"]["index.knn"]
    assert m["mappings"]["properties"]["embedding"]["dimension"] == busqueda.DIMENSION == 2048
    assert m["mappings"]["properties"]["embedding"]["method"]["space_type"] == "innerproduct", "vienen normalizados"
    assert busqueda.pipeline_de_ingesta("M")["processors"][0]["text_embedding"] == \
        {"model_id": "M", "field_map": {"texto": "embedding"}, "batch_size": busqueda.LOTE}, "de a lotes a NVIDIA"
    norm = busqueda.pipeline_de_busqueda()["phase_results_processors"][0]["normalization-processor"]
    assert norm["combination"]["parameters"]["weights"] == [0.3, 0.7]
    h = busqueda.consulta_hibrida("cobro rechazado", "M")["query"]["hybrid"]["queries"]
    assert h[0]["multi_match"]["query"] == "cobro rechazado" and h[1]["neural"]["embedding"]["model_id"] == "M"
    assert busqueda.resultados({"hits": {"hits": [{"_score": 2.0, "_source": {"texto": "a", "cuenta": 3}}]}}) == \
        [{"texto": "a", "cuenta": 3, "puntaje": 2.0}]


# ── Provisionar ─────────────────────────────────────────────────────────────
def _cluster(monkeypatch, distintos=120, transform_existe=False, fallado=None, agrupa="msg"):
    pedidos = []

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        pedidos.append((m, url.replace("http://x", ""), json_body))
        if url.endswith("/_search") and json_body and "aggs" in json_body:
            return _R(200, {"aggregations": {"n": {"value": distintos}}})
        if m == "GET" and "/_transform/" in url:
            return (_R(200, {"_id": "t", "transform": {"groups": [{"terms": {"source_field": agrupa}}]}})
                    if transform_existe else _R(404))
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
    assert r["ok"] and ("DELETE", "/_plugins/_transform/s-busqueda?force=true", None) in pedidos


def test_el_run_prepara_el_modelo_una_vez_y_cada_caso(monkeypatch, tmp_path):
    estados = {}
    monkeypatch.setattr(main, "_guardar_estados", lambda td, slug, nuevo: estados.setdefault(slug, {}).update(nuevo))
    monkeypatch.setattr(main, "_asegurar_modelo_de_embeddings", lambda *a: ({"passage": "M1", "query": "Q1"}, ""))
    import maas_integrator
    monkeypatch.setattr(maas_integrator, "get_nvidia_api_key", lambda: "clave-de-prueba")
    monkeypatch.setattr(main, "_provisionar_busqueda", lambda b, u, p, slug, ip, fields, m, force:
                        {"ok": True, "campo": "msg", "textos": 10, "reason": "ok"} if slug == "a"
                        else {"ok": False, "no_aplica": True, "campo": "msg", "reason": "es una categoría"})
    pipe = {"a": {"fields": [F("msg")]}, "b": {"fields": [F("msg")]}, "c": {"fields": [F("monto", "float")]},
            "d": {"fields": [F("msg")], "excluir": ["busqueda"]}}
    main._busqueda_original({"public_endpoint": "x:9200"}, "a", "p", False, tmp_path, ["a", "b", "c", "d"], pipe, False, main.runs.start("capabilities"))
    assert estados["_cluster"]["embeddings"] == {"ok": True, "motivo": "", "model_id": "M1", "model_id_consulta": "Q1",
                                                 "clave": main._huella("clave-de-prueba"), "dimensiones": 2048}
    assert estados["a"]["busqueda"]["ok"] and estados["a"]["busqueda"]["textos"] == 10
    assert estados["b"]["busqueda"]["no_aplica"] and estados["b"]["busqueda"]["motivo"] == "es una categoría"
    assert "c" not in estados and "d" not in estados, "sin texto libre, o apagada en el paso 2: nada"
    # Sin el modelo, cada caso lo dice.
    estados.clear()
    monkeypatch.setattr(main, "_asegurar_modelo_de_embeddings", lambda *a: ({}, "sin salida a NVIDIA"))
    main._busqueda_original({"public_endpoint": "x:9200"}, "a", "p", False, tmp_path, ["a"], pipe, False, main.runs.start("capabilities"))
    assert estados["a"]["busqueda"] == {"ok": False, "motivo": "sin el modelo de embeddings: sin salida a NVIDIA", "ya": False,
                                        "campo": "msg", "textos": None, "no_aplica": False}


def test_la_comparacion_va_en_search_relevance(monkeypatch, tmp_path):
    """La plataforma no replica Dashboards: da el índice, el pipeline, las dos
    consultas y el link a Search Relevance, que las muestra lado a lado."""
    import json as _json
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    c = TestClient(main.app)
    assert c.get("/api/v1/plugins/busqueda/comparar?slug=ventas").status_code == 409, "sin armar"
    monkeypatch.setattr(main, "_read_estados", lambda td: {
        "_cluster": {"embeddings": {"model_id": "TEXTOS", "model_id_consulta": "CONSULTAS"}},
        "ventas": {"busqueda": {"ok": True, "campo": "comentario"}}})
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_build_dashboards_url", lambda cluster, https: "https://dash.example/app/home")
    d = c.get("/api/v1/plugins/busqueda/comparar", params={"slug": "ventas"}).json()
    assert d["indice"] == "busqueda-ventas" and d["pipeline"] == "plataforma-hibrida" and d["campo"] == "comentario"
    assert d["configuraciones"] == ["ventas · por palabras", "ventas · híbrida"], "las que se eligen en Single Query Comparison"
    assert d["query_set"] == "ventas · búsquedas" and d["evaluacion"] is None, "sin evaluación todavía"
    assert d["experimentos"] == {} and d["url_experimentos"] == "https://dash.example/app/searchRelevance#/experiment"
    assert d["url"] == "https://dash.example/app/searchRelevance#/"
    assert _json.loads(d["palabras"]) == {"query": {"multi_match": {"query": "%SearchText%", "fields": ["texto", "texto.es"]}},
                                          "_source": ["texto", "cuenta"]}
    assert _json.loads(d["hibrida"])["_source"] == ["texto", "cuenta"], "sin el vector en los resultados"
    assert "\n" not in d["palabras"] and "\n" not in d["hibrida"], "en una línea: se copian, no se leen"
    neural = _json.loads(d["hibrida"])["query"]["hybrid"]["queries"][1]["neural"]["embedding"]
    assert neural == {"query_text": "%SearchText%", "model_id": "CONSULTAS", "k": 10}, "con el modelo de consultas"
    import pathlib
    assert "/api/v1/plugins/buscar" not in pathlib.Path(main.__file__).read_text(encoding="utf-8"), "no se busca desde la plataforma"


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
                   "PUT busqueda-hotel", '"url": "https://integrate.api.nvidia.com/v1/embeddings"',
                   '"api_key": "<NVIDIA_API_KEY>"', '"model_id": "<MODEL_ID_TEXTOS>"', '"model_id": "<MODEL_ID_CONSULTAS>"',
                   "75.2.113.119", "Los textos viajan a NVIDIA"):
        assert pedazo in texto, pedazo
    assert "hotel-busqueda" not in exportar.devtools("hotel", campos, label="Hotel", excluir=["busqueda"])


def test_la_tarjeta():
    ok = pv._busqueda({"ok": True, "campo": "comentario"})
    assert ok["estado"] == pv.OK and "«comentario»" in ok["que"] and ok["numero"] == "busqueda" and ok["campo"] == "comentario"
    con_base = pv._busqueda({"ok": True, "campo": "comentario"}, "https://d")
    assert con_base["links"] == [{"texto": "Comparar en Search Relevance", "url": "https://d/app/searchRelevance#/"}]
    assert "accion" not in ok, "sin botón: se compara en Dashboards"
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
    monkeypatch.setattr(main, "_asegurar_modelo_de_embeddings", lambda *a: ({}, "no se pudo leer el estado del modelo"))
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


def test_se_queda_con_el_primer_candidato_con_frases(monkeypatch):
    """En SIEM el campo eran códigos ("sqli", "xss") y en FortiAnalyzer el
    primero, una categoría: buscar por significado sobre eso no aporta."""
    muestras = {"logdesc": ["Admin login", "IPS"], "msg": ["Connection blocked by policy", "File download allowed", "Virus detected in"]}
    cuentas = {"logdesc": 3, "msg": 40}
    pedidos = []

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        pedidos.append((m, url, json_body))
        if url.endswith("/_search") and json_body and "aggs" in json_body:
            campo = json_body["aggs"]["n"]["cardinality"]["field"]
            return _R(200, {"aggregations": {"n": {"value": cuentas[campo]},
                                             "muestra": {"buckets": [{"key": k} for k in muestras[campo]]}}})
        return _R(404) if m == "GET" else _R(200, {"acknowledged": True})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_motivo_si_fallo", lambda ruta, u, p: None)
    r = main._provisionar_busqueda("http://x", "a", "p", "f", "f-*", [F("logdesc"), F("msg")], "M1", force=False)
    assert r["ok"] and r["campo"] == "msg", r
    transform = next(b for m, u, b in pedidos if m == "PUT" and "/_transform/" in u)
    assert transform["transform"]["groups"][0]["terms"]["source_field"] == "msg"
    # Solo códigos: no aplica, y dice por qué.
    muestras["msg"] = ["sqli", "xss", "path_traversal", "Adobe.Flash.Execution"]
    r = main._provisionar_busqueda("http://x", "a", "p", "f", "f-*", [F("logdesc"), F("msg")], "M1", force=False)
    assert r["no_aplica"] and "logdesc tiene 3 valores distintos" in r["reason"] and "msg son códigos cortos (sqli, xss" in r["reason"]


def test_son_frases():
    assert busqueda.son_frases(["Myocardial Infarction", "Cardiac Arrest", "Fever"])
    assert not busqueda.son_frases(["sqli", "xss", "path_traversal", "Adobe.Flash.Execution"])
    assert busqueda.son_frases([]), "sin muestra no se descarta"
    assert busqueda.campos_de_texto_libre([F("logdesc"), F("msg"), F("reason"), F("codigo")]) == ["logdesc", "msg", "reason"]


def test_el_catalogo_no_cuenta_donde_no_aplica(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {})
    monkeypatch.setattr(main, "_read_security", lambda td: {})
    monkeypatch.setattr(main, "_read_estados", lambda td: {
        "a": {"busqueda": {"ok": True}}, "b": {"busqueda": {"ok": False, "no_aplica": True}}})
    reg = {s: {"index": f"{s}-*", "fields": [F("@timestamp", "date", role="timestamp"), F("comentario", "text")]} for s in ("a", "b")}
    cat = {c["plugin"]: c for c in main._catalogo_de_plugins(tmp_path, reg)}
    assert cat["busqueda"]["aplica"] == ["a"] and cat["busqueda"]["aplicado"] == ["a"], "lista, no «1 de 2»"


def test_si_deja_de_aplicar_se_va_lo_que_quedo(monkeypatch):
    pedidos = _cluster(monkeypatch, distintos=3, transform_existe=True)
    r = main._provisionar_busqueda("http://x", "a", "p", "s", "s-*", [F("msg")], "M1", force=True)
    assert r["no_aplica"]
    for esperado in (("POST", "/_plugins/_transform/s-busqueda/_stop"), ("DELETE", "/_plugins/_transform/s-busqueda?force=true"),
                     ("DELETE", "/busqueda-s")):
        assert esperado in [(m, u) for m, u, _ in pedidos], esperado
    pedidos = _cluster(monkeypatch, distintos=3, transform_existe=False)
    main._provisionar_busqueda("http://x", "a", "p", "s", "s-*", [F("msg")], "M1", force=True)
    assert not [u for m, u, _ in pedidos if m == "DELETE"], "si no había nada, no borra"


def test_rehacer_espera_a_que_el_anterior_se_borre(monkeypatch):
    """Visto: rehacer la de Streaming OTT dio 409 (version conflict): el PUT
    del Transform nuevo llegaba antes de que terminara de borrarse el viejo."""
    pedidos, gets = [], {"n": 0}

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        pedidos.append((m, url.replace("http://x", "")))
        if m == "GET" and url.endswith("/_transform/s-busqueda"):
            gets["n"] += 1
            return _R(200, {"_id": "t"}) if gets["n"] < 4 else _R(404)   # tarda en irse
        if url.endswith("/_search"):
            return _R(200, {"aggregations": {"n": {"value": 50}}})
        return _R(200, {"acknowledged": True})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_motivo_si_fallo", lambda ruta, u, p: None)
    assert main._provisionar_busqueda("http://x", "a", "p", "s", "s-*", [F("msg")], "M1", force=True)["ok"]
    borrado = pedidos.index(("DELETE", "/_plugins/_transform/s-busqueda?force=true"))
    ultimo_get = max(i for i, (m, u) in enumerate(pedidos) if m == "GET" and u.endswith("/_transform/s-busqueda"))
    assert borrado < ultimo_get < pedidos.index(("PUT", "/_plugins/_transform/s-busqueda")), "crea recién cuando ya no está"


def test_las_configuraciones_de_search_relevance():
    """Single Query Comparison (Dashboards) compara Search Configurations
    guardadas: la plataforma deja las dos de cada caso, sin pegar consultas."""
    import json as _json
    palabras, hibrida = busqueda.configuraciones_de_busqueda("ventas", "Ventas", "Q1")
    assert palabras["name"] == "Ventas · por palabras" and hibrida["name"] == "Ventas · híbrida"
    assert palabras["index"] == hibrida["index"] == "busqueda-ventas"
    assert isinstance(palabras["query"], str) and "%SearchText%" in palabras["query"], "el query va como texto"
    assert "searchPipeline" not in palabras and hibrida["searchPipeline"] == "plataforma-hibrida"
    assert _json.loads(hibrida["query"])["query"]["hybrid"]["queries"][1]["neural"]["embedding"]["model_id"] == "Q1"


def test_se_rehacen_al_armar_la_busqueda(monkeypatch):
    pedidos = []

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        pedidos.append((m, url.replace("http://x", ""), json_body))
        if m == "GET" and url.endswith("/search_configurations"):
            return _R(200, {"hits": {"hits": [{"_id": "VIEJA", "_source": {"name": "Ventas · híbrida"}},
                                              {"_id": "OTRA", "_source": {"name": "De otro"}}]}})
        return _R(200, {"_id": "nueva"})

    monkeypatch.setattr(main, "_os_req", req)
    assert main._configurar_search_relevance("http://x", "a", "p", "ventas", "Ventas", "Q1") == ""
    assert ("DELETE", "/_plugins/_search_relevance/search_configurations/VIEJA", None) in pedidos
    assert not [u for m, u, _ in pedidos if m == "DELETE" and u.endswith("OTRA")], "las de otros, no"
    assert [b["name"] for m, u, b in pedidos if m == "PUT"] == ["Ventas · por palabras", "Ventas · híbrida"]


# ── Qué escribir en Search ──────────────────────────────────────────────────
def test_las_busquedas_propuestas_se_validan():
    textos = ["Admin login failed", "Fortigate CPU usage high", "Línea\n  partida"]
    datos = {"busquedas": [
        {"busqueda": "no puede entrar", "texto": "Admin login failed"},
        {"busqueda": "otra para el mismo", "texto": "Admin login failed"},       # ese texto ya tiene búsqueda
        {"busqueda": "inventado", "texto": "No existe"},                        # no es uno de los textos
        {"busqueda": "x", "texto": "Fortigate CPU usage high"},                  # muy corta
        {"busqueda": "fortigate cpu usage high", "texto": "Fortigate CPU usage high"},  # el mismo texto
        "basura", {"busqueda": 3, "texto": "Fortigate CPU usage high"},
        {"busqueda": "  línea   cortada ", "texto": "Línea partida"}]}           # como quedó en el prompt
    assert busqueda.candidatas(datos, textos) == [
        {"busqueda": "no puede entrar", "objetivo": "Admin login failed"},
        {"busqueda": "línea cortada", "objetivo": "Línea\n  partida"}]
    assert busqueda.candidatas({"busquedas": "no"}, textos) == [] and busqueda.candidatas(None, textos) == []
    # El LLM equivocó la clave ("buquedas") y se perdían todas: vale la primera lista.
    assert busqueda.candidatas({"buquedas": [{"busqueda": "no puede entrar", "texto": "Admin login failed"}]}, textos) == \
        [{"busqueda": "no puede entrar", "objetivo": "Admin login failed"}]
    assert busqueda.consultas_a_evaluar({"consulta": ["dolor de cabeza"]}) == ["dolor de cabeza"]
    p = busqueda.prompt_de_busquedas(textos, "msg", "FortiAnalyzer")
    assert "\nLínea partida" in p and "\nAdmin login failed" in p and f"Proponé {busqueda.CANDIDATAS} búsquedas" in p
    assert "los valores del campo «msg» de «FortiAnalyzer»" in p, "de dónde son: «Health» no es bienestar físico"
    assert "los textos de un caso" in busqueda.prompt_de_busquedas(textos)


def test_una_sugerencia_prueba_la_diferencia():
    c = {"busqueda": "no puede entrar", "objetivo": "Admin login failed"}
    hits = lambda *ts: [{"texto": t} for t in ts]  # noqa: E731
    assert busqueda.probar(c, hits("Otro"), hits("X", "Admin login failed")) == {
        "busqueda": "no puede entrar", "encuentra": "Admin login failed", "puesto": 2, "por_palabras": 1,
        "puesto_por_palabras": 0}
    assert busqueda.probar(c, hits("Admin login failed"), hits("Admin login failed")) is None, "por palabras también"
    assert busqueda.probar(c, hits("a", "b", "Admin login failed"), hits("Admin login failed")) is None, "casi igual de arriba"
    # En castellano casi siempre está entre los diez por palabras: vale si queda bastante más abajo.
    assert busqueda.probar(c, hits(*"abcdef", "Admin login failed"), hits("Admin login failed"))["puesto_por_palabras"] == 7
    assert busqueda.probar(c, hits(), hits(*"abcde", "Admin login failed")) is None, "la híbrida, no entre los primeros"
    p = lambda puesto, n: {"puesto": puesto, "por_palabras": n}  # noqa: E731
    assert busqueda.elegir([p(3, 0), p(1, 5), p(1, 0), p(2, 0)]) == [p(1, 0), p(1, 5), p(2, 0)]


def test_que_escribir_en_search(monkeypatch, tmp_path):
    """Las sugerencias salen de los textos del caso (LLM) y quedan solo las que
    el cluster prueba: la híbrida trae el texto y por palabras no."""
    from fastapi.testclient import TestClient
    import semantica
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    c = TestClient(main.app)
    url = "/api/v1/plugins/busqueda/sugerencias?slug=ventas"
    assert c.get(url).status_code == 409, "sin armar"
    monkeypatch.setattr(main, "_read_estados", lambda td: {
        "_cluster": {"embeddings": {"model_id_consulta": "CONSULTAS"}}, "ventas": {"busqueda": {"ok": True, "campo": "comentario"}}})
    monkeypatch.setattr(main, "_cluster_del_entorno", lambda stage: ("http://x", "a", "p"))
    textos = ["llegó roto", "tardó un mes", "excelente atención"]
    pedidos, llm = [], []
    hits = lambda *ts: _R(200, {"hits": {"hits": [{"_source": {"texto": t}} for t in ts]}})  # noqa: E731

    def req(m, u, user, password, json_body=None, timeout=30, headers=None):
        pedidos.append(u.replace("http://x", ""))
        if json_body.get("sort"):
            return hits(*textos)
        if "hybrid" in json_body["query"]:
            assert u.endswith("?search_pipeline=plataforma-hibrida")
            neural = json_body["query"]["hybrid"]["queries"][1]["neural"]["embedding"]
            assert neural["model_id"] == "CONSULTAS"
            return hits({"producto dañado": "llegó roto", "demora en la entrega": "tardó un mes"}[neural["query_text"]])
        return hits(*{"demora en la entrega": ["tardó un mes"]}.get(json_body["query"]["multi_match"]["query"], []))

    def llamar(prompt):
        llm.append(prompt)
        return json.dumps({"busquedas": [{"busqueda": "producto dañado", "texto": "llegó roto"},
                                         {"busqueda": "demora en la entrega", "texto": "tardó un mes"}]})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(semantica, "_llamar_al_llm", llamar)
    d = c.get(url).json()
    assert d == {"sugerencias": [{"busqueda": "producto dañado", "encuentra": "llegó roto", "puesto": 1, "por_palabras": 0,
                                  "puesto_por_palabras": 0}],
                 "ejemplos": textos, "motivo": ""}, "la de la demora, por palabras también la encuentra"
    assert "llegó roto" in llm[0] and "«comentario» de «ventas»" in llm[0]
    assert all(u.startswith("/busqueda-ventas/_search") for u in pedidos)
    # Guardadas: la segunda vez, ni el LLM ni el cluster; `nuevas` las rehace.
    n = len(pedidos)
    assert c.get(url).json()["sugerencias"] == d["sugerencias"] and len(llm) == 1 and len(pedidos) == n
    c.get(url + "&nuevas=1")
    assert len(llm) == 2
    # Sin el LLM: textos que hay, para buscarlos con otras palabras.
    monkeypatch.setattr(semantica, "_llamar_al_llm", lambda p: (_ for _ in ()).throw(RuntimeError("sin key")))
    d = c.get(url + "&nuevas=1").json()
    assert d["sugerencias"] == [] and d["ejemplos"] == textos and "RuntimeError" in d["motivo"]


# ── La evaluación en Search Relevance ───────────────────────────────────────
def test_el_juez_usa_el_formato_de_search_relevance():
    import capabilities
    c = capabilities.build_judge_connector("k-de-prueba", endpoint="maas.example")
    accion = c["actions"][0]
    assert '"messages": ${parameters.messages}' in accion["request_body"], "el plugin manda la conversación armada"
    assert '"thinking": false' in accion["request_body"] and c["credential"] == {"maas_key": "k-de-prueba"}
    assert "post_process_function" not in accion, "el plugin lee choices[0].message.content crudo"
    assert accion["url"] == "https://${parameters.endpoint}/openai/v1/chat/completions"


def test_las_busquedas_a_evaluar_y_los_pedidos():
    assert busqueda.consultas_a_evaluar({"consultas": ["dolor  de cabeza", "Dolor de cabeza", "x", 3, "control de embarazo",
                                                       "a" * 81]}) == ["dolor de cabeza", "control de embarazo"]
    assert busqueda.consultas_a_evaluar({"consultas": [f"q{i}" for i in range(30)]}) == [f"q{i}" for i in range(10)]
    assert busqueda.consultas_a_evaluar(None) == [] and busqueda.consultas_a_evaluar({"consultas": "no"}) == []
    p = busqueda.prompt_de_consultas(["Prenatal visit"], "desc", "Encuentros clínicos")
    assert "Escribí 10 búsquedas realistas" in p and "La mitad con palabras que aparecen en los textos" in p
    assert "«desc» de «Encuentros clínicos»" in p and p.endswith("Prenatal visit\n")
    largo = busqueda.nombres_de_evaluacion("x" * 60)
    assert len(largo["query_set"]) == len(largo["juicios"]) == 50, "el tope de Search Relevance"
    qs = busqueda.query_set("Ventas", ["a b", "c d"])
    assert qs["name"] == "Ventas · búsquedas" and qs["sampling"] == "manual" and len(qs["description"]) <= 250
    assert qs["querySetQueries"] == [{"queryText": "a b"}, {"queryText": "c d"}]
    j = busqueda.juicios("Ventas", "QS", ["P", "H"], "JUEZ")
    assert j == {"name": "Ventas · juicios del LLM", "type": "LLM_JUDGMENT", "modelId": "JUEZ", "querySetId": "QS",
                 "searchConfigurationList": ["P", "H"], "size": 10, "contextFields": ["texto"]}
    pares, eval_p, eval_h = busqueda.experimentos("QS", ["P", "H"], "J")
    assert pares == {"querySetId": "QS", "size": 10, "searchConfigurationList": ["P", "H"], "type": "PAIRWISE_COMPARISON"}
    assert eval_p["searchConfigurationList"] == ["P"] and eval_h["searchConfigurationList"] == ["H"], "una por evaluación"
    assert eval_p["judgmentList"] == ["J"] and eval_p["type"] == "POINTWISE_EVALUATION"


class _SR:
    """Un Search Relevance de mentira: lo que hay, lo que se borra y se crea."""

    def __init__(self, hay=None, estado="COMPLETED", juez_contesta=True):
        self.hay = hay or {}
        self.estado, self.juez_contesta, self.pedidos = estado, juez_contesta, []

    def __call__(self, m, url, user, password, json_body=None, timeout=30, headers=None):
        ruta = url.replace("http://x/", "")
        self.pedidos.append((m, ruta, json_body))
        if ruta.endswith("/_search") and ruta.startswith("busqueda-"):
            return _R(200, {"hits": {"hits": [{"_source": {"texto": "Prenatal visit"}}]}})
        if ruta.endswith("/_predict"):
            return _R(200, {"inference_results": [{"output": [{"dataAsMap": {"choices": [{"message": {"content": "ok"}}]}}]}]}) \
                if self.juez_contesta else _R(500, {})
        if m == "GET" and ruta.startswith("_plugins/_search_relevance/"):
            partes = ruta.split("/")
            if len(partes) == 3:
                assert json_body == {"size": 1000}, "el tamaño, en el body"
                return _R(200, {"hits": {"hits": [{"_id": i, "_source": s} for i, s in self.hay.get(partes[2], [])]}})
            return _R(200, {"hits": {"hits": [{"_source": {"status": self.estado}}]}})
        if m == "PUT":
            clave = {"query_sets": "query_set_id", "judgments": "judgment_id", "experiments": "experiment_id"}[ruta.split("/")[-1]]
            return _R(200, {clave: f"nuevo-{ruta.split('/')[-1]}"})
        return _R(200, {})


def test_la_evaluacion_se_rehace_entera(monkeypatch):
    import semantica
    sr = _SR(hay={
        "search_configurations": [("P", {"name": "Ventas · por palabras"}), ("H", {"name": "Ventas · híbrida"})],
        "query_sets": [("QS-VIEJO", {"name": "Ventas · búsquedas"}), ("QS-OTRO", {"name": "De otro"})],
        "experiments": [("E-VIEJO", {"querySetId": "QS-VIEJO"}), ("E-OTRO", {"querySetId": "QS-OTRO"})],
        "judgments": [("J-VIEJO", {"name": "Ventas · juicios del LLM"}), ("J-OTRO", {"name": "x"})]})
    monkeypatch.setattr(main, "_os_req", sr)
    monkeypatch.setattr(main, "_SONDEO_SR_S", 0)
    prompts = []
    monkeypatch.setattr(semantica, "_llamar_al_llm", lambda p: prompts.append(p) or json.dumps(
        {"consultas": ["prenatal visit", "control de embarazo", "chequeo del bebé"]}))
    r = main._evaluar_busqueda("http://x", "a", "p", "ventas", "Ventas", "comentario", ("JUEZ", ""))
    assert r == {"ok": True, "consultas": 3, "motivo": "3 búsquedas juzgadas por el LLM"}
    borrados = [ruta.split("/")[-1] for m, ruta, _ in sr.pedidos if m == "DELETE"]
    assert borrados == ["E-VIEJO", "J-VIEJO", "QS-VIEJO"], "lo del caso, y nada de otros"
    creados = [(ruta.split("/")[-1], b) for m, ruta, b in sr.pedidos if m == "PUT"]
    assert [c for c, _ in creados] == ["query_sets", "judgments", "experiments", "experiments", "experiments"]
    assert creados[0][1]["querySetQueries"][1] == {"queryText": "control de embarazo"}
    assert creados[1][1]["modelId"] == "JUEZ" and creados[1][1]["searchConfigurationList"] == ["P", "H"]
    assert "Prenatal visit" in prompts[0] and "«comentario» de «Ventas»" in prompts[0]
    # Sin el juez, sin el LLM o con un experimento roto: dice por qué (y saca lo viejo igual).
    assert main._evaluar_busqueda("http://x", "a", "p", "ventas", "Ventas", "c", ("", "sin API key de MaaS")) == \
        {"ok": False, "motivo": "sin el juez: sin API key de MaaS"}
    monkeypatch.setattr(semantica, "_llamar_al_llm", lambda p: (_ for _ in ()).throw(TimeoutError()))
    assert main._evaluar_busqueda("http://x", "a", "p", "ventas", "Ventas", "c", ("JUEZ", ""))["motivo"] == \
        "el LLM no propuso búsquedas (TimeoutError)"
    monkeypatch.setattr(semantica, "_llamar_al_llm", lambda p: json.dumps({"consultas": ["a b", "c d", "e f"]}))
    sr.estado = "ERROR"
    assert main._evaluar_busqueda("http://x", "a", "p", "ventas", "Ventas", "c", ("JUEZ", ""))["motivo"] == \
        "los juicios del LLM no terminaron (ERROR)"


def test_el_juez_se_reusa_si_contesta_y_si_no_se_rehace(monkeypatch):
    import maas_integrator
    monkeypatch.setattr(maas_integrator, "get_maas_api_key", lambda: "k")
    monkeypatch.setattr(main, "_modelos_por_nombre", lambda b, u, p, nombre: ["VIEJO"] if nombre == busqueda.NOMBRE_DEL_JUEZ else [])
    sr = _SR()
    monkeypatch.setattr(main, "_os_req", sr)
    assert main._asegurar_juez("http://x", "a", "p") == ("VIEJO", "")
    assert not [p for p in sr.pedidos if p[0] == "DELETE"]
    borrados, creados = [], []
    monkeypatch.setattr(main, "_borrar_modelo_remoto", lambda b, u, p, mid: borrados.append(mid))
    monkeypatch.setattr(main, "_ml_create", lambda b, u, p, ruta, cuerpo, clave: creados.append(cuerpo) or "CON")
    monkeypatch.setattr(main, "_estado_del_modelo", lambda *a: "DEPLOYED")
    contesta = iter([False, True])   # el viejo no (otra key), el nuevo sí

    def req(m, url, *a, json_body=None, **k):
        if url.endswith("/_predict"):
            return _R(200, {"inference_results": [{"output": [{"dataAsMap": {"choices": [{"message": {"content": "ok"}}]}}]}]}) \
                if next(contesta) else _R(500, {})
        return _R(200, {"model_id": "NUEVO"})

    monkeypatch.setattr(main, "_os_req", req)
    assert main._asegurar_juez("http://x", "a", "p") == ("NUEVO", "")
    assert borrados == ["VIEJO"] and creados[0]["credential"] == {"maas_key": "k"}
    monkeypatch.setattr(maas_integrator, "get_maas_api_key", lambda: "")
    assert main._asegurar_juez("http://x", "a", "p") == ("", "sin API key de MaaS")


def test_la_evaluacion_al_activar_y_no_se_rehace_si_ya_esta(monkeypatch, tmp_path):
    estados = {}
    monkeypatch.setattr(main, "_read_estados", lambda td: estados)
    monkeypatch.setattr(main, "_guardar_estados", lambda td, slug, nuevo: estados.setdefault(slug, {}).update(nuevo))
    monkeypatch.setattr(main, "_salida_a_nvidia", lambda *a: None)
    monkeypatch.setattr(main, "_asegurar_modelo_de_embeddings", lambda *a: ({"passage": "M1", "query": "Q1"}, ""))
    monkeypatch.setattr(main, "_esperar_cluster_libre", lambda *a: None)
    monkeypatch.setattr(main, "_provisionar_busqueda", lambda *a: {"ok": True, "campo": "msg", "textos": 10, "reason": "ok"})
    monkeypatch.setattr(main, "_configurar_search_relevance", lambda *a: "")
    jueces, evaluadas = [], []
    monkeypatch.setattr(main, "_asegurar_juez", lambda *a: jueces.append(1) or ("JUEZ", ""))
    monkeypatch.setattr(main, "_evaluar_busqueda", lambda b, u, p, slug, label, campo, juez: evaluadas.append((slug, campo, juez))
                        or {"ok": True, "consultas": 10, "motivo": "10 búsquedas juzgadas por el LLM"})
    pipe = {"a": {"fields": [F("msg")]}, "b": {"fields": [F("msg")]}}
    activar = lambda force=False: main._busqueda_original({"public_endpoint": "x:9200"}, "a", "p", False, tmp_path,  # noqa: E731
                                                          ["a", "b"], pipe, force, main.runs.start("capabilities"))
    activar()
    assert len(jueces) == 1, "un juez para todos los casos"
    assert evaluadas == [("a", "msg", ("JUEZ", "")), ("b", "msg", ("JUEZ", ""))]
    assert estados["a"]["busqueda"]["evaluacion"] == {"ok": True, "consultas": 10, "motivo": "10 búsquedas juzgadas por el LLM",
                                                      "modelo": "Q1"}
    activar()
    assert len(evaluadas) == 2, "ya armada y con el mismo modelo: no se rehace"
    activar(force=True)
    assert len(evaluadas) == 4
    # Un "ya estaba" de la búsqueda igual guarda la evaluación nueva.
    estados["a"]["busqueda"] = {"ok": True, "motivo": "1234 textos", "campo": "msg"}
    monkeypatch.setattr(main, "_provisionar_busqueda", lambda *a: {"ok": True, "campo": "msg", "reason": "ya estaba"})
    activar()
    assert estados["a"]["busqueda"]["motivo"] == "1234 textos" and estados["a"]["busqueda"]["evaluacion"]["ok"]


def test_un_link_a_cada_experimento_del_caso(monkeypatch, tmp_path):
    """En Search Relevance, la lista de Experiments tiene los de todos los
    casos (y los ítems del menú son formularios): el panel lleva a cada uno."""
    from fastapi.testclient import TestClient
    sr = _SR(hay={
        "query_sets": [("QS", {"name": "Ventas · búsquedas"}), ("QS-OTRO", {"name": "Otro · búsquedas"})],
        "search_configurations": [("P", {"name": "Ventas · por palabras"}), ("H", {"name": "Ventas · híbrida"})],
        "experiments": [
            ("E1", {"querySetId": "QS", "type": "POINTWISE_EVALUATION", "searchConfigurationList": ["H"], "status": "COMPLETED", "timestamp": "2026-10-13T00:52"}),
            ("E2", {"querySetId": "QS", "type": "POINTWISE_EVALUATION", "searchConfigurationList": ["H"], "status": "COMPLETED", "timestamp": "2026-10-13T09:00"}),
            ("E3", {"querySetId": "QS", "type": "POINTWISE_EVALUATION", "searchConfigurationList": ["P"], "status": "COMPLETED", "timestamp": "2026-10-13T00:53"}),
            ("E4", {"querySetId": "QS", "type": "PAIRWISE_COMPARISON", "searchConfigurationList": ["P", "H"], "status": "PROCESSING", "timestamp": "2026-10-13T00:54"}),
            ("E5", {"querySetId": "QS-OTRO", "type": "PAIRWISE_COMPARISON", "searchConfigurationList": ["X", "Y"], "status": "COMPLETED"})]})
    monkeypatch.setattr(main, "_os_req", sr)
    assert main._experimentos_del_caso("http://x", "a", "p", "Ventas") == {"hibrida": "E2", "palabras": "E3"}, \
        "el último de cada uno, terminado y del caso"
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_read_estados", lambda td: {
        "_cluster": {"embeddings": {"model_id_consulta": "Q"}},
        "ventas": {"busqueda": {"ok": True, "evaluacion": {"ok": True, "consultas": 10}}}})
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_build_dashboards_url", lambda cluster, https: "https://dash.example/app/home")
    monkeypatch.setattr(main, "_cluster_admin_password", lambda td: "pw")
    monkeypatch.setattr(main, "_experimentos_del_caso", lambda b, u, p, label: {"hibrida": "E2"} if label == "ventas" else {})
    d = TestClient(main.app).get("/api/v1/plugins/busqueda/comparar?slug=ventas").json()
    assert d["experimentos"] == {"hibrida": "https://dash.example/app/searchRelevance#/experiment/view/E2"}
    monkeypatch.setattr(main, "_experimentos_del_caso", lambda *a: (_ for _ in ()).throw(ConnectionError()))
    d = TestClient(main.app).get("/api/v1/plugins/busqueda/comparar?slug=ventas").json()
    assert d["experimentos"] == {} and d["url_experimentos"].endswith("#/experiment"), "sin cluster, queda la lista"



def test_la_observacion_es_texto_libre_y_gana_a_una_categoria():
    """Logística (visto en la ECS): `evento.observacion` no se reconocía y la
    búsqueda se armó sobre `evento.motivo`, una categoría de 14 valores."""
    fields = [
        {"field_path": "evento.motivo", "type": "string", "role": "critical_indicator", "dimension": True},
        {"field_path": "evento.observacion", "type": "string", "role": None, "dimension": False},
    ]
    assert busqueda.campos_de_texto_libre(fields) == ["evento.observacion", "evento.motivo"]
    assert busqueda.campo_de_texto_libre([{"field_path": "notas_del_operador", "type": "keyword"}]) == "notas_del_operador"
    # A igual pista, la que no es categoría primero; el orden de las pistas no cambia.
    assert busqueda.campos_de_texto_libre([
        {"field_path": "logdesc", "type": "keyword", "dimension": True},
        {"field_path": "msg", "type": "keyword", "dimension": False},
        {"field_path": "detalle", "type": "keyword", "dimension": False}]) == ["msg", "logdesc", "detalle"]


def test_el_campo_de_un_transform_ya_creado():
    cuerpo = {"_id": "t", "transform": {"groups": [{"terms": {"source_field": "evento.motivo", "target_field": "texto"}}]}}
    assert busqueda.campo_del_transform(cuerpo) == "evento.motivo"
    assert busqueda.campo_del_transform({}) == "" and busqueda.campo_del_transform(None) == ""



def test_la_evaluacion_espera_textos_vectorizados(monkeypatch):
    """Evaluaba segundos después de crear el Transform y el índice estaba vacío
    (en la ECS, en los cinco casos). Espera a que haya textos con vector."""
    cuentas = iter([0, 0, 7, 25])
    sondeos = []

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        if url.endswith("/_count"):
            assert {"exists": {"field": "embedding"}} in json_body["query"]["bool"]["must"]
            return _R(200, {"count": next(cuentas)})
        return _R(200, {})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main.time, "sleep", lambda s: sondeos.append(s))
    assert main._esperar_textos_vectorizados("http://x", "a", "p", "s") == 25
    assert len(sondeos) == 3
    # Pocos textos que ya no suben: no espera hasta el tope.
    cuentas = iter([3] * 10)
    sondeos.clear()
    assert main._esperar_textos_vectorizados("http://x", "a", "p", "s") == 3 and len(sondeos) == 4
    # Si el cluster no dice cuántos, no espera.
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: _R(200, {"acknowledged": True}))
    sondeos.clear()
    assert main._esperar_textos_vectorizados("http://x", "a", "p", "s") is None and not sondeos



def test_gana_el_texto_libre_con_mas_textos(monkeypatch):
    """Prepaga (visto en la ECS): con «observación» como pista, la búsqueda pasó
    de `diagnostico` (375 textos) a `observacion_auditor` (13). De los que
    califican, gana el de más textos distintos."""
    distintos = {"observacion_auditor": 13, "diagnostico": 375}

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        if url.endswith("/_search") and json_body and "aggs" in json_body:
            campo = json_body["aggs"]["n"]["cardinality"]["field"]
            return _R(200, {"aggregations": {"n": {"value": distintos[campo]}, "muestra": {"buckets": [
                {"key": "dolor lumbar con irradiación"}, {"key": "se autoriza con la documentación"}]}}})
        if m == "GET" and "/_transform/" in url:
            return _R(404)
        return _R(200, {"acknowledged": True})

    monkeypatch.setattr(main, "_os_req", req)
    campos = [F("observacion_auditor"), F("diagnostico")]
    assert busqueda.campos_de_texto_libre(campos)[0] == "observacion_auditor", "la pista la pone primera..."
    r = main._provisionar_busqueda("http://x", "a", "p", "prepaga", "prepaga-*", campos, "M1", force=False)
    assert r["ok"] and r["campo"] == "diagnostico" and r["textos"] == 375, "...pero gana la de más textos"
