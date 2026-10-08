"""El documento de traspaso: lo que el cliente se lleva además del cluster.
Qué se configuró y por qué, cómo quedó, cuánto cluster hace falta en
producción y cómo se opera y se replica sin la plataforma."""
import json
import re
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import dimensionamiento as dm
import main
import plan_de_cluster
import plugins_vista as pv
import traspaso

HOTEL = [{"field_path": "@timestamp", "type": "date", "role": "timestamp"},
         {"field_path": "estado", "type": "keyword", "role": "primary_dimension", "business_label": "Estado"},
         {"field_path": "huesped", "type": "keyword", "role": "entity_id", "entity": True, "sensitive": True,
          "business_label": "Huésped"},
         {"field_path": "tarifa", "type": "double", "role": "measure", "business_label": "Tarifa"}]
AHORA = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _plan(**kw):
    return plan_de_cluster.plan("hotel", HOTEL, "Hotel", **kw)


def test_el_plan_con_sus_motivos_y_lo_apagado():
    md = traspaso.documento("hotel", "Hotel", _plan(retencion_dias=30), excluir=["perfil"], ahora=AHORA)
    assert md.startswith("# Traspaso: Hotel\n") and "2026-10-07 12:00 UTC" in md
    assert "| Pronósticos (Forecasting) | sí |" in md
    assert re.search(r"\| Perfil por entidad \(Transform\) \| apagado en el paso 2 \|", md)
    assert "| Ciclo de vida de los índices (ISM) | sí | solo lectura a los 35 días y borrado pasada la retención de 30 días |" in md
    assert "Y para todo el cluster:" in md, "lo del cluster, aparte"
    assert "Todavía no se desplegó" in md
    assert "Sin el volumen diario del cliente no se dimensionó" in md
    # Cómo se opera: solo lo que se configura (lo apagado no).
    assert "- **Ciclo de vida de los índices (ISM)**: Dashboards → Index Management → State management policies." in md
    assert "Transform jobs" not in md


def test_con_el_cluster_y_el_dimensionamiento():
    tarjetas = [pv._tarjeta("forecasting", "Forecasting", "Pronostica 1 medida.", pv.PARCIAL, "fallaron: A",
                            filas=[{"texto": "tarifa", "estado": pv.PARCIAL, "detalle": "backtest parcial: 3 de 600 pasos",
                                    "numero": "", "url": "https://d/app/forecasting#/forecasters/F1"}],
                            links=[{"texto": "Ver los forecasters", "url": "https://d/app/forecasting#/forecasters"}])]
    dim = dm.dimensionar(400, 5_000_000, 90, True)
    md = traspaso.documento("hotel", "Hotel", _plan(), tarjetas=tarjetas, dimensionamiento=dim,
                            dashboards_url="https://d", ahora=AHORA)
    assert "Dashboards: https://d" in md
    assert "- **Forecasting**: parcial. Pronostica 1 medida. Motivo: fallaron: A." in md
    assert "  - tarifa: backtest parcial: 3 de 600 pasos (https://d/app/forecasting#/forecasters/F1)" in md
    assert "Con **5.000.000 eventos por día** (400 bytes cada uno, de la muestra), **90 días** de retención y alta disponibilidad:" in md
    assert f"- **{dim['nodos']} nodos {dim['flavor']}**" in md
    assert "Es una estimación. Supuestos:" in md
    assert "- ~2,6 GB por día; 624 GB de disco en total." in md, "números como se escriben acá"
    assert traspaso._num(1234567) == "1.234.567" and traspaso._num(2.6) == "2,6" and traspaso._num(624.0) == "624"


def test_el_endpoint(monkeypatch, tmp_path):
    import custom_cases
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    caso = {"label": "Hotel", "fields": HOTEL, "index_base": "hotel", "sample": "x" * 399 + "\n" + "y" * 401,
            "volumen": {"eventos_por_dia": 1_000_000, "alta_disponibilidad": True}, "retencion_dias": 30}
    monkeypatch.setattr(custom_cases, "get_case", lambda slug: caso if slug == "hotel" else None)
    r = TestClient(main.app).get("/api/v1/cases/hotel/traspaso")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert 'filename="hotel-traspaso.md"' in r.headers["content-disposition"]
    assert "(400 bytes cada uno, de la muestra), **30 días** de retención" in r.text
    assert not re.search(r"HPUA|vJfk|VTd4|Huawei1234|k72q30", r.text)
    assert TestClient(main.app).get("/api/v1/cases/no-existe/traspaso").status_code == 404


def test_desplegado_lleva_como_quedo(monkeypatch, tmp_path):
    import custom_cases
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(custom_cases, "get_case", lambda slug: {"label": "Hotel", "fields": HOTEL} if slug == "hotel" else None)
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"id": "C", "public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_build_dashboards_url", lambda cluster, https=True: "https://consola/x/app/login")
    main._write_pipelines_registry(tmp_path, {"hotel": {"fields": HOTEL, "dashboards_imported": True, "dashboard_id": "D"}})
    r = TestClient(main.app).get("/api/v1/cases/hotel/traspaso")
    assert "Dashboards: https://consola/x" in r.text
    assert "- **Dashboard del caso**: listo." in r.text and "https://consola/x/app/dashboards#/view/D" in r.text


def test_se_baja_desde_la_plataforma():
    import pathlib
    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert '<a href="/api/v1/cases/${encodeURIComponent(p.slug)}/traspaso" download>${icon(\'download\')} Traspaso</a>' in html
