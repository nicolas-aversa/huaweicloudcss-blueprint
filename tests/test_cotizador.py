"""El cotizador: el modelo validado en un cluster real (un OMS FIX de 30 sesiones
en LA-Santiago) reproduce las cifras del documento de dimensionamiento, y la
vista pinta lo que devuelve la API. Se prueba en node con el código real."""
import json
import pathlib
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
    assert "74 %" in r["supuestos"][0]["fuente"]


@pytest.mark.parametrize("azs, replicas, minimo, par", [(2, 1, 4, True), (1, 1, 3, False), (2, 0, 1, True), (1, 0, 1, False)])
def test_minimos_por_az_y_replicas(azs, replicas, minimo, par):
    r = cotizador.cotizar({"gb_por_dia": 1, "azs": azs, "replicas": replicas, "retencion_dias": 7, "dias_hot": 7})
    n = r["cluster"]["hot"]["nodos"]
    assert n >= minimo and (n % 2 == 0 or not par)


def test_los_dias_hot_no_pasan_la_retencion():
    r = cotizador.cotizar({"gb_por_dia": 10, "retencion_dias": 7, "dias_hot": 30})
    assert r["entrada"]["dias_hot"] == 7 and not any(c["concepto"] == "Índices congelados" for c in r["costos"])


def test_dias_con_log_como_en_el_documento():
    assert [cotizador.dias_con_log(d, 5) for d in (7, 30, 90, 365)] == [5, 22, 65, 261]
    assert cotizador.dias_con_log(30, 7) == 30


@pytest.mark.parametrize("datos, que", [({"resto": "otro"}, "resto"), ({"gb_por_dia": -1}, "gb_por_dia"),
                                        ({"azs": 3}, "azs"), ({"pico": "mucho"}, "pico"), ({"enlace": "x"}, "enlace")])
def test_datos_invalidos(datos, que):
    with pytest.raises(cotizador.DatoInvalido, match=que):
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


# ── La API ──────────────────────────────────────────────────────────────────
def test_la_api():
    c = TestClient(main.app)
    r = c.post("/api/v1/cotizador", json={**FIX, "gb_por_dia": 240, "retencion_dias": 7, "dias_hot": 7, "respaldo": True})
    assert r.status_code == 200 and round(r.json()["total_mes"]) == 2953
    mal = c.post("/api/v1/cotizador", json={"resto": "otro"})
    assert mal.status_code == 400 and "resto" in mal.json()["detail"]["message"]
    v = c.get("/api/v1/cotizador/valores").json()
    assert v["defaults"]["gb_por_dia"] == cotizador.DEFAULTS["gb_por_dia"] and v["region"] == "LA-Santiago"


# ── La vista ────────────────────────────────────────────────────────────────
def test_la_vista_y_el_nav():
    html = _INDEX.read_text(encoding="utf-8")
    assert 'data-view="cotizador"' in html and 'id="view-cotizador"' in html
    assert "if (name === 'cotizador') initCotizador();" in html
    nombres = {"gb_por_dia", "bytes_por_evento", "dias_por_semana", "horas_activas", "pico", "retencion_dias", "dias_hot",
               "resto", "respaldo", "replicas", "azs", "enlace", "respuesta_corta", "crecimiento", "mb_s_por_vcpu"}
    vista = html[html.index('id="view-cotizador"'):html.index("<!-- ═══ Vista: Panel de control")]
    assert nombres == set(cotizador.DEFAULTS), "el formulario cubre todo lo que acepta el cotizador"
    for n in nombres:
        assert f'name="{n}"' in vista, n


def _funciones(html: str) -> str:
    i = html.index("    let _cotIniciado = false")
    return html[i:html.index("    async function copiarCotizacion() {", i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
""" + "{FUNCIONES}" + r"""
const r = {RESPUESTA};
const html = cotizacionHTML(r);
const texto = textoDeLaCotizacion(r);
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
check('el total', html.includes('US$ 2.953'), html.slice(0, 400));
check('el cluster', html.includes('4 nodos hot de 16 vCPU'), html);
check('uso en el pico', html.includes('74 %'));
check('los doce meses', (html.match(/<td>Mes \d+<\/td>/g) || []).length === 12);
check('con respaldo, su columna', html.includes('En el respaldo'));
check('los avisos', html.includes('Para tener en cuenta'));
check('el texto', texto.includes('Total: US$ 2.953 por mes.') && texto.includes('Cluster: 4 nodos hot'), texto);
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(not shutil.which("node"), reason="hace falta node")
def test_la_vista_pinta_la_cotizacion(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    r = cotizador.cotizar({**FIX, "gb_por_dia": 240, "retencion_dias": 7, "dias_hot": 7, "respaldo": True})
    r["avisos"] = r["avisos"] or ["algo"]
    script = tmp_path / "t.js"
    script.write_text(_ARNES.replace("{FUNCIONES}", _funciones(html)).replace("{RESPUESTA}", json.dumps(r)), encoding="utf-8")
    p = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
