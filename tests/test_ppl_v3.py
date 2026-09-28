"""El chat con PPL 3 (Calcite) y el modo Investigar.

Con el motor viejo el prompt prohibía hacer cuentas en la consulta (`eval`
después de `stats`, tasas) y ante un "¿por qué?" con texto libre solo se leían
muestras. PPL 3 permite tasas, `eventstats` y `patterns`, que agrupa textos
parecidos para CONTARLOS. Y un "¿por qué?" ahora se contesta cruzando varias
consultas.
"""
import json

import pytest

import capabilities as caps
import main

CAMPOS = {"review_score": "Puntaje (1-5)", "review_comment_message": "Comentario"}
_OK1 = (True, {"schema": [{"name": "total"}], "datarows": [[1]]})


class _Fakes:
    def __init__(self, ppls, respuesta="ok", ejecuciones=None):
        self.ppls, self.respuesta = list(ppls), respuesta
        self.ejecuciones = list(ejecuciones or [_OK1] * 5)
        self.prompts_ppl, self.prompts_llm, self.ejecutadas = [], [], []

    def ppl(self, prompt):
        self.prompts_ppl.append(prompt)
        return self.ppls.pop(0)

    def llm(self, prompt):
        self.prompts_llm.append(prompt)
        return self.respuesta

    def ejecutar(self, ppl):
        self.ejecutadas.append(ppl)
        return self.ejecuciones.pop(0)


def _charlar(f, pregunta="¿cuántas?", **kw):
    return main._conversar(pregunta, [], CAMPOS, f.ppl, f.llm, f.ejecutar, **kw)


# ── El prompt ───────────────────────────────────────────────────────────────
def test_sin_ppl3_el_prompt_no_cambia():
    sp = caps.build_ppl_system_prompt("r-*", ["a"], CAMPOS, "00", "R")
    assert sp == caps.build_ppl_system_prompt("r-*", ["a"], CAMPOS, "00", "R", ppl_v3=False)
    assert caps._REGLAS_PPL_V2 in sp and caps._REGLAS_PPL_V3 not in sp
    assert "NEVER calculate rates or percentages in the query" in sp
    assert "patterns" not in sp and "eventstats" not in sp


def test_con_ppl3_las_cuentas_van_en_la_consulta():
    sp = caps.build_ppl_system_prompt("r-*", ["a"], CAMPOS, "00", "R", ppl_v3=True)
    assert caps._REGLAS_PPL_V3 in sp and "NEVER calculate rates" not in sp
    assert "eval failed_pct = round(failed * 100.0 / total, 2)" in sp
    assert "eventstats sum(total) as grand_total" in sp
    assert "patterns <text_field> method=brain mode=aggregation | sort -pattern_count | head 10" in sp
    # Lo demás sigue igual: fechas, sort, vacíos.
    for regla in ("7. To filter by date/time", "9. For sort direction", "10. Empty or missing values"):
        assert regla in sp
    # Sin código de éxito no hay ejemplo de tasa de fallos, pero sí de participación.
    sin_codigo = caps.build_ppl_system_prompt("r-*", [], CAMPOS, "", "R", ppl_v3=True)
    assert "# Failure rate by field" not in sin_codigo and "# Share of the total" in sin_codigo
    assert "# Failure rate by field" in sp


def test_las_reglas_del_chat_con_y_sin_ppl3():
    assert main._reglas_del_chat(False) is main._REGLAS_DEL_CHAT
    v3 = main._reglas_del_chat(True)
    assert v3 is main._REGLAS_DEL_CHAT_V3
    assert "patterns <text_field> method=brain mode=aggregation" in v3 and "do NOT aggregate" not in v3
    assert "do NOT aggregate" in main._REGLAS_DEL_CHAT and "patterns" not in main._REGLAS_DEL_CHAT
    # A, B, C y la segunda parte de D, iguales en las dos.
    for pedazo in ("A. If the user asks HOW or WHY", "B. If answering needs information",
                   "C. Use the CONVERSATION", "(2) otherwise, if there are fields",
                   "Output NO_DATA only if no field relates."):
        assert pedazo in v3 and pedazo in main._REGLAS_DEL_CHAT, pedazo
    d = v3[v3.index("D. WHY"):]
    assert d.index("FREE-TEXT") < d.index("(2) otherwise") < d.index("NO_DATA only")


# ── patterns: agrupar para contar, o leer si no agrupa ──────────────────────
_PATRONES = "source=r-* | where review_score <= 2 and isnotnull(review_comment_message) | patterns review_comment_message method=brain mode=aggregation | sort -pattern_count | head 10"
_SCHEMA_P = [{"name": "patterns_field"}, {"name": "pattern_count"}, {"name": "sample_logs"}]


def _grupos(*filas):
    return {"schema": _SCHEMA_P, "datarows": [list(f) for f in filas]}


@pytest.mark.parametrize("resultado, util", [
    (_grupos(["Connection timeout to <*> after <*> ms", 812, ["Connection timeout to db1 after 30 ms"]]), True),
    (_grupos(["produto chegou", 1, ["produto chegou"]], ["não recebi", 1, ["não recebi"]]), False),   # de a uno
    (_grupos(["<*> <*> <*>", 5000, ["a b c"]], ["<*> <*>", 900, ["a b"]]), False),                    # puro <*>
    (_grupos(["<*> x <*>", 30, []]), False),                                                         # una sola fija de 1 letra
    (_grupos(["<*> a de <*>", 30, []]), False),                                                      # "a" no es una palabra fija
    ({"schema": [{"name": "total"}], "datarows": [[3]]}, False),                                      # no es de patterns
])
def test_que_grupos_sirven(resultado, util):
    assert main._grupos_utiles(resultado) is util


def test_la_muestra_que_reemplaza_a_patterns():
    assert main._muestra_en_vez_de_patrones(_PATRONES) == (
        "source=r-* | where review_score <= 2 and isnotnull(review_comment_message) "
        "| fields review_comment_message | head 30")
    assert main._muestra_en_vez_de_patrones("source=r-* | stats count()") is None
    assert main._es_patrones(_PATRONES) and not main._es_patrones("source=r-* | fields a | head 5")


def test_si_agrupa_se_cuentan_los_grupos():
    grupos = _grupos(["Connection timeout to <*> after <*> ms", 812,
                      ["Connection timeout to db1 after 30 ms", "Connection timeout to db2 after 45 ms", "tercero"]],
                     ["User <*> login failed", 97, ["x" * 900]])
    f = _Fakes([_PATRONES], respuesta="Hay 812 timeouts.", ejecuciones=[(True, grupos)])
    res = _charlar(f, "¿qué errores se repiten?")
    assert f.ejecutadas == [_PATRONES], "agrupó: no hace falta leer muestras"
    pedido = f.prompts_llm[0]
    assert "Grupos de textos parecidos (2)" in pedido and '"cantidad": 812' in pedido
    assert "no lo copies, explicalo" in pedido and "si dos grupos dicen lo mismo, sumalos" in pedido
    # Hasta dos ejemplos por grupo, cortos.
    assert "tercero" not in pedido and "x" * 200 in pedido and "x" * 201 not in pedido
    assert res.answer == "Hay 812 timeouts." and res.ppl == _PATRONES


def test_si_no_agrupa_se_leen_muestras():
    """Reseñas escritas por personas: patterns las deja de a uno."""
    de_a_uno = _grupos(["produto chegou", 1, ["produto chegou"]], ["não recebi", 1, ["não recebi"]])
    filas = {"schema": [{"name": "review_comment_message"}], "datarows": [["Não recebi o produto"]]}
    f = _Fakes([_PATRONES], ejecuciones=[(True, de_a_uno), (True, filas)])
    res = _charlar(f, "¿de qué se quejan?")
    muestra = main._muestra_en_vez_de_patrones(_PATRONES)
    assert f.ejecutadas == [_PATRONES, muestra]
    assert res.ppl == muestra and "Filas de MUESTRA (1)" in f.prompts_llm[0]


def test_si_la_muestra_falla_quedan_los_grupos():
    de_a_uno = _grupos(["a b", 1, ["a b"]])
    f = _Fakes([_PATRONES], ejecuciones=[(True, de_a_uno), (False, "boom")])
    res = _charlar(f, "¿de qué se quejan?")
    assert res.ppl == _PATRONES and "Grupos de textos parecidos" in f.prompts_llm[0]


# ── Investigar ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pregunta, es", [
    ("¿Por qué fallan las transacciones?", True),
    ("por que bajaron las ventas", True),
    ("¿A qué se deben las malas reseñas?", True),
    ("¿Qué causa los rechazos?", True),
    ("Why are payments failing?", True),
    ("¿Cuántas transacciones hay?", False),
    ("porcentaje de fallos por canal", False),
])
def test_que_es_un_por_que(pregunta, es):
    assert main._es_por_que(pregunta) is es


def test_el_plan_de_investigacion():
    tres = ["source=t | stats count() by a", "source=t | stats count() by b", "source=t | stats count() by c"]
    assert main._plan_de_investigacion("Acá va:\n```json\n" + json.dumps(tres + ["source=t | x"]) + "\n```") == tres
    assert main._plan_de_investigacion("source=t | a\nsource=t | a\nsource=t | b\nnada") == ["source=t | a", "source=t | b"]
    assert main._plan_de_investigacion('["no es ppl", 3]') == []
    assert main._plan_de_investigacion(None) == []


def _q(n):
    return f"source=t | stats count() as total by c{n}"


def test_un_por_que_se_investiga_con_varias_consultas():
    f = _Fakes([json.dumps([_q(1), _q(2), _q(3)])], respuesta="Fallan por timeout.",
               ejecuciones=[(True, {"schema": [{"name": "c1"}], "datarows": [["A", 10]]}),
                            (False, "Field [c2] not found"),
                            (True, {"schema": [{"name": "c3"}], "datarows": [["B", 5]]})])
    res = _charlar(f, "¿Por qué fallan?", contexto="entre 10:00 y 11:00, IP 1.2.3.4")
    assert f.ejecutadas == [_q(1), _q(2), _q(3)] and len(f.prompts_ppl) == 1
    plan = f.prompts_ppl[0]
    assert "INVESTIGATION: ¿Por qué fallan?" in plan and "JSON array with 2 or 3 PPL queries" in plan
    assert "CONTEXT: entre 10:00 y 11:00, IP 1.2.3.4" in plan
    assert res.answer == "Fallan por timeout."
    assert res.ppl == f"{_q(1)}\n{_q(3)}", "la memoria guarda las que corrieron"
    assert res.result["datarows"] == [["A", 10]]
    assert [c["ok"] for c in res.consultas] == [True, False, True]
    assert res.consultas[1]["error"] == "Field [c2] not found" and res.consultas[1]["result"] == {}
    pedido = f.prompts_llm[0]
    assert "Contexto: entre 10:00 y 11:00" in pedido and "Consulta 2:" in pedido and "Falló: Field [c2]" in pedido
    assert "no inventes causas" in pedido and "No muestres JSON ni las consultas" in pedido


def test_investigar_se_pide_para_cualquier_pregunta():
    f = _Fakes([json.dumps([_q(1), _q(2)])])
    res = _charlar(f, "ventas por canal", investigar=True)
    assert len(res.consultas) == 2


def test_una_pregunta_comun_no_se_investiga():
    f = _Fakes(["source=t | stats count() as total"])
    res = _charlar(f, "¿cuántas hay?")
    assert res.consultas == [] and len(f.prompts_ppl) == 1 and "INVESTIGATION" not in f.prompts_ppl[0]


def test_sin_plan_se_contesta_con_una_consulta():
    f = _Fakes(["no sé qué hacer", "source=t | stats count() as total"])
    res = _charlar(f, "¿Por qué fallan?", contexto="IP 1.2.3.4")
    assert res.consultas == [] and f.ejecutadas == ["source=t | stats count() as total"]
    assert f.prompts_ppl[1].startswith("¿Por qué fallan?") and "CONTEXT: IP 1.2.3.4" in f.prompts_ppl[1]


def test_una_sola_consulta_no_es_una_investigacion():
    f = _Fakes([json.dumps([_q(1)]), "source=t | stats count() as total"])
    res = _charlar(f, "¿Por qué fallan?")
    assert res.consultas == [] and f.ejecutadas == ["source=t | stats count() as total"]


def test_si_la_primera_falla_el_grafico_sale_de_la_que_corrio():
    f = _Fakes([json.dumps([_q(1), _q(2)])],
               ejecuciones=[(False, "x"), (True, {"schema": [{"name": "c2"}], "datarows": [["B", 5]]})])
    res = _charlar(f, "¿Por qué fallan?")
    assert res.result["datarows"] == [["B", 5]] and res.ppl == _q(2)


def test_si_no_corre_ninguna_se_contesta_con_una_consulta():
    f = _Fakes([json.dumps([_q(1), _q(2)]), "source=t | stats count() as total"],
               ejecuciones=[(False, "x"), (False, "y"), _OK1])
    res = _charlar(f, "¿Por qué fallan?")
    assert res.consultas == [] and f.ejecutadas[-1] == "source=t | stats count() as total"


def test_investigar_respeta_las_reglas_de_la_conversacion():
    anterior = main.ChatTurno(pregunta="¿cuántas fallaron?", ppl="source=t | stats count()", respuesta="10")
    f = _Fakes([main._SIN_CONSULTA], respuesta="Conté todas las fallidas.")
    res = main._conversar("¿por qué decís eso?", [anterior], CAMPOS, f.ppl, f.llm, f.ejecutar)
    assert not f.ejecutadas and res.ppl == anterior.ppl
    f2 = _Fakes([f"{main._SIN_DATO}: el motivo"], respuesta="No tengo el motivo.")
    assert _charlar(f2, "¿Por qué renunció?").answer == "No tengo el motivo."


def test_una_investigacion_no_trae_miles_de_filas():
    f = _Fakes([json.dumps(["source=t | fields c | head 9000", _q(2)])])
    _charlar(f, "¿Por qué?")
    assert f.ejecutadas[0] == f"source=t | fields c | head {main._MAX_FILAS_DE_MUESTRA}"


# ── El endpoint ─────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


def _endpoint(monkeypatch, feats, registrar=None):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {"s": {"ppl_model_id": "PM", "llm_model_id": "LM"}})
    monkeypatch.setattr(main, "_resolve_capability_spec",
                        lambda *a, **k: {"index_pattern": "s*", "operations": [], "fields": CAMPOS, "label": "S"})
    monkeypatch.setattr(main, "_index_time_window", lambda *a, **k: None)
    monkeypatch.setattr(main, "_read_cluster_features", lambda td: feats)
    llamadas = {"registrar": 0, "prompts": [], "conversar": None}

    def fake_registrar(*a, **k):
        llamadas["registrar"] += 1
        return registrar

    monkeypatch.setattr(main, "_registrar_capacidades", fake_registrar)

    def fake_conversar(pregunta, historial, campos, pp, pl, ej, investigar=False, contexto=""):
        llamadas["conversar"] = (investigar, contexto)
        pp("hola")
        return main.PplChatResponse(answer="ok")

    monkeypatch.setattr(main, "_conversar", fake_conversar)
    monkeypatch.setattr(main, "_ml_predict",
                        lambda base, u, p, model, params, timeout=60: llamadas["prompts"].append(params) or "x")
    return TestClient(main.app), llamadas


def test_el_chat_usa_ppl3_si_el_cluster_lo_tiene(monkeypatch):
    client, ll = _endpoint(monkeypatch, {"ppl_v3": True})
    r = client.post("/api/v1/capabilities/ppl-chat",
                    json={"question": "¿por qué?", "slug": "s", "opensearch_password": "pw",
                          "investigar": True, "contexto": "IP 1.2.3.4"})
    assert r.status_code == 200
    sp = ll["prompts"][0]["system_prompt"]
    assert "eventstats" in sp and "patterns <text_field> method=brain" in sp
    assert ll["registrar"] == 0, "ya estaba detectado"
    assert ll["conversar"] == (True, "IP 1.2.3.4")


def test_sin_ppl3_sigue_el_prompt_de_siempre(monkeypatch):
    client, ll = _endpoint(monkeypatch, {"ppl_v3": False})
    client.post("/api/v1/capabilities/ppl-chat", json={"question": "hola", "slug": "s", "opensearch_password": "pw"})
    sp = ll["prompts"][0]["system_prompt"]
    assert "NEVER calculate rates" in sp and "eventstats" not in sp and "do NOT aggregate" in sp
    assert ll["conversar"] == (False, "")


def test_un_entorno_sin_detectar_se_detecta_una_vez(monkeypatch):
    client, ll = _endpoint(monkeypatch, {}, registrar={"ppl_v3": True})
    client.post("/api/v1/capabilities/ppl-chat", json={"question": "hola", "slug": "s", "opensearch_password": "pw"})
    assert ll["registrar"] == 1 and "eventstats" in ll["prompts"][0]["system_prompt"]
    # Si el cluster no contesta, sin PPL 3 (lo de siempre).
    client2, ll2 = _endpoint(monkeypatch, {}, registrar=None)
    client2.post("/api/v1/capabilities/ppl-chat", json={"question": "hola", "slug": "s", "opensearch_password": "pw"})
    assert "eventstats" not in ll2["prompts"][0]["system_prompt"]


def test_el_contexto_tiene_tope():
    with pytest.raises(Exception):
        main.PplChatRequest(question="x", contexto="a" * 501)
    r = main.PplChatRequest(question="x")
    assert r.investigar is False and r.contexto == ""


# ── Prender Calcite si alguien lo apagó ─────────────────────────────────────
@pytest.mark.parametrize("feats, pone", [
    ({"ppl": True, "calcite": False}, True),
    ({"ppl": True, "calcite": True}, False),
    ({"ppl": True, "calcite": None}, False),     # sin dato no se toca
    ({"ppl": False, "calcite": False}, False),   # sin plugin SQL no hay a qué
    (None, False),
])
def test_calcite_se_prende_solo_si_esta_apagado(monkeypatch, tmp_path, feats, pone):
    pedidos, pasos = [], []

    def fake(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((method, url, json_body))
        return _Resp(200, {})

    monkeypatch.setattr(main, "_os_req", fake)
    monkeypatch.setattr(main.runs, "step", lambda run, name, ok, reason="": pasos.append((name, ok, reason)))
    monkeypatch.setattr(main, "_registrar_capacidades", lambda *a, **k: {"ppl": True, "calcite": True, "ppl_v3": True})
    res = main._asegurar_ppl_v3({"public_endpoint": "x:9200"}, "admin", "pw", False, tmp_path, {"id": "r"}, feats)
    if pone:
        assert pedidos == [("PUT", "http://x:9200/_cluster/settings",
                            {"persistent": {"plugins.calcite.enabled": True}})]
        assert pasos == [("PPL 3 (Calcite)", True, "")] and res["ppl_v3"] is True
    else:
        assert pedidos == [] and pasos == [] and res == feats


def test_provisionar_prende_calcite_con_lo_que_detecto():
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    cuerpo = src[i:src.index("\n    for slug in slugs:", i)]
    assert ("_asegurar_ppl_v3(cluster, user, password, request.https_enabled, terraform_dir, run,\n"
            "                     _registrar_capacidades(") in cuerpo


def test_si_no_se_puede_prender_se_dice(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: _Resp(403, {}))
    pasos = []
    monkeypatch.setattr(main.runs, "step", lambda run, name, ok, reason="": pasos.append((name, ok, reason)))
    feats = {"ppl": True, "calcite": False}
    assert main._asegurar_ppl_v3({"public_endpoint": "x:9200"}, "admin", "pw", False, tmp_path, {"id": "r"}, feats) == feats
    assert pasos == [("PPL 3 (Calcite)", False, "no se pudo prender: status 403")]
