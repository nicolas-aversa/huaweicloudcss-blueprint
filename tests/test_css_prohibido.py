"""CSS rechaza todo configuration file con el texto `call(`.

Lo destapó un dataset nuevo: el ruby de limpieza de vacíos invocaba su lambda
con `limpiar.call(v)` y CSS respondió "CSS.0001 : Incorrect parameters. (config
is forbidden, change it.)" a los 7 minutos del apply. Probado contra la API de
CSS (la-south-2, 2026-09-24): `f.call(1)`, `x.call(1)` y hasta `a = "call("` se
rechazan; `f.call 1` y `f[1]` pasan. El generador ya no lo escribe, los casos
guardados se corrigen al desplegar y el deploy lo frena antes de empezar.
"""
import json

import pytest

import conf_lint
import main
import perfilador
import raiz

_BASE = ('input {\n  beats {\n    port => 5044\n  }\n}\n\nfilter {\n%s\n}\n\n'
         'output {\n  elasticsearch {\n    hosts => []\n    index => "x"\n  }\n}\n')


def _ruby(codigo: str) -> str:
    return "  ruby {\n    code => '\n" + codigo + "\n    '\n  }"


# ── Lo que genera la plataforma ─────────────────────────────────────────────
_CORPUS = {
    "jsonl": [json.dumps({"order_id": f"O-{i}", "rating": i % 5 + 1, "total": f"{1000 + i},50",
                          "detalle": {"canal": "web", "nota": "" if i % 2 else "ok"},
                          "created_at": "2026-09-10 10:00:00"}) for i in range(12)],
    "csv": ["order_id,rating,total,created_at"]
           + [f"O-{i},{i % 5 + 1},{1000 + i},2026-09-10 10:00:00" for i in range(12)],
    "kv": [f'ts="2026-09-10 10:00:0{i % 9}" user=u{i} status=OK bytes={i * 10}' for i in range(12)],
}


@pytest.mark.parametrize("formato", sorted(_CORPUS))
def test_el_generador_no_escribe_call(formato):
    perfil = perfilador.perfilar(_CORPUS[formato])
    codigo, _ = raiz.promover(perfilador.armar_filter(perfil, "data"),
                              perfilador.campos(perfil, "data"))
    assert "call(" not in codigo
    assert not [p for p in conf_lint.lint_filtro(codigo) if p.nivel == conf_lint.ERROR]


def test_la_limpieza_sigue_limpiando_con_corchetes():
    """`limpiar[v]` es la misma invocación del lambda (incluida la recursión)."""
    bloque = "\n".join(perfilador._ruby_limpieza("data"))
    assert "limpiar[v]" in bloque and "limpiar[d]" in bloque
    assert "call" not in bloque


# ── El chequeo antes del deploy ─────────────────────────────────────────────
@pytest.mark.parametrize("codigo", [
    "f = lambda { |x| x }\nf.call(1)",
    'a = "call("',                           # hasta en un string
    "# no usar x.call(1)\nb = 1",            # y en un comentario
])
def test_lint_marca_call_donde_este(codigo):
    errores = [p for p in conf_lint.lint(_BASE % _ruby(codigo)) if p.nivel == conf_lint.ERROR]
    assert any("call(" in p.mensaje and "config is forbidden" in p.mensaje for p in errores), errores


@pytest.mark.parametrize("codigo", ["f = lambda { |x| x }\nf[1]", "f = lambda { |x| x }\nf.call 1",
                                    'a = "llamada"'])
def test_lint_deja_pasar_lo_que_css_acepta(codigo):
    assert not [p for p in conf_lint.lint(_BASE % _ruby(codigo)) if p.nivel == conf_lint.ERROR]


def test_la_linea_del_error_es_la_del_call():
    conf = _BASE % _ruby("a = 1\nb = 2\nf.call(1)")
    [p] = [p for p in conf_lint.lint(conf) if "call(" in p.mensaje]
    assert conf.splitlines()[p.linea - 1].strip() == "f.call(1)"


def test_con_error_de_sintaxis_igual_avisa_el_call():
    conf = "input { beats { port => 5044 } }\nfilter { ruby { code => 'f.call(1)' }\n"
    mensajes = [p.mensaje for p in conf_lint.lint(conf)]
    assert any("call(" in m for m in mensajes) and len(mensajes) >= 2


# ── Los casos guardados se corrigen solos ───────────────────────────────────
def test_normalizar_pasa_call_a_corchetes():
    viejo = "      limpiar = lambda do |h|\n        limpiar.call(v)\n      end\n      limpiar.call(d)\n"
    nuevo, notas = conf_lint.normalizar(viejo)
    assert "limpiar[v]" in nuevo and "limpiar[d]" in nuevo and "call(" not in nuevo
    assert any("call(" in n for n in notas)
    assert conf_lint.normalizar("f.call()")[0] == "f[]"


def test_normalizar_no_adivina_parentesis_anidados():
    """`f.call(g(1))` no se toca: el lint lo frena con el motivo."""
    assert conf_lint.normalizar("x = f.call(g(1))")[0] == "x = f.call(g(1))"


def test_el_conf_viejo_de_un_caso_guardado_despliega_corregido():
    viejo_filter = ("filter {\n" + _ruby("limpiar = lambda do |h|\n  h\nend\nd = {}\nlimpiar.call(d)")
                    + "\n}\n")
    req = main.TerraformDeployRequest(
        pipeline_conf=_BASE % _ruby("limpiar = lambda { |h| h }\nlimpiar.call(1)"),
        cases=[{"slug": "reviews-ordenes", "raw_log": "", "filter_code": viejo_filter,
                "fields": [], "index_name": "r-%{+YYYY.MM}", "obs_prefix": "r/"}])

    main._normalizar_conf(req)

    assert "call(" not in req.pipeline_conf and "call(" not in req.cases[0].filter_code
    main._check_conf_compila(req)   # no corta


def test_un_call_que_no_se_puede_corregir_frena_el_deploy():
    req = main.TerraformDeployRequest(pipeline_conf=_BASE % _ruby("x = f.call(g(1))"),
                                      pipeline_slug="x")
    main._normalizar_conf(req)
    with pytest.raises(main.HTTPException) as exc:
        main._check_conf_compila(req)
    assert "call(" in exc.value.detail["message"]


def test_lo_que_ya_corria_vuelve_a_terraform_corregido(monkeypatch, tmp_path):
    """Una pipeline del registro (desplegada antes del arreglo) viaja con su
    `.conf` guardado: sin normalizarlo ahí, CSS lo volvería a rechazar."""
    td = tmp_path / "terraform"
    td.mkdir()
    main._write_pipelines_registry(td, {"vieja": {
        "pipeline_conf": _BASE % _ruby("f = lambda { |x| x }\nf.call(1)"),
        "start_ingestion": False, "index": "v", "obs_prefix": "v/"}})
    monkeypatch.setattr(main, "_write_destroy_creds", lambda *a, **k: None)
    req = main.TerraformDeployRequest(pipeline_conf=_BASE % '  mutate { add_field => { "a" => "b" } }',
                                      pipeline_slug="nueva", opensearch_index="n")

    main._prepare_deploy_tfvars(req, td)

    tfvars = json.loads((td / "deploy.auto.tfvars.json").read_text(encoding="utf-8"))
    assert "call(" not in tfvars["pipelines"]["vieja"]["pipeline_conf"]
    assert "f[1]" in tfvars["pipelines"]["vieja"]["pipeline_conf"]
