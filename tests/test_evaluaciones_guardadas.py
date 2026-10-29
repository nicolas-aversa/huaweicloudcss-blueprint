"""La evaluación de la búsqueda, juzgada una sola vez por el LLM.

Eran dos minutos por caso de llamadas al LLM en cada "Activar los plugins"
(19 de 26 minutos con 11 casos). Con el mismo dataset da lo mismo: la primera
vez se guardan las búsquedas y la nota de cada texto, y después se importan
(IMPORT_JUDGMENT) sin llamar al LLM ni armar el juez.
"""
import json

import busqueda
import evaluaciones_guardadas as eg
import main
import semantica

TEXTOS = {"t1": "falla en la bomba", "t2": "pérdida en la línea", "t3": "sin comunicación con la RTU"}


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


class _Cluster:
    """El índice de búsqueda (ids distintos en cada entorno) y Search Relevance."""

    def __init__(self, ids=None, hay=None):
        self.ids = ids or {"t1": "A", "t2": "B", "t3": "C"}          # texto → id en ESTE entorno
        self.hay = hay or {}
        self.pedidos = []

    def __call__(self, m, url, user, password, json_body=None, timeout=30, headers=None):
        ruta = url.replace("http://x/", "")
        self.pedidos.append((m, ruta, json_body))
        de_id = {v: TEXTOS[k] for k, v in self.ids.items()}
        if "/_mget" in ruta:
            if set(json_body) != {"ids"}:   # como OpenSearch: `_source` en el body es un 400
                return _R(400, {"error": "unknown key [_source] for a START_ARRAY, expected [docs] or [ids]"})
            return _R(200, {"docs": [{"_id": i, "found": i in de_id, "_source": {"texto": de_id.get(i)}}
                                     for i in json_body["ids"]]})
        if ruta.endswith("/_search") and ruta.startswith("busqueda-"):
            pedidos = json_body["query"]["terms"]["texto.keyword"]
            return _R(200, {"hits": {"hits": [{"_id": i, "_source": {"texto": t}} for i, t in de_id.items()
                                              if t in pedidos]}})
        if m == "GET" and ruta.startswith("_plugins/_search_relevance/"):
            partes = ruta.split("/")
            if len(partes) == 3:
                return _R(200, {"hits": {"hits": [{"_id": i, "_source": s} for i, s in self.hay.get(partes[2], [])]}})
            if partes[2] == "judgments":
                return _R(200, {"hits": {"hits": [{"_source": {"status": "COMPLETED", "judgmentRatings": [
                    {"query": "bomba que falla", "ratings": [{"docId": "A", "rating": "1.0"}, {"docId": "B", "rating": "0.0"}]},
                    {"query": "corte de comunicación", "ratings": [{"docId": "C", "rating": "1.0"}]}]}}]}})
            return _R(200, {"hits": {"hits": [{"_source": {"status": "COMPLETED"}}]}})
        if m == "PUT":
            clave = {"query_sets": "query_set_id", "judgments": "judgment_id", "experiments": "experiment_id"}[ruta.split("/")[-1]]
            return _R(200, {clave: f"nuevo-{ruta.split('/')[-1]}"})
        return _R(200, {})

    def creados(self, que):
        return [b for m, ruta, b in self.pedidos if m == "PUT" and ruta.endswith(que)]


def _preparar(monkeypatch, cluster, llm):
    monkeypatch.setattr(main, "_os_req", cluster)
    monkeypatch.setattr(main, "_SONDEO_SR_S", 0)
    monkeypatch.setattr(main, "_esperar_textos_vectorizados", lambda *a: 25)
    monkeypatch.setattr(main, "_textos_del_caso", lambda *a: list(TEXTOS.values()))
    monkeypatch.setattr(semantica, "_llamar_al_llm", lambda p: llm.append(p) or json.dumps(
        {"consultas": ["bomba que falla", "corte de comunicación", "pérdidas"]}))
    cluster.hay["search_configurations"] = [("P", {"name": "Pozos · por palabras"}), ("H", {"name": "Pozos · híbrida"})]


def _evaluar(jueces):
    return main._evaluar_busqueda("http://x", "a", "p", "pozos", "Pozos", "comentario",
                                  lambda: jueces.append(1) or ("JUEZ", ""), modelo="M1")


def test_la_primera_vez_juzga_el_llm_y_quedan_guardados_por_texto(monkeypatch):
    cluster, llm, jueces = _Cluster(), [], []
    _preparar(monkeypatch, cluster, llm)
    r = _evaluar(jueces)
    assert r["ok"] and r["motivo"] == "3 búsquedas juzgadas por el LLM"
    assert len(llm) == 1 and jueces == [1] and cluster.creados("judgments")[0]["type"] == "LLM_JUDGMENT"
    guardada = eg.leer("pozos", "comentario", "M1")
    assert guardada["consultas"] == ["bomba que falla", "corte de comunicación", "pérdidas"]
    assert guardada["juicios"] == {"bomba que falla": [["falla en la bomba", "1.0"], ["pérdida en la línea", "0.0"]],
                                   "corte de comunicación": [["sin comunicación con la RTU", "1.0"]]}
    assert eg.leer("pozos", "otro_campo", "M1") is None and eg.leer("pozos", "comentario", "M2") is None


def test_despues_se_importan_sin_llm_ni_juez_aunque_cambien_los_ids(monkeypatch):
    eg.guardar("pozos", "comentario", "M1", ["bomba que falla", "corte de comunicación"],
               {"bomba que falla": [["falla en la bomba", "1.0"], ["pérdida en la línea", "0.0"]],
                "corte de comunicación": [["sin comunicación con la RTU", "1.0"]]})
    cluster, llm, jueces = _Cluster(ids={"t1": "X1", "t2": "X2", "t3": "X3"}), [], []   # otro entorno, otros ids
    _preparar(monkeypatch, cluster, llm)
    r = _evaluar(jueces)
    assert r == {"ok": True, "consultas": 2,
                 "motivo": "2 búsquedas con los juicios del LLM guardados (sin volver a llamarlo)"}
    assert llm == [] and jueces == [], "ni el LLM ni el juez"
    assert cluster.creados("query_sets")[0]["querySetQueries"] == [{"queryText": "bomba que falla"},
                                                                    {"queryText": "corte de comunicación"}]
    juicio = cluster.creados("judgments")[0]
    assert juicio == {"name": "Pozos · juicios del LLM", "type": "IMPORT_JUDGMENT", "judgmentRatings": [
        {"query": "bomba que falla", "ratings": [{"docId": "X1", "rating": "1.0"}, {"docId": "X2", "rating": "0.0"}]},
        {"query": "corte de comunicación", "ratings": [{"docId": "X3", "rating": "1.0"}]}]}
    assert len(cluster.creados("experiments")) == 3, "los experimentos se corren igual"


def test_si_el_dataset_cambio_se_vuelve_a_juzgar(monkeypatch):
    """Con menos de COBERTURA_MINIMA de los textos en el índice, el caso es otro."""
    eg.guardar("pozos", "comentario", "M1", ["bomba que falla"],
               {"bomba que falla": [["falla en la bomba", "1.0"], ["un texto que ya no está", "1.0"],
                                    ["otro que tampoco", "0.0"]]})
    cluster, llm, jueces = _Cluster(), [], []
    _preparar(monkeypatch, cluster, llm)
    r = _evaluar(jueces)
    assert r["motivo"] == "3 búsquedas juzgadas por el LLM" and len(llm) == 1 and jueces == [1]


def test_lo_que_ya_estaba_en_el_cluster_se_guarda_para_la_proxima(monkeypatch):
    """Un entorno evaluado antes de que se guardaran: "Volver a activar" los
    toma del cluster (por nombre) y quedan para los próximos deploys."""
    cluster = _Cluster(hay={
        "judgments": [("J", {"name": "Pozos · juicios del LLM", "status": "COMPLETED", "judgmentRatings": [
            {"query": "bomba que falla", "ratings": [{"docId": "A", "rating": "1.0"}, {"docId": "ZZ", "rating": "0.5"}]}]})],
        "query_sets": [("Q", {"name": "Pozos · búsquedas", "querySetQueries": [{"queryText": "bomba que falla"}]})]})
    monkeypatch.setattr(main, "_os_req", cluster)
    assert main._recordar_evaluacion("http://x", "a", "p", "pozos", "Pozos", "comentario", "M1") is True
    assert eg.leer("pozos", "comentario", "M1")["juicios"] == {"bomba que falla": [["falla en la bomba", "1.0"]]}, \
        "un documento que ya no está se descarta"
    cluster.pedidos.clear()
    assert main._recordar_evaluacion("http://x", "a", "p", "pozos", "Pozos", "comentario", "M1") is True
    assert cluster.pedidos == [], "ya guardada: ni la busca"
    sin_nada = _Cluster()
    monkeypatch.setattr(main, "_os_req", sin_nada)
    assert main._recordar_evaluacion("http://x", "a", "p", "otro", "Otro", "comentario", "M1") is False
    assert eg.leer("otro", "comentario", "M1") is None


def test_las_notas_por_texto_y_la_cobertura():
    guardada = {"consultas": ["q1", "q2"], "juicios": {"q1": [["a", "1.0"], ["b", "0.0"]], "q2": [["c", "0.5"]]}}
    cuerpo, cobertura = eg.para_importar("N", guardada, {"a": "1", "c": "3"})
    assert cobertura == 2 / 3
    assert cuerpo["judgmentRatings"] == [{"query": "q1", "ratings": [{"docId": "1", "rating": "1.0"}]},
                                         {"query": "q2", "ratings": [{"docId": "3", "rating": "0.5"}]}]
    _, cero = eg.para_importar("N", guardada, {})
    assert cero == 0.0
    assert eg.textos(guardada) == ["a", "b", "c"]
    assert eg.por_texto([{"query": "q", "ratings": [{"docId": "1", "rating": 1}]}, {"ratings": []}], {"1": "a"}) == \
        {"q": [["a", "1"]]}
    assert busqueda.nombres_de_evaluacion("Pozos")["juicios"] == "Pozos · juicios del LLM"
