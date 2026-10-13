"""El agente conversacional busca por significado: en los casos con búsqueda
híbrida tiene un VectorDBTool sobre `busqueda-<caso>`; primero encuentra cómo
se dijo algo y después cuenta esas frases con su PPLTool."""
import json
import pathlib
import shutil
import subprocess

import pytest

import agente_chat
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
    assert json.loads(p["source_field"]) == ["texto", "cuenta"] and p["k"] == "10" and p["doc_size"] == "10"
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


def test_se_rearma_el_agente_solo_si_hay_busqueda_nueva(tmp_path, monkeypatch):
    llamadas = []
    monkeypatch.setattr(main, "_registrar_agente", lambda *a, **k: llamadas.append(a) or ("AG2", {}))
    monkeypatch.setattr(main, "_os_base", lambda c, h: "http://x:9200")
    import maas_integrator
    monkeypatch.setattr(maas_integrator, "get_maas_api_key", lambda: "k")
    pasos = []
    monkeypatch.setattr(main.runs, "step", lambda run, nombre, ok, detalle="": pasos.append((nombre, ok, detalle)))
    run = {"events": []}
    registro = {"envios": {"agent_id": "AG", "ppl_model_id": "P", "llm_model_id": "L", "busca_por_significado": False}}
    _estados(tmp_path, {"envios": {"busqueda": {"ok": True, "campo": "c"}}}, registro)
    main._sumar_la_busqueda_al_agente({}, "admin", "pw", True, tmp_path, run)
    assert len(llamadas) == 1, "la búsqueda quedó lista y el agente no la tenía"
    assert pasos and pasos[0][0] == "Asistente · búsqueda por significado"
    registro["envios"]["busca_por_significado"] = True
    _estados(tmp_path, {"envios": {"busqueda": {"ok": True, "campo": "c"}}}, registro)
    main._sumar_la_busqueda_al_agente({}, "admin", "pw", True, tmp_path, run)
    assert len(llamadas) == 1, "ya la tenía: no se rearma"


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
    assert main._read_capabilities(tmp_path)["envios"]["busca_por_significado"] is False


def test_la_respuesta_lleva_lo_que_busco():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert 'busquedas=[b for b in leido.get("busquedas") or [] if b.get("frases")])' in src
    assert '_sumar_la_busqueda_al_agente(cluster, user, password, request.https_enabled, terraform_dir, run)' in src


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
