"""Cuánto cluster necesita un dataset en producción: el puente entre la PoC y
la compra. Una estimación con sus supuestos a la vista."""
import pathlib
import shutil
import subprocess

import pytest

import dimensionamiento as dm
import main

_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"


def test_los_bytes_por_evento_salen_de_la_muestra():
    assert dm.bytes_por_evento("abcd\n\n  \nabcdefgh\n") == 6, "promedio, sin las líneas vacías"
    assert dm.bytes_por_evento("ñ") == 2, "en UTF-8"
    assert dm.bytes_por_evento("") == 0


def test_un_volumen_chico_con_alta_disponibilidad():
    d = dm.dimensionar(400, 1_000_000, 90, True)
    assert (d["nodos"], d["flavor"], d["replicas"]) == (3, "ess.spec-4u8g", 1), "alta disponibilidad: 3 nodos y una réplica"
    assert d["gb_por_dia"] == 0.52 and d["gb_de_datos"] == 46.8
    assert d["gb_de_disco"] == round(46.8 * 2 / 0.75, 1), "la réplica y el margen de disco"
    assert d["disco_por_nodo_gb"] == 50 and d["shards_primarios"] == 1 and d["nodos_logstash"] == 1
    assert not d["excede"] and len(d["supuestos"]) == 6


def test_sin_alta_disponibilidad_alcanza_un_nodo():
    d = dm.dimensionar(400, 1_000_000, 30, False)
    assert (d["nodos"], d["replicas"], d["disco_por_nodo_gb"]) == (1, 0, dm.DISCO_MINIMO_GB)


def test_a_igual_costo_menos_nodos_mas_grandes():
    d = dm.dimensionar(400, 50_000_000, 90, True)
    assert (d["nodos"], d["flavor"]) == (4, "ess.spec-16u32g"), "64 vCPU en 4 nodos y no en 16"
    assert d["nodos"] * d["ram_por_nodo_gb"] * dm.DISCO_POR_GB_DE_RAM >= d["gb_de_disco"]
    assert d["shards_primarios"] == 27, "índices mensuales de ~800 GB, en shards de hasta 30 GB"


def test_lo_que_no_entra_lo_dice():
    d = dm.dimensionar(400, 2_000_000_000, 90, True)
    assert d["excede"] and dm.en_palabras(d).startswith("más de 32 nodos ess.spec-32u64g")


def test_en_palabras():
    assert dm.en_palabras(dm.dimensionar(400, 1_000_000, 90, True)) == (
        "3 nodos ess.spec-4u8g con 50 GB de disco cada uno · 1 shard primario por índice mensual, "
        "1 réplica · 1 nodo de Logstash")
    assert dm.en_palabras(dm.dimensionar(400, 0, 90)) == "sin volumen diario no se puede dimensionar"


def test_el_endpoint_usa_la_retencion_del_ciclo_de_vida():
    from fastapi.testclient import TestClient
    r = TestClient(main.app).post("/api/v1/onboarding/dimensionar", json={
        "bytes_por_evento": 400, "eventos_por_dia": 1_000_000, "retencion_dias": 0, "alta_disponibilidad": True})
    d = r.json()
    assert r.status_code == 200 and d["retencion_dias"] == 90 and d["en_palabras"].startswith("3 nodos")


def test_el_volumen_se_guarda_con_el_caso():
    import custom_cases
    assert custom_cases._volumen({"eventos_por_dia": "5000000", "alta_disponibilidad": False}) == \
        {"eventos_por_dia": 5_000_000, "alta_disponibilidad": False}
    assert custom_cases._volumen({"eventos_por_dia": 0}) == {} and custom_cases._volumen("x") == {}
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert '"volumen": request.get("volumen") or {},' in src
    html = _INDEX.read_text(encoding="utf-8")
    assert html.count("volumen: state.planVolumen || {}") == 2, "una fuente o varias"


_ARNES = r"""
globalThis.state = { logFileContent: 'abcd\nabcdefgh\n\n' };
""" + "{FUNCION}" + r"""
const f = [];
if (bytesPorEvento() !== 6) f.push('bytes ' + bytesPorEvento());
state.logFileContent = ''; state.rawLog = 'ñ';
if (bytesPorEvento() !== 2) f.push('utf8 ' + bytesPorEvento());
state.rawLog = '';
if (bytesPorEvento() !== 0) f.push('vacío');
console.log(f.join('\n')); process.exit(f.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_los_bytes_en_el_navegador(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function bytesPorEvento() {")
    js = tmp_path / "dim.mjs"
    js.write_text(_ARNES.replace("{FUNCION}", html[i:html.index("    function dimensionamientoHTML() {", i)]), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_el_paso_2_lo_muestra():
    html = _INDEX.read_text(encoding="utf-8")
    assert "${dimensionamientoHTML()}" in html
    assert "lugar.querySelector('.plan-dim__eventos')?.addEventListener('change', volumen);" in html
    assert "fetch('/api/v1/onboarding/dimensionar'" in html
