"""Base de conocimiento 100 % OpenSearch: el archivo entra por un pipeline de
ingesta (attachment → text_chunking) y un pipeline de búsqueda RAG responde con
él. La plataforma solo manda el archivo, como cualquier aplicación. Lo medido en
CSS 3.4 está en el docstring de conocimiento.py."""
import base64
import json
import pathlib
import shutil
import subprocess

import pytest

import conocimiento as kb
import main


# ── Lo que hace OpenSearch ──────────────────────────────────────────────────
def test_el_pipeline_de_ingesta_extrae_parte_y_no_guarda_el_archivo():
    procs = kb.build_pipeline_de_ingesta()["processors"]
    assert [next(iter(p)) for p in procs] == ["attachment", "rename", "fail", "text_chunking", "remove"]
    assert procs[0]["attachment"]["field"] == "archivo" and procs[0]["attachment"]["indexed_chars"] == -1
    # Un PDF escaneado: Tika devuelve blancos y se indexaba vacío.
    assert procs[2]["fail"] == {"if": "ctx.texto == null || ctx.texto.trim().isEmpty()", "message": kb.SIN_TEXTO}
    assert procs[3]["text_chunking"]["field_map"] == {"texto": "fragmentos"}
    assert procs[4]["remove"]["field"] == ["archivo", "texto"], "ni el binario ni el texto entero"


def test_el_indice_usa_el_pipeline_y_analiza_en_tres_idiomas():
    m = kb.mapping()
    assert m["settings"]["index.default_pipeline"] == kb.PIPELINE_DE_INGESTA
    f = m["mappings"]["properties"]["fragmentos"]
    assert f["fields"]["es"]["analyzer"] == "spanish" and f["fields"]["en"]["analyzer"] == "english"


def test_la_busqueda_trae_el_contexto_y_resalta_el_pasaje():
    q = kb.busqueda("¿reinicio?", "m")
    assert {"fragmentos.es", "fragmentos.en"} <= set(q["query"]["multi_match"]["fields"])
    assert "fragmentos" in q["_source"], "el procesador RAG lee el contexto del _source"
    assert set(q["highlight"]["fields"]) == {"fragmentos", "fragmentos.es", "fragmentos.en"}
    assert (q["highlight"]["boundary_scanner"], q["highlight"]["order"]) == ("sentence", "score"),         "la oración que más pesa, no el comienzo del fragmento"
    assert q["ext"]["generative_qa_parameters"]["llm_question"] == "¿reinicio?"


def test_el_pipeline_rag():
    proc = kb.build_pipeline("M")["response_processors"][0]["retrieval_augmented_generation"]
    assert proc["model_id"] == "M" and proc["context_field_list"] == ["titulo", "fragmentos"]
    assert "nunca inventes" in proc["system_prompt"]
    c = kb.build_connector("K", "e", "deepseek-v4.1-flash")
    assert '"messages": ${parameters.messages}' in c["actions"][0]["request_body"]


@pytest.mark.parametrize("nombre, tamano, motivo", [
    ("a.docx", 10, ""), ("a.PDF", 10, ""), ("a.exe", 10, "formato no soportado (.exe)"),
    ("sin_extension", 10, "sin extensión"), ("a.txt", kb.MAX_BYTES + 1, "pesa más de 10 MB"), ("a.txt", 0, "está vacío"),
])
def test_validar(nombre, tamano, motivo):
    assert motivo in kb.validar(nombre, tamano) and (kb.validar(nombre, tamano) == "") == (motivo == "")


def test_el_error_de_un_pdf_sin_texto_se_entiende():
    err = '{"error":{"root_cause":[{"type":"illegal_argument_exception","reason":"field [adjunto.content] not present as part of path [adjunto.content]"}]}}'
    assert kb.motivo_de_ingesta(err) == "no tiene texto (¿es un PDF escaneado o una imagen?)"
    assert kb.motivo_de_ingesta('{"error":{"reason":"otra cosa"}}') == "otra cosa"
    assert kb.motivo_de_ingesta('{"error":{"reason":"el documento no tiene texto"}}') == "no tiene texto (¿es un PDF escaneado o una imagen?)"


# ── La respuesta ────────────────────────────────────────────────────────────
def _hit(titulo, score, resaltado=None):
    h = {"_score": score, "_source": {"titulo": titulo, "fragmentos": ["primer fragmento " * 30]}}
    if resaltado:
        h["highlight"] = {"fragmentos.es": [resaltado]}
    return h


def test_las_fuentes_son_las_relevantes_con_su_pasaje():
    resp = {"ext": {"retrieval_augmented_generation": {"answer": " Al CISO. "}},
            "hits": {"hits": [_hit("Runbook", 4.0, "avisar al CISO dentro de la hora"), _hit("Política", 1.0)]}}
    r = kb.respuesta(resp)
    assert r == {"respuesta": "Al CISO.", "fuentes": [{"titulo": "Runbook", "extracto": "avisar al CISO dentro de la hora"}]}
    sin = kb.respuesta({"hits": {"hits": [_hit("X", 1.0)]}})
    assert len(sin["fuentes"][0]["extracto"]) == 280, "sin resaltado, el comienzo del primer fragmento"


def test_el_listado():
    resp = {"hits": {"hits": [{"_source": {"titulo": "a.pdf", "adjunto": {"content_type": "application/pdf; x", "content_length": 9}}}]}}
    assert kb.listado(resp) == [{"titulo": "a.pdf", "tipo": "application/pdf", "caracteres": 9}]


# ── Los endpoints ───────────────────────────────────────────────────────────
class _R:
    def __init__(self, status, data=None, text=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = text if text is not None else json.dumps(self._d)

    def json(self):
        return self._d


@pytest.fixture
def cluster(monkeypatch):
    estado = {"pedidos": [], "indice": False, "modelo": None, "pipeline": False, "ingesta_falla": ""}

    def req(method, url, user, password, json_body=None, timeout=30):
        ruta = url.split(":9200", 1)[1]
        estado["pedidos"].append((method, ruta, json_body))
        if ruta == "/conocimiento-plataforma" and method == "PUT":
            ya = estado["indice"]; estado["indice"] = True
            return _R(400, text="resource_already_exists_exception") if ya else _R(200)
        if ruta.startswith("/conocimiento-plataforma/_doc"):
            return _R(500, text=estado["ingesta_falla"]) if estado["ingesta_falla"] else _R(201, {"result": "created"})
        if ruta.startswith("/conocimiento-plataforma/_delete_by_query"):
            return _R(200, {"deleted": 1})
        if ruta == "/_plugins/_ml/models/_search":
            return _R(200, {"hits": {"hits": [{"_id": estado["modelo"]}] if estado["modelo"] else []}})
        if ruta == "/_plugins/_ml/connectors/_create":
            return _R(200, {"connector_id": "C1"})
        if ruta == "/_plugins/_ml/models/_register":
            estado["modelo"] = "M1"
            return _R(200, {"model_id": "M1"})
        if ruta.startswith("/_plugins/_ml/models/M1"):
            return _R(200, {"model_state": "DEPLOYED"})
        if ruta == "/_search/pipeline/plataforma-rag":
            if method == "PUT":
                estado["pipeline"] = True
            return _R(200) if estado["pipeline"] else _R(404)
        if ruta.startswith("/conocimiento-plataforma/_search"):
            if not estado["indice"]:
                return _R(404)
            if "search_pipeline" in ruta:
                return _R(200, {"ext": {"retrieval_augmented_generation": {"answer": "Los domingos."}},
                                "hits": {"hits": [_hit("Mantenimiento.pdf", 3.0, "los domingos de 02 a 05")]}})
            return _R(200, {"hits": {"hits": [{"_source": {"titulo": "Mantenimiento.pdf", "adjunto": {"content_type": "application/pdf"}}}]}})
        return _R(200, {"acknowledged": True})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_cluster_del_entorno", lambda stage: ("http://x:9200", "admin", "pw"))
    monkeypatch.setattr("maas_integrator.get_maas_api_key", lambda: "CLAVE")
    from fastapi.testclient import TestClient
    return estado, TestClient(main.app)


_B64 = base64.b64encode(b"%PDF-1.4 algo").decode()


def test_subir_manda_el_archivo_al_pipeline_de_ingesta(cluster):
    estado, client = cluster
    r = client.post("/api/v1/conocimiento/documentos", json={"nombre": "Mantenimiento.pdf", "contenido_b64": _B64})
    assert r.status_code == 200 and r.json() == {"titulo": "Mantenimiento.pdf"}
    hechos = {(m, p.split("?")[0]): (p, b) for m, p, b in estado["pedidos"]}
    assert hechos[("PUT", "/_ingest/pipeline/conocimiento-ingesta")][1] == kb.build_pipeline_de_ingesta()
    ruta, cuerpo = hechos[("POST", "/conocimiento-plataforma/_doc")]
    assert "pipeline=conocimiento-ingesta" in ruta
    assert cuerpo == {"titulo": "Mantenimiento.pdf", "archivo": _B64}, "la plataforma no procesa nada"
    borrar = [b for m, p, b in estado["pedidos"] if "_delete_by_query" in p]
    assert borrar == [{"query": {"term": {"titulo.k": "Mantenimiento.pdf"}}}], "el mismo nombre reemplaza"
    # La segunda vez el índice ya existe: no es un error.
    assert client.post("/api/v1/conocimiento/documentos", json={"nombre": "Mantenimiento.pdf", "contenido_b64": _B64}).status_code == 200


def test_lo_que_falla_en_la_ingesta_se_dice(cluster):
    estado, client = cluster
    # Como en CSS 3.4: el `fail` del pipeline vuelve como 500.
    estado["ingesta_falla"] = '{"error":{"reason":"el documento no tiene texto"}}'
    r = client.post("/api/v1/conocimiento/documentos", json={"nombre": "escaneado.pdf", "contenido_b64": _B64})
    assert r.status_code == 400 and r.json()["detail"]["message"] == "escaneado.pdf: no tiene texto (¿es un PDF escaneado o una imagen?)"


def test_un_formato_no_soportado_no_llega_al_cluster(cluster):
    estado, client = cluster
    r = client.post("/api/v1/conocimiento/documentos", json={"nombre": "a.exe", "contenido_b64": _B64})
    assert r.status_code == 400 and "a.exe: formato no soportado" in r.json()["detail"]["message"]
    assert estado["pedidos"] == []


def test_preguntar_crea_el_rag_la_primera_vez(cluster):
    estado, client = cluster
    estado["indice"] = True
    r = client.post("/api/v1/conocimiento/preguntar", json={"pregunta": "¿Cuándo reinicio?"})
    assert r.status_code == 200 and r.json() == {"respuesta": "Los domingos.",
                                                 "fuentes": [{"titulo": "Mantenimiento.pdf", "extracto": "los domingos de 02 a 05"}]}
    hechos = [(m, p) for m, p, _ in estado["pedidos"]]
    assert ("POST", "/_plugins/_ml/connectors/_create") in hechos and ("PUT", "/_search/pipeline/plataforma-rag") in hechos
    estado["pedidos"].clear()
    client.post("/api/v1/conocimiento/preguntar", json={"pregunta": "¿Y en feriados?"})
    assert not [p for m, p, _ in estado["pedidos"] if "_create" in p or "_register" in p]
    # El pipeline se pone siempre: uno de una versión anterior pedía otro campo.
    assert ("PUT", "/_search/pipeline/plataforma-rag", kb.build_pipeline("M1")) in estado["pedidos"]


def test_sin_documentos_avisa(cluster):
    _, client = cluster
    r = client.post("/api/v1/conocimiento/preguntar", json={"pregunta": "¿algo?"})
    assert r.status_code == 200 and "Todavía no hay documentos" in r.json()["aviso"]
    assert client.get("/api/v1/conocimiento/documentos").json() == {"documentos": []}


def test_listar_y_borrar(cluster):
    estado, client = cluster
    estado["indice"] = True
    assert client.get("/api/v1/conocimiento/documentos").json() == {
        "documentos": [{"titulo": "Mantenimiento.pdf", "tipo": "application/pdf", "caracteres": None}]}
    assert client.delete("/api/v1/conocimiento/documentos/Mantenimiento.pdf").json() == {"borrados": 1}


# ── La vista ────────────────────────────────────────────────────────────────
_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"


def test_la_pestana_esta_y_carga_sola():
    html = _INDEX.read_text(encoding="utf-8")
    assert "{ id: 'documentos', label: 'Documentos', icon: 'file', html: documentosHTML() }," in html
    assert "documentos: ['#infra-documentos-actualizar']," in html
    assert '<symbol id="ic-file"' in html
    # Acepta lo mismo que valida el backend.
    i = html.index("const _EXT_DOCUMENTOS = '") + len("const _EXT_DOCUMENTOS = '")
    assert tuple(html[i:html.index("'", i)].split(",")) == kb.EXTENSIONES


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_vista_en_node(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    const _TIPO_DOC = {")
    fns = html[i:html.index("    async function cargarDocumentos() {", i)]
    js = tmp_path / "docs.mjs"
    js.write_text(r"""
const icon = (n) => `<i:${n}>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
""" + fns + r"""
const f = [];
const check = (n, c, x) => { if (!c) f.push(n + ' -> ' + x); };
let h = documentosRespuestaHTML({ respuesta: 'Al <CISO>', fuentes: [{ titulo: 'R.pdf', extracto: 'ante un acceso' }] });
check('respuesta escapada', h.includes('<div class="docs__respuesta">Al &lt;CISO&gt;</div>'), h);
check('consultados con su pasaje', h.includes('Documentos consultados') && h.includes('<strong>R.pdf</strong>') && h.includes('…ante un acceso…'), h);
check('sin fuentes, sin título', !documentosRespuestaHTML({ respuesta: 'x', fuentes: [] }).includes('Documentos consultados'));
check('aviso', documentosRespuestaHTML({ respuesta: '', fuentes: [], aviso: 'Ningún documento' }).includes('Ningún documento'));
const l = documentosListaHTML([{ titulo: 'a "b".pdf', tipo: 'application/pdf' }, { titulo: 'c', tipo: '' }]);
check('lista con el tipo', l.includes('>PDF<') && l.includes('data-titulo="a &quot;b&quot;.pdf"'), l);
check('vacía', documentosListaHTML([]).includes('Todavía no hay documentos'));
console.log(f.join('\n')); process.exit(f.length ? 1 : 0);
""", encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
