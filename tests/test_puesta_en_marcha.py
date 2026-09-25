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
    for boton, texto, rehacer in (("infra-apply-schema-btn", "Aplicar index template + dashboards", "Volver a aplicar"),
                                  ("infra-ingest-btn", "Iniciar ingesta con Logstash", "Reiniciar ingesta"),
                                  ("infra-capabilities-btn", "Provisionar plugins", "Volver a provisionar plugins")):
        assert f"'{boton}', '{texto}', '{rehacer}')" in vista, boton


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


# ── Plugins ─────────────────────────────────────────────────────────────────
def test_capabilities_se_llama_plugins_en_lo_que_se_ve():
    visibles = ["Capabilities de OpenSearch", "Provisionar capabilities", "provisionar capabilities",
                "capabilities provisionadas",
                "label: 'Capabilities'", "→ ingesta → capabilities"]
    assert not [v for v in visibles if v in HTML]
    assert "'Plugins de OpenSearch'" in _vista() and "label: 'Plugins'" in HTML
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert "Corré 'Provisionar plugins'" in src and "Plugins provisionados" in src


# ── Verificar ingesta ───────────────────────────────────────────────────────
def test_verificar_ingesta_es_rojo_y_dice_todos_los_pipelines():
    vista = _vista()
    assert '<button class="btn btn-primary btn-sm" id="infra-verify-btn">Verificar ingesta</button>' in vista
    i = HTML.index("async function verificarIngesta(btn)")
    assert "'Todos los pipelines tienen documentos.'" in HTML[i:i + 4000]
    assert "Todas las pipelines" not in HTML
