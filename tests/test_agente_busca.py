"""El agente conversacional busca por significado: en los casos con búsqueda
híbrida tiene un VectorDBTool sobre `busqueda-<caso>`; primero encuentra cómo
se dijo algo y después cuenta esas frases con su PPLTool."""
import json
import pathlib
import shutil
import subprocess

import pytest

import agente_chat
import busqueda as busqueda_mod
import capabilities as caps
import main

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"
VERT = [{"tool_name": "PPLTool-envios", "label": "Seguimiento de envíos", "index_pattern": "envios*",
         "operations": [], "fields": {}, "success_code": "", "ppl_system_prompt": "p"}]
BUSCA = [{"slug": "envios", "label": "Seguimiento de envíos", "index": "busqueda-envios",
          "campo": "data.evento.observacion", "model_id": "M-CONSULTA"}]


def test_la_herramienta_del_agente():
    agente = caps.build_conversational_agent("LLM", "PPL", "i", VERT, buscadores=BUSCA)
    tool = next(t for t in agente["tools"] if t["type"] == "VectorDBTool")
    assert tool["name"] == "BuscarTextos-envios"
    p = tool["parameters"]
    assert (p["index"], p["embedding_field"], p["model_id"]) == ("busqueda-envios", "embedding", "M-CONSULTA")
    assert json.loads(p["source_field"]) == ["texto", "cuenta"] and p["k"] == "25" and p["doc_size"] == "25"
    assert "PPLTool-envios" in tool["description"] and "data.evento.observacion" in tool["description"]
    sin = caps.build_conversational_agent("LLM", "PPL", "i", VERT)
    assert [t["type"] for t in sin["tools"]] == ["PPLTool"], "sin búsqueda armada, como antes"


def test_el_agente_sabe_cuando_usarla():
    con = caps.build_agent_system_instruction(VERT, BUSCA)
    assert "BuscarTextos-envios" in con and "FIRST call that search tool" in con and "verbatim" in con
    assert "BuscarTextos" not in caps.build_agent_system_instruction(VERT)


def test_de_las_trazas_sale_lo_que_busco():
    trazas = [
        {"trace_number": 1, "origin": "BuscarTextos-envios", "input": "paquetes rotos",
         "response": "\n".join(json.dumps({"_index": "busqueda-envios", "_id": str(i), "_score": 0.8,
                                           "_source": {"texto": t, "cuenta": c}})
                               for i, (t, c) in enumerate([("La caja llegó aplastada", 120), ("Envoltorio roto", 98)]))},
        {"trace_number": 2, "origin": "PPLTool-envios", "input": '{"question": "contá"}',
         "response": json.dumps({"ppl": "source=envios* | stats count()", "executionResult": json.dumps(
             {"schema": [{"name": "count()"}], "datarows": [[218]]})})},
    ]
    leido = agente_chat.de_las_trazas(trazas)
    assert leido["busquedas"] == [{"tema": "paquetes rotos", "frases": [
        {"texto": "La caja llegó aplastada", "cuenta": 120}, {"texto": "Envoltorio roto", "cuenta": 98}]}]
    assert len(leido["consultas"]) == 1 and leido["consultas"][0]["ok"]
    # La entrada como JSON y una salida que no es JSON no rompen.
    otra = agente_chat.de_las_trazas([{"origin": "BuscarTextos-x", "input": '{"input": "fuga"}', "response": "nada"}])
    assert otra["busquedas"] == [{"tema": "fuga", "frases": []}]


def _estados(tmp_path, estados, registro=None):
    (tmp_path / main._ESTADOS_NAME).write_text(json.dumps(estados), encoding="utf-8")
    if registro is not None:
        main._write_capabilities(tmp_path, registro)


def test_los_buscadores_salen_de_la_busqueda_armada(tmp_path):
    _estados(tmp_path, {"_cluster": {"embeddings": {"model_id": "M-TEXTOS", "model_id_consulta": "M-CONSULTA"}},
                        "envios": {"busqueda": {"ok": True, "campo": "data.evento.observacion"}},
                        "otro": {"busqueda": {"ok": True, "no_aplica": True, "campo": "x"}}})
    vert = VERT + [{"tool_name": "PPLTool-otro", "label": "Otro"}, {"tool_name": "PPLTool-sin", "label": "Sin"}]
    assert main._buscadores_del_agente(tmp_path, vert) == BUSCA
    _estados(tmp_path, {"envios": {"busqueda": {"ok": True, "campo": "c"}}})
    assert main._buscadores_del_agente(tmp_path, VERT) == [], "sin modelo de embeddings, nada"


def test_se_rearma_el_agente_solo_si_cambio_su_configuracion(tmp_path, monkeypatch):
    """Un prompt de PPL mejorado, una búsqueda nueva u otro campo cambian la
    configuración: el agente se vuelve a registrar. Con la misma, no."""
    llamadas = []
    monkeypatch.setattr(main, "_registrar_agente", lambda *a, **k: llamadas.append(a) or ("AG2", {}))
    monkeypatch.setattr(main, "_os_base", lambda c, h: "http://x:9200")
    monkeypatch.setattr(main, "_fuentes_del_agente", lambda *a, **k: VERT)
    import maas_integrator
    monkeypatch.setattr(maas_integrator, "get_maas_api_key", lambda: "k")
    pasos = []
    monkeypatch.setattr(main.runs, "step", lambda run, nombre, ok, detalle="": pasos.append((nombre, ok, detalle)))
    registro = {"envios": {"agent_id": "AG", "ppl_model_id": "P", "llm_model_id": "L"}}
    estados = {"_cluster": {"embeddings": {"model_id_consulta": "Q"}}, "envios": {"busqueda": {"ok": True, "campo": "c"}}}
    _estados(tmp_path, estados, registro)
    main._rearmar_el_agente_si_cambio({}, "admin", "pw", True, tmp_path, {})
    assert len(llamadas) == 1, "sin firma guardada (un entorno de antes): se rearma"
    assert pasos and pasos[0][0] == "Asistente · al día"
    # Con la firma de lo que tendría que tener, no se toca.
    deseado = caps.build_conversational_agent("L", "P", caps.build_agent_system_instruction(
        VERT, main._buscadores_del_agente(tmp_path, VERT)), VERT, buscadores=main._buscadores_del_agente(tmp_path, VERT))
    estados["_cluster"]["agente"] = {"firma": main._firma_del_agente(deseado)}
    _estados(tmp_path, estados, registro)
    main._rearmar_el_agente_si_cambio({}, "admin", "pw", True, tmp_path, {})
    assert len(llamadas) == 1, "misma configuración: no se rearma"
    # Cambió el prompt de PPL de un caso (o cualquier otra cosa): se rearma.
    monkeypatch.setattr(main, "_fuentes_del_agente", lambda *a, **k: [{**VERT[0], "ppl_system_prompt": "otro"}])
    main._rearmar_el_agente_si_cambio({}, "admin", "pw", True, tmp_path, {})
    assert len(llamadas) == 2


def test_sin_vectordbtool_el_agente_igual_queda(tmp_path, monkeypatch):
    """Si el cluster rechaza el agente con la herramienta, se registra sin ella."""
    cuerpos = []

    def crear(base, user, pw, ruta, cuerpo, clave):
        cuerpos.append(cuerpo)
        return None if any(t["type"] == "VectorDBTool" for t in cuerpo["tools"]) else "AG"

    monkeypatch.setattr(main, "_fuentes_del_agente", lambda *a, **k: VERT)
    monkeypatch.setattr(main, "_buscadores_del_agente", lambda *a, **k: BUSCA)
    monkeypatch.setattr(main, "_search_ids", lambda *a, **k: [])
    monkeypatch.setattr(main, "_ml_create", crear)
    monkeypatch.setattr(main, "_set_os_chat_root_agent", lambda *a, **k: True)
    monkeypatch.setattr(main, "_provisionar_text2viz", lambda *a, **k: {})
    registro = {"envios": {"llm_model_id": "L"}}
    agent_id, _ = main._registrar_agente("http://x", "admin", "pw", "k", tmp_path, registro, "P", "L")
    assert agent_id == "AG" and len(cuerpos) == 2
    assert main._read_capabilities(tmp_path)["envios"]["busca_por_significado"] == ""
    # Queda la firma de lo que se quiso registrar: la próxima activación lo reintenta.
    assert main._read_estados(tmp_path)["_cluster"]["agente"]["firma"] == main._firma_del_agente(cuerpos[0])


def test_la_respuesta_lleva_lo_que_busco():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert 'busquedas=[b for b in leido.get("busquedas") or [] if b.get("frases")])' in src
    assert '_rearmar_el_agente_si_cambio(cluster, user, password, request.https_enabled, terraform_dir, run)' in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_el_chat_muestra_lo_que_busco(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function capChatCabeceraHTML(data) {")
    fuente = html[i:html.index("    // \"ver PPL / datos\"", i)]
    js = tmp_path / "busco.mjs"
    js.write_text("const icon = (n) => `<svg data-i=\"${n}\"></svg>`;\n"
                  "const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');\n"
                  + fuente + r"""
const f = [];
const frases = ['La caja llegó aplastada', 'Envoltorio roto', 'Paquete mojado', 'El producto llegó roto', 'Caja golpeada']
  .map((texto, i) => ({ texto, cuenta: 10 - i }));
const h = capChatCabeceraHTML({ busquedas: [{ tema: 'paquetes rotos', frases }], consultas: [] });
if (!h.includes('Buscó por significado «paquetes rotos»: La caja llegó aplastada · Envoltorio roto · Paquete mojado · El producto llegó roto · y 1 más')) f.push(h);
if (capChatCabeceraHTML({ busquedas: [{ tema: 'x', frases: [] }] }) !== '') f.push('sin frases, nada');
if (!capChatCabeceraHTML({ consultas: [{}, {}] }).includes('Investigación · 2 consultas')) f.push('la investigación sigue');
console.log(f.join('\n')); process.exit(f.length ? 1 : 0);
""", encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr



class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data or {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


def test_si_cambio_el_campo_la_busqueda_se_rehace(monkeypatch):
    """Un Transform que agrupa otro campo no es "ya estaba": se borra y se arma
    con el nuevo (si no, el estado decía observación y el índice seguía con motivo)."""
    pedidos = []

    def req(metodo, url, user, password, json_body=None, timeout=30):
        pedidos.append((metodo, url.split(":9200", 1)[1]))
        if url.endswith("/_search"):
            return _R(200, {"aggregations": {"n": {"value": 220}, "muestra": {"buckets": [
                {"key": "La caja llegó aplastada"}, {"key": "Nadie atendió en el domicilio"}]}}})
        if metodo == "GET" and "/_transform/" in url:
            return _R(200, {"transform": {"groups": [{"terms": {"source_field": "evento.motivo"}}]}})
        return _R(200, {})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_motivo_si_fallo", lambda *a, **k: None)
    monkeypatch.setattr(main, "_borrar_transform", lambda ruta, u, p: pedidos.append(("BORRAR", ruta.split(":9200", 1)[1])))
    fields = [{"field_path": "evento.motivo", "type": "string", "dimension": True, "role": "critical_indicator"},
              {"field_path": "evento.observacion", "type": "string", "dimension": False}]
    r = main._provisionar_busqueda("http://x:9200", "admin", "pw", "envios", "envios-*", fields, "M", False)
    assert r["ok"] and r["campo"] == "evento.observacion" and r.get("reason") != "ya estaba"
    assert ("BORRAR", "/_plugins/_transform/" + busqueda_mod.nombre_del_transform("envios")) in pedidos
    assert ("PUT", "/" + busqueda_mod.indice("envios")) in pedidos


def test_las_sugeridas_y_la_evaluacion_dependen_del_campo():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert 'and previa.get("campo") == caso.get("campo")' in src
    assert 'and anterior.get("campo") == res.get("campo"):' in src



def test_la_plantilla_dice_de_cuando_son_los_datos(monkeypatch):
    """Con «julio» sin año, el PPLTool del agente elegía 2025 (emergencias, en la
    ECS). Cada caso lleva su ventana de datos, por día."""
    monkeypatch.setattr(main, "_index_time_window", lambda b, u, p, ip: ("2026-01-01 03:00:00", "2026-10-12 23:59:58"))
    v = main._ventana_para_el_ppl("http://x", "a", "p", "emergencias-*")
    assert "DATA TIME WINDOW: @timestamp ranges from 2026-01-01 to 2026-10-12" in v
    assert "choose the year that falls within this window" in v
    monkeypatch.setattr(main, "_index_time_window", lambda *a: None)
    assert main._ventana_para_el_ppl("http://x", "a", "p", "x-*") == ""
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert src.count("+ _ventana_para_el_ppl(base, user, password, ip2),") == 2, "en las dos clases de fuente"
