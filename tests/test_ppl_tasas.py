"""PPL: el span siempre con alias (sin él, PPL 3 no lo encuentra) y los
caudales (m3/d) no se suman lectura por lectura (visto en la ECS con los pozos)."""
import capabilities as caps

CAMPOS = [
    {"field_path": "pozo.id", "type": "string", "business_label": "Pozo", "dimension": True, "role": "entity_id"},
    {"field_path": "medicion.petroleo_m3d", "type": "float", "business_label": "Petróleo", "unit": "m3/d"},
    {"field_path": "medicion.agua", "type": "float", "business_label": "Agua", "unit": "m³/día"},
    {"field_path": "medicion.presion_boca_psi", "type": "float", "business_label": "Presión de boca", "unit": "psi"},
    {"field_path": "costo_ars", "type": "float", "business_label": "Costo", "unit": "ARS"},
    {"field_path": "@timestamp", "type": "date"},
]


def test_que_es_un_caudal():
    por = {f["field_path"]: caps.es_tasa(f) for f in CAMPOS}
    assert por == {"pozo.id": False, "medicion.petroleo_m3d": True, "medicion.agua": True,
                   "medicion.presion_boca_psi": False, "costo_ars": False, "@timestamp": False}
    assert caps.es_tasa({"field_path": "caudal_bpd", "type": "float"}), "por el nombre, sin unidad"
    assert not caps.es_tasa({"field_path": "tiempo_respuesta_min", "type": "float", "unit": "min"})


def test_el_caudal_se_totaliza_por_entidad():
    spec = caps.build_spec_from_fields("pozos", "pozos-*", CAMPOS, "Pozos")
    d = spec["fields"]["medicion.petroleo_m3d"]
    assert "RATE" in d and "NEVER sum its raw readings" in d and "sum/avg it" not in d
    assert ("stats avg(medicion.petroleo_m3d) as tasa by span(@timestamp, 1d) as dia, pozo.id "
            "| stats sum(tasa) as total by dia | sort dia") in d
    assert "sum/avg it" in spec["fields"]["costo_ars"], "una cantidad sí se suma"


def test_el_span_siempre_con_alias():
    for v3 in (False, True):
        p = caps.build_ppl_system_prompt("pozos-*", [], {"pozo.id": "Pozo"}, ppl_v3=v3)
        assert "ALWAYS alias the span" in p and "NEVER refer to `span(...)` by name" in p
        assert "stats count() as total by span(@timestamp, 1d) as dia | sort dia" in p
        assert "by span(@timestamp, 1d)\"" not in p
