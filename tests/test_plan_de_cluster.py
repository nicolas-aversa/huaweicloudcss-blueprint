"""El plan del cluster: qué plugins de OpenSearch aplican a un dataset y por
qué, derivado solo de sus campos. Un dataset que no es ninguna demo."""
import capabilities as caps
import custom_cases
import plan_de_cluster as pc

# Reservas de un hotel: no se parece a ninguna vertical.
HOTEL = [
    {"field_path": "fecha_reserva", "raw_name": "fecha_reserva", "type": "date", "role": "timestamp",
     "business_label": "Fecha de reserva"},
    {"field_path": "huesped", "raw_name": "huesped", "type": "string", "role": "entity_id", "entity": True,
     "sensitive": True, "business_label": "Huésped"},
    {"field_path": "id_reserva", "raw_name": "id_reserva", "type": "string", "role": "entity_id",
     "business_label": "Reserva"},
    {"field_path": "canal", "raw_name": "canal", "type": "string", "dimension": True,
     "role": "primary_dimension", "business_label": "Canal"},
    {"field_path": "noches", "raw_name": "noches", "type": "integer", "role": "measure", "business_label": "Noches"},
    {"field_path": "importe", "raw_name": "importe", "type": "float", "role": "measure", "principal": True,
     "business_label": "Importe"},
    {"field_path": "cancelada", "raw_name": "cancelada", "type": "integer", "role": "critical_indicator",
     "business_label": "Cancelada"},
    {"field_path": "pais_origen", "raw_name": "pais_origen", "type": "string", "dimension": True,
     "business_label": "País de origen"},
    {"field_path": "comentario", "raw_name": "comentario", "type": "text", "business_label": "Comentario"},
]


def _plan(fields):
    return {i["plugin"]: i for i in pc.plan("hotel", fields)}


def test_un_dataset_nuevo_tiene_todos_los_plugins_que_justifican_sus_datos():
    p = _plan(HOTEL)
    for plugin in ("agente", "forecasting", "anomalias", "alertas", "perfil", "analista", "mapa",
                   "explicar", "text2viz", "documentos", "query_insights"):
        assert p[plugin]["aplica"], (plugin, p[plugin]["motivo"])
        assert p[plugin]["motivo"] and p[plugin]["titulo"]
    assert "Fecha de reserva" in p["forecasting"]["motivo"] and "total de Importe" in p["forecasting"]["motivo"]
    assert p["perfil"]["config"]["campo"] == "huesped" and "Huésped" in p["perfil"]["motivo"]
    assert p["analista"]["config"] == ["huesped"]
    assert p["mapa"]["config"] == {"tipo": "pais", "campo": "pais_origen"}
    assert p["explicar"]["config"] == {"pattern_field": "comentario"}


def test_entre_varias_medidas_y_entidades_gana_la_principal():
    fcs = {fc["feature_name"]: fc["aggregation_query"] for fc in _plan(HOTEL)["forecasting"]["config"]}
    assert fcs["measure_sum"] == {"measure_sum": {"sum": {"field": "importe"}}}, "no la primera (noches)"
    assert fcs["unique_entities"] == {"unique_entities": {"cardinality": {"field": "huesped"}}}
    # Un crítico numérico (0/1) está en todos los registros: se suma, no se cuenta.
    assert fcs["critical_events"] == {"critical_events": {"sum": {"field": "cancelada"}}}
    assert len(fcs) == 4


def test_sin_fecha_del_evento_no_hay_series_y_lo_dice():
    p = _plan([f for f in HOTEL if f["field_path"] != "fecha_reserva"])
    for plugin in ("forecasting", "anomalias", "alertas", "explicar"):
        assert not p[plugin]["aplica"]
    assert "fecha del evento" in p["forecasting"]["motivo"]
    assert p["agente"]["aplica"] and p["perfil"]["aplica"], "lo demás no depende de la fecha"


def test_lo_que_no_aplica_trae_su_motivo():
    minimo = [{"field_path": "fecha", "type": "date", "role": "timestamp"},
              {"field_path": "valor", "type": "float"}]
    p = _plan(minimo)
    for plugin, palabra in (("perfil", "Entidad"), ("analista", "Sensible"), ("mapa", "país")):
        assert not p[plugin]["aplica"] and palabra in p[plugin]["motivo"], p[plugin]
    assert all(i["aplica"] for i in pc.plan("x", minimo) if (i["config"] or {}) == {"alcance": "cluster"})


def test_el_mapa_por_coordenadas_gana_y_un_codigo_de_pais_no_une():
    assert pc.campo_de_mapa([{"field_path": "geo.loc", "type": "geo_point"}] + HOTEL) == ("coordenadas", "geo.loc")
    assert pc.campo_de_mapa([{"field_path": "country_iso_code", "type": "string"}]) is None


def test_el_campo_de_patrones():
    assert caps.campo_de_patrones(HOTEL) == "comentario", "el texto libre primero"
    sin_texto = [{"field_path": "srcip", "type": "ip"}, {"field_path": "action", "type": "string"},
                 {"field_path": "msg", "type": "string"}]
    assert caps.campo_de_patrones(sin_texto) == "action", "el que cuenta qué pasó"
    assert caps.campo_de_patrones([{"field_path": "canal", "type": "string", "role": "primary_dimension"}]) == "canal"
    assert caps.campo_de_patrones([{"field_path": "monto", "type": "float"}]) == ""
    assert caps.build_spec_from_fields("hotel", "hotel-*", HOTEL)["pattern_field"] == "comentario"


def test_un_caso_guardado_conserva_las_marcas_del_paso_2():
    limpios = {f["field_path"]: f for f in custom_cases._clean_fields(HOTEL)}
    assert limpios["huesped"]["entity"] is True and limpios["huesped"]["sensitive"] is True
    assert limpios["importe"]["principal"] is True


def test_el_endpoint_del_paso_2():
    from fastapi.testclient import TestClient
    import main

    r = TestClient(main.app).post("/api/v1/onboarding/plan-del-cluster", json={"fields": HOTEL})
    assert r.status_code == 200
    items = {i["plugin"]: i for i in r.json()["items"]}
    assert items["perfil"]["aplica"] and items["perfil"]["opcional"]
    assert not items["agente"]["opcional"] and not items["text2viz"]["opcional"]


def test_lo_apagado_viaja_en_el_registro_y_apaga_perfil_y_analista(tmp_path):
    import json
    import main

    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    main._prepare_deploy_tfvars(main.TerraformDeployRequest(
        pipeline_conf="filter { }", opensearch_index="hotel-%{+YYYY.MM}", fields=HOTEL,
        excluir=["perfil", "analista"]), td)
    entrada = json.loads((td / main._PIPELINES_REGISTRY_NAME).read_text(encoding="utf-8"))["hotel"]
    assert entrada["excluir"] == ["perfil", "analista"]
    assert main._perfil_de("hotel", entrada) is None and main._enmascarados_de("hotel", entrada) == []
    assert main._perfil_de("hotel", {"fields": HOTEL})["campo"] == "huesped"
    assert main._enmascarados_de("hotel", {"fields": HOTEL}) == ["huesped"]


def test_en_un_deploy_de_varios_casos_cada_uno_lleva_lo_suyo(tmp_path):
    import json
    import main

    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    main._prepare_deploy_tfvars(main.TerraformDeployRequest(pipeline_conf="filter { }", cases=[
        main.PipelineCase(slug="hotel", filter_code="filter { }", fields=HOTEL, index_name="hotel-%{+YYYY.MM}",
                          excluir=["anomalias"]),
        main.PipelineCase(slug="otro", filter_code="filter { }", index_name="otro-%{+YYYY.MM}"),
    ]), td)
    registro = json.loads((td / main._PIPELINES_REGISTRY_NAME).read_text(encoding="utf-8"))
    assert registro["hotel"]["excluir"] == ["anomalias"] and registro["otro"]["excluir"] == []


def test_la_entidad_marcada_tambien_se_pronostica():
    """La Entidad del paso 2 (sin rol) arma el perfil y también el pronóstico
    de entidades únicas: son la misma."""
    campos = [{"field_path": "fecha", "type": "date", "role": "timestamp"},
              {"field_path": "cliente", "type": "string", "entity": True},
              {"field_path": "id_pedido", "type": "string", "role": "entity_id"}]
    fcs = {fc["feature_name"]: fc for fc in caps.build_spec_from_fields("x", "x-*", campos)["forecasts"]}
    assert fcs["unique_entities"]["aggregation_query"] == {"unique_entities": {"cardinality": {"field": "cliente"}}}
