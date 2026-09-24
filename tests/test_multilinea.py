"""Un CSV con campos de varias líneas se ingiere como un documento por registro.

Lo destapó "reviews-ordenes" (el dataset público de reseñas de Olist): los
comentarios traen saltos de línea entre comillas, Logstash lee línea por línea
y cada reseña de varias líneas terminaba en varios documentos. Aparecían ~8.700
reseñas "sin puntaje", puntajes 18 y 90, y totales inflados. Ahora el perfilador
agrupa los registros, deduce con qué empieza cada uno y el input s3 lleva un
`codec => multiline` con ese patrón.
"""
import pathlib
import re

import pytest

import conf_lint
import main
import perfilador

_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"

# Como el CSV de Olist: comillas en todo, comentarios con saltos de línea (y
# una línea vacía adentro de uno).
OLIST = [
    '"review_id","order_id","review_score","review_comment_title","review_comment_message","review_creation_date"',
    '"7bc2406110b926393aa56f80a40eba40","73fc7af87114b39712e6da79b0a377eb",4,,,"2018-01-18 00:00:00"',
    '"80e641a11e56f04c1ad469d5645fdfde","a548910a1c6147796b98fdf73dbeba33",5,,"Recebi bem antes do prazo.","2018-03-10 00:00:00"',
    '"e64fb393e7b32834bb789ff8bb30750e","658677c97b385a9be170737859d3511b",1,"Ruim","Não recebi o produto.',
    'Péssimo atendimento.',
    '',
    'Não comprem","2017-04-21 00:00:00"',
    '"f7c4243c7fe1938f181bec41a392bdeb","8e6bfb81e283fa7e4f11123a3fb894f1",5,,"Adorei, disse ""ótimo""","2018-03-01 00:00:00"',
    '"15197aa66ff4d0650b5434f1b46cda19","b18dcdf73be66366873cd26c5724d1dc",1,,"Aguardando',
    'entrega","2018-04-13 00:00:00"',
]


# ── Los registros ───────────────────────────────────────────────────────────
def test_las_lineas_de_un_registro_se_juntan():
    registros = perfilador._registros(OLIST)
    assert len(registros) == 6        # header + 5 reseñas
    assert registros[3].count("\n") == 3 and registros[3].endswith('"2017-04-21 00:00:00"')
    assert "\n\n" in registros[3], "la línea vacía adentro del comentario queda"


def test_las_comillas_escapadas_no_abren_nada():
    assert perfilador._registros(['a,"dijo ""hola""",b', "c,d,e"]) == ['a,"dijo ""hola""",b', "c,d,e"]


def test_un_registro_que_la_muestra_corto_se_descarta():
    assert perfilador._registros(['a,b', '"x","empieza', 'y sigue']) == ["a,b"]


def test_el_perfil_de_olist():
    p = perfilador.perfilar(OLIST)
    assert (p.formato, p.filas, p.multilinea) == ("delimitado", 5, True)
    assert p.inicio_registro == '^"?[0-9a-fA-F]{32}"?,'
    assert [c.nombre for c in p.columnas][:3] == ["review_id", "order_id", "review_score"]
    assert p.columna("review_score").tipo == "integer", "sin basura de las continuaciones"
    assert p.columna("review_creation_date").tipo == "date"
    v = perfilador.verificar(p)
    assert (v.total, v.ok) == (5, 5)


# ── El patrón de inicio ─────────────────────────────────────────────────────
@pytest.mark.parametrize("primera, patron", [
    (["1001", "1002", "1003"], r'^"?\d+"?,'),
    (["2024-01-02 10:00:00", "2024-01-03 11:00:00", "2024-01-04 12:00:00"],
     r'^"?\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"?,'),
    (["0b8e2c5a-1f3d-4e6a-9c7b-2d4f6a8b0c1e", "1c9f3d6b-2a4e-5f7b-8d0c-3e5a7b9c1d2f",
      "2d0a4e7c-3b5f-6a8c-9e1d-4f6b8c0d2e3a"],
     '^"?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"?,'),
])
def test_el_inicio_sale_de_la_primera_columna(primera, patron):
    # `momento` y no `id`: una columna que se llama id queda texto a propósito.
    lineas = ["momento,nota,valor"]
    for i, v in enumerate(primera):
        lineas += [f'{v},"linea uno', f'linea dos",{i}']
    p = perfilador.perfilar(lineas)
    assert p.multilinea and p.inicio_registro == patron


def test_sin_un_inicio_confiable_no_hay_patron():
    """Una continuación que parece un inicio pegaría mal: mejor ningún patrón
    (y el aviso) que uno que parte los registros en otro lado."""
    lineas = ["id,nota,valor",
              '1001,"primera', '1005,parece un inicio",1',
              '1002,"otra', 'sigue",2']
    p = perfilador.perfilar(lineas)
    assert p.multilinea and p.inicio_registro == ""


def test_texto_libre_en_la_primera_columna_no_da_patron():
    lineas = ["nombre,nota", 'Ana,"hola', 'chau"', 'Juan,"x', 'y"']
    assert perfilador.perfilar(lineas).inicio_registro == ""


def test_un_csv_de_una_linea_por_registro_no_cambia():
    p = perfilador.perfilar(["a,b,c", "1,x,2", "2,y,3"])
    assert (p.multilinea, p.inicio_registro) == (False, "")


def test_una_comilla_suelta_no_rompe_la_tabla():
    """Si juntar deja una tabla inconsistente, se vuelve a partir por línea."""
    lineas = ["a,b,c", '1,5" pulgadas,2', "2,x,3", "3,y,4", "4,z,5"]
    p = perfilador.perfilar(lineas)
    assert p.formato == "delimitado" and p.filas == 4


# ── El input ────────────────────────────────────────────────────────────────
def test_el_input_s3_lleva_el_codec_multiline():
    bloque = main.gen_input_s3(main.S3InputConfig(bucket="b", prefix="r/",
                                                   multiline_pattern='^"?[0-9a-fA-F]{32}"?,'))
    assert "codec => multiline {" in bloque
    assert "pattern => '^\"?[0-9a-fA-F]{32}\"?,'" in bloque
    assert "negate => true" in bloque and 'what => "previous"' in bloque
    assert "auto_flush_interval => 2" in bloque, "el último registro del archivo tiene que salir"
    assert "codec => plain" not in bloque
    conf = ("input {\n" + bloque + "\n}\nfilter {}\noutput {\n  elasticsearch {\n"
            "    hosts => []\n  }\n}\n")
    assert not [p for p in conf_lint.lint(conf) if p.nivel == conf_lint.ERROR]


def test_sin_patron_el_input_queda_como_siempre():
    bloque = main.gen_input_s3(main.S3InputConfig(bucket="b", prefix="r/"))
    assert "codec => plain" in bloque and "multiline" not in bloque


def test_un_patron_con_comilla_simple_no_se_escribe():
    """Iría entre comillas simples: una adentro rompería el .conf."""
    bloque = main.gen_input_s3(main.S3InputConfig(bucket="b", prefix="r/", multiline_pattern="^a'b"))
    assert "multiline" not in bloque


def test_el_deploy_de_un_caso_saca_el_patron_de_su_dataset(monkeypatch, tmp_path):
    """Así un caso ya guardado (como reviews-ordenes) se ingiere bien sin
    volver a crearlo."""
    dataset = tmp_path / "reviews.csv"
    dataset.write_text("\n".join(OLIST) + "\n", encoding="utf-8")
    monkeypatch.setattr(main.custom_cases, "dataset_path", lambda slug: dataset)
    monkeypatch.setattr(main.custom_cases, "case_type_for", lambda slug: "dataset")
    req = main.TerraformDeployRequest(pipeline_conf="input {} output {}", obs_bucket="demos",
                                      obs_access_key="ak", obs_secret_key="sk")
    caso = main.PipelineCase(slug="reviews-ordenes", filter_code="filter {}",
                             index_name="r-%{+YYYY.MM}", obs_prefix="reviews-ordenes-logs/")

    conf = main._build_pipeline_conf_for_case(caso, req)

    assert "codec => multiline {" in conf and "[0-9a-fA-F]{32}" in conf


def test_un_caso_sin_dataset_no_lleva_codec(monkeypatch):
    monkeypatch.setattr(main.custom_cases, "dataset_path", lambda slug: None)
    assert main._inicio_de_registro_del_caso("fintech") == ""


# ── De punta a punta: /generate-filter y el front ───────────────────────────
def _sin_llm(monkeypatch):
    """La semántica sin LLM: acá solo importa el .conf y el patrón."""
    monkeypatch.setattr(main.semantica, "enriquecer", lambda perfil: type(
        "S", (), {"nota": "", "preguntas": [], "filas": "", "fuente": "heuristica"})())


def test_generate_filter_devuelve_el_inicio(monkeypatch):
    _sin_llm(monkeypatch)
    res = main.generate_filter_endpoint(main.GenerateFilterRequest(raw_log="\n".join(OLIST)))
    assert res.inicio_registro == '^"?[0-9a-fA-F]{32}"?,'
    assert res.verificacion.multilinea is True
    assert (res.verificacion.filas, res.verificacion.filas_ok) == (5, 5)


def test_generate_filter_avisa_si_no_hay_inicio(monkeypatch):
    _sin_llm(monkeypatch)
    lineas = ["nombre,nota,valor", 'Ana,"hola', 'chau",1', 'Juan,"x', 'y",2']
    res = main.generate_filter_endpoint(main.GenerateFilterRequest(raw_log="\n".join(lineas)))
    assert res.inicio_registro == ""
    assert any("varias líneas" in p for p in res.verificacion.problemas)


def test_el_front_manda_el_patron_en_el_input_y_lo_limpia():
    html = _INDEX.read_text(encoding="utf-8")
    assert "state.inicioRegistro = data.inicio_registro || '';" in html
    i = html.index("function collectInputConfig()")
    fn = html[i:html.index("\n    }\n", i)]
    assert re.search(r"config\.multiline_pattern = state\.inicioRegistro;", fn)
    j = html.index("function clearMapping()")
    assert "state.inicioRegistro = '';" in html[j:html.index("}", j)]


def test_un_hex_de_largo_variable_no_es_un_id():
    """Un hash tiene largo fijo; hex de largos distintos puede ser texto."""
    lineas = ["codigo,nota,valor", 'abcdef0123,"uno', 'dos",1', 'abcdef01234,"tres', 'cuatro",2']
    assert perfilador.perfilar(lineas).inicio_registro == ""


def test_un_registro_sin_primera_columna_anula_el_patron():
    """Esa línea no calzaría con el inicio y se pegaría al registro anterior."""
    lineas = ["codigo,nota,valor",
              '7bc2406110b926393aa56f80a40eba40,"uno', 'dos",1',
              ',"sin codigo', 'sigue",2',
              '80e641a11e56f04c1ad469d5645fdfde,"tres', 'cuatro",3']
    assert perfilador.perfilar(lineas).inicio_registro == ""
