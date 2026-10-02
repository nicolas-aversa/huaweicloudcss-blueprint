"""Pestaña Forecasting: lo que calculó cada forecaster, contra lo que pasó.
Formatos de los resultados del backtest como los devolvió el cluster real."""
import json
import pathlib
import shutil
import subprocess

import pytest

import main
import pronosticos as pr

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"
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


# ── El endpoint ─────────────────────────────────────────────────────────────
class _R:
    def __init__(self, status, data):
        self.status_code, self._d, self.text = status, data, json.dumps(data)

    def json(self):
        return self._d


def _cluster(monkeypatch, reales, horizonte, task="T1"):
    pedidos = []

    def req(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((url, json_body))
        if "/_plugins/_forecast/forecasters/" in url:
            return _R(200, {"forecaster": _FC, "run_once_task": {"task_id": task} if task else {}})
        if url.endswith("/produccion-pozos*/_search"):
            return _R(200, {"hits": {"total": {"value": 9}}, "aggregations": {
                "tmin": {"value": T0}, "tmax": {"value": T0 + 20 * PASO}}})
        if "opensearch-forecast-results" in url:
            filtros = json.dumps(json_body)
            fuente = horizonte if "horizon_index" in filtros and "must_not" not in filtros else reales
            return _R(200, {"hits": {"hits": [{"_source": x} for x in fuente]}})
        return _R(404, {})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "x:9200"})
    monkeypatch.setattr(main, "_read_https_enabled_from_state", lambda td: False)
    monkeypatch.setattr(main, "_cluster_admin_password", lambda td: "pw")
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {"produccion-pozos": {"forecaster_ids": ["F1"]}})
    return pedidos


def test_el_endpoint_arma_el_pronostico(monkeypatch):
    reales = [_real(i, 100.0) for i in range(12)]
    pedidos = _cluster(monkeypatch, reales, [_pron(1, 110.0, 8), _pron(2, 100.0, 8), _pron(3, 100.0, 8)])
    r = main.pronosticos_del_caso("produccion-pozos")
    p = r.pronosticos[0]
    assert p["medida"] == "oil_volume" and p["intervalo_min"] == 262 and p["horizonte"] == 3 and p["error"] == ""
    assert p["ancla"] == pr._iso(T0 + 8 * PASO) and len(p["pronostico"]) == 3
    assert p["error_pct"] == round(10 / 300 * 100, 1)
    # Los valores reales son los resultados SIN horizon_index (feature_data no está indexado).
    consulta = next(b for u, b in pedidos if "opensearch-forecast-results" in u)
    assert {"bool": {"must_not": [{"exists": {"field": "horizon_index"}}]}} in consulta["query"]["bool"]["filter"]
    assert {"term": {"task_id": "T1"}} in consulta["query"]["bool"]["filter"]


def test_sin_backtest_o_sin_datos_lo_dice(monkeypatch):
    _cluster(monkeypatch, [], [], task="")
    assert main.pronosticos_del_caso("produccion-pozos").pronosticos[0]["error"] == "el forecaster no tiene backtest"
    _cluster(monkeypatch, [_real(i, 0) for i in range(12)], [])
    assert main.pronosticos_del_caso("produccion-pozos").pronosticos[0]["error"] == \
        "el backtest todavía no tiene pasos con datos"


def test_un_caso_sin_forecasters(monkeypatch):
    _cluster(monkeypatch, [], [])
    assert main.pronosticos_del_caso("siem").pronosticos == []


# ── La vista ────────────────────────────────────────────────────────────────
def _funciones(html: str) -> str:
    i = html.index("    function _forecastersDe(ids) {")
    return html[i:html.index("    async function verPronosticos(btn) {", i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = { 'produccion-pozos': 'Producción de pozos' };
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
check('sin forecasters, nada', pronosticosHTML({ s: { detector_id: 'D' } }) === '' && pronosticosHTML(null) === '');
const card = pronosticosHTML({ 'produccion-pozos': { forecaster_ids: ['a', 'b', 'c'] }, cts: { forecaster_id: 'z' } });
check('cuántos', card.includes('4 pronósticos en 2 casos'), card);
check('un botón por caso', card.includes('class="btn btn-secondary btn-sm pron__caso" data-slug="produccion-pozos">Producción de pozos<')
  && card.includes('data-slug="cts">cts<'), card);
const d = pronosticosDetalleHTML({ pronosticos: [
  { medida: 'oil_volume', intervalo_min: 262, horizonte: 8, error_pct: 4.3, error: '' },
  { medida: 'revenue', intervalo_min: 30, horizonte: 8, error_pct: 32.3, error: '' },
  { medida: 'x', intervalo_min: 60, horizonte: 8, error_pct: 93.5, error: '' },
  { medida: 'y', intervalo_min: 60, horizonte: 8, error_pct: null, error: 'el backtest todavía no tiene pasos con datos' },
] });
check('el intervalo en horas', d.includes('cada 4,4 h · 8 pasos hacia adelante'), d);
check('en minutos', d.includes('cada 30 min'), d);
check('error bajo en verde', d.includes('sev--ok">error medio 4,3 %'), d);
check('intermedio en amarillo', d.includes('sev--high">error medio 32,3 %'), d);
check('alto en rojo', d.includes('sev--critical">error medio 93,5 %'), d);
check('el lugar del gráfico', d.includes('<div class="pron__grafico" data-i="0"></div>'), d);
check('el motivo cuando no hay', d.includes('el backtest todavía no tiene pasos con datos') && !d.includes('data-i="3"'), d);
check('sin pronósticos', pronosticosDetalleHTML({ pronosticos: [] }).includes('no tiene pronósticos'));
const spec = specPronostico({ medida: 'oil_volume', ancla: '2026-06-29 12:15:44',
  real: [{ t: 'a', v: 1 }], pronostico: [{ t: 'b', v: 2, lo: 1, hi: 3 }] }, {});
check('real y pronóstico', spec.data.values.length === 2 && spec.data.values[1].serie === 'Pronóstico' && spec.data.values[1].hi === 3);
check('banda, líneas y regla', spec.layer.length === 3 && spec.layer[0].mark.type === 'area' && spec.layer[2].mark.type === 'rule'
  && spec.layer[2].data.values[0].t === '2026-06-29 12:15:44');
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_tarjeta_y_el_detalle_en_node(tmp_path):
    js = tmp_path / "pron.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", _funciones(_INDEX.read_text(encoding="utf-8"))), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_la_pestana_y_el_motor_de_graficos():
    html = _INDEX.read_text(encoding="utf-8")
    assert "{ id: 'pronosticos', label: 'Forecasting', icon: 'trending', cuenta: nPronosticos, html: pronosticosHTML(data.capabilities) }," in html
    assert '<symbol id="ic-trending"' in html
    assert "capVega = { ensure: _ensureVega, theme: _vegaTheme, config: _vegaConfig };" in html
    i = html.index("    async function verPronosticos(btn) {")
    fn = html[i:html.index("\n    }\n", i)]
    assert "fetch('/api/v1/forecast/' + encodeURIComponent(btn.dataset.slug))" in fn
    assert "embed(lugar, specPronostico(p, config), { actions: false, renderer: 'svg' })" in fn
    assert "const b = e.target.closest('.pron__caso');\n        if (b) verPronosticos(b);" in html
