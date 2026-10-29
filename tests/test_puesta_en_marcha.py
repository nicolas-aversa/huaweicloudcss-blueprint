"""La puesta en marcha: cada paso se puede rehacer, botones de texto y a su
medida, mensajes con el estilo de la plataforma, "Plugins" en vez de
"Capabilities", y Verificar ingesta en rojo.
"""
import json
import pathlib
import re

import pytest

import main

_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"
HTML = _INDEX.read_text(encoding="utf-8")
CSS = HTML[:HTML.index("</style>")]


def _regla(selector: str) -> str:
    i = CSS.index(f"    {selector} {{")
    return CSS[i:CSS.index("}", i)]


def _vista() -> str:
    i = HTML.index("function renderInfraView(")
    return HTML[i:HTML.index("// Acciones: reusan las funciones del flujo.", i)]


# ── Mensajes ────────────────────────────────────────────────────────────────
def test_hecho_y_error_se_ven_como_la_plataforma():
    """Eran recuadros verde y rojo; ahora, la superficie de la casa con un
    indicador chico a la izquierda."""
    regla = _regla(".status-success, .status-error")
    assert "background: var(--bg-primary)" in regla and "border: 1px solid var(--border-subtle)" in regla
    assert "rgba(" not in regla
    assert 'content: "✓"; background: var(--accent-green)' in CSS
    assert 'content: "!"; background: var(--accent)' in CSS


def test_los_mensajes_no_traen_su_propio_icono():
    llamadas = [l for l in HTML.splitlines() if "showStatus(" in l and ("'success')" in l or "'error')" in l)]
    assert llamadas
    assert not [l for l in llamadas if re.search(r"['`](✓|⚠) ", l)]


# ── Los tres pasos ──────────────────────────────────────────────────────────
def test_cada_paso_hecho_se_puede_rehacer():
    vista = _vista()
    i = vista.index("const setupStep = ")
    paso = vista[i:vista.index("</div>`;", i)]
    hecho = paso[paso.index("${done"):paso.index(": `<button class=\"btn ${enabled")]
    assert "Hecho" in hecho and 'id="${btnId}">${redoLabel}</button>' in hecho
    assert "'infra-apply-schema-btn', paso1.boton, 'Volver a aplicar')" in vista
    # El de la ingesta nombra los casos nuevos (ver el test de abajo).
    assert "'infra-ingest-btn', ingestaBoton, 'Reiniciar ingesta'" in vista
    for boton, texto, rehacer in (("infra-capabilities-btn", "Activar los plugins", "Volver a activar los plugins"),):
        assert f"'{boton}', '{texto}', '{rehacer}'" in vista or f"'{boton}', activando ? 'Activando…' : '{texto}', '{rehacer}'" in vista, boton


def test_los_botones_son_solo_texto_y_a_su_medida():
    vista = _vista()
    assert "icon('check') + ' Aplicar" not in vista and "icon('play') + ' Iniciar" not in vista
    assert "${icon('spark')} ${capsDone" not in vista
    assert "min-width: 300px" not in CSS


def test_la_puesta_en_marcha_queda_plegada_cuando_todo_esta_hecho():
    """Si desaparecía, no había dónde rehacer un paso."""
    vista = _vista()
    assert "const showSetup = pipelines.length > 0;" in vista
    assert "<details class=\"section-card setup-card\" ${allDone ? '' : 'open'}>" in vista


def test_reiniciar_la_ingesta_no_borra_el_indice():
    vista = HTML[HTML.index("function renderInfraView("):]
    assert "infraStartIngestion(e.currentTarget, { conservarIndices: ingestDone })" in vista
    i = HTML.index("async function infraStartIngestion(btn, { conservarIndices = false } = {})")
    fn = HTML[i:HTML.index("\n    }\n", i)]
    assert "clear_indices: !conservarIndices" in fn


class _Corte(Exception):
    pass


@pytest.mark.parametrize("limpiar", [True, False])
def test_clear_indices_decide_si_se_borra(monkeypatch, tmp_path, limpiar):
    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    monkeypatch.setattr(main, "get_huawei_settings", lambda: {
        "vpc_id": "v", "subnet_id": "s", "security_group_id": "g",
        "availability_zone": "la-south-2a", "region": "la-south-2"})
    borrados, subidas = [], []
    monkeypatch.setattr(main, "_clear_case_indices", lambda *a, **k: borrados.append(1))
    monkeypatch.setattr(main, "_do_obs_upload", lambda req: subidas.append(1))
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda d: {})

    def _cortar(d):
        raise _Corte()          # lo que importa ya pasó: no hace falta Terraform
    monkeypatch.setattr(main, "_backend_init_args", _cortar)
    req = main.TerraformDeployRequest(pipeline_conf="input {} filter {} output {}", pipeline_slug="r",
                                      opensearch_index="r", start_ingestion=True, clear_indices=limpiar)
    with pytest.raises(_Corte):
        list(main._deploy_stream_gen_raw(req, td, None, None, []))
    assert borrados == ([1] if limpiar else [])
    assert subidas == [], "la fase 2 nunca sube datos"


def test_iniciar_la_ingesta_de_un_caso_nuevo_no_borra_los_que_ya_ingerian(monkeypatch, tmp_path):
    """Un caso agregado a un entorno andando: tras recargar la página, "Iniciar
    ingesta" se rearma con TODAS las pipelines. Borraba los índices de las que
    ya ingerían, y Logstash no vuelve a leer lo que ya leyó: quedaban vacíos."""
    td = tmp_path / "terraform"
    (td / ".terraform" / "providers").mkdir(parents=True)
    monkeypatch.setattr(main, "get_huawei_settings", lambda: {
        "vpc_id": "v", "subnet_id": "s", "security_group_id": "g",
        "availability_zone": "la-south-2a", "region": "la-south-2"})
    main._write_pipelines_registry(td, {
        "viejo": {"pipeline_conf": "x", "start_ingestion": True, "index": "viejo-%{+YYYY.MM}"},
        "nuevo": {"pipeline_conf": "x", "start_ingestion": False, "index": "nuevo-%{+YYYY.MM}"}})
    borrados = []
    monkeypatch.setattr(main, "_delete_indices", lambda ep, pat, pw, https, user="admin": borrados.append(pat))
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda d: {"public_endpoint": "1.2.3.4:9200"})
    olvidadas = []
    monkeypatch.setattr(main, "_olvidar_preguntas_por_significado", lambda d, slugs: olvidadas.extend(slugs))

    def _cortar(d):
        raise _Corte()
    monkeypatch.setattr(main, "_backend_init_args", _cortar)
    req = main.TerraformDeployRequest(
        pipeline_conf="input {} filter {} output {}", opensearch_password="pw", start_ingestion=True,
        clear_indices=True, cases=[
            main.PipelineCase(slug="viejo", filter_code="filter {}", fields=[], index_name="viejo-%{+YYYY.MM}"),
            main.PipelineCase(slug="nuevo", filter_code="filter {}", fields=[], index_name="nuevo-%{+YYYY.MM}")])
    with pytest.raises(_Corte):
        list(main._deploy_stream_gen_raw(req, td, None, None, []))
    assert borrados == ["nuevo-*"], borrados
    assert olvidadas == ["nuevo"], "las preguntas de las que siguen igual tampoco se descartan"


def test_el_boton_de_ingesta_nombra_los_casos_nuevos(tmp_path):
    """"Iniciar ingesta con Logstash" no decía si arrancaba todo o solo lo
    nuevo. Con casos nuevos en un entorno que ya ingería, los nombra."""
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("node no está instalado")
    vista = _vista()
    i = vista.index("      const sinIngesta = ")
    logica = vista[i:vista.index("      const setupTotal = ", i)]
    arnes = logica + r"""
return { ingestaBoton, ingestaTexto };
"""
    js = tmp_path / "boton.mjs"
    js.write_text(r"""
const escapeHtml = s => String(s);
const nombreCaso = s => ({ 'seguimiento-de-envios': 'Seguimiento de envíos', fix: 'FIX' })[s] || s;
const calcular = (pipelines) => { """ + arnes + r""" };
const p = (slug, active) => ({ slug, active });
const salida = {
  todos: calcular([p('fix', false), p('pozos', false)]),
  uno: calcular([p('fix', true), p('pozos', true), p('seguimiento-de-envios', false)]),
  dos: calcular([p('fix', true), p('a', false), p('b', false)]),
};
console.log(JSON.stringify(salida));
""", encoding="utf-8")
    res = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert res.returncode == 0, res.stderr
    s = json.loads(res.stdout)
    assert s["todos"]["ingestaBoton"] == "Iniciar ingesta con Logstash"
    assert s["uno"]["ingestaBoton"] == "Iniciar ingesta de Seguimiento de envíos"
    assert "Las otras 2 pipelines siguen como están" in s["uno"]["ingestaTexto"]
    assert s["dos"]["ingestaBoton"] == "Iniciar ingesta de los 2 casos nuevos"
    assert s["dos"]["ingestaTexto"].startswith("Arranca a, b.") and "La otra pipeline sigue" in s["dos"]["ingestaTexto"]
    assert "'infra-ingest-btn', ingestaBoton, 'Reiniciar ingesta'" in vista and ": ingestaTexto," in vista


# ── Plugins ─────────────────────────────────────────────────────────────────
def test_capabilities_se_llama_plugins_en_lo_que_se_ve():
    visibles = ["Capabilities de OpenSearch", "Provisionar capabilities", "provisionar capabilities",
                "capabilities provisionadas",
                "label: 'Capabilities'", "→ ingesta → capabilities"]
    assert not [v for v in visibles if v in HTML]
    assert "'Plugins de OpenSearch'" in _vista() and "label: 'Plugins'" in HTML
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert "activalo con «Activar los plugins» (paso 3)" in src and "Plugins provisionados" in src


# ── El conteo de documentos ─────────────────────────────────────────────────
def test_los_documentos_se_cuentan_solos_y_se_pueden_actualizar():
    """Antes había que tocar "Verificar ingesta"; ahora se cuentan al abrir el
    entorno (de nuevo si pasó un minuto) y el botón de actualizar los recuenta."""
    vista = _vista()
    assert 'id="infra-docs-refresh" title="Contar de nuevo"' in vista
    i = HTML.index("async function contarDocumentos(")
    fn = HTML[i:HTML.index("function buildDeployBodyFromStatus(", i)]
    assert "Date.now() - state.pipelineHealthAt < _DOCS_VIGENTE_MS" in fn, "sin contar en cada repintado"
    assert "state.pipelineHealthAt = Date.now();" in fn.split("finally")[1], "aun si falla: sin reintentar en bucle"
    assert "toast(" not in fn, "solo, no avisa: lo dice el número"


def test_con_seguridad_el_paso_1_dice_que_crea_los_detectores():
    """El paso 1 crea el índice y los detectores de Security Analytics: su
    nombre lo tiene que decir, porque tienen que existir antes que los datos."""
    vista = _vista()
    i = vista.index("const paso1 = ")
    bloque = vista[i:vista.index("};", vista.index(": { titulo:", i)) + 2]
    assert "pipelines.some(p => SECURITY_SLUGS.has(p.slug))" in bloque
    con, sin = bloque.split(": { titulo:", 1)
    assert "'Index template, dashboards y detectores'" in con and "Security Analytics" in con
    assert "'Aplicar template, dashboards y detectores'" in con
    assert "'Index template + dashboards'" in sin and "'Aplicar index template + dashboards'" in sin
