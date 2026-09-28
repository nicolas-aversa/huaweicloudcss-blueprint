"""Anomaly Detection y Alerting por caso.

Estaban sacados: con ventanas fijas los detectores no veían los datos de las
demos, que son del pasado. Ahora el intervalo sale del rango real del índice,
se corre un análisis HISTÓRICO sobre ese rango y un monitor de Alerting avisa
de las anomalías altas. En la vista, una tarjeta con las más fuertes y un
"Explicar" que abre el asistente sobre ese intervalo.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

import capabilities as caps
import main


class _Resp:
    def __init__(self, status, data=None, text=None):
        self.status_code, self._data = status, data if data is not None else {}
        self.text = text if text is not None else json.dumps(self._data)

    def json(self):
        return self._data


# ── Builders ────────────────────────────────────────────────────────────────
def test_las_medidas_salen_de_los_forecasts():
    spec = {"volume_field": "type", "forecasts": [
        {"feature_name": "bytes", "aggregation_query": {"bytes": {"sum": {"field": "sentbyte"}}}},
        {"feature_name": "eventos"},
        {"feature_name": "c"}, {"feature_name": "d"}]}
    f = caps.features_de_anomalias(spec)
    assert [x["feature_name"] for x in f] == ["bytes", "eventos", "c"], "hasta tres"
    assert f[0]["aggregation_query"] == {"bytes": {"sum": {"field": "sentbyte"}}}
    assert f[1]["aggregation_query"] == {"eventos": {"value_count": {"field": "type"}}}
    assert caps.features_de_anomalias({"volume_field": "v"}) == [
        {"feature_name": "volumen", "aggregation_query": {"volumen": {"value_count": {"field": "v"}}}}]
    assert caps.features_de_anomalias({}) == []


def test_el_detector_de_una_entidad_sobre_el_pattern():
    f = [{"feature_name": "a", "aggregation_query": {"a": {"sum": {"field": "x"}}}}]
    d = caps.build_ad_detector("siem", "siem-*", f, 17)
    assert d["name"] == "siem-anomalias" and d["indices"] == ["siem-*"] and d["time_field"] == "@timestamp"
    assert d["detection_interval"] == {"period": {"interval": 17, "unit": "Minutes"}}
    assert d["window_delay"] == {"period": {"interval": 1, "unit": "Minutes"}}
    assert d["feature_attributes"] == [{"feature_name": "a", "feature_enabled": True,
                                        "aggregation_query": {"a": {"sum": {"field": "x"}}}}]
    assert "category_field" not in d, "de una entidad: con datos sintéticos, las HC no entrenan"


def test_el_monitor_mira_los_resultados_del_detector():
    m = caps.build_monitor_de_anomalias("siem", "D1")
    assert m["name"] == "siem-anomalias-alerta" and m["monitor_type"] == "query_level_monitor" and m["enabled"]
    busqueda = m["inputs"][0]["search"]
    assert busqueda["indices"] == [".opendistro-anomaly-results*"]
    filtros = busqueda["query"]["query"]["bool"]["filter"]
    assert {"term": {"detector_id": "D1"}} in filtros and {"range": {"anomaly_grade": {"gte": 0.7}}} in filtros
    assert "range" not in json.dumps([x for x in filtros if "term" not in x and "anomaly_grade" not in json.dumps(x)])
    cond = m["triggers"][0]["query_level_trigger"]["condition"]["script"]["source"]
    assert "ctx.results[0].hits.total.value > 0" in cond


def test_el_intervalo_da_unos_mil():
    dia = 24 * 3600 * 1000
    assert main._intervalo_de_anomalias(0, 365 * dia) == (365 * 24 * 60) // 1000
    assert main._intervalo_de_anomalias(0, 60_000 * 30) == 1, "nunca menos de un minuto"


def test_fecha_para_ppl():
    assert main._fecha_ppl(1741918200000) == "2025-03-14 02:10:00"
    assert main._fecha_ppl(None) == "" and main._fecha_ppl("x") == ""


# ── Provisionar ─────────────────────────────────────────────────────────────
_SPEC = {"volume_field": "type", "forecasts": [{"feature_name": "eventos"}]}


def _preparar(monkeypatch, estados=("RUNNING", "FINISHED"), crear=201, arrancar=200, listo=(True, ""),
              rango=(1_000_000.0, 61_000_000.0, 5000)):
    pedidos, esperas = [], []
    estados = list(estados)

    def fake(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((method, url, json_body))
        if url.endswith("/_plugins/_anomaly_detection/detectors"):
            return _Resp(crear, {"_id": "D1"} if crear < 300 else {}, None if crear < 300 else "bad feature")
        if url.endswith("/_start"):
            return _Resp(arrancar, {}, None if arrancar < 300 else "no data")
        if "?task=true" in url:
            return _Resp(200, {"historical_analysis_task": {"state": estados.pop(0) if estados else "FINISHED"}})
        return _Resp(404)

    monkeypatch.setattr(main, "_os_req", fake)
    monkeypatch.setattr(main, "_index_ready_for_capabilities", lambda *a, **k: listo)
    monkeypatch.setattr(main, "_index_time_bounds", lambda *a, **k: rango)
    monkeypatch.setattr(main.time, "sleep", lambda s: esperas.append(s))
    return pedidos, esperas


def test_crea_el_detector_y_corre_el_historico(monkeypatch):
    pedidos, esperas = _preparar(monkeypatch)
    ids = {}
    res = main._provisionar_anomalias("http://x", "admin", "pw", "siem", "siem-*", _SPEC, ids)
    assert ids == {"detector_id": "D1"}
    crear = next(b for m, u, b in pedidos if u.endswith("/detectors"))
    assert crear == caps.build_ad_detector("siem", "siem-*", caps.features_de_anomalias(_SPEC), 1)
    # El histórico va sobre TODO el rango, más un intervalo para incluir el último dato.
    start = next(b for m, u, b in pedidos if u.endswith("/detectors/D1/_start"))
    assert start == {"start_time": 1_000_000, "end_time": 61_000_000 + 60_000}
    assert res["ok"] is True and res["estado"] == "FINISHED" and res["intervalo_min"] == 1
    assert res["note"] == "análisis histórico terminado, de a 1 min, con 1 medida"
    assert esperas == [main._AD_ESPERA_S], "espera entre sondeos, no después del último"


def test_en_curso_tambien_esta_bien(monkeypatch):
    pedidos, esperas = _preparar(monkeypatch, estados=["RUNNING"] * 10)
    res = main._provisionar_anomalias("http://x", "admin", "pw", "s", "s-*", _SPEC, {})
    assert res["ok"] is True and res["estado"] == "RUNNING" and "en curso" in res["note"]
    assert len([u for m, u, b in pedidos if "?task=true" in u]) == 4
    assert len(esperas) == 3, "entre sondeos: después del último no se espera"


@pytest.mark.parametrize("kw, motivo", [
    ({"listo": (False, "índice sin documentos aún")}, "índice sin documentos aún"),
    ({"rango": None}, "rango de @timestamp utilizable"),
    ({"crear": 400}, "no se pudo crear el detector: status 400: bad feature"),
])
def test_lo_que_impide_crearlo_se_dice(monkeypatch, kw, motivo):
    _preparar(monkeypatch, **kw)
    ids = {}
    res = main._provisionar_anomalias("http://x", "admin", "pw", "s", "s-*", _SPEC, ids)
    assert res["ok"] is False and motivo in res["reason"] and ids == {}


def test_si_el_historico_no_arranca_queda_el_detector(monkeypatch):
    _preparar(monkeypatch, arrancar=400)
    ids = {}
    res = main._provisionar_anomalias("http://x", "admin", "pw", "s", "s-*", _SPEC, ids)
    assert ids == {"detector_id": "D1"}, "para poder borrarlo"
    assert res["ok"] is False and "el análisis histórico no arrancó: status 400: no data" in res["reason"]


def test_si_el_historico_falla_no_es_ok(monkeypatch):
    _preparar(monkeypatch, estados=["FAILED"])
    res = main._provisionar_anomalias("http://x", "admin", "pw", "s", "s-*", _SPEC, {})
    assert res["ok"] is False and res["reason"] == "el análisis histórico terminó en FAILED"


def test_sin_medidas_no_hay_detector(monkeypatch):
    pedidos, _ = _preparar(monkeypatch)
    res = main._provisionar_anomalias("http://x", "admin", "pw", "s", "s-*", {}, {})
    assert res["ok"] is False and not pedidos


def _provisionar_caso(monkeypatch, tmp_path, feats, registro=None, monitor=201):
    """`_provision_capabilities` entero, con todo lo demás apagado."""
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_resolve_capability_spec",
                        lambda *a, **k: {"index_pattern": "s-*", "volume_field": "type", "fields": {}, "operations": []})
    monkeypatch.setattr(main, "_read_capabilities", lambda td: dict(registro or {}))
    guardado = {}
    monkeypatch.setattr(main, "_write_capabilities", lambda td, reg: guardado.update(reg))
    monkeypatch.setattr(main, "_read_cluster_features", lambda td: feats)
    monkeypatch.setattr(main, "_cluster_hwc_creds", lambda td: ("", ""))
    import maas_integrator
    monkeypatch.setattr(maas_integrator, "get_maas_api_key", lambda: "")

    def fake_anomalias(base, user, password, slug, ip, spec, ids):
        ids["detector_id"] = "D1"
        return {"ok": True, "detector_id": "D1"}

    monkeypatch.setattr(main, "_provisionar_anomalias", fake_anomalias)
    pedidos = []

    def fake(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((method, url, json_body))
        if url.endswith("/_plugins/_alerting/monitors"):
            return _Resp(monitor, {"_id": "M1"} if monitor < 300 else {}, None if monitor < 300 else "denied")
        return _Resp(404)

    monkeypatch.setattr(main, "_os_req", fake)
    res = main._provision_capabilities({"public_endpoint": "x:9200"}, "s", "admin", "pw", False)
    return res, guardado, pedidos


def test_el_caso_queda_con_detector_y_alerta(monkeypatch, tmp_path):
    res, guardado, pedidos = _provisionar_caso(monkeypatch, tmp_path, {"ad": True, "alerting": True})
    assert res["anomalias"] == {"ok": True, "detector_id": "D1"}
    assert res["alertas"]["ok"] is True and res["alertas"]["monitor_id"] == "M1"
    assert guardado["s"]["detector_id"] == "D1" and guardado["s"]["monitor_id"] == "M1"
    monitor = next(b for m, u, b in pedidos if u.endswith("/monitors"))
    assert monitor == caps.build_monitor_de_anomalias("s", "D1")


def test_sin_los_plugins_se_dice(monkeypatch, tmp_path):
    res, guardado, _ = _provisionar_caso(monkeypatch, tmp_path, {"ad": False, "alerting": False})
    assert res["anomalias"] == {"ok": False, "reason": "Anomaly Detection no está en este cluster"}
    assert res["alertas"] == {"ok": False, "reason": "sin detector de anomalías"}
    res2, _, _ = _provisionar_caso(monkeypatch, tmp_path, {"ad": True, "alerting": False})
    assert res2["alertas"] == {"ok": False, "reason": "Alerting no está en este cluster"}
    # Sin detectar (entorno viejo): se intenta.
    res3, _, _ = _provisionar_caso(monkeypatch, tmp_path, {})
    assert res3["anomalias"]["ok"] is True


def test_ya_provisionado_no_se_repite(monkeypatch, tmp_path):
    res, _, pedidos = _provisionar_caso(monkeypatch, tmp_path, {"ad": True, "alerting": True},
                                        registro={"s": {"detector_id": "D0", "monitor_id": "M0"}})
    assert res["anomalias"]["reason"] == "ya provisionado" and res["alertas"]["reason"] == "ya provisionado"
    assert not [u for m, u, b in pedidos if u.endswith("/monitors")]


def test_si_el_monitor_falla_se_dice(monkeypatch, tmp_path):
    res, guardado, _ = _provisionar_caso(monkeypatch, tmp_path, {"ad": True, "alerting": True}, monitor=403)
    assert res["alertas"] == {"ok": False, "reason": "no se pudo crear el monitor: status 403: denied"}
    assert "monitor_id" not in guardado["s"]


# ── Borrar ──────────────────────────────────────────────────────────────────
def test_se_para_antes_de_borrar(monkeypatch):
    pedidos = []
    monkeypatch.setattr(main, "_os_req", lambda m, u, *a, **k: pedidos.append((m, u)))
    main._borrar_detector_ad("http://x", "admin", "pw", "D1")
    assert pedidos == [("POST", "http://x/_plugins/_anomaly_detection/detectors/D1/_stop?historical=true"),
                       ("POST", "http://x/_plugins/_anomaly_detection/detectors/D1/_stop"),
                       ("DELETE", "http://x/_plugins/_anomaly_detection/detectors/D1")]


def test_los_huerfanos_se_buscan_por_nombre(monkeypatch):
    buscados, pedidos = [], []

    def fake_buscar(base, u, p, ruta, nombre, name_field="name.keyword"):
        buscados.append((ruta, nombre, name_field))
        return [f"H-{nombre}"] if nombre.startswith("siem-anomalias") else []

    monkeypatch.setattr(main, "_search_ids", fake_buscar)
    monkeypatch.setattr(main, "_os_req", lambda m, u, *a, **k: pedidos.append((m, u)))
    main._teardown_orphans_by_name("http://x", "admin", "pw")
    # El monitor se borra, y el detector se para antes de borrarlo.
    assert ("DELETE", "http://x/_plugins/_alerting/monitors/H-siem-anomalias-alerta") in pedidos
    d = "http://x/_plugins/_anomaly_detection/detectors/H-siem-anomalias"
    i = pedidos.index(("POST", f"{d}/_stop?historical=true"))
    assert pedidos[i + 2] == ("DELETE", d)
    for s in caps.get_capability_slugs():
        assert ("/_plugins/_alerting/monitors/_search", f"{s}-anomalias-alerta", "monitor.name.keyword") in buscados
        assert ("/_plugins/_anomaly_detection/detectors/_search", f"{s}-anomalias", "name.keyword") in buscados
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert src.count("_borrar_detector_ad(base, user, password, ids[\"detector_id\"])") == 2
    assert "AD y alerting eliminados" not in src


# ── Leer las anomalías ──────────────────────────────────────────────────────
def test_una_anomalia_para_la_vista():
    a = main._anomalia({"data_start_time": 1741918200000, "data_end_time": 1741918800000,
                        "anomaly_grade": 0.93456, "confidence": 0.8811,
                        "feature_data": [{"feature_name": "eventos", "data": 1840.0}]})
    assert a == {"inicio": "2025-03-14 02:10:00", "fin": "2025-03-14 02:20:00", "grado": 0.93,
                 "confianza": 0.88, "valores": [{"nombre": "eventos", "valor": 1840.0}]}


def test_el_resumen_en_vivo(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {"s": {"detector_id": "D1"}, "otro": {"agent_id": "A"}})
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "x:9200"})
    monkeypatch.setattr(main, "_cluster_admin_password", lambda td: "pw")
    monkeypatch.setattr(main, "_read_https_enabled_from_state", lambda td: False)
    pedidos = []
    fuente = {"data_start_time": 1741918200000, "data_end_time": 1741918800000, "anomaly_grade": 0.9,
              "confidence": 0.9, "feature_data": []}

    def fake(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((method, url, json_body))
        if "results/_search" in url:
            return _Resp(200, {"hits": {"total": {"value": 37}, "hits": [{"_source": fuente}] * 8}})
        return _Resp(200, {"historical_analysis_task": {"state": "RUNNING"}})

    monkeypatch.setattr(main, "_os_req", fake)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/api/v1/anomalias/resumen")
    caso = r.json()["casos"]
    assert len(caso) == 1, "solo los casos con detector"
    c = caso[0]
    assert c["slug"] == "s" and c["estado"] == "RUNNING" and c["total"] == 37 and len(c["top"]) == 5 and c["error"] == ""
    busqueda = next(b for m, u, b in pedidos if "results/_search" in u)
    assert {"term": {"detector_id": "D1"}} in busqueda["query"]["bool"]["filter"]
    assert busqueda["sort"] == [{"anomaly_grade": {"order": "desc"}}] and busqueda["size"] == 5
    assert len([u for m, u, b in pedidos if "?task=true" in u]) == 1, "un solo sondeo del estado"


def test_sin_detectores_no_se_toca_el_cluster(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_read_capabilities", lambda td: {"s": {"agent_id": "A"}})
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: pytest.fail("no debería llamar al cluster"))
    from fastapi.testclient import TestClient
    assert TestClient(main.app).get("/api/v1/anomalias/resumen").json() == {"casos": []}


# ── La vista ────────────────────────────────────────────────────────────────
_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"

_ARNES = r"""
const icon = (n) => `<i:${n}>`;
const escapeHtml = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = { s: 'Billetera' };
let capChatPreguntar = null;
const toasts = [];
const toast = (m) => toasts.push(m);
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
check('sin detectores, nada', anomaliasHTML({ s: { agent_id: 'A' } }) === '' && anomaliasHTML(null) === '');
const card = anomaliasHTML({ s: { detector_id: 'D', monitor_id: 'M' }, t: { detector_id: 'E' }, u: { agent_id: 'A' } });
check('cuántos', card.includes('2 detectores') && card.includes('1 con alerta (grado ≥ 0,7)'), card);
check('los casos', card.includes('Billetera · t') && !card.includes('>u<'));
check('botón', card.includes('id="infra-anomalias-ver"') && card.includes('id="infra-anomalias-detalle"'));
const d = anomaliasDetalleHTML([
  { slug: 's', estado: 'FINISHED', total: 37, error: '', top: [
    { inicio: '2025-03-14 02:10:00', fin: '2025-03-14 02:20:00', grado: 0.93, valores: [{ nombre: 'eventos', valor: 1840.5 }] },
    { inicio: '2025-03-02 19:40:00', fin: '2025-03-02 19:50:00', grado: 0.61, valores: [] },
    { inicio: '2025-03-05 10:00:00', fin: '2025-03-05 10:10:00', grado: 0.75, valores: [] }] },
  { slug: 't', estado: 'RUNNING', total: 0, error: '', top: [] },
  { slug: 'u', estado: 'FINISHED', total: 0, error: '', top: [] },
  { slug: 'v', estado: '', total: 0, error: 'status 500: x', top: [] },
]);
check('grado en %', d.includes('sev--critical">93 %<') && d.includes('sev--high">61 %<'), d);
check('desde 70 % es alto', d.includes('sev--critical">75 %<'), d);
check('el intervalo', d.includes('2025-03-14 02:10:00 → 02:20:00'), d);
check('valores', d.includes('eventos: 1.840,5'), d);
check('explicar con sus datos', d.includes('class="btn btn-secondary btn-sm anomalia__explicar" data-slug="s" data-inicio="2025-03-14 02:10:00" data-fin="2025-03-14 02:20:00" data-grado="93" data-valores="eventos: 1.840,5"'), d);
check('estado', d.includes('Análisis terminado') && d.includes('Analizando…'));
check('total', d.includes('37 anomalías'));
check('corriendo, sin anomalías', d.includes('el análisis histórico sigue corriendo'));
check('terminado, sin anomalías', d.includes('el análisis terminó y no encontró ninguna'));
check('error', d.includes('No se pudieron leer: status 500: x'));
check('nada', anomaliasDetalleHTML([]).includes('No hay detectores de anomalías'));
const b = { dataset: { slug: 's', inicio: '2025-03-14 02:10:00', fin: '2025-03-14 02:20:00', grado: '93', valores: 'eventos: 1.840' } };
check('sin asistente, avisa', explicarAnomalia(b) === false && toasts.length === 1 && toasts[0].includes('Provisionar plugins'));
let pedido = null;
capChatPreguntar = (slug, pregunta, contexto) => { pedido = { slug, pregunta, contexto }; return true; };
check('con asistente', explicarAnomalia(b) === true && toasts.length === 1);
check('la pregunta', pedido.pregunta === '¿Qué pasó en ese intervalo? ¿Por qué es una anomalía?');
check('el contexto', pedido.contexto === 'Anomalía detectada entre 2025-03-14 02:10:00 y 2025-03-14 02:20:00 (UTC), grado 93 %; valores: eventos: 1.840. Compará ese intervalo con el resto, filtrando @timestamp entre esas dos fechas.', pedido.contexto);
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


def _funciones(html: str) -> str:
    i = html.index("    function anomaliasHTML(capabilities) {")
    return html[i:html.index("    async function verAnomalias(btn) {", i)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_tarjeta_y_las_anomalias_en_node(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    js = tmp_path / "ad.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", _funciones(html)), encoding="utf-8")
    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, "checks fallidos:\n" + (res.stdout or res.stderr)


def test_la_vista_la_pinta_con_sus_fichas():
    html = _INDEX.read_text(encoding="utf-8")
    assert "${anomaliasHTML(data.capabilities)}" in html
    assert "body.querySelector('#infra-anomalias-ver')?.addEventListener('click', (e) => verAnomalias(e.currentTarget));" in html
    assert "const b = e.target.closest('.anomalia__explicar');\n        if (b) explicarAnomalia(b);" in html
    assert "anomalias: 'Detección de anomalías'," in html and "alertas: 'Alertas'," in html
    assert "anomalias: ids.detector_id ? {ok: true, detector_id: ids.detector_id} : {ok: false, reason: 'no provisionado'}," in html
    assert "alertas: ids.monitor_id ? {ok: true, monitor_id: ids.monitor_id} : {ok: false, reason: 'no provisionado'}," in html
    k = html.index("async function verAnomalias(btn) {")
    ver = html[k:html.index("\n    }\n", k)]
    assert "fetch('/api/v1/anomalias/resumen')" in ver and "destino.innerHTML = anomaliasDetalleHTML(data.casos || []);" in ver
