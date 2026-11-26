"""Las dimensiones con más valores que un enum (19 tipos de evento, 23 plazas)
le pasan sus valores al prompt del chat, sin cambiar lo que deciden los enums
(la dimensión principal, el código de éxito, los pronósticos)."""
import capabilities as caps
import main

TIPOS = ["Pasada", "Cobro", "Inicio de sesión fallido", "Inicio de sesión", "Alarma de vía", "Reclamo",
         "Liquidación a concesionaria", "Cobro rechazado", "Inhabilitación del TAG", "Rehabilitación del TAG",
         "Envío del TAG", "Activación del TAG", "Adhesión", "Pago de deuda", "Devolución", "Cambio de datos",
         "Pedido de TAG adicional", "Baja", "Reemplazo de TAG"]
FIELDS = [
    {"field_path": "evento.tipo", "type": "string", "dimension": True, "role": "success_indicator",
     "business_label": "Tipo de evento"},
    {"field_path": "canal", "type": "string", "dimension": True, "business_label": "Canal"},
    {"field_path": "monto_ars", "type": "long", "role": "measure", "business_label": "Monto"},
]


def test_los_valores_van_al_prompt_y_no_deciden_nada_mas():
    enums = {"canal": ["Vía", "App", "Web"]}
    sin = caps.build_spec_from_fields("peaje", "peaje*", FIELDS, "Peaje", enums)
    con = caps.build_spec_from_fields("peaje", "peaje*", FIELDS, "Peaje", enums, {"evento.tipo": TIPOS})
    assert "Alarma de vía" not in sin["fields"]["evento.tipo"]
    assert "Alarma de vía" in con["fields"]["evento.tipo"] and "Reemplazo de TAG" in con["fields"]["evento.tipo"]
    assert "…" not in con["fields"]["evento.tipo"], "los 19, sin cortar"
    for k in ("operations", "success_code", "forecasts"):
        assert con.get(k) == sin.get(k), k
    # Un enum manda sobre los valores (son los mismos datos, con su tope).
    otro = caps.build_spec_from_fields("peaje", "peaje*", FIELDS, "Peaje", enums, {"canal": ["X"]})
    assert "Vía" in otro["fields"]["canal"] and "X" not in otro["fields"]["canal"]


def test_los_valores_del_chat_son_los_que_no_son_enums(monkeypatch):
    pedidos = []

    def fake(base, user, password, ip, fields, max_terms=12, max_cardinality=15):
        pedidos.append((max_terms, max_cardinality))
        return {"canal": ["Vía", "App"], "evento.tipo": TIPOS}
    monkeypatch.setattr(main, "_discover_enums", fake)
    vals = main._valores_para_el_chat("b", "u", "p", "peaje*", FIELDS, {"canal": ["Vía", "App"]})
    assert vals == {"evento.tipo": TIPOS}
    assert pedidos == [(main._MAX_VALORES_CHAT, main._MAX_VALORES_CHAT)] and main._MAX_VALORES_CHAT >= 19


def test_cada_busqueda_dice_de_que_campo_es():
    con = caps.build_agent_system_instruction(
        [{"tool_name": "PPLTool-peaje", "label": "Peaje", "index_pattern": "peaje*", "operations": [],
          "fields": {}, "success_code": "", "ppl_system_prompt": "p"}],
        [{"slug": "peaje", "label": "Peaje", "index": "busqueda-peaje", "campo": "alarma.detalle", "model_id": "M"}])
    assert "BuscarTextos-peaje: ONLY the texts of field alarma.detalle" in con
    assert "a complaint," not in con, "el ejemplo empujaba a buscar reclamos en cualquier campo de texto"
