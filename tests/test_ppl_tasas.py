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



class _R:
    def __init__(self, status, data=None):
        import json
        self.status_code, self._d = status, data or {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


def test_el_conector_de_ppl_se_pone_generico(monkeypatch):
    """Un conector con el prompt de un caso (los de antes) se repliega, se
    cambia y se vuelve a desplegar; con el genérico, no se toca."""
    import main
    pedidos = []
    estados = iter(["UNDEPLOYED", "DEPLOYING", "DEPLOYED"])
    prompt = {"actual": "You are a PPL query generator... source=pozos-vaca-muerta-*"}

    def req(m, url, user, password, json_body=None, timeout=30):
        ruta = url.split(":9200", 1)[1]
        pedidos.append((m, ruta))
        if m == "GET" and ruta == "/_plugins/_ml/models/PPL":
            return _R(200, {"connector_id": "C1", "model_state": next(estados, "DEPLOYED")})
        if m == "GET" and ruta == "/_plugins/_ml/connectors/C1":
            return _R(200, {"parameters": {"model": "deepseek", "system_prompt": prompt["actual"]}})
        if m == "PUT":
            assert json_body["parameters"] == {"model": "deepseek", "system_prompt": caps.PPL_SISTEMA}
        return _R(200, {})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    assert main._conector_de_ppl_al_dia("http://x:9200", "a", "p", "PPL") == ""
    hechos = [p for p in pedidos if p[0] in ("POST", "PUT")]
    assert hechos == [("POST", "/_plugins/_ml/models/PPL/_undeploy"), ("PUT", "/_plugins/_ml/connectors/C1"),
                      ("POST", "/_plugins/_ml/models/PPL/_deploy")]
    pedidos.clear()
    prompt["actual"] = caps.PPL_SISTEMA
    assert main._conector_de_ppl_al_dia("http://x:9200", "a", "p", "PPL") == ""
    assert not [p for p in pedidos if p[0] in ("POST", "PUT")], "ya estaba: no se repliega"


def test_la_plantilla_del_ppltool():
    t = caps.plantilla_del_ppltool("line 1\nsource=pozos-*")
    assert t == "line 1\\nsource=pozos-*\\n\\nQUESTION: ${indexInfo.question}\\nPPL:"
