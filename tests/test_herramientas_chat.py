"""Respuestas del asistente que salen de las herramientas de OpenSearch 3.4
(DataDistributionTool, LogPatternAnalysisTool, SearchAlertsTool) y no de PPL.
Los formatos de las respuestas son los que devolvió el cluster real."""
import json

import pytest

import herramientas_chat as hc
import main

# Lo que devolvió DataDistributionTool para la anomalía de pozos del 12/07.
_DIST = {"comparisonAnalysis": [
    {"field": "wat_vol", "divergence": 0.147, "topChanges": [
        {"value": "0.0-0.1", "selectionPercentage": 0.75, "baselinePercentage": 0.75},
        {"value": "0.2-0.3", "selectionPercentage": 0.17, "baselinePercentage": 0.07}]},
    {"field": "on_stream_hrs", "divergence": 0.588, "topChanges": [
        {"value": "24.0", "selectionPercentage": 1.0, "baselinePercentage": 0.41},
        {"value": "22.3", "selectionPercentage": 0.0, "baselinePercentage": 0.15}]},
    {"field": "sin_referencia", "divergence": 0.9, "topChanges": [
        {"value": "x", "selectionPercentage": 1.0, "baselinePercentage": None}]},
]}
# Lo de LogPatternAnalysisTool para la anomalía del SIEM del 21/10.
_PATRONES = {"patternMapDifference": [
    {"pattern": "block", "base": 0.11, "selection": 0.006, "lift": 17.2},
    {"pattern": "ssh_login", "base": 0.22, "selection": 0.84, "lift": 3.81},
    {"pattern": "executeSql", "base": 0.0, "selection": 0.47, "lift": None},
]}


def test_la_ventana_de_una_anomalia_y_su_referencia():
    v = hc.ventanas("2025-07-12 09:45:00", "2025-07-12 18:30:00")
    assert v == {"selectionTimeRangeStart": "2025-07-12 09:45:00", "selectionTimeRangeEnd": "2025-07-12 18:30:00",
                 # La referencia: el período anterior, 3 veces el intervalo (8 h 45 → 26 h 15).
                 "baselineTimeRangeStart": "2025-07-11 07:30:00", "baselineTimeRangeEnd": "2025-07-12 09:45:00"}


def test_un_intervalo_corto_se_compara_contra_un_dia():
    v = hc.ventanas("2025-07-12 10:00:00", "2025-07-12 10:10:00")
    assert v["baselineTimeRangeStart"] == "2025-07-11 10:00:00"


def test_un_hallazgo_es_la_hora_alrededor_del_evento():
    v = hc.ventanas("2025-10-21 03:14:07")
    assert (v["selectionTimeRangeStart"], v["selectionTimeRangeEnd"]) == ("2025-10-21 02:14:07", "2025-10-21 04:14:07")
    assert v["baselineTimeRangeEnd"] == "2025-10-21 02:14:07"


def test_una_fecha_rota_no_es_una_ventana():
    with pytest.raises(ValueError):
        hc.ventanas("ayer")


def test_el_resultado_de_una_herramienta():
    cuerpo = {"inference_results": [{"output": [{"name": "response", "result": json.dumps(_DIST)}]}]}
    assert hc.resultado_de_herramienta(cuerpo) == _DIST
    texto = {"inference_results": [{"output": [{"result": "Alerts=[]TotalAlerts=0"}]}]}
    assert hc.resultado_de_herramienta(texto) == "Alerts=[]TotalAlerts=0"
    assert hc.resultado_de_herramienta({}) is None


def test_lo_que_mas_cambio_va_primero_y_sin_referencia_no_cuenta():
    cambios = hc.cambios_de_distribucion(_DIST)
    assert [c["campo"] for c in cambios] == ["on_stream_hrs", "wat_vol"]
    assert cambios[0] == {"campo": "on_stream_hrs", "valor": "24.0", "intervalo": 1.0, "referencia": 0.41,
                          "divergencia": 0.59}
    # De cada campo, el valor que más se movió (0.2-0.3: de 7 % a 17 %).
    assert cambios[1]["valor"] == "0.2-0.3"
    assert hc.cambios_de_distribucion(None) == [] and hc.cambios_de_distribucion("texto") == []


def test_los_patrones_que_subieron_los_nuevos_primero():
    p = hc.cambios_de_patrones(_PATRONES)
    assert [x["valor"] for x in p] == ["executeSql", "ssh_login"], "block bajó: no explica la anomalía"
    assert hc.cambios_de_patrones({"patternMapDifference": []}) == []


def test_la_tabla_y_el_prompt_llevan_los_numeros():
    d, p = hc.cambios_de_distribucion(_DIST), hc.cambios_de_patrones(_PATRONES)
    tabla = hc.tabla_de_cambios(d, p, "event.action")
    assert [c["name"] for c in tabla["schema"]] == ["valor", "campo", "% en el intervalo", "% antes"]
    assert ["ssh_login", "event.action", 84, 22] in tabla["datarows"]
    assert ["24.0", "on_stream_hrs", 100, 41] in tabla["datarows"]
    v = hc.ventanas("2025-07-12 09:45:00", "2025-07-12 18:30:00")
    prompt = hc.prompt_de_explicacion("¿Por qué?", "Anomalía…", v, d, p, "event.action")
    assert "- on_stream_hrs = 24.0: 100 % del intervalo vs 41 % antes" in prompt
    assert "- event.action = ssh_login: 84 % del intervalo vs 22 % antes" in prompt
    assert "- event.action = executeSql: 47 % del intervalo vs 0 % antes" in prompt
    assert "No digas que no se puede determinar" in prompt


@pytest.mark.parametrize("pregunta, tipo", [
    ("¿Qué alertas se dispararon hoy?", "alertas"),
    ("¿Cuántas alertas hay?", "alertas"),
    ("mostrame las últimas alertas", "alertas"),
    ("¿Qué anomalías hubo en pozos?", "anomalias"),
    ("¿Cuántas anomalías encontró el detector?", "anomalias"),
    ("¿Por qué es una anomalía?", ""),          # "Explicar": va por la ventana, no se lista
    ("¿Cuál es el total de ventas?", ""),
    ("alertas", ""),                              # sin pedir verlas
])
def test_cuando_pide_ver_alertas_o_anomalias(pregunta, tipo):
    assert hc.pide_listado(pregunta) == tipo


def test_las_alertas_del_texto_de_la_herramienta():
    texto = ("Alerts=[Alert(id=jA, version=1, monitorId=4A, monitorName=cts-anomalias-alerta, "
             "monitorUser=User[name=admin, backend_roles=[admin], roles=[own_index, all_access]], "
             "triggerId=3w, triggerName=Anomalias en cts, findingIds=[], state=ACTIVE, "
             "startTime=2026-10-01T18:25:27.882Z, endTime=null, severity=2, errorHistory=[])"
             "Alert(id=jB, monitorName=m, triggerName=t, state=COMPLETED, startTime=2026-09-30T10:00:00Z, severity=1)]"
             "TotalAlerts=2")
    a = hc.alertas_del_texto(texto)
    assert a == [{"inicio": "2026-10-01 18:25:27", "estado": "ACTIVE", "severidad": "2",
                  "monitor": "cts-anomalias-alerta", "disparador": "Anomalias en cts"},
                 {"inicio": "2026-09-30 10:00:00", "estado": "COMPLETED", "severidad": "1",
                  "monitor": "m", "disparador": "t"}]
    assert hc.tabla_de_alertas(a)["datarows"][0] == ["2026-10-01 18:25:27", "ACTIVE", "2", "Anomalias en cts"]
    assert hc.alertas_del_texto("Alerts=[]TotalAlerts=0") == []


# ── El cableado en main ─────────────────────────────────────────────────────
class _R:
    def __init__(self, status, data):
        self.status_code, self._d, self.text = status, data, json.dumps(data)

    def json(self):
        return self._d


def _herramientas(monkeypatch, respuestas):
    """`_os_req` de mentira: cada herramienta devuelve su resultado (o falla)."""
    pedidos = []

    def req(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((url, json_body))
        if "/_plugins/_ml/tools/_execute/" in url:
            nombre = url.rsplit("/", 1)[1]
            if nombre not in respuestas:
                return _R(500, {"error": "boom"})
            r = respuestas[nombre]
            return _R(200, {"inference_results": [{"output": [{"result": r if isinstance(r, str) else json.dumps(r)}]}]})
        if "/_plugins/_anomaly_detection/detectors/results/_search" in url:
            return _R(200, respuestas.get("_anomalias", {"hits": {"total": {"value": 0}, "hits": []}}))
        return _R(404, {})

    monkeypatch.setattr(main, "_os_req", req)
    return pedidos


def test_explicar_con_las_dos_herramientas(monkeypatch):
    pedidos = _herramientas(monkeypatch, {"DataDistributionTool": _DIST, "LogPatternAnalysisTool": _PATRONES})
    prompts = []
    r = main._explicar_con_herramientas("http://x:9200", "admin", "pw", "siem*", "event.action", "¿Por qué?", "ctx",
                                        "2025-10-21 00:45:00", "2025-10-21 09:30:00",
                                        lambda p: prompts.append(p) or "Subieron los logins SSH.")
    assert r.answer == "Subieron los logins SSH." and r.ppl == ""
    assert ["ssh_login", "event.action", 84, 22] in r.result["datarows"]
    dist = next(b for u, b in pedidos if u.endswith("DataDistributionTool"))["parameters"]
    assert dist["index"] == "siem*" and dist["selectionTimeRangeStart"] == "2025-10-21 00:45:00" and dist["size"] == "1000"
    pat = next(b for u, b in pedidos if u.endswith("LogPatternAnalysisTool"))["parameters"]
    assert pat["logFieldName"] == "event.action" and pat["baseTimeRangeEnd"] == "2025-10-21 00:45:00"
    assert "ssh_login" in prompts[0]


def test_sin_campo_de_patrones_solo_la_distribucion(monkeypatch):
    pedidos = _herramientas(monkeypatch, {"DataDistributionTool": _DIST})
    r = main._explicar_con_herramientas("http://x:9200", "admin", "pw", "pozos*", "", "¿Por qué?", "",
                                        "2025-07-12 09:45:00", "2025-07-12 18:30:00", lambda p: "ok")
    assert r is not None and not [u for u, _ in pedidos if "LogPattern" in u]


def test_si_las_herramientas_no_dan_nada_se_responde_como_siempre(monkeypatch):
    _herramientas(monkeypatch, {})
    assert main._explicar_con_herramientas("http://x:9200", "a", "p", "x*", "f", "?", "",
                                           "2025-07-12 09:45:00", "", lambda p: "no") is None
    assert main._explicar_con_herramientas("http://x:9200", "a", "p", "x*", "", "?", "", "roto", "", lambda p: "no") is None


def test_listar_alertas_del_monitor_del_caso(monkeypatch):
    texto = ("Alerts=[Alert(id=a, monitorName=m, triggerName=Anomalias en cts, state=ACTIVE, "
             "startTime=2026-10-01T18:25:27.882Z, severity=2)]TotalAlerts=7")
    pedidos = _herramientas(monkeypatch, {"SearchAlertsTool": texto})
    prompts = []
    r = main._listar_alertas_o_anomalias("http://x:9200", "a", "p", "alertas", {"monitor_id": "M1"}, "¿Qué alertas?",
                                         lambda p: prompts.append(p) or "Hay 7 alertas.")
    assert r.answer == "Hay 7 alertas." and r.result["datarows"] == [["2026-10-01 18:25:27", "ACTIVE", "2", "Anomalias en cts"]]
    assert next(b for u, b in pedidos if u.endswith("SearchAlertsTool"))["parameters"]["monitorId"] == "M1"
    assert "Hay 7 alertas del caso" in prompts[0]


def test_listar_anomalias_del_detector_del_caso(monkeypatch):
    fuente = {"data_start_time": 1752313500000, "data_end_time": 1752345000000, "anomaly_grade": 1.0,
              "confidence": 0.9, "feature_data": [{"feature_name": "oil_volume", "data": 2547.26}]}
    _herramientas(monkeypatch, {"_anomalias": {"hits": {"total": {"value": 6}, "hits": [{"_source": fuente}]}}})
    r = main._listar_alertas_o_anomalias("http://x:9200", "a", "p", "anomalias", {"detector_id": "D1"}, "¿Qué anomalías?",
                                         lambda p: "Hubo 6.")
    assert r.answer == "Hubo 6." and r.result["datarows"][0][2] == 100
    assert r.result["datarows"][0][3] == "oil_volume: 2547.26"


def test_sin_alertas_lo_dice_sin_llamar_al_modelo(monkeypatch):
    _herramientas(monkeypatch, {"SearchAlertsTool": "Alerts=[]TotalAlerts=0"})
    r = main._listar_alertas_o_anomalias("http://x:9200", "a", "p", "alertas", {"monitor_id": "M"}, "?",
                                         lambda p: pytest.fail("no hace falta el modelo"))
    assert r.answer == "No hay alertas de este caso."


def test_sin_el_plugin_no_se_lista(monkeypatch):
    _herramientas(monkeypatch, {})
    assert main._listar_alertas_o_anomalias("http://x:9200", "a", "p", "alertas", {}, "?", lambda p: "") is None
    assert main._listar_alertas_o_anomalias("http://x:9200", "a", "p", "anomalias", {"monitor_id": "M"}, "?",
                                            lambda p: "") is None


def test_el_chat_usa_las_herramientas_antes_que_ppl():
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def ppl_chat(")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    a = cuerpo.index("if request.explicar_desde:")
    b = cuerpo.index("tipo = hc.pide_listado(request.question)")
    c = cuerpo.index("return _conversar(")
    assert a < b < c


def test_siem_y_cts_declaran_su_campo_de_patrones():
    import verticals
    specs = verticals.capability_specs()
    assert specs["siem"]["pattern_field"] == "event.action"
    assert specs["cts"]["pattern_field"] == "trace_name"
