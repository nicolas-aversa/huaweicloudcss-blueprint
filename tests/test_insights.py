"""Rendimiento: las consultas más pesadas según Query Insights. Formato de
`top_queries` como lo devolvió el cluster real."""
import json
import pathlib
from datetime import datetime, timezone

import pytest

import insights

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


def _q(indices, lat, cpu_ns, mem_b, source, ts=1790878106094):
    return {"timestamp": ts, "indices": indices, "total_shards": 13, "source": source,
            "measurements": {"latency": {"number": lat}, "cpu": {"number": cpu_ns}, "memory": {"number": mem_b}}}


TOP = [
    _q(["fortianalyzer-*"], 2055, 11_900_000, 277_914, {"size": 0, "track_total_hits": 2147483647}),
    _q(["fortianalyzer*"], 693, 1_215_202_854, 281_174_835,
       {"size": 0, "query": {"bool": {}}, "aggregations": {"x": {"date_range": {}, "aggs": {"y": {"sum": {}}}}}}),
    _q([".opendistro-alerting-config"], 9999, 9_999_999_999, 9_999_999, {"size": 100}),
    _q(["siem*"], 12, 1_000_000, 2048, {"size": 10, "query": {"term": {}}}),
]


def test_la_ventana_de_24_horas():
    assert insights.ventana(datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)) == \
        ("2026-09-30T18:00:00.000Z", "2026-10-01T18:00:00.000Z")


def test_sin_las_internas_y_ordenadas_por_lo_pedido():
    lat = insights.consultas(TOP, "latency")
    assert [c["indices"] for c in lat] == ["fortianalyzer-*", "fortianalyzer*", "siem*"], "sin .opendistro-*"
    assert lat[0] == {"hora": "2026-10-01 18:08:26", "indices": "fortianalyzer-*", "latencia_ms": 2055,
                      "cpu_ms": 11.9, "memoria_kb": 271.4, "forma": "conteo total", "shards": 13}
    cpu = insights.consultas(TOP, "cpu")
    assert cpu[0]["indices"] == "fortianalyzer*" and cpu[0]["cpu_ms"] == 1215.2
    assert len(insights.consultas(TOP, "latency", n=1)) == 1 and insights.consultas([], "cpu") == []


@pytest.mark.parametrize("source, forma", [
    ({"size": 0, "aggregations": {"a": {"date_range": {}, "aggs": {}}, "b": {"terms": {}}}}, "agregación (date_range, terms)"),
    ({"size": 0, "aggs": {"a": {"meta": {}}}}, "agregación"),
    ({"size": 10, "query": {}}, "búsqueda"),
    ({"size": 10, "query": {"term": {}}}, "búsqueda con filtro"),
    ({"size": 0, "query": {"term": {}}}, "conteo con filtro"),
    ({"size": 0}, "conteo total"),
])
def test_que_hace_la_consulta(source, forma):
    assert insights.forma_de_la_consulta(source) == forma


class _R:
    def __init__(self, status, data):
        self.status_code, self._d, self.text = status, data, json.dumps(data)

    def json(self):
        return self._d


def _funciones(html: str) -> str:
    i = html.index("    const _TIPOS_INSIGHTS = ")
    return html[i:html.index("    async function verRendimiento(btn) {", i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
const card = rendimientoHTML();
check('tres criterios', card.includes('data-tipo="latency">Latencia<') && card.includes('data-tipo="cpu">CPU<') && card.includes('data-tipo="memory">Memoria<'), card);
const d = rendimientoDetalleHTML({ error: '', consultas: [{ hora: '2026-10-01 18:08:26', indices: 'fortianalyzer-*', forma: 'conteo total',
  latencia_ms: 2055, cpu_ms: 1215.2, memoria_kb: 274584.8 }] });
check('la fila', d.includes('<td>fortianalyzer-*</td>') && d.includes('<td>2.055 ms</td>') && d.includes('<td>1.215,2 ms</td>') && d.includes('<td>274.584,8 KB</td>'), d);
check('vacío', rendimientoDetalleHTML({ error: '', consultas: [] }).includes('Sin consultas'));
check('error', rendimientoDetalleHTML({ error: 'boom', consultas: [] }).includes('No se pudieron leer: boom'));
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""
