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


def test_el_volumen_se_guarda_con_el_caso():
    import custom_cases
    assert custom_cases._volumen({"eventos_por_dia": "5000000", "alta_disponibilidad": False}) == \
        {"eventos_por_dia": 5_000_000, "alta_disponibilidad": False}
    assert custom_cases._volumen({"eventos_por_dia": 0}) == {} and custom_cases._volumen("x") == {}
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert '"volumen": request.get("volumen") or {},' in src
    html = _INDEX.read_text(encoding="utf-8")
    assert html.count("volumen: state.planVolumen || {}") == 2, "una fuente o varias"


def test_el_paso_2_ya_no_lo_muestra():
    """«Para producción» se fue del paso 2 (y su endpoint): el dimensionamiento
    queda para el traspaso y el runbook, con el volumen guardado del caso."""
    from fastapi.testclient import TestClient
    html = _INDEX.read_text(encoding="utf-8")
    assert "dimensionamientoHTML" not in html and "/api/v1/onboarding/dimensionar" not in html
    assert TestClient(main.app).post("/api/v1/onboarding/dimensionar", json={}).status_code in (404, 405)
