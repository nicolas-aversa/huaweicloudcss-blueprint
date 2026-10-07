"""El chat de la plataforma conversa con el agente de OpenSearch (con la
memoria en ml-commons) y el gráfico lo arma text to visualization. Formatos medidos en CSS 3.4 (ver agente_chat.py y text2viz.py)."""
import json
import pathlib
import shutil
import subprocess

import pytest

import agente_chat as ac
import main
import text2viz


# ── Leer al agente ──────────────────────────────────────────────────────────
def test_la_pregunta_lleva_lo_que_el_usuario_esta_mirando():
    q = ac.pregunta_para_el_agente("¿cuántos hay?", "SIEM", "siem*", "regla X")
    assert q.startswith("¿cuántos hay?") and "datos de SIEM, índice siem*" in q and "(Contexto: regla X)" in q
    assert ac.pregunta_para_el_agente("hola") == "hola"


def test_la_respuesta_del_execute():
    resp = {"inference_results": [{"output": [
        {"name": "memory_id", "result": "MEM"}, {"name": "parent_interaction_id", "result": "PID"},
        {"name": "response", "dataAsMap": {"response": " La IP es 5.188.206.18. "}}]}]}
    assert ac.respuesta(resp) == {"respuesta": "La IP es 5.188.206.18.", "memory_id": "MEM", "parent_interaction_id": "PID"}


def _ppl(ppl, filas):
    return json.dumps({"ppl": ppl, "executionResult": json.dumps({"schema": [{"name": "total", "type": "bigint"}], "datarows": filas})})


TRAZAS = [
    {"origin": "LLM", "trace_number": 1, "input": "q", "response": "pienso"},
    {"origin": "PPLTool-siem", "trace_number": 2, "input": '{"question": "top IP"}', "response": _ppl("source=siem* | head 1", [[12972]])},
    {"origin": "PPLTool-siem", "trace_number": 3, "input": "top IP sin nulos",
     "response": "Failed to run the tool PPLTool-siem with the error message execute ppl: boom"},
    {"origin": "PPLTool-siem", "trace_number": 4, "input": '{"question": "top IP sin nulos"}', "response": _ppl("source=siem* | where isnotnull(source.ip)", [[571]])},
]


def test_las_consultas_salen_de_las_trazas():
    r = ac.de_las_trazas(list(reversed(TRAZAS)))   # el orden lo da trace_number
    assert [c["pregunta"] for c in r["consultas"]] == ["top IP", "top IP sin nulos", "top IP sin nulos"]
    assert [c["ok"] for c in r["consultas"]] == [True, False, True]
    assert r["consultas"][1]["error"] == "execute ppl: boom"
    assert r["consultas"][2]["result"] == {"schema": [{"name": "total", "type": "bigint"}], "datarows": [[571]]}
    assert list(r) == ["consultas"]


def test_la_que_respondio_es_la_ultima_que_corrio():
    r = ac.de_las_trazas(TRAZAS)
    assert ac.ultima_buena(r["consultas"])["ppl"] == "source=siem* | where isnotnull(source.ip)"
    assert ac.ultima_buena([{"ok": False}]) is None


# ── Text to visualization ───────────────────────────────────────────────────
def test_text2viz_tiene_su_modelo_sin_razonar():
    """Con el LLM del agente (que razona) cada gráfico tardaba 6 a 12 s."""
    c = text2viz.build_connector("CLAVE", "e", "deepseek-v4.1-flash")
    assert '"chat_template_kwargs": {"thinking": false}' in c["actions"][0]["request_body"]
    assert c["credential"] == {"maas_key": "CLAVE"} and c["parameters"]["model"] == "deepseek-v4.1-flash"


def test_el_workflow_es_la_plantilla_oficial_sobre_ese_modelo():
    w = text2viz.build_workflow("T2V")
    nodos = w["workflows"]["provision"]["nodes"]
    assert [n["type"] for n in nodos] == ["create_tool", "create_tool", "register_agent", "register_agent"]
    herr = nodos[0]["user_inputs"]
    assert herr["type"] == "MLModelTool" and herr["parameters"]["model_id"] == "T2V"
    assert herr["parameters"]["response_filter"] == "$.choices[0].message.content"
    assert "wrap your vega-lite json in <vega-lite> </vega-lite> tags" in herr["parameters"]["prompt"]
    assert "${parameters.input_instruction}" in nodos[1]["user_inputs"]["parameters"]["prompt"]
    assert {n["user_inputs"]["name"] for n in nodos[2:]} == {"t2vega agent", "t2vega instruction based agent"}
    assert text2viz.CONFIG == {"t2vega agent": "os_text2vega", "t2vega instruction based agent": "os_text2vega_with_instructions"}


def test_los_parametros_van_escapados():
    """ml-commons los mete en el prompt sin escapar: las comillas de la muestra
    rompían el pedido ("Invalid payload", visto en CSS 3.4)."""
    p = text2viz.parametros('¿y "esto"?', "source=x | stats count() by a", {
        "schema": [{"name": "a", "type": "string"}, {"name": "n", "type": "bigint"}], "datarows": [["x", 1]] * 30})
    assert p["input_question"] == '¿y \\"esto\\"?'
    assert p["sampleData"].startswith('[{\\"a\\": \\"x\\", \\"n\\": 1}')
    assert json.loads(json.loads('"' + p["sampleData"] + '"'))[-1] == {"a": "x", "n": 1}
    assert len(json.loads(json.loads('"' + p["sampleData"] + '"'))) == text2viz.MAX_FILAS_DE_MUESTRA
    assert "input_instruction" not in p


@pytest.mark.parametrize("texto, ok", [
    ('Number of metrics: 1\n<vega-lite> {"mark": "bar", "data": {"values": []}, "encoding": {}} </vega-lite>', True),
    ('{"mark": "line", "encoding": {}}', True),
    ("no hay gráfico", False),
    ('<vega-lite> {roto </vega-lite>', False),
    ('{"title": "sin mark ni encoding"}', False),
])
def test_la_especificacion(texto, ok):
    spec = text2viz.especificacion({"inference_results": [{"output": [{"name": "response", "result": texto}]}]})
    assert (spec is not None) is ok
    if spec:
        assert "data" not in spec, "los datos los pone la plataforma"


# ── En el endpoint ──────────────────────────────────────────────────────────
class _R:
    def __init__(self, status, data):
        self.status_code, self._d = status, data
        self.text = json.dumps(data)

    def json(self):
        return self._d


def test_conversar_con_el_agente(monkeypatch):
    pedidos = []

    def req(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((method, url.split(":9200", 1)[1], json_body))
        if url.endswith("/_execute") and "/AG/" in url:
            return _R(200, {"inference_results": [{"output": [
                {"name": "memory_id", "result": "MEM"}, {"name": "parent_interaction_id", "result": "PID"},
                {"name": "response", "dataAsMap": {"response": "La IP es 5.188.206.18."}}]}]})
        if url.endswith("/traces"):
            return _R(200, {"traces": TRAZAS})
        return _R(404, {})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_visualizar", lambda *a: pytest.fail("la respuesta no espera al gráfico"))
    pedido = main.PplChatRequest(question="¿top IP?", slug="siem", memory_id="MEM0")
    r = main._conversar_con_el_agente("http://x:9200", "a", "p", "AG", pedido, "SIEM", "siem*")
    assert r.answer == "La IP es 5.188.206.18." and r.memory_id == "MEM"
    assert r.ppl == "source=siem* | where isnotnull(source.ip)" and r.result["datarows"] == [[571]]
    assert len(r.consultas) == 3
    assert r.vega == {}, "el gráfico se pide aparte"
    ejecucion = next(b for m, ruta, b in pedidos if ruta.endswith("/_execute"))
    assert ejecucion["parameters"]["memory_id"] == "MEM0", "sigue la conversación"
    assert "datos de SIEM" in ejecucion["parameters"]["question"]


def test_si_el_agente_falla_sigue_el_camino_de_antes(monkeypatch):
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: _R(500, {"error": "boom"}))
    pedido = main.PplChatRequest(question="¿top IP?", slug="siem")
    assert main._conversar_con_el_agente("http://x:9200", "a", "p", "AG", pedido, "SIEM", "siem*") is None
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("    if ids.get(\"agent_id\"):\n        r = _conversar_con_el_agente(")
    assert src.index("return _conversar(request.question", i) > i, "el respaldo, después"


def test_sin_agente_t2vega_o_con_una_fila_no_hay_grafico(monkeypatch):
    main._AGENTE_T2VEGA.clear()
    monkeypatch.setattr(main, "_search_ids", lambda *a, **k: [])
    res = {"schema": [{"name": "a"}], "datarows": [[1], [2]]}
    assert main._visualizar("http://x:9200", "a", "p", "q", "ppl", res) is None
    # Con agente: una fila (un número) no se grafica, ni se le pregunta al agente.
    monkeypatch.setattr(main, "_search_ids", lambda *a, **k: ["T2V"])
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: pytest.fail("no debería llamar al agente"))
    main._AGENTE_T2VEGA.clear()
    assert main._visualizar("http://x:9200", "a", "p", "q", "ppl", {"schema": [{"name": "a"}], "datarows": [[1]]}) is None


def test_el_paso_3_provisiona_text2viz_y_acepta_el_202(monkeypatch):
    estado = {"agentes": {}}

    def req(method, url, user, password, json_body=None, timeout=30):
        ruta = url.split(":9200", 1)[1]
        if ruta.startswith("/_plugins/_flow_framework/workflow?provision=true"):
            return _R(202, {"workflow_id": "W"})
        if ruta.endswith("/_status"):
            return _R(200, {"state": "COMPLETED", "resources_created": [
                {"resource_type": "agent_id", "workflow_step_id": "t2vega_agent", "resource_id": "A1"},
                {"resource_type": "agent_id", "workflow_step_id": "t2vega_instruction_based_agent", "resource_id": "A2"}]})
        if ruta.startswith("/.plugins-ml-config/_doc/"):
            estado["agentes"][ruta.rsplit("/", 1)[1]] = json_body["configuration"]["agent_id"]
        return _R(200, {})

    monkeypatch.setattr(main, "_os_req", req)
    # Los agentes no están; el modelo platform-t2v sí (desplegado).
    monkeypatch.setattr(main, "_search_ids", lambda b, u, p, ruta, nombre, *a: ["T2V"] if "models" in ruta else [])
    monkeypatch.setattr(main, "_ml_wait_deployed", lambda *a: (True, "DEPLOYED"))
    r = main._provisionar_text2viz("http://x:9200", "a", "p", "CLAVE")
    assert r["ok"] and estado["agentes"] == {"os_text2vega": "A1", "os_text2vega_with_instructions": "A2"}


# ── En la vista ─────────────────────────────────────────────────────────────
_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_el_grafico_en_node(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    // El Vega-Lite que armó el agente de text to visualization")
    fns = html[i:html.index("    function capChatDetallesHTML(data) {", i)]
    js = tmp_path / "chat.mjs"
    js.write_text(r"""
const icon = (n) => `<i:${n}>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
""" + fns + r"""
const f = [];
const check = (n, c, x) => { if (!c) f.push(n + ' -> ' + x); };
const res = { schema: [{ name: 'event.dataset' }, { name: 'total' }], datarows: [['waf', 3], ['auth', 5]] };
const spec = capChatVegaConDatos({ mark: 'bar', data: { url: 'x' }, encoding: { y: { field: 'event.dataset' }, x: { field: 'total' } },
                                   layer: [{ encoding: { color: { field: 'event.dataset' } } }] }, res, { c: 1 });
check('datos de la consulta', JSON.stringify(spec.data) === JSON.stringify({ values: [{ 'event.dataset': 'waf', total: 3 }, { 'event.dataset': 'auth', total: 5 }] }), JSON.stringify(spec.data));
check('el punto escapado', spec.encoding.y.field === 'event\\.dataset' && spec.layer[0].encoding.color.field === 'event\\.dataset', spec.encoding.y.field);
check('lo demás igual', spec.encoding.x.field === 'total' && spec.width === 'container' && spec.config.c === 1);
console.log(f.join('\n')); process.exit(f.length ? 1 : 0);
""", encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_la_vista_manda_y_guarda_la_memoria():
    html = _INDEX.read_text(encoding="utf-8")
    assert "memory_id: capChatMemoria[slug] || ''," in html
    assert "if (data.memory_id) capChatMemoria[slug] = data.memory_id;" in html
    assert "function capChatLimpiar(slug) { capChats[slug] = []; delete capChatMemoria[slug]; }" in html, \
        "conversación nueva, memoria nueva"
    # El gráfico del agente se pide DESPUÉS de mostrar la respuesta.
    assert "if (conT2v) _graficoDelAgente(pend, lugar, question, data);" in html
    assert "else _renderCapChartInto(lugar, data.result, data.vega);" in html
    i = html.index("async function _graficoDelAgente(")
    fn = html[i:html.index("      function _typeInto(", i)]
    assert "fetch('/api/v1/capabilities/visualizar'" in fn and "Armando el gráfico" in fn
    assert "_renderCapChartInto(actual, data.result, pend.vega);" in fn, "sin el del agente, el de las reglas"


def test_el_grafico_se_pide_aparte(monkeypatch):
    monkeypatch.setattr(main, "_cluster_del_entorno", lambda stage: ("http://x:9200", "a", "p"))
    vistos = []
    monkeypatch.setattr(main, "_visualizar", lambda base, u, pw, q, ppl, res: vistos.append((q, ppl, res)) or {"mark": "line"})
    from fastapi.testclient import TestClient
    res = {"schema": [{"name": "a"}], "datarows": [[1], [2]]}
    r = TestClient(main.app).post("/api/v1/capabilities/visualizar", json={"question": "q", "ppl": "source=x", "result": res})
    assert r.json() == {"vega": {"mark": "line"}} and vistos == [("q", "source=x", res)]
    monkeypatch.setattr(main, "_visualizar", lambda *a: None)
    assert TestClient(main.app).post("/api/v1/capabilities/visualizar", json={"question": "q", "ppl": "p"}).json() == {"vega": {}}
