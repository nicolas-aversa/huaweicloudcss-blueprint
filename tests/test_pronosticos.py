"""Lo que calculó cada forecaster, contra lo que pasó: el error medio que
muestra la tarjeta de Forecasting en la vista Plugins.
Formatos de los resultados del backtest como los devolvió el cluster real."""
import json

import main
import pronosticos as pr

_FC = {"name": "pozos-oil", "forecast_interval": {"period": {"interval": 262, "unit": "Minutes"}}, "horizon": 3,
       "indices": ["produccion-pozos*"], "feature_attributes": [{"feature_name": "oil_volume"}]}
PASO = 262 * 60_000
T0 = 1_782_000_000_000


def _real(i, v):
    return {"data_end_time": T0 + i * PASO, "feature_data": [{"data": v}]}


def _pron(h, v, ancla_i, desfase=0):
    return {"data_end_time": T0 + ancla_i * PASO, "horizon_index": h, "forecast_value": v,
            "forecast_lower_bound": v - 10, "forecast_upper_bound": v + 10,
            "forecast_data_end_time": T0 + (ancla_i + h) * PASO + desfase}


def test_el_intervalo_y_el_limite():
    assert pr.intervalo_ms(_FC) == PASO
    assert pr.intervalo_ms({"forecast_interval": {"period": {"interval": 2, "unit": "Hours"}}}) == 7_200_000
    assert pr.limite_del_ancla(T0 + 0.5, _FC) == T0 - 3 * PASO
    assert isinstance(pr.limite_del_ancla(1.78e12, _FC), int), "un float (1.78E12) no parsea como fecha"


def test_el_ancla_es_el_ultimo_paso_con_datos_despues():
    # Los últimos pasos valen 0 (el final del dataset del SIEM): el ancla no puede caer ahí.
    reales = [_real(i, 100 + i) for i in range(10)] + [_real(i, 0) for i in range(10, 15)]
    assert pr.elegir_ancla(reales, 3) == T0 + 7 * PASO
    assert pr.elegir_ancla([_real(i, 0) for i in range(10)], 3) is None
    assert pr.elegir_ancla([_real(0, 5)], 3) is None, "sin horizonte después, no hay con qué comparar"


def test_la_serie_y_el_error_contra_lo_que_paso():
    reales = [_real(i, 100.0) for i in range(6)]
    pron = [_pron(1, 110.0, 2), _pron(2, 90.0, 2), _pron(3, 100.0, 2), {"data_end_time": T0, "forecast_value": 1}]
    s = pr.serie(reales, pron, T0 + 2 * PASO, PASO)
    assert len(s["real"]) == 6 and [p["v"] for p in s["pronostico"]] == [110.0, 90.0, 100.0]
    assert s["pronostico"][0] == {"t": pr._iso(T0 + 3 * PASO), "v": 110.0, "lo": 100.0, "hi": 120.0}
    assert s["error_pct"] == round((10 + 10 + 0) / 300 * 100, 1)
    assert s["ancla"] == pr._iso(T0 + 2 * PASO)


def test_el_pronostico_se_empareja_con_el_paso_mas_cercano():
    """Los pasos del pronóstico y del valor real no caen en el mismo milisegundo."""
    reales = [_real(i, 100.0) for i in range(6)]
    s = pr.serie(reales, [_pron(1, 150.0, 2, desfase=11_000)], T0 + 2 * PASO, PASO)
    assert s["error_pct"] == 50.0
    lejos = pr.serie([_real(0, 100.0)], [_pron(1, 150.0, 5)], T0 + 5 * PASO, PASO)
    assert lejos["error_pct"] is None


def test_sin_valor_real_no_hay_error():
    assert pr.serie([], [_pron(1, 5.0, 0)], T0, PASO)["error_pct"] is None
    assert pr.valor_real({"feature_data": []}) is None and pr.valor_real({"feature_data": [{"data": "x"}]}) is None


# ── Contra el cluster ───────────────────────────────────────────────────────
class _R:
    def __init__(self, status, data):
        self.status_code, self._d, self.text = status, data, json.dumps(data)

    def json(self):
        return self._d


def _cluster(monkeypatch, reales, horizonte, task="T1", estado="TEST_COMPLETE", history=0):
    pedidos = []

    def req(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((url, json_body))
        if "/_plugins/_forecast/forecasters/" in url:
            fc = {**_FC, **({"history": history} if history else {})}
            return _R(200, {"forecaster": fc,
                            "run_once_task": {"task_id": task, "state": estado} if task else {}})
        if "opensearch-forecast-results" in url and url.endswith("/_count"):
            return _R(200, {"count": len(reales)})
        if url.endswith("/produccion-pozos*/_search"):
            return _R(200, {"hits": {"total": {"value": 9}}, "aggregations": {
                "tmin": {"value": T0}, "tmax": {"value": T0 + 20 * PASO}}})
        if "opensearch-forecast-results" in url:
            filtros = json.dumps(json_body)
            if "horizon_index" in filtros and "must_not" not in filtros:
                fuente = [h for h in horizonte if str(h["data_end_time"]) in filtros]
            else:
                # Como OpenSearch: el rango de fechas y el orden pedido.
                rango = next((f["range"]["data_end_time"] for f in json_body["query"]["bool"]["filter"]
                              if "range" in f), {})
                fuente = [r for r in reales if rango.get("gte", -1) <= r["data_end_time"] <= rango.get("lte", 10**15)]
                if json_body.get("sort", [{}])[0].get("data_end_time", {}).get("order") == "desc":
                    fuente = sorted(fuente, key=lambda r: -r["data_end_time"])
                fuente = fuente[:json_body.get("size", 10)]
            return _R(200, {"hits": {"hits": [{"_source": x} for x in fuente]}})
        return _R(404, {})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "x:9200"})
    monkeypatch.setattr(main, "_read_https_enabled_from_state", lambda td: False)
    monkeypatch.setattr(main, "_cluster_admin_password", lambda td: "pw")
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {"produccion-pozos": {"forecaster_ids": ["F1"]}})
    return pedidos


def test_el_pronostico_contra_lo_que_paso(monkeypatch):
    reales = [_real(i, 100.0) for i in range(12)]
    pedidos = _cluster(monkeypatch, reales, [_pron(1, 110.0, 8), _pron(2, 100.0, 8), _pron(3, 100.0, 8)])
    p = main._pronostico_de("http://x:9200", "admin", "pw", "F1")
    assert p["medida"] == "oil_volume" and p["intervalo_min"] == 262 and p["horizonte"] == 3 and p["error"] == ""
    assert p["ancla"] == pr._iso(T0 + 8 * PASO) and len(p["pronostico"]) == 3
    assert p["error_pct"] == round(10 / 300 * 100, 1)
    # Los valores reales son los resultados SIN horizon_index (feature_data no está indexado).
    consulta = next(b for u, b in pedidos if "opensearch-forecast-results" in u)
    assert {"bool": {"must_not": [{"exists": {"field": "horizon_index"}}]}} in consulta["query"]["bool"]["filter"]
    assert {"term": {"task_id": "T1"}} in consulta["query"]["bool"]["filter"]


def test_sin_grafico_dice_lo_que_de_verdad_paso(monkeypatch):
    """Antes decía "todavía no tiene pasos con datos" siempre, y hacía esperar
    resultados que no iban a llegar (el cluster había rechazado el backtest)."""
    error = lambda: main._pronostico_de("http://x:9200", "admin", "pw", "F1")["error"]
    _cluster(monkeypatch, [], [], task="")
    assert error().startswith("no se lanzó el backtest (el cluster lo rechazó)")
    _cluster(monkeypatch, [], [], estado="INIT_TEST")
    assert error() == "el backtest todavía está corriendo: en unos minutos se ve"
    _cluster(monkeypatch, [], [])
    assert error().startswith("el backtest no escribió resultados")
    _cluster(monkeypatch, [_real(i, 0) for i in range(12)], [], history=600)
    assert error().startswith("backtest parcial: 12 de 600 pasos")


def test_un_backtest_parcial_se_dibuja_con_lo_que_escribio(monkeypatch):
    """Sus pasos quedaron lejos del fin de los datos (el fin está a 20 pasos;
    acá hay datos solo hasta el 10, y antes del 300 vacío): igual se dibuja."""
    lejos = [_real(i - 400, 100.0) for i in range(12)]
    _cluster(monkeypatch, lejos, [_pron(1, 100.0, -392), _pron(2, 100.0, -392), _pron(3, 100.0, -392)])
    p = main._pronostico_de("http://x:9200", "admin", "pw", "F1")
    assert p["error"] == "" and p["ancla"] == pr._iso(T0 - 392 * PASO)
