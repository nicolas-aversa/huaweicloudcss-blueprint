"""Perfil por entidad (Transforms): una fila por IP, cliente o pozo."""
import json
import pathlib

import pytest

import main
import perfiles
import verticals

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"
PERFIL = verticals.get_vertical("siem")["perfil"]


def test_el_transform_resume_el_caso_en_un_indice_aparte():
    t = perfiles.build_transform("siem", "siem*", PERFIL)["transform"]
    assert t["source_index"] == "siem*" and t["target_index"] == "perfil-siem"
    # Fuera del pattern del caso: no se mezcla con los datos crudos.
    assert not t["target_index"].startswith("siem-")
    assert t["groups"] == [{"terms": {"source_field": "source.ip", "target_field": "entidad"}}]
    assert t["continuous"] is True and t["enabled"] is True
    assert t["data_selection_query"] == {"exists": {"field": "source.ip"}}, "sin el grupo de eventos sin IP"
    assert "data_selection_query" not in perfiles.build_transform(
        "ventas-ecommerce", "v*", verticals.get_vertical("ventas-ecommerce")["perfil"])["transform"]


@pytest.mark.parametrize("slug", ["siem", "ventas-ecommerce", "produccion-pozos"])
def test_las_medidas_son_las_que_soporta_transforms(slug):
    p = verticals.get_vertical(slug)["perfil"]
    for nombre, agg in p["medidas"].items():
        assert set(agg) <= {"value_count", "sum", "avg", "min", "max"}, (nombre, agg)
    assert set(p.get("fechas", [])) <= set(p["medidas"]) and set(p["nombres"]) == set(p["medidas"])
    assert p["campo"] in verticals.get_vertical(slug)["capability"]["fields"]


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data or {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


def _cluster(monkeypatch, existe=False, falla="", estado="finished", continuo=True):
    pedidos = []

    def req(method, url, user, password, json_body=None, timeout=30):
        ruta = url.split(":9200", 1)[1]
        pedidos.append((method, ruta))
        if falla and ruta.endswith(falla) and method != "GET":
            return _R(400, {"error": "malo"})
        if method == "GET" and ruta == "/_plugins/_transform/siem-perfil":
            return _R(200, {"transform": {"continuous": continuo}}) if existe else _R(404)
        if method == "GET" and ruta == "/_plugins/_transform/siem-perfil/_explain":
            return _R(200, {"siem-perfil": {"transform_metadata": {
                "status": estado, "failure_reason": "Failed to index the documents" if estado == "failed" else None}}})
        return _R(200, {"acknowledged": True})

    monkeypatch.setattr(main, "_os_req", req)
    return pedidos


def test_alta_del_perfil(monkeypatch):
    pedidos = _cluster(monkeypatch)
    r = main._provisionar_perfil("http://x:9200", "a", "p", "siem", "siem-*", PERFIL, force=False)
    assert r == {"ok": True, "reason": "siem-perfil → perfil-siem, una fila por IP de origen"}
    assert pedidos == [("GET", "/_plugins/_transform/siem-perfil"), ("PUT", "/_plugins/_transform/siem-perfil"),
                       ("POST", "/_plugins/_transform/siem-perfil/_start")]


def test_si_ya_estaba_se_deja_y_con_force_se_rehace(monkeypatch):
    pedidos = _cluster(monkeypatch, existe=True)
    assert main._provisionar_perfil("http://x:9200", "a", "p", "siem", "siem-*", PERFIL, force=False) == \
        {"ok": True, "reason": "ya estaba"}
    assert [m for m, _ in pedidos] == ["GET", "GET"], "solo mira; no toca nada"
    pedidos = _cluster(monkeypatch, existe=True)
    main._provisionar_perfil("http://x:9200", "a", "p", "siem", "siem-*", PERFIL, force=True)
    assert ("DELETE", "/_plugins/_transform/siem-perfil") in pedidos and ("DELETE", "/perfil-siem") in pedidos
    assert pedidos.index(("DELETE", "/perfil-siem")) < pedidos.index(("PUT", "/_plugins/_transform/siem-perfil"))


@pytest.mark.parametrize("falla, motivo", [("/siem-perfil", "status 400"), ("/_start", "creado pero no arrancó")])
def test_un_error_se_dice(monkeypatch, falla, motivo):
    _cluster(monkeypatch, falla=falla)
    r = main._provisionar_perfil("http://x:9200", "a", "p", "siem", "siem-*", PERFIL, force=False)
    assert r["ok"] is False and motivo in r["reason"]


def _funciones(html: str) -> str:
    i = html.index("    function perfilesHTML(pipelines) {")
    return html[i:html.index("    async function verPerfil(btn) {", i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = { siem: 'SIEM' };
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
check('sin perfiles, nada', perfilesHTML([{ slug: 'cts', perfil: '' }]) === '');
const card = perfilesHTML([{ slug: 'siem', perfil: 'IP de origen' }, { slug: 'cts', perfil: '' }, { slug: 'mi-dataset', perfil: 'Cliente' }]);
check('un dataset nuevo también', card.includes('data-slug="mi-dataset"') && card.includes('ip de origen, cliente'), card);
check('la tarjeta', card.includes('una fila por ip de origen') && card.includes('class="maestro__item perfil__caso" data-slug="siem"><span>SIEM</span><span class="maestro__n">IP de origen</span>') && !card.includes('data-slug="cts"'), card);
const d = perfilDetalleHTML({ estado: 'finished', columnas: ['IP de origen', 'Eventos', 'Riesgo máx.', 'Primer evento'], fechas: [3],
  filas: [['5.188.206.18', 12571, '-Infinity', 1751372608000]], error: '' });
check('encabezado', d.includes('Las 1 de mayor eventos') && d.includes('Transform: Terminado'), d);
check('números y fechas', d.includes('<td>12.571</td>') && d.includes('<td>—</td>') && d.includes('<td>2025-07-01 12:23</td>'), d);
check('error', perfilDetalleHTML({ error: 'el perfil todavía no existe' }).includes('todavía no existe'));
check('vacío', perfilDetalleHTML({ error: '', filas: [], estado: 'started' }).includes('todavía está vacío (Corriendo…)'));
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


def test_si_habia_fallado_se_rehace(monkeypatch):
    """Visto: con el cluster saturado, el de SIEM indexó 581 de 34.259 IPs y
    quedó `failed`. "Volver a provisionar" lo tiene que rehacer, no saltear."""
    pedidos = _cluster(monkeypatch, existe=True, estado="failed")
    r = main._provisionar_perfil("http://x:9200", "a", "p", "siem", "siem-*", PERFIL, force=False)
    assert r["ok"] and "(se rehízo: había fallado (Failed to index the documents))" in r["reason"]
    assert pedidos.index(("DELETE", "/perfil-siem")) < pedidos.index(("PUT", "/_plugins/_transform/siem-perfil"))         < pedidos.index(("POST", "/_plugins/_transform/siem-perfil/_start"))


@pytest.mark.parametrize("estado", ["started", "finished", None])
def test_corriendo_o_terminado_no_se_toca(monkeypatch, estado):
    pedidos = _cluster(monkeypatch, existe=True, estado=estado)
    assert main._provisionar_perfil("http://x:9200", "a", "p", "siem", "siem-*", PERFIL, force=False)["reason"] == "ya estaba"
    assert all(m == "GET" for m, _ in pedidos)


# ── Una entidad sin el campo de una medida ──────────────────────────────────
def test_una_medida_que_puede_quedar_vacia_toma_cero():
    """Visto en SIEM: 419 de las primeras 1.000 IPs no tenían `event.risk_score`;
    su máximo quedó vacío, el Transform no pudo indexarlas y el perfil quedó
    en 581 filas de 34.259 (dos veces seguidas, con el cluster tranquilo)."""
    aggs = perfiles.build_transform("siem", "siem-*", verticals.get_vertical("siem")["perfil"])["transform"]["aggregations"]
    assert aggs["riesgo_max"] == {"max": {"field": "event.risk_score", "missing": 0}}
    # Las fechas no: un 0 sería 1970.
    assert aggs["primero"] == {"min": {"field": "@timestamp"}} and aggs["ultimo"] == {"max": {"field": "@timestamp"}}
    assert aggs["eventos"] == {"value_count": {"field": "@timestamp"}}


def test_sum_y_lo_ya_declarado_no_se_tocan():
    perfil = {"campo": "x", "fechas": [], "medidas": {
        "total": {"sum": {"field": "v"}}, "prom": {"avg": {"field": "v"}},
        "propio": {"min": {"field": "v", "missing": 5}}}}
    assert perfiles.medidas_sin_vacios(perfil) == {
        "total": {"sum": {"field": "v"}}, "prom": {"avg": {"field": "v", "missing": 0}},
        "propio": {"min": {"field": "v", "missing": 5}}}
    assert perfil["medidas"]["prom"] == {"avg": {"field": "v"}}, "no modifica el spec del vertical"


def test_el_transform_es_continuo():
    """De una sola pasada se armaba con la ingesta corriendo y quedaba con lo que
    había entrado (visto: 2.153 IPs de 34.259 en SIEM)."""
    assert perfiles.build_transform("siem", "siem-*", PERFIL)["transform"]["continuous"] is True


def test_uno_de_una_sola_pasada_se_rehace(monkeypatch):
    pedidos = _cluster(monkeypatch, existe=True, continuo=False)
    r = main._provisionar_perfil("http://x:9200", "a", "p", "siem", "siem-*", PERFIL, force=False)
    assert r["ok"] and r["reason"].endswith("(se rehízo: el anterior era de una sola pasada y quedó con los datos de ese momento)")
    assert ("PUT", "/_plugins/_transform/siem-perfil") in pedidos and ("DELETE", "/perfil-siem") in pedidos
