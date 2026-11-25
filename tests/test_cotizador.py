"""El cotizador: el modelo validado en un cluster real (un OMS FIX de 30 sesiones
en LA-Santiago) reproduce las cifras del documento de dimensionamiento, sirve
para cualquier escenario (varias fuentes, hardware elegido, 3 AZ, precios de
otra región) y la vista pinta lo que devuelve la API. Se prueba en node con el
código real."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import cotizador
import main

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"
# Un OMS FIX: 8 GB por sesión por día, mensajes de 333 bytes, días hábiles, casi
# todo de 10 a 17 h y un pico de tres veces el promedio.
FIX = {"bytes_por_evento": 333, "dias_por_semana": 5, "horas_activas": 7, "pico": 3}


def _fix(sesiones, **kw):
    return cotizador.cotizar({**FIX, "gb_por_dia": 8 * sesiones, **kw})


# ── Las cifras del documento ────────────────────────────────────────────────
@pytest.mark.parametrize("sesiones, total, hot, flavor, clientes, shards", [
    (15, 1487, 4, "8u32g", 0, 4),
    (30, 2953, 4, "16u64g", 0, 8),
    (60, 6430, 8, "16u64g", 2, 16),
])
def test_una_semana_en_hot_da_lo_del_documento(sesiones, total, hot, flavor, clientes, shards):
    r = _fix(sesiones, retencion_dias=7, dias_hot=7, respaldo=True)
    assert round(r["total_mes"]) == total
    c = r["cluster"]
    assert (c["hot"]["nodos"], c["hot"]["flavor"], c["clientes"], c["shards"]) == (hot, flavor, clientes, shards)
    assert round(c["uso_pico"] * 100) == 74, "el pico usa el 74 % de lo medido, en los tres"
    assert not c["a_mano"]


def test_un_anio_con_congelados_da_lo_del_documento():
    r = _fix(30, retencion_dias=365, dias_hot=30, resto="congelados", respaldo=True)
    assert round(r["progresion"][0]["usd"]) == 5583 and round(r["progresion"][-1]["usd"]) == 8974
    assert round(r["anual"]["primer_anio"]) == 87330
    assert r["cluster"]["hot"]["disco_gb"] == 4980


def test_un_anio_con_nodos_cold():
    r = _fix(30, retencion_dias=365, dias_hot=30, resto="cold", respaldo=True)
    assert round(r["anual"]["primer_anio"], -1) == 165260
    cold = r["cluster"]["cold"]
    assert cold["nodos"] >= 4 and cold["nodos"] % 2 == 0, "con dos AZ, al menos 4 y en cantidad par"
    assert r["progresion"][0]["nodos_cold"] >= 4, "los cold se crean con el cluster"


def test_el_disco_queda_al_80_visto_por_opensearch():
    """÷ 0,74: el 5 % que reserva ext4 cuenta como usado (medido en un CSS 3.4)."""
    r = _fix(30, retencion_dias=7, dias_hot=7, respaldo=False)
    assert r["cluster"]["hot"]["disco_gb"] == 1300
    assert any("74 %" in s["fuente"] for s in r["supuestos"])


# ── Reglas ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("azs, replicas, minimo", [(2, 1, 4), (1, 1, 3), (2, 0, 4), (1, 0, 1), (3, 1, 3), (3, 0, 3)])
def test_minimos_por_az_y_replicas(azs, replicas, minimo):
    r = cotizador.cotizar({"gb_por_dia": 1, "azs": azs, "replicas": replicas, "retencion_dias": 7, "dias_hot": 7})
    n = r["cluster"]["hot"]["nodos"]
    assert n >= minimo and n % azs == 0


def test_tres_az():
    r = _fix(60, retencion_dias=7, dias_hot=7, azs=3)
    c = r["cluster"]
    assert c["hot"]["nodos"] % 3 == 0 and c["clientes"] in (0, 3, 6)
    assert "repartidos en 3 AZ" in r["resumen"]
    assert any("3 AZ" in a for a in r["avisos"])


def test_los_dias_hot_no_pasan_la_retencion():
    r = cotizador.cotizar({"gb_por_dia": 10, "retencion_dias": 7, "dias_hot": 30})
    assert r["entrada"]["fuentes"][0]["dias_hot"] == 7
    assert not any(c["concepto"] == "Índices congelados" for c in r["costos"])


def test_dias_con_log_como_en_el_documento():
    assert [cotizador.dias_con_log(d, 5) for d in (7, 30, 90, 365)] == [5, 22, 65, 261]
    assert cotizador.dias_con_log(30, 7) == 30


@pytest.mark.parametrize("datos, que", [
    ({"resto": "otro"}, "Pasados los días en hot"), ({"gb_por_dia": -1}, "Log por día"), ({"azs": 4}, "AZ"),
    ({"pico": "mucho"}, "Pico"), ({"enlace": "x"}, "Cómo llegan"), ({"fuentes": []}, "Fuentes"),
    ({"fuentes": [{"nombre": "FW", "gb_por_dia": 0}, {}]}, "FW · Log por día"),
    ({"nodos_hot": 2}, "al menos 4"), ({"azs": 3, "nodos_cold": 2}, "al menos 3"),
    ({"flavor_hot": "8u32g", "disco_hot_gb": 5000}, "admite hasta 3.200"), ({"flavor_hot": "99u"}, "Flavor hot"),
    ({"precios": {"flavors": {"99u": 1}}}, "no hay un flavor"), ({"precios": {"meses_pago_anual": 13}}, "Pago anual"),
])
def test_datos_invalidos(datos, que):
    with pytest.raises(cotizador.DatoInvalido, match=re.escape(que)):
        cotizador.cotizar(datos)


def test_si_lo_viejo_no_entra_en_32_cold_lo_dice():
    with pytest.raises(cotizador.DatoInvalido, match="32 nodos de datos cold"):
        cotizador.cotizar({"gb_por_dia": 5000, "retencion_dias": 365, "dias_hot": 7, "resto": "cold"})


def test_avisos():
    r = _fix(60, retencion_dias=30, dias_hot=7, enlace="publico", respaldo=True)
    assert any("acceso público llega a 200" in a for a in r["avisos"])
    assert any("antes de congelarlo" in a for a in r["avisos"])
    assert r["enlace"]["contratado_mbit"] == 200
    sin = cotizador.cotizar({"gb_por_dia": 10, "retencion_dias": 30, "dias_hot": 7, "respaldo": False, "replicas": 0})
    assert any("una sola copia" in a for a in sin["avisos"]) and any("Sin réplica" in a for a in sin["avisos"])


def test_la_vpn_cuesta_el_lado_del_cluster_y_la_respuesta_corta_baja_el_trafico():
    corta = _fix(30, retencion_dias=7, dias_hot=7, enlace="vpn", respuesta_corta=True)
    larga = _fix(30, retencion_dias=7, dias_hot=7, enlace="vpn", respuesta_corta=False)
    assert 100 < corta["enlace"]["mbit_pico"] < 115, "unos 108 Mbit/s con 30 sesiones (medido: 150 B por mensaje)"
    assert larga["enlace"]["vuelta_gb_mes"] > 8 * corta["enlace"]["vuelta_gb_mes"]
    assert corta["enlace"]["usd"] < larga["enlace"]["usd"]
    assert any("lado de LA-Santiago" in a for a in corta["avisos"])


def test_pago_anual_y_por_hora():
    r = _fix(30, retencion_dias=7, dias_hot=7, respaldo=False)
    a = r["anual"]
    assert round(a["anio_siguiente_pago_anual"]) == round(r["total_mes"] * 10)
    # Por hora, los nodos cuestan un 39 % más; el disco, lo mismo.
    assert round(a["por_hora_mes"]) == round(r["total_mes"] + 4 * cotizador.FLAVORS["16u64g"][2] * 0.39)


# ── Cualquier escenario ─────────────────────────────────────────────────────
def test_una_fuente_partida_en_dos_cuesta_lo_mismo():
    una = cotizador.cotizar({"gb_por_dia": 100, "retencion_dias": 30, "dias_hot": 7})
    dos = cotizador.cotizar({"fuentes": [{"nombre": "A", "gb_por_dia": 60, "retencion_dias": 30, "dias_hot": 7},
                                         {"nombre": "B", "gb_por_dia": 40, "retencion_dias": 30, "dias_hot": 7}]})
    assert round(una["total_mes"], 2) == round(dos["total_mes"], 2)
    assert una["cluster"]["hot"] == dos["cluster"]["hot"]


def test_varias_fuentes_con_su_retencion():
    r = cotizador.cotizar({"fuentes": [
        {"nombre": "Firewall", "gb_por_dia": 100, "retencion_dias": 90, "dias_hot": 7},
        {"nombre": "Auditoría", "gb_por_dia": 5, "retencion_dias": 365, "dias_hot": 30, "dias_por_semana": 5},
    ]})
    fw, au = r["fuentes"]
    assert (fw["nombre"], au["nombre"]) == ("Firewall", "Auditoría")
    assert r["volumen"]["gb_viejo"] == pytest.approx(100 * 1.33 * 83 + 5 * 1.33 * (261 - 22))
    n = r["cluster"]["hot"]["nodos"]
    assert fw["shards"] % n == 0 and au["shards"] % n == 0 and r["cluster"]["shards"] == fw["shards"] + au["shards"]
    assert r["supuestos"][0]["que"] == "Varias fuentes en un cluster"
    # Los picos se suman.
    assert r["volumen"]["mb_s_pico"] == pytest.approx(fw["mb_s_pico"] + au["mb_s_pico"])


def test_hardware_a_mano_que_no_alcanza():
    r = _fix(30, retencion_dias=7, dias_hot=7, flavor_hot="8u32g", nodos_hot=4, disco_hot_gb=500)
    hot = r["cluster"]["hot"]
    assert (hot["flavor"], hot["nodos"], hot["disco_gb"]) == ("8u32g", 4, 500)
    assert any("usaría cerca del" in a for a in r["avisos"]), "no escribe el pico"
    assert any("no entra" in a for a in r["avisos"]), "no entra en el disco"
    assert r["supuestos"][0]["que"] == "Hardware elegido a mano" and r["cluster"]["a_mano"]


def test_nodos_a_mano_con_flavor_automatico_elige_el_que_alcanza():
    r = _fix(30, retencion_dias=7, dias_hot=7, nodos_hot=4)
    assert r["cluster"]["hot"]["flavor"] == "16u64g", "con 4 de 8 vCPU no escribe el pico"
    assert not any("no entra" in a for a in r["avisos"])


def test_nodos_que_no_son_multiplo_de_las_az_avisan():
    r = cotizador.cotizar({"gb_por_dia": 10, "nodos_hot": 5})
    assert r["cluster"]["hot"]["nodos"] == 5 and any("múltiplo de 2" in a for a in r["avisos"])


def test_disco_high_y_masters_siempre():
    r = cotizador.cotizar({"gb_por_dia": 10, "disco_hot": "high", "masters": "siempre"})
    assert r["cluster"]["masters"] == 3
    assert any(c["detalle"].endswith("High I/O") for c in r["costos"] if c["concepto"] == "Disco de los nodos hot")
    assert any("Ultra-high I/O" in a for a in r["avisos"])


def test_cold_a_mano():
    r = _fix(30, retencion_dias=365, dias_hot=30, resto="cold", flavor_cold="16u128g", nodos_cold=8)
    cold = r["cluster"]["cold"]
    assert (cold["flavor"], cold["nodos"]) == ("16u128g", 8)
    assert all(m["nodos_cold"] == 8 for m in r["progresion"])


def test_precios_de_otra_region():
    base = _fix(30, retencion_dias=7, dias_hot=7, respaldo=False)
    otra = _fix(30, retencion_dias=7, dias_hot=7, respaldo=False,
                precios={"region": "Otra región", "disco_ultra_gb": 0.2, "flavors": {"16u64g": 600}})
    assert otra["precios"]["editados"] and otra["precios"]["region"] == "Otra región"
    esperado = base["total_mes"] + 4 * (600 - 507.68) + 4 * 1300 * (0.2 - 0.171)
    assert otra["total_mes"] == pytest.approx(esperado)
    assert any("a mano para Otra región" in s["fuente"] for s in otra["supuestos"])
    # Los de lista, mandados tal cual, no cuentan como editados.
    lista = cotizador.valores()
    igual = _fix(30, retencion_dias=7, dias_hot=7, respaldo=False,
                 precios={**lista["precios"], "region": lista["region"],
                          "flavors": {f["clave"]: f["usd_mes"] for f in lista["flavors"]}})
    assert not igual["precios"]["editados"] and igual["total_mes"] == pytest.approx(base["total_mes"])


# ── La API ──────────────────────────────────────────────────────────────────
def test_la_api():
    c = TestClient(main.app)
    r = c.post("/api/v1/cotizador", json={**FIX, "gb_por_dia": 240, "retencion_dias": 7, "dias_hot": 7, "respaldo": True})
    assert r.status_code == 200 and round(r.json()["total_mes"]) == 2953
    dos = c.post("/api/v1/cotizador", json={"fuentes": [{**FIX, "gb_por_dia": 120, "retencion_dias": 7, "dias_hot": 7}] * 2})
    assert dos.status_code == 200 and round(dos.json()["total_mes"]) == 2953
    mal = c.post("/api/v1/cotizador", json={"resto": "otro"})
    assert mal.status_code == 400 and "Pasados los días en hot" in mal.json()["detail"]["message"]
    v = c.get("/api/v1/cotizador/valores").json()
    assert v["defaults"] == cotizador.DEFAULTS and v["fuente"] == cotizador.FUENTE and v["region"] == "LA-Santiago"
    assert {f["clave"] for f in v["flavors"] if f["datos"]} == set(cotizador.DE_DATOS)
    assert [c["clave"] for c in v["precios_campos"]] == list(v["precios"])


# ── La vista ────────────────────────────────────────────────────────────────
def _vista(html: str) -> str:
    return html[html.index('id="view-cotizador"'):html.index("<!-- ═══ Vista: Panel de control")]


def test_la_vista_y_el_nav():
    html = _INDEX.read_text(encoding="utf-8")
    assert 'data-view="cotizador"' in html and 'id="view-cotizador"' in html
    assert "if (name === 'cotizador') initCotizador();" in html
    vista = _vista(html)
    for n in cotizador.DEFAULTS:
        assert f'name="{n}"' in vista, f"el formulario tiene {n}"
    for n in cotizador.FUENTE:
        assert f'data-f="{n}"' in vista, f"cada fuente tiene {n}"
    for sel in ("flavor_hot", "flavor_cold"):
        assert re.search(rf'name="{sel}" data-flavors', vista), "los flavors los arma el script"


def _funciones(html: str) -> str:
    i = html.index("    let _cotIniciado = false")
    return html[i:html.index("    async function copiarCotizacion() {", i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
const r = {UNA};
let html = cotizacionHTML(r), texto = textoDeLaCotizacion(r);
check('el total', html.includes('US$ 2.953'), html.slice(0, 400));
check('el cluster', html.includes('4 nodos hot de 16 vCPU'), html);
check('uso en el pico', html.includes('74 %'));
check('los doce meses', (html.match(/<td>Mes \d+<\/td>/g) || []).length === 12);
check('con respaldo, su columna', html.includes('En el respaldo'));
check('sin lo viejo, sin su columna', !html.includes('<th class="n">Congelados</th>'));
check('los avisos', html.includes('Para tener en cuenta'));
check('una fuente, sin tabla de fuentes', !html.includes('Las fuentes'));
check('precios de lista', html.includes('Precios de lista de LA-Santiago'));
check('el texto', texto.includes('Total: US$ 2.953 por mes.') && texto.includes('Cluster: 4 nodos hot') && texto.includes('Datos: '), texto);
const v = {VARIAS};
html = cotizacionHTML(v); texto = textoDeLaCotizacion(v);
check('varias: la tabla', html.includes('Las fuentes') && html.includes('Firewall') && html.includes('Aplicaciones &amp; web'), html);
check('varias: lo viejo', html.includes('<th class="n">Congelados</th>'));
check('varias: precios a mano', html.includes('Precios cargados a mano para Otra región'));
check('varias: hardware a mano', html.includes('Hardware elegido a mano'));
check('varias: el texto', texto.includes('Fuentes:\n- Firewall: ') && texto.includes('precios cargados a mano'), texto);
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(not shutil.which("node"), reason="hace falta node")
def test_la_vista_pinta_la_cotizacion(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    una = cotizador.cotizar({**FIX, "gb_por_dia": 240, "retencion_dias": 7, "dias_hot": 7, "respaldo": True})
    una["avisos"] = una["avisos"] or ["algo"]
    varias = cotizador.cotizar({"fuentes": [{"nombre": "Firewall", "gb_por_dia": 100, "retencion_dias": 90},
                                            {"nombre": "Aplicaciones & web", "gb_por_dia": 30}],
                                "nodos_hot": 6, "precios": {"region": "Otra región"}})
    script = tmp_path / "t.js"
    script.write_text(_ARNES.replace("{FUNCIONES}", _funciones(html)).replace("{UNA}", json.dumps(una))
                      .replace("{VARIAS}", json.dumps(varias)), encoding="utf-8")
    p = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
