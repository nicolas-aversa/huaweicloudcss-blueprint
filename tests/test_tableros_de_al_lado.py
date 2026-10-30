"""Problemas y Tiempos al lado del dashboard de cada caso.

Los del Builder los traían; los curados (siem, fortianalyzer, billetera…) solo
tenían su dashboard hecho a mano: el NDJSON de disco va antes que el generador.
Ahora salen de sus campos, con el rol que el Builder les daría por el nombre, y
se enlazan con el hecho a mano.

Y al revés: cuando un SA guardó su propio principal (FIX OMS), los generados
quedaban sueltos ("Fix Oms · Problemas" al lado de "FIX OMS · Rechazos…").
"""
import json
from pathlib import Path

import pytest

import auth
import dashboards
import dashboards_guardados as dg
import main
import verticals

DISCO = Path(dashboards.__file__).parent / "docs" / "dashboards"


def _objetos(nd):
    return [json.loads(l) for l in nd.splitlines() if l.strip()]


def _con(slug):
    nd = (DISCO / f"{slug}.ndjson").read_text(encoding="utf-8")
    return nd, dashboards.con_tableros_de_al_lado(nd, slug, main._campos_de_un_caso_curado(slug),
                                                  verticals.get_vertical(slug)["label"])


def _campos_de_las_aggs(objetos):
    usados = set()
    for o in objetos:
        if o["type"] != "visualization":
            continue
        for a in json.loads(o["attributes"]["visState"]).get("aggs") or []:
            if (a.get("params") or {}).get("field"):
                usados.add(a["params"]["field"])
    return usados


def test_un_caso_curado_suma_problemas_y_tiempos_enlazados():
    nd, con = _con("streaming-ott")
    antes, objs = _objetos(nd), _objetos(con)
    tableros = [o for o in objs if o["type"] == "dashboard"]
    principal = next(o for o in antes if o["type"] == "dashboard")
    assert tableros[0]["id"] == principal["id"] == dashboards.id_del_dashboard(con), "el link de la plataforma no cambia"
    assert [t["attributes"]["title"] for t in tableros[1:]] == [
        "[streaming-ott] Streaming OTT · Problemas", "[streaming-ott] Streaming OTT · Tiempos"]
    # Arriba del hecho a mano, la fila para ir a los otros; lo demás, más abajo.
    paneles = json.loads(tableros[0]["attributes"]["panelsJSON"])
    viejos = json.loads(principal["attributes"]["panelsJSON"])
    nav = paneles[0]
    assert nav["gridData"]["y"] == 0 and nav["gridData"]["w"] == 48
    assert [p["gridData"]["y"] for p in paneles[1:]] == [p["gridData"]["y"] + nav["gridData"]["h"] for p in viejos]
    ref = next(r for r in tableros[0]["references"] if r["name"] == nav["panelRefName"])
    md = json.loads(next(o for o in objs if o["id"] == ref["id"])["attributes"]["visState"])["params"]["markdown"]
    assert all(f"#/view/{t['id']}" in md for t in tableros[1:])
    # Y desde cada uno, "Resumen" lleva al hecho a mano.
    for t in tableros[1:]:
        encabezados = [o for o in objs if o["id"] in {r["id"] for r in t["references"]} and o["type"] == "visualization"
                       and json.loads(o["attributes"]["visState"])["type"] == "markdown"]
        assert any(f"[Resumen](#/view/{principal['id']})" in json.loads(e["attributes"]["visState"])["params"]["markdown"]
                   for e in encabezados)
    # Todo lo que se referencia está, y un solo index pattern con los campos que usan.
    ids = {o["id"] for o in objs}
    assert all(r["id"] in ids for o in objs for r in o.get("references") or [])
    patrones = [o for o in objs if o["type"] == "index-pattern"]
    assert len(patrones) == 1
    nombres = {f["name"] for f in json.loads(patrones[0]["attributes"]["fields"])}
    assert _campos_de_las_aggs(objs) <= nombres, _campos_de_las_aggs(objs) - nombres
    # Lo hecho a mano sigue entero.
    assert {o["id"] for o in antes} <= ids
    assert dashboards.con_tableros_de_al_lado(con, "streaming-ott", main._campos_de_un_caso_curado("streaming-ott"),
                                              "Streaming OTT") == con, "no se duplica"


@pytest.mark.parametrize("slug,esperados", [
    ("siem", ["Problemas"]), ("fortianalyzer", ["Problemas", "Tiempos"]),
    ("transacciones-billetera", ["Problemas"]), ("streaming-ott", ["Problemas", "Tiempos"]),
    ("cts", []), ("transacciones-alyc", []),
])
def test_cada_curado_tiene_lo_que_sus_campos_justifican(slug, esperados):
    """Sin un campo de error, un nivel o una duración no hay qué mostrar."""
    nd, con = _con(slug)
    titulos = [o["attributes"]["title"].rsplit(" · ", 1)[-1] for o in _objetos(con) if o["type"] == "dashboard"][1:]
    assert titulos == esperados
    if not esperados:
        assert con == nd
        return
    # Un solo index pattern, con los campos que usan (fortianalyzer no traía
    # `duration` ni `latency` en el suyo: la viz no encontraba el campo).
    objs = _objetos(con)
    patrones = [o for o in objs if o["type"] == "index-pattern"]
    assert len(patrones) == 1
    nombres = {f["name"] for f in json.loads(patrones[0]["attributes"]["fields"])}
    assert _campos_de_las_aggs(objs) <= nombres, _campos_de_las_aggs(objs) - nombres


def test_los_campos_curados_traen_rol_y_fecha():
    campos = main._campos_de_un_caso_curado("transacciones-billetera")
    assert campos[0] == {"field_path": "@timestamp", "type": "date", "role": "timestamp", "business_label": "Fecha"}
    rol = {c["field_path"]: c["role"] for c in campos}
    assert rol["transaction.funnel.failed"] == "critical_indicator"
    assert rol["transaction.response_code"] == "success_indicator"


def test_la_plataforma_los_carga_con_el_hecho_a_mano(monkeypatch, tmp_path):
    escrito = []
    main._write_pipelines_registry(tmp_path, {"transacciones-billetera": {"index": "transacciones-billetera-%{+YYYY.MM}"}})
    monkeypatch.setattr(main, "_import_dashboards_via_opensearch", lambda nd, *a, **k: escrito.append(nd) or True)
    assert main._import_dashboards("transacciones-billetera", {"public_endpoint": "1.2.3.4:9200"}, "pw", True, tmp_path)
    titulos = [o["attributes"]["title"] for o in _objetos(escrito[0]) if o["type"] == "dashboard"]
    assert "[transacciones-billetera] Transacciones billetera · Problemas" in titulos
    reg = main._read_pipelines_registry(tmp_path)["transacciones-billetera"]
    assert reg["dashboard_id"] == dashboards.id_del_dashboard(dashboards.build_ndjson("transacciones-billetera"))


# ── Un principal guardado por un SA ─────────────────────────────────────────
OMS = [{"field_path": "@timestamp", "type": "date", "role": "timestamp"},
       {"field_path": "fix.rechazo_motivo", "type": "text", "role": "critical_indicator", "business_label": "Motivo"},
       {"field_path": "fix.comp_id", "type": "keyword", "role": "entity_id"},
       {"field_path": "latencia_ms", "type": "float", "role": "measure", "unit": "ms", "business_label": "Latencia (ms)"},
       {"field_path": "mercado", "type": "keyword", "role": "primary_dimension"}]


def _generado():
    return dashboards.build_ndjson_from_fields("fix-oms", "fix-oms-%{+YYYY.MM}", OMS, "FIX OMS")


def test_sin_los_de_al_lado_queda_el_principal_y_lo_suyo():
    nd = _generado()
    principal = dashboards.id_del_dashboard(nd)
    # Con la tabla del perfil el principal pasa al final: no es "el primero".
    con_perfil = dashboards.con_panel_de_perfil(nd, "fix-oms", {"medidas": ["latencia_ms"], "campo": "fix.comp_id"})
    solo = _objetos(dashboards.sin_tableros_de_al_lado(con_perfil, principal))
    assert [o["id"] for o in solo if o["type"] == "dashboard"] == [principal]
    ids = {o["id"] for o in solo}
    assert all(r["id"] in ids for o in solo for r in o.get("references") or []), "no falta nada de lo que usa"
    de_al_lado = {r["id"] for o in _objetos(nd) if o["type"] == "dashboard" and o["id"] != principal
                  for r in o["references"]}
    del_principal = {r["id"] for o in _objetos(nd) if o["id"] == principal for r in o["references"]}
    assert not (de_al_lado - del_principal) & ids, "lo que usaban solo ellos, tampoco"
    assert dashboards.sin_tableros_de_al_lado(con_perfil, "otro") == con_perfil, "sin ese principal, igual"


def test_un_principal_guardado_que_no_lleva_a_los_de_al_lado_los_saca(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    nd = _generado()
    principal = dashboards.id_del_dashboard(nd)
    assert not dg.reemplaza_al_principal("fix-oms", nd, principal), "sin nada guardado"
    # El que armó un SA, con su propio encabezado (a sus propios dashboards).
    propio = {"type": "dashboard", "id": principal, "attributes": {"title": "[fix-oms] FIX OMS · Resumen"},
              "references": [{"type": "visualization", "id": "su-encabezado", "name": "panel_0"}]}
    suyo = {"type": "visualization", "id": "su-encabezado",
            "attributes": {"visState": json.dumps({"type": "markdown", "params": {"markdown": "[Rechazos](#/view/r)"}})},
            "references": []}
    dg.guardar("fix-oms", [propio, suyo], "ana@x.com")
    assert dg.reemplaza_al_principal("fix-oms", nd, principal)
    # El generado, editado: conserva el encabezado con los links.
    editado = next(o for o in _objetos(nd) if o["id"] == principal)
    dg.guardar("fix-oms", [{**editado, "attributes": {**editado["attributes"], "title": "otro título"}}], "ana@x.com")
    assert not dg.reemplaza_al_principal("fix-oms", nd, principal)


def test_al_importar_no_van_los_que_quedaron_sueltos(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    td = tmp_path / "tf"
    td.mkdir()
    main._write_pipelines_registry(td, {"fix-oms": {"index": "fix-oms-%{+YYYY.MM}", "label": "FIX OMS"}})
    escrito = []
    monkeypatch.setattr(main, "_import_dashboards_via_opensearch", lambda nd, *a, **k: escrito.append(nd) or True)
    main._import_dashboards("fix-oms", {"public_endpoint": "x"}, "pw", True, td, OMS, "fix-oms-%{+YYYY.MM}")
    assert len([o for o in _objetos(escrito[-1]) if o["type"] == "dashboard"]) == 3
    principal = dashboards.id_del_dashboard(_generado())
    dg.guardar("fix-oms", [{"type": "dashboard", "id": principal, "attributes": {"title": "mío"}, "references": []}],
               "ana@x.com")
    main._import_dashboards("fix-oms", {"public_endpoint": "x"}, "pw", True, td, OMS, "fix-oms-%{+YYYY.MM}")
    assert [o["id"] for o in _objetos(escrito[-1]) if o["type"] == "dashboard"] == [principal]
