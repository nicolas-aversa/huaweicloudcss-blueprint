"""El dashboard automático de un dataset nuevo: un Resumen y, si los campos lo
justifican, Problemas y Tiempos, enlazados entre sí.

Era un tablero de rankings ("Top X" de cada columna). Sigue el modelo de los
que se armaron a mano para un OMS (qué pasa en el tiempo, qué falla y a quién,
cuánto tarda), pero sale de los roles de los campos: sirve para cualquier
dataset.
"""
import json
import re

import pytest

import dashboards
import perfiles


def _campo(path, tipo, **kw):
    return {"field_path": path, "type": tipo, "business_label": kw.pop("label", path.rsplit(".", 1)[-1]),
            "dimension": tipo in ("keyword", "text", "boolean"), **kw}


OMS = [_campo("@timestamp", "date", role="timestamp", dimension=False),
       _campo("fix.msg_type", "keyword", role="primary_dimension", label="Tipo de mensaje"),
       _campo("fix.rechazo_motivo", "text", role="critical_indicator", label="Motivo del rechazo", dimension=False),
       _campo("fix.comp_id_propio", "keyword", entity=True, role="entity_id", label="Participante"),
       _campo("fix.monto_ejecutado", "float", role="measure", label="Monto ejecutado", dimension=False),
       _campo("fix.latencia_ms", "long", role="measure", label="Latencia (ms)", dimension=False),
       _campo("log.level", "keyword", label="Nivel", dimension=False),
       _campo("log.logger", "keyword", label="Logger", dimension=False),
       _campo("fix.contraparte", "keyword", label="Mercado"),
       _campo("fix.evento", "text", label="Evento", dimension=False)]
VENTAS = [_campo("fecha", "date", role="timestamp", dimension=False),
          _campo("sucursal", "keyword", role="primary_dimension"),
          _campo("medio_pago", "keyword"),
          _campo("cliente", "keyword", entity=True),
          _campo("importe", "float", role="measure", dimension=False)]
FRAUDE = [_campo("ts", "keyword", role="timestamp", dimension=False),
          _campo("canal", "keyword", role="primary_dimension"),
          _campo("is_fraud", "integer", role="critical_indicator", dimension=False),
          _campo("monto", "float", role="measure", dimension=False),
          _campo("exitosa", "boolean"),
          _campo("geo", "geo_point", dimension=False)]
SERVICIO = [_campo("@timestamp", "date", dimension=False),
            _campo("severity", "keyword"),
            _campo("service", "keyword", dimension=False),
            _campo("host", "keyword", role="primary_dimension"),
            _campo("tiempo_respuesta_ms", "long", dimension=False),
            _campo("tiempo_cola_seg", "float", dimension=False)]
SIN_FECHA = [_campo("estado", "keyword"), _campo("monto", "float", role="measure", dimension=False)]
DATASETS = {"oms": OMS, "ventas": VENTAS, "fraude": FRAUDE, "servicio": SERVICIO, "sin-fecha": SIN_FECHA}


def _tableros(fields, slug="mi-log"):
    return dashboards._tableros_desde_campos(slug, f"{slug}-%{{+YYYY.MM}}", fields)


def _de(t, tipo):
    return [p for p in t["panels"] if p["type"] == tipo]


# ── Qué dashboards hay ──────────────────────────────────────────────────────
def test_cada_dashboard_aparece_si_los_campos_lo_justifican():
    assert [t["titulo"] for t in _tableros(OMS, "oms")] == ["Oms · Resumen", "Oms · Problemas", "Oms · Tiempos"]
    assert [t["clave"] for t in _tableros(VENTAS)] == ["resumen"], "sin crítico, nivel ni duraciones"
    assert [t["clave"] for t in _tableros(FRAUDE)] == ["resumen", "problemas"]
    assert [t["clave"] for t in _tableros(SERVICIO)] == ["resumen", "problemas", "tiempos"], \
        "el nivel del log alcanza para Problemas"


def test_el_resumen_conserva_el_id_de_antes_y_va_primero():
    """Un dashboard guardado reemplaza al de la plataforma por su id (el caso
    FIX tiene el suyo): el Resumen sigue siendo ese dashboard, y es el que abre
    el link de la plataforma."""
    viejo = dashboards._stable_id("oms", "dashboard", "Oms")
    nd = dashboards.build_ndjson_from_fields("oms", "oms-%{+YYYY.MM}", OMS)
    assert dashboards.id_del_dashboard(nd) == viejo
    tableros = [json.loads(x) for x in nd.splitlines() if json.loads(x)["type"] == "dashboard"]
    assert [t["attributes"]["title"] for t in tableros] == ["[oms] Oms · Resumen", "[oms] Oms · Problemas",
                                                            "[oms] Oms · Tiempos"]
    assert len({t["id"] for t in tableros}) == 3


def test_los_dashboards_se_enlazan_entre_si():
    tableros = _tableros(OMS, "oms")
    ids = {t["clave"]: t["id"] for t in tableros}
    for t in tableros:
        md = _de(t, "markdown")[0]["md"]
        for clave, did in ids.items():
            assert (f"(#/view/{did})" in md) == (clave != t["clave"]), (t["clave"], clave, md)
    assert "(#/view/" not in _de(_tableros(VENTAS)[0], "markdown")[0]["md"], "solo, sin navegación"


def test_los_de_al_lado_abren_con_el_rango_del_momento():
    """El principal lo ajusta la plataforma a los datos; los otros, sin rango
    propio, toman el que se trae del principal (o el default, que la plataforma
    deja en el de los datos). Con uno fijo quedaban en 2025-2026."""
    nd = dashboards.build_ndjson_from_fields("oms", "oms-%{+YYYY.MM}", OMS)
    tableros = [json.loads(x) for x in nd.splitlines() if json.loads(x)["type"] == "dashboard"]
    assert tableros[0]["attributes"]["timeRestore"] is True
    assert all(t["attributes"]["timeRestore"] is False and "timeFrom" not in t["attributes"] for t in tableros[1:])


# ── El Resumen ──────────────────────────────────────────────────────────────
def test_el_resumen_tiene_las_cifras_la_serie_las_tablas_y_los_filtros():
    resumen = _tableros(OMS, "oms")[0]
    assert [p["title"] for p in _de(resumen, "metric")] == [
        "Eventos", "Con Motivo del rechazo", "Errores y advertencias", "Participante (distintos)",
        "Monto ejecutado total", "Latencia (ms) promedio"]
    assert [c["field"] for c in _de(resumen, "controls")[0]["controls"]] == \
        ["fix.comp_id_propio", "fix.msg_type", "fix.contraparte"]
    assert [p["split"] for p in _de(resumen, "area")] == ["fix.msg_type"]
    tablas = {p["field"]: p for p in _de(resumen, "table")}
    assert set(tablas) == {"fix.msg_type", "fix.comp_id_propio"}
    assert tablas["fix.comp_id_propio"]["metricas"] == [
        ("count", None, "Eventos"), ("sum", "fix.monto_ejecutado", "Monto ejecutado"),
        ("avg", "fix.latencia_ms", "Latencia (ms) (promedio)")], "el monto se suma; el tiempo, no"
    assert [p for p in _de(resumen, "bar") if p.get("agg_field") == "fix.monto_ejecutado"], "dónde se concentra"


def test_la_fecha_que_ya_se_llama_timestamp_alcanza_para_las_series():
    """Se salteaba el campo `@timestamp` antes de mirar que era la fecha: un
    dataset sin otra fecha se quedaba sin series."""
    campos = [_campo("@timestamp", "date", dimension=False), _campo("estado", "keyword")]
    assert _de(_tableros(campos)[0], "area")
    assert not _de(_tableros([_campo("estado", "keyword")])[0], "area")


EMERGENCIAS = [_campo("@timestamp", "date", role="timestamp", dimension=False),
               _campo("afiliado_id", "keyword", entity=True, role="entity_id", label="Afiliado", dimension=False,
                      frecuentes=[]),
               _campo("sexo_paciente", "keyword", role="entity_id", label="Sexo paciente", frecuentes=["F", "M", "X"]),
               _campo("tipo_servicio", "keyword", role="primary_dimension", label="Tipo servicio",
                      frecuentes=["Urgencia", "Emergencia"]),
               _campo("canal", "keyword", label="Canal", frecuentes=["Teléfono", "App"]),
               _campo("tiempo_respuesta_min", "long", role="measure", label="Tiempo respuesta", dimension=False)]


def test_la_entidad_es_la_marcada_aunque_tenga_miles_de_valores():
    """El perfilador guarda valores frecuentes solo de las dimensiones: un
    afiliado, con miles, no los trae y no por eso es un id por fila. Es la
    entidad del perfil; el dashboard tomaba el sexo del paciente (tres valores,
    también con rol de entidad)."""
    clases = dashboards._clasificar(EMERGENCIAS)
    assert clases["entidad"]["path"] == "afiliado_id" == perfiles.entidad_propuesta(EMERGENCIAS)
    # La dimensión principal del análisis sigue siéndolo aunque el nombre
    # termine como el servicio de un log, y no se repite como componente.
    assert clases["principal"]["path"] == "tipo_servicio" and clases["componente"] is None
    tablas = [p["title"] for p in _de(_tableros(EMERGENCIAS)[0], "table")]
    assert tablas == ["Por Tipo servicio", "Por Afiliado"], tablas


# ── Problemas ───────────────────────────────────────────────────────────────
def test_problemas_dice_que_fallo_cuando_donde_y_a_quien():
    problemas = _tableros(OMS, "oms")[1]
    q = "fix.rechazo_motivo:*"
    motivos = [p for p in _de(problemas, "bar") if p["field"] == "fix.rechazo_motivo.keyword"]
    assert motivos and motivos[0]["query"] == q, "un texto se agrupa por su keyword"
    calor = _de(problemas, "heatmap")
    assert calor and calor[0]["field"] == "fix.comp_id_propio" and calor[0]["query"] == q
    tabla = _de(problemas, "table")[0]
    assert tabla["field"] == "fix.comp_id_propio" and tabla["sub_field"] == "fix.rechazo_motivo.keyword"
    errores = [p for p in _de(problemas, "histogram") if p["title"].startswith("Errores")]
    assert errores and errores[0]["split"] == "log.logger", "por el componente que los escribió"
    assert 'not log.level:("INFO"' in errores[0]["query"]


@pytest.mark.parametrize("fields", [OMS, VENTAS, FRAUDE, SERVICIO, EMERGENCIAS], ids=lambda f: f[1]["field_path"])
def test_la_partida_va_antes_que_la_fecha(fields):
    """Anidada en la fecha, cada intervalo traía sus propios N valores: el
    mapa de calor de una entidad con miles juntaba cientos de filas ("There are
    too many series defined (526). The configured maximum is 50") y las
    leyendas de las series, lo mismo."""
    vistos = 0
    for tablero in _tableros(fields):
        for panel in tablero["panels"]:
            if panel["type"] in ("markdown", "search"):
                continue
            aggs = dashboards._panel_to_vis_state(panel)["aggs"]
            tipos = [a["type"] for a in aggs]
            if "date_histogram" in tipos and any(a["schema"] == "group" for a in aggs):
                vistos += 1
                partida = next(i for i, a in enumerate(aggs) if a["schema"] == "group")
                assert partida < tipos.index("date_histogram"), (panel["title"], tipos)
    assert vistos, "el dataset tiene al menos una serie partida"


def test_los_ndjson_de_disco_tambien():
    """Salen de `python -m dashboards`: si no se regeneran, quedan con el orden viejo."""
    from pathlib import Path
    vistos = 0
    for f in sorted((Path(dashboards.__file__).parent / "docs" / "dashboards").glob("*.ndjson")):
        for linea in f.read_text(encoding="utf-8").splitlines():
            obj = json.loads(linea) if linea.strip() else {}
            if obj.get("type") != "visualization":
                continue
            aggs = json.loads(obj["attributes"]["visState"]).get("aggs") or []
            tipos = [a["type"] for a in aggs]
            if "date_histogram" in tipos and any(a["schema"] == "group" for a in aggs):
                vistos += 1
                partida = next(i for i, a in enumerate(aggs) if a["schema"] == "group")
                assert partida < tipos.index("date_histogram"), (f.name, obj["attributes"]["title"])
    assert vistos >= 19


def test_un_critico_numerico_cuenta_los_que_valen_uno():
    problemas = _tableros(FRAUDE)[1]
    assert {p["query"] for p in _de(problemas, "metric")} == {"is_fraud > 0"}
    assert not [p for p in _de(problemas, "bar") if p["field"] == "is_fraud"], "un 0/1 no tiene motivos"


def test_un_nivel_sin_valores_conocidos_cuenta_lo_que_no_es_informativo():
    """Sin los valores del índice: lo que no es INFO/DEBUG/TRACE/NOTICE (una
    severidad propia, como "Alta", también es un problema)."""
    q = dashboards._consulta_de_problemas_del_nivel({"path": "severity"})
    assert q.startswith("severity:* and not severity:(")
    assert all(f'"{v}"' in q for v in ("INFO", "info", "DEBUG", "TRACE", "NOTICE"))
    assert '"ERROR"' not in q and '"WARN"' not in q


# ── Tiempos ─────────────────────────────────────────────────────────────────
def test_tiempos_con_percentiles_y_quien_tiene_los_mas_lentos():
    tiempos = _tableros(SERVICIO, "srv")[2]
    kpis = _de(tiempos, "metric")
    assert [k["field"] for k in kpis] == ["tiempo_respuesta_ms", "tiempo_cola_seg"]
    assert all(k["agg"] == "percentiles" and k["percents"] == [50, 95, 99] for k in kpis)
    lineas = _de(tiempos, "line")
    assert any(p.get("split") == "host" and p["percents"] == [95] for p in lineas), "el p95 por dimensión"
    tabla = _de(tiempos, "table")[0]
    assert tabla["metricas"][0] == ("max", "tiempo_respuesta_ms", "tiempo_respuesta_ms máximo"), \
        "ordenada por el máximo"
    lista = _de(tiempos, "search")[0]
    assert lista["sort"] == [["tiempo_respuesta_ms", "desc"]]


def test_un_monto_no_es_un_tiempo():
    clases = dashboards._clasificar(OMS)
    assert [c["path"] for c in clases["montos"]] == ["fix.monto_ejecutado"]
    assert [c["path"] for c in clases["duraciones"]] == ["fix.latencia_ms"]
    con_unidad = dashboards._clasificar([_campo("espera", "float", unit="min", dimension=False)])
    assert [c["path"] for c in con_unidad["duraciones"]] == ["espera"]


# ── Todo lo que se dibuja existe ───────────────────────────────────────────
_CAMPO_KQL = re.compile(r"(?<![\w.\"])([@a-z][\w.@]*)\s*(?::|>|<)")


@pytest.mark.parametrize("nombre", list(DATASETS))
def test_todo_campo_que_usa_un_panel_esta_en_el_index_pattern(nombre):
    nd = dashboards.build_ndjson_from_fields(nombre, f"{nombre}-%{{+YYYY.MM}}", DATASETS[nombre])
    objetos = [json.loads(x) for x in nd.splitlines()]
    patron = objetos[0]
    assert patron["type"] == "index-pattern"
    campos = {c["name"]: c for c in json.loads(patron["attributes"]["fields"])}
    ids = {(o["type"], o["id"]) for o in objetos}
    for o in objetos[1:]:
        a = o["attributes"]
        if o["type"] == "visualization":
            vs = json.loads(a["visState"])
            for agg in vs["aggs"]:
                prm = agg.get("params") or {}
                if prm.get("field"):
                    assert prm["field"] in campos and campos[prm["field"]]["aggregatable"], (a["title"], prm["field"])
                for f in prm.get("filters") or []:
                    assert all(c in campos for c in _CAMPO_KQL.findall(f["input"]["query"])), f
                if agg["type"] == "terms":
                    assert prm["orderBy"] in [x["id"] for x in vs["aggs"]], (a["title"], prm["orderBy"])
            for c in (vs.get("params") or {}).get("controls") or []:
                assert c["fieldName"] in campos, c
        if o["type"] == "search":
            assert all(c in campos for c in a["columns"]) and all(s[0] in campos for s in a["sort"]), a
        if o["type"] in ("visualization", "search"):
            ssj = json.loads(a["kibanaSavedObjectMeta"]["searchSourceJSON"])
            consulta = (ssj.get("query") or {}).get("query") or ""
            assert all(c in campos for c in _CAMPO_KQL.findall(consulta)), (a["title"], consulta)
        if o["type"] == "dashboard":
            for r in o["references"]:
                assert (r["type"], r["id"]) in ids, (a["title"], r)
            paneles = json.loads(a["panelsJSON"])
            assert len(paneles) == len(o["references"])
            assert all(p["gridData"]["x"] + p["gridData"]["w"] <= 48 for p in paneles)


def test_las_tablas_tienen_sus_columnas_y_sus_filas():
    """La tabla por entidad lleva eventos, monto sumado y tiempo promediado; la
    de Problemas, la entidad y adentro sus motivos."""
    nd = dashboards.build_ndjson_from_fields("oms", "oms-%{+YYYY.MM}", OMS)
    vis = {o["attributes"]["title"]: json.loads(o["attributes"]["visState"])
           for o in map(json.loads, nd.splitlines()) if o["type"] == "visualization"}
    por = vis["[oms] Por Participante"]
    assert [(a["type"], a["schema"], a["params"].get("field")) for a in por["aggs"]] == [
        ("count", "metric", None), ("sum", "metric", "fix.monto_ejecutado"), ("avg", "metric", "fix.latencia_ms"),
        ("terms", "bucket", "fix.comp_id_propio")]
    motivos = vis["[oms] Participante y Motivo del rechazo"]
    assert [a["params"].get("field") for a in motivos["aggs"] if a["type"] == "terms"] == [
        "fix.comp_id_propio", "fix.rechazo_motivo.keyword"]
    p95 = vis["[oms] Latencia (ms) p95 por Tipo de mensaje"]
    assert p95["type"] == "line" and p95["aggs"][0]["params"]["percents"] == [95]
    assert [a["type"] for a in p95["aggs"]] == ["percentiles", "terms", "date_histogram"]


def test_el_perfil_va_al_resumen():
    nd = dashboards.build_ndjson_from_fields("oms", "oms-%{+YYYY.MM}", OMS)
    con = dashboards.con_panel_de_perfil(nd, "oms", perfiles.perfil_desde_campos(OMS))
    tableros = {o["id"]: o for o in map(json.loads, con.splitlines()) if o["type"] == "dashboard"}
    resumen = tableros[dashboards.id_del_dashboard(nd)]
    perfil = dashboards._stable_id("oms", "vis", "perfil por entidad")
    assert perfil in {r["id"] for r in resumen["references"]}
    assert all(perfil not in {r["id"] for r in t["references"]} for i, t in tableros.items() if t is not resumen)
