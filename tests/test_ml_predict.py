"""`_ml_predict`: qué texto se saca de la respuesta de ml-commons.

Caso real: el plan de una investigación ("¿por qué es una anomalía?") es un
array JSON de consultas. Como es JSON válido, ml-commons lo entrega ya parseado
en `dataAsMap.response` (una lista). Se descartaba ("sin content"), el plan
quedaba vacío y la respuesta caía a una sola consulta con un descargo del tipo
"la consulta solo cuenta… no permite determinar por qué".
"""
import json

import main

_PLAN = ["source=siem* | where @timestamp >= '2025-08-12 09:30:00' | stats count() by event.dataset",
         "source=siem* | where @timestamp < '2025-08-12 09:30:00' | stats count() by event.dataset"]


class _Resp:
    status_code = 200

    def __init__(self, data_as_map):
        self._d = {"inference_results": [{"output": [{"dataAsMap": data_as_map}]}]}

    def json(self):
        return self._d


def _predecir(monkeypatch, data_as_map):
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: _Resp(data_as_map))
    return main._ml_predict("http://x:9200", "admin", "pw", "M", {"prompt": "p"})


def test_un_plan_que_llega_como_lista_se_devuelve_como_json(monkeypatch):
    texto = _predecir(monkeypatch, {"response": _PLAN})
    assert json.loads(texto) == _PLAN
    # Y así el plan de la investigación tiene sus dos consultas.
    assert main._plan_de_investigacion(texto) == _PLAN


def test_un_dict_tambien(monkeypatch):
    assert json.loads(_predecir(monkeypatch, {"response": {"a": "ñ"}})) == {"a": "ñ"}


def test_el_texto_sigue_igual(monkeypatch):
    assert _predecir(monkeypatch, {"response": "  hola  "}) == "hola"
    assert _predecir(monkeypatch, "  plano  ") == "plano"
    assert _predecir(monkeypatch, {"choices": [{"message": {"content": " openai "}}]}) == "openai"


def test_sin_nada_util_es_none(monkeypatch):
    assert _predecir(monkeypatch, {"response": []}) is None
    assert _predecir(monkeypatch, {"otra": "cosa"}) is None


def test_la_investigacion_corre_con_el_plan_en_lista(monkeypatch):
    """De punta a punta: el modelo devuelve el plan como lista y se ejecutan
    las dos consultas, en vez de caer a una sola."""
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: _Resp({"response": _PLAN}))
    predecir_ppl = lambda prompt: main._ml_predict("http://x:9200", "admin", "pw", "PM", {"prompt": prompt})
    corridas = []

    def ejecutar(ppl):
        corridas.append(ppl)
        return True, {"schema": [{"name": "n"}], "datarows": [[1]]}

    res = main._investigar("¿Por qué es una anomalía?", [], {"event.dataset": "fuente"},
                           predecir_ppl, lambda prompt: "cruce", ejecutar)
    assert res is not None and res.answer == "cruce"
    assert [main._con_tope(c) for c in _PLAN] == corridas
    assert len(res.consultas) == 2
