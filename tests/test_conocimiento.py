"""Base de conocimiento: el usuario sube sus documentos, se parten e indexan en el
cluster del entorno, y un pipeline RAG de OpenSearch responde con ellos.
Lo medido en CSS 3.4 está en el docstring de conocimiento.py."""
import base64
import json
import pathlib
import shutil
import subprocess

import pytest

import conocimiento as kb
import main


# ── Extraer el texto ────────────────────────────────────────────────────────
def _pdf(texto: str) -> bytes:
    """Un PDF mínimo de una página con `texto` (sin dependencias)."""
    contenido = f"BT /F1 12 Tf 72 720 Td ({texto}) Tj ET".encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(contenido) + contenido + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_extrae_texto_de_pdf():
    assert "Ventana de mantenimiento los domingos" in kb.extraer_texto("politica.PDF", _pdf("Ventana de mantenimiento los domingos"))


def test_extrae_texto_plano_y_html():
    assert kb.extraer_texto("a.md", "# Título\n\n\n\nTexto   con  espacios".encode()) == "# Título\n\nTexto con espacios"
    assert kb.extraer_texto("a.txt", "año".encode("latin-1")) == "año", "no todo viene en UTF-8"
    h = kb.extraer_texto("a.html", b"<html><style>x{}</style><script>alert(1)</script><p>Hola &amp; chau</p></html>")
    assert h == "Hola & chau"


@pytest.mark.parametrize("nombre, contenido, motivo", [
    ("a.docx", b"x", "formato no soportado (.docx)"),
    ("sin_extension", b"x", "sin extensión"),
    ("a.txt", b"   \n  ", "no tiene texto"),
    ("a.pdf", b"no es un pdf", "no se pudo leer el PDF"),
    ("a.txt", b"x" * (kb.MAX_BYTES + 1), "pesa más de 10 MB"),
], ids=["docx", "sin-extension", "vacio", "pdf-roto", "muy-grande"])
def test_lo_que_no_se_puede_usar_se_dice(nombre, contenido, motivo):
    with pytest.raises(kb.DocumentoInvalido, match=motivo.replace("(", r"\(").replace(")", r"\)")):
        kb.extraer_texto(nombre, contenido)


# ── Partir en fragmentos ────────────────────────────────────────────────────
def test_fragmenta_por_parrafos_con_solape():
    parrafos = [f"Párrafo {i}. " + "palabra " * 60 for i in range(10)]
    partes = kb.fragmentar("\n\n".join(parrafos), tam=1200, solape=150)
    assert len(partes) > 1 and all(len(p) <= 1200 for p in partes)
    assert partes[1].startswith(partes[0][-150:]), "el siguiente arranca con el final del anterior"
    assert "Párrafo 9." in partes[-1]


def test_un_parrafo_enorme_se_corta_igual():
    partes = kb.fragmentar("x" * 5000, tam=1200, solape=0)
    assert [len(p) for p in partes] == [1200, 1200, 1200, 1200, 200]


def test_los_documentos_a_indexar():
    docs = kb.documentos_para_indexar("Runbook.md", "uno\n\ndos")
    assert docs == [{"titulo": "Runbook.md", "fragmento": 1, "fragmentos": 1, "texto": "uno\n\ndos"}]


def test_el_texto_se_analiza_en_tres_idiomas():
    """Con el análisis estándar "reiniciar" no encontraba "reinician"."""
    t = kb.mapping()["mappings"]["properties"]["texto"]
    assert t["fields"]["es"]["analyzer"] == "spanish" and t["fields"]["en"]["analyzer"] == "english"
    q = kb.busqueda("¿reinicio?", "m")
    assert {"texto", "texto.es", "texto.en"} <= set(q["query"]["multi_match"]["fields"])
    # El procesador lee el contexto del _source: tiene que venir.
    assert {"titulo", "texto"} <= set(q["_source"])
    assert q["ext"]["generative_qa_parameters"]["llm_question"] == "¿reinicio?"


def test_el_pipeline_le_pasa_el_titulo_y_prohibe_inventar():
    proc = kb.build_pipeline("M")["response_processors"][0]["retrieval_augmented_generation"]
    assert proc["model_id"] == "M" and proc["context_field_list"] == ["titulo", "texto"]
    assert "nunca inventes" in proc["system_prompt"]


def test_el_connector_recibe_los_mensajes_armados():
    c = kb.build_connector("K", "e", "deepseek-v4.1-flash")
    assert '"messages": ${parameters.messages}' in c["actions"][0]["request_body"]
    assert '"thinking": true' in c["actions"][0]["request_body"]


# ── La respuesta y sus fuentes ──────────────────────────────────────────────
def _hit(titulo, frag, score):
    return {"_score": score, "_source": {"titulo": titulo, "fragmento": frag, "fragmentos": 3, "texto": "t" * 400}}


def test_las_fuentes_son_las_relevantes_y_sin_repetir():
    resp = {"ext": {"retrieval_augmented_generation": {"answer": " Al CISO. "}},
            "hits": {"hits": [_hit("Runbook", 2, 4.0), _hit("Runbook", 2, 3.9), _hit("Runbook", 1, 2.5), _hit("Política", 1, 1.0)]}}
    r = kb.respuesta(resp)
    assert r["respuesta"] == "Al CISO."
    assert [(f["titulo"], f["fragmento"]) for f in r["fuentes"]] == [("Runbook", 2), ("Runbook", 1)], \
        "la de menos de la mitad del mejor puntaje no se muestra"
    assert len(r["fuentes"][0]["extracto"]) == 280


def test_el_listado():
    assert kb.listado({"aggregations": {"docs": {"buckets": [{"key": "a.md", "doc_count": 3}]}}}) == \
        [{"titulo": "a.md", "fragmentos": 3}]


# ── Los endpoints ───────────────────────────────────────────────────────────
class _R:
    def __init__(self, status, data=None, text=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = text if text is not None else json.dumps(self._d)

    def json(self):
        return self._d


@pytest.fixture
def cluster(monkeypatch):
    estado = {"pedidos": [], "bulk": None, "indice": False, "modelo": None, "pipeline": False}

    def req(method, url, user, password, json_body=None, timeout=30):
        ruta = url.split(":9200", 1)[1]
        estado["pedidos"].append((method, ruta, json_body))
        if ruta == "/conocimiento-plataforma" and method == "PUT":
            ya = estado["indice"]; estado["indice"] = True
            return _R(400, text="resource_already_exists_exception") if ya else _R(200)
        if ruta.startswith("/conocimiento-plataforma/_delete_by_query"):
            return _R(200, {"deleted": 2})
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
                                "hits": {"hits": [_hit("Mantenimiento.md", 1, 3.0)]}})
            return _R(200, {"aggregations": {"docs": {"buckets": [{"key": "Mantenimiento.md", "doc_count": 1}]}}})
        return _R(200, {"acknowledged": True})

    def bulk(base, user, password, ndjson):
        estado["bulk"] = ndjson
        return _R(200, {"errors": False})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main, "_os_bulk", bulk)
    monkeypatch.setattr(main, "_cluster_del_entorno", lambda stage: ("http://x:9200", "admin", "pw"))
    monkeypatch.setattr("maas_integrator.get_maas_api_key", lambda: "CLAVE")
    from fastapi.testclient import TestClient
    return estado, TestClient(main.app)


def test_subir_indexa_los_fragmentos_y_reemplaza_el_anterior(cluster):
    estado, client = cluster
    b64 = base64.b64encode("Los domingos de 02 a 05.".encode()).decode()
    r = client.post("/api/v1/conocimiento/documentos", json={"nombre": "Mantenimiento.md", "contenido_b64": b64})
    assert r.status_code == 200 and r.json() == {"titulo": "Mantenimiento.md", "fragmentos": 1, "caracteres": 24}
    lineas = estado["bulk"].strip().split("\n")
    assert json.loads(lineas[0]) == {"index": {"_index": "conocimiento-plataforma"}}
    assert json.loads(lineas[1])["texto"] == "Los domingos de 02 a 05."
    borrar = [p for p in estado["pedidos"] if "_delete_by_query" in p[1]]
    assert borrar[0][2] == {"query": {"term": {"titulo.k": "Mantenimiento.md"}}}, "el mismo nombre reemplaza"
    # La segunda vez el índice ya existe: no es un error.
    assert client.post("/api/v1/conocimiento/documentos", json={"nombre": "Mantenimiento.md", "contenido_b64": b64}).status_code == 200


def test_un_archivo_invalido_es_400_con_el_motivo(cluster):
    _, client = cluster
    r = client.post("/api/v1/conocimiento/documentos",
                    json={"nombre": "a.docx", "contenido_b64": base64.b64encode(b"x").decode()})
    assert r.status_code == 400 and "a.docx: formato no soportado" in r.json()["detail"]["message"]


def test_preguntar_crea_el_rag_la_primera_vez(cluster):
    estado, client = cluster
    estado["indice"] = True
    r = client.post("/api/v1/conocimiento/preguntar", json={"pregunta": "¿Cuándo reinicio?"})
    assert r.status_code == 200 and r.json()["respuesta"] == "Los domingos."
    assert r.json()["fuentes"][0]["titulo"] == "Mantenimiento.md"
    hechos = [(m, p) for m, p, _ in estado["pedidos"]]
    assert ("POST", "/_plugins/_ml/connectors/_create") in hechos and ("PUT", "/_search/pipeline/plataforma-rag") in hechos
    # La segunda vez ya está todo: no se crea nada.
    estado["pedidos"].clear()
    client.post("/api/v1/conocimiento/preguntar", json={"pregunta": "¿Y en feriados?"})
    assert not [p for m, p, _ in estado["pedidos"] if "_create" in p or "_register" in p or m == "PUT"]


def test_sin_documentos_avisa(cluster):
    _, client = cluster
    r = client.post("/api/v1/conocimiento/preguntar", json={"pregunta": "¿algo?"})
    assert r.status_code == 200 and "Todavía no hay documentos" in r.json()["aviso"]
    assert client.get("/api/v1/conocimiento/documentos").json() == {"documentos": []}


def test_borrar(cluster):
    _, client = cluster
    r = client.delete("/api/v1/conocimiento/documentos/Mantenimiento.md")
    assert r.status_code == 200 and r.json() == {"borrados": 2}


# ── La vista ────────────────────────────────────────────────────────────────
_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"


def test_la_pestana_esta_y_carga_sola():
    html = _INDEX.read_text(encoding="utf-8")
    assert "{ id: 'documentos', label: 'Documentos', icon: 'file', html: documentosHTML() }," in html
    assert "documentos: ['#infra-documentos-actualizar']," in html
    assert '<symbol id="ic-file"' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_respuesta_en_node(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function documentosListaHTML(docs) {")
    fns = html[i:html.index("    async function cargarDocumentos() {", i)]
    js = tmp_path / "docs.mjs"
    js.write_text(r"""
const icon = (n) => `<i:${n}>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
""" + fns + r"""
const f = [];
const check = (n, c, x) => { if (!c) f.push(n + ' -> ' + x); };
let h = documentosRespuestaHTML({ respuesta: 'Al <CISO>', fuentes: [{ titulo: 'R.pdf', fragmento: 3, fragmentos: 12, extracto: 'ante un acceso' }] });
check('respuesta escapada', h.includes('<div class="docs__respuesta">Al &lt;CISO&gt;</div>'), h);
check('documentos consultados', h.includes('Documentos consultados') && h.includes('<strong>R.pdf</strong>') && h.includes('fragmento 3 de 12'), h);
h = documentosRespuestaHTML({ respuesta: 'x', fuentes: [{ titulo: 'a', fragmento: 1, fragmentos: 1, extracto: 'e' }] });
check('un solo fragmento no se numera', !h.includes('fragmento 1 de 1'), h);
check('sin fuentes, sin título', !documentosRespuestaHTML({ respuesta: 'x', fuentes: [] }).includes('Documentos consultados'));
check('aviso', documentosRespuestaHTML({ respuesta: '', fuentes: [], aviso: 'Ningún documento' }).includes('Ningún documento'));
const l = documentosListaHTML([{ titulo: 'a "b".md', fragmentos: 1 }]);
check('lista', l.includes('1 fragmento<') && l.includes('data-titulo="a &quot;b&quot;.md"'), l);
check('vacía', documentosListaHTML([]).includes('Todavía no hay documentos'));
console.log(f.join('\n')); process.exit(f.length ? 1 : 0);
""", encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
