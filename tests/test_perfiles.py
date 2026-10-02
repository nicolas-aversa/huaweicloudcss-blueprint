"""Perfil por entidad (Transforms): una fila por IP, cliente o pozo."""
import json
import pathlib
import shutil
import subprocess

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
    assert t["continuous"] is False and t["enabled"] is True
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


def test_la_tabla_del_perfil():
    hits = [{"_source": {"entidad": "5.188.206.18", "eventos": 571.0, "riesgo_max": 30.0, "primero": 1751372608000.0,
                         "ultimo": 1782843939000.0}}]
    t = perfiles.tabla_del_perfil(hits, PERFIL)
    assert t["columnas"] == ["IP de origen", "Eventos", "Riesgo máx.", "Primer evento", "Último evento"]
    assert t["filas"] == [["5.188.206.18", 571.0, 30.0, 1751372608000.0, 1782843939000.0]]
    assert t["fechas"] == [3, 4]
    assert perfiles.orden_del_perfil(PERFIL) == "eventos"


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data or {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


def _cluster(monkeypatch, existe=False, falla=""):
    pedidos = []

    def req(method, url, user, password, json_body=None, timeout=30):
        ruta = url.split(":9200", 1)[1]
        pedidos.append((method, ruta))
        if falla and ruta.endswith(falla) and method != "GET":
            return _R(400, {"error": "malo"})
        if method == "GET" and ruta == "/_plugins/_transform/siem-perfil":
            return _R(200 if existe else 404)
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
    assert len(pedidos) == 1
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
    i = html.index("    function perfilesHTML(slugs) {")
    return html[i:html.index("    async function verPerfil(btn) {", i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = { siem: 'SIEM' };
const LOG_EXAMPLES = [{ id: 'siem', perfil: 'IP de origen' }, { id: 'cts', perfil: '' }];
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
check('sin perfiles, nada', perfilesHTML(['cts']) === '');
const card = perfilesHTML(['siem', 'cts']);
check('la tarjeta', card.includes('una fila por ip de origen') && card.includes('data-slug="siem">SIEM<') && !card.includes('data-slug="cts"'), card);
const d = perfilDetalleHTML({ estado: 'finished', columnas: ['IP de origen', 'Eventos', 'Riesgo máx.', 'Primer evento'], fechas: [3],
  filas: [['5.188.206.18', 12571, '-Infinity', 1751372608000]], error: '' });
check('encabezado', d.includes('Las 1 de mayor eventos') && d.includes('Transform: Terminado'), d);
check('números y fechas', d.includes('<td>12.571</td>') && d.includes('<td>—</td>') && d.includes('<td>2025-07-01 12:23</td>'), d);
check('error', perfilDetalleHTML({ error: 'el perfil todavía no existe' }).includes('todavía no existe'));
check('vacío', perfilDetalleHTML({ error: '', filas: [], estado: 'started' }).includes('todavía está vacío (Corriendo…)'));
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_pestana_en_node(tmp_path):
    js = tmp_path / "perfiles.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", _funciones(_INDEX.read_text(encoding="utf-8"))), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_la_vista_la_registra():
    html = _INDEX.read_text(encoding="utf-8")
    assert "{ id: 'perfiles', label: 'Perfiles', icon: 'layers', html: perfilesHTML(pipelines.map(p => p.slug)) }," in html
    assert "const b = e.target.closest('.perfil__caso');\n        if (b) verPerfil(b);" in html
