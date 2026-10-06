"""Campañas del SIEM: la línea de tiempo de cada una, desde los datos
(event.campaign). Formato de la agregación como lo devolvió el cluster real."""
import json
import pathlib
import shutil
import subprocess

import pytest

import main
import seguridad
import verticals

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


def _paso(fuente, accion, n, ini, fin):
    return {"key": fuente, "doc_count": n, "acciones": {"buckets": [
        {"key": accion, "doc_count": n, "inicio": {"value_as_string": ini}, "fin": {"value_as_string": fin}}]}}


_RESP = {"aggregations": {"c": {"buckets": [
    {"key": "CMP-001", "doc_count": 4, "nombre": {"buckets": [{"key": "Web App Compromise"}]},
     "ips": {"buckets": [{"key": "176.10.99.200"}]},
     "fuentes": {"buckets": [_paso("cloudaudit", "create_access_key", 1, "2025-09-16T22:01:10.000Z", "2025-09-16T22:01:10.000Z"),
                             _paso("waf", "block", 3, "2025-09-16T20:46:10.000Z", "2025-09-16T21:20:10.000Z")]}},
    # La misma campaña partida por el fin de línea que dejaba el grok.
    {"key": "CMP-003", "doc_count": 1, "nombre": {"buckets": [{"key": "Lateral Movement"}]},
     "ips": {"buckets": [{"key": "23.129.64.130"}]},
     "fuentes": {"buckets": [_paso("cloudaudit", "create_server", 1, "2026-05-20T22:43:57.000Z", "2026-05-20T22:43:57.000Z")]}},
    {"key": "CMP-003\n", "doc_count": 1, "nombre": {"buckets": []}, "ips": {"buckets": [{"key": "23.129.64.130"}]},
     "fuentes": {"buckets": [_paso("fortigate", "blocked", 1, "2026-05-20T18:25:57.000Z", "2026-05-20T18:25:57.000Z")]}},
]}}}


def test_las_campanas_en_orden_y_unidas():
    c = seguridad.campanas(_RESP)
    assert [k["id"] for k in c] == ["CMP-001", "CMP-003"]
    cmp1 = c[0]
    assert cmp1["nombre"] == "Web App Compromise" and cmp1["eventos"] == 4 and cmp1["ips"] == ["176.10.99.200"]
    assert [(p["fuente"], p["accion"]) for p in cmp1["pasos"]] == [("waf", "block"), ("cloudaudit", "create_access_key")]
    assert (cmp1["desde"], cmp1["hasta"]) == ("2025-09-16T20:46:10.000Z", "2025-09-16T22:01:10.000Z")
    assert cmp1["fuentes"] == ["cloudaudit", "waf"]
    cmp3 = c[1]
    assert cmp3["eventos"] == 2 and cmp3["ips"] == ["23.129.64.130"] and cmp3["nombre"] == "Lateral Movement"
    assert [p["fuente"] for p in cmp3["pasos"]] == ["fortigate", "cloudaudit"]
    assert seguridad.campanas({}) == [] and seguridad.campanas(None) == []


def test_la_consulta_va_a_los_eventos_de_campana():
    q = seguridad.consulta_de_campanas()
    assert q["query"] == {"exists": {"field": "event.campaign"}} and q["size"] == 0
    pasos = q["aggs"]["c"]["aggs"]["fuentes"]
    assert pasos["terms"]["field"] == "event.dataset"
    assert pasos["aggs"]["acciones"]["terms"]["missing"] == "-"


def test_el_filtro_limpia_la_campana():
    f = verticals.get_vertical("siem")["filter_code"]
    assert 'strip => ["[event][campaign]", "[event][campaign_name]"]' in f
    assert "\\n" not in f.split("strip =>")[0][-200:]


class _R:
    def __init__(self, status, data):
        self.status_code, self._d, self.text = status, data, json.dumps(data)

    def json(self):
        return self._d


def test_el_endpoint_de_campanas(monkeypatch):
    pedidos = []
    monkeypatch.setattr(main, "_os_req", lambda m, url, *a, json_body=None, **k: pedidos.append((url, json_body)) or _R(200, _RESP))
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "x:9200"})
    monkeypatch.setattr(main, "_read_https_enabled_from_state", lambda td: False)
    monkeypatch.setattr(main, "_cluster_admin_password", lambda td: "pw")
    monkeypatch.setattr(main, "_read_security", lambda td: {"siem": {}, "fortianalyzer": {}})
    r = main.campanas_de_seguridad()
    assert [c["slug"] for c in r.casos] == ["siem"], "fortianalyzer no declara correlaciones"
    assert [k["id"] for k in r.casos[0]["campanas"]] == ["CMP-001", "CMP-003"]
    assert pedidos[0][0] == "http://x:9200/siem*/_search"
    monkeypatch.setattr(main, "_read_security", lambda td: {})
    assert main.campanas_de_seguridad().casos == []


def _funciones(html: str) -> str:
    i = html.index("    function campanasHTML(sa) {")
    return html[i:html.index("    async function verCampanas(btn) {", i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = { siem: 'SIEM' };
const toasts = [];
const toast = (m) => toasts.push(m);
let capChatPreguntar = null;
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
check('sin correlaciones, nada', campanasHTML({ fortianalyzer: { correlaciones: 0 } }) === '' && campanasHTML(null) === '');
const card = campanasHTML({ siem: { correlaciones: 2 } });
check('la tarjeta', card.includes('id="infra-campanas-ver"') && card.includes('Campañas') && card.includes('SIEM'), card);
const d = campanasDetalleHTML([{ slug: 'siem', error: '', campanas: [{ id: 'CMP-001', nombre: 'Web App Compromise', eventos: 4,
  fuentes: ['cloudaudit', 'waf'], ips: ['176.10.99.200'], desde: '2025-09-16T20:46:10.000Z', hasta: '2025-09-16T22:01:10.000Z',
  pasos: [{ fuente: 'waf', accion: 'block', eventos: 3, inicio: '2025-09-16T20:46:10.000Z' },
          { fuente: 'cloudaudit', accion: 'create_access_key', eventos: 1, inicio: '2025-09-16T22:01:10.000Z' }] }] }]);
check('el título', d.includes('CMP-001 · Web App Compromise') && d.includes('4 eventos en 2 fuentes · desde 176.10.99.200'), d);
check('los pasos en orden', d.indexOf('block') < d.indexOf('create_access_key') && d.includes('<span class="campana__hora">2025-09-16 20:46:10</span>'), d);
check('explicar con su ventana', d.includes('data-desde="2025-09-16 20:46:10"') && d.includes('data-hasta="2025-09-16 22:01:10"'), d);
check('sin campañas', campanasDetalleHTML([{ slug: 'siem', error: '', campanas: [] }]).includes('sin eventos de campaña'));
check('error', campanasDetalleHTML([{ slug: 'siem', error: 'boom', campanas: [] }]).includes('No se pudieron leer: boom'));
const b = { dataset: { slug: 'siem', campana: 'CMP-001', nombre: 'Web App Compromise', desde: '2025-09-16 20:46:10', hasta: '2025-09-16 22:01:10', ips: '176.10.99.200' } };
check('sin asistente, avisa', explicarCampana(b) === false && toasts.length === 1);
let pedido = null;
capChatPreguntar = (slug, pregunta, contexto, explicar) => { pedido = { slug, pregunta, contexto, explicar }; return true; };
check('con asistente', explicarCampana(b) === true);
check('la pregunta', pedido.pregunta === '¿Qué pasó en la campaña CMP-001 (Web App Compromise)?', pedido.pregunta);
check('la ventana', JSON.stringify(pedido.explicar) === JSON.stringify({ desde: '2025-09-16 20:46:10', hasta: '2025-09-16 22:01:10' }));
check('el contexto', pedido.contexto.includes('desde 176.10.99.200') && pedido.contexto.includes('en qué orden'), pedido.contexto);
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_tarjeta_y_la_linea_de_tiempo_en_node(tmp_path):
    js = tmp_path / "campanas.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", _funciones(_INDEX.read_text(encoding="utf-8"))), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_la_vista_las_conecta():
    html = _INDEX.read_text(encoding="utf-8")
    assert "seguridadHTML(data.security_analytics) + campanasHTML(data.security_analytics)" in html
    assert "body.querySelector('#infra-campanas-ver')?.addEventListener('click', (e) => verCampanas(e.currentTarget));" in html
    assert "const b = e.target.closest('.campana__explicar');\n        if (b) explicarCampana(b);" in html
    i = html.index("    async function verCampanas(btn) {")
    assert "fetch('/api/v1/security/campanas')" in html[i:html.index("\n    }\n", i)]
