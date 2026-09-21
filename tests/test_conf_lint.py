"""El `.conf` de Logstash leído en serio, no adivinado con regex.

Los casos de este archivo son los que rompían a las expresiones regulares que
había antes: un bloque con sub-bloques multilínea (el `_S3_BLOCK_RE` lo cortaba
en el primer `\\n  }`), un regex sin comillas con llaves adentro (descuadraba
cualquier contador de llaves) y una palabra clave dentro de un comentario.
"""
import conf_lint


# Un input s3 con un sub-bloque adentro: exactamente la forma que truncaba el
# regex viejo, que cortaba en el primer `\n  }` y no llegaba a ver el `bucket`.
CONF_S3_ANIDADO = '''input {
  s3 {
    access_key_id => "AK"
    additional_settings => {
      force_path_style => true
    }
    bucket => "demoscss"
    prefix => "sp500-logs/"
  }
}

filter {
  if [message] =~ /^Symbol,\\d{3}/ {
    drop {}
  }
  csv {
    separator => ","
    columns => ["a", "b"]
  }
}

output {
  elasticsearch {
    hosts => []
    index => "sp500-%{+YYYY.MM}"
  }
}
'''


# ── El scanner ──────────────────────────────────────────────────────────────
def test_el_scanner_separa_codigo_de_strings_comentarios_y_regex():
    conf = 'filter {\n  # bucket => "de mentira"\n  mutate { add_field => { "p" => "a/b" } }\n}\n'
    m = conf_lint.scan(conf)

    i = conf.index("# bucket")
    assert set(m[i:conf.index("\n", i)]) == {conf_lint.COMENTARIO}
    j = conf.index('"de mentira"')
    assert m[j] == conf_lint.COMENTARIO, "un string dentro de un comentario sigue siendo comentario"
    k = conf.index('"a/b"')
    assert set(m[k:k + 5]) == {conf_lint.STRING}, "la barra de un path no abre un regex"


def test_un_regex_sin_comillas_no_descuadra_las_llaves():
    """`/^\\d{3}/` trae una llave que no abre nada. Contándolas a mano, el
    archivo entero queda desbalanceado y el bloque siguiente se lee mal."""
    m = conf_lint.scan(CONF_S3_ANIDADO)
    i = CONF_S3_ANIDADO.index("/^Symbol")
    assert m[i] == conf_lint.REGEX
    assert m[CONF_S3_ANIDADO.index("\\d{3}") + 2] == conf_lint.REGEX, "la llave del regex es regex"

    assert [s.nombre for s in conf_lint.secciones(CONF_S3_ANIDADO)] == ["input", "filter", "output"]


def test_una_comilla_escapada_no_corta_el_string():
    conf = 'filter {\n  mutate { replace => { "q" => "dijo \\"hola\\" y siguió" } }\n}\n'
    m = conf_lint.scan(conf)
    assert m[conf.index("y siguió")] == conf_lint.STRING
    assert [b.nombre for b in conf_lint.secciones(conf)] == ["filter"]


# ── Bloques ─────────────────────────────────────────────────────────────────
def test_los_hashes_no_son_bloques_pero_los_plugins_anidados_si():
    """`match => { … }` es un valor, no un plugin. `drop {}` dentro de un `if` sí
    es un plugin, y el lint tiene que verlo aunque esté anidado."""
    nombres = [b.nombre for b in conf_lint.plugins(CONF_S3_ANIDADO)]

    assert "csv" in nombres and "drop" in nombres and "s3" in nombres
    assert "additional_settings" not in nombres, "un hash no es un plugin"
    assert "if" not in nombres and "else" not in nombres


def test_buscar_plugin_respeta_la_seccion():
    conf = ('input { s3 { bucket => "de-entrada" } }\n'
            'output { s3 { bucket => "de-salida" } }\n')

    entrada = conf_lint.buscar_plugin(conf, "s3", "input")
    salida = conf_lint.buscar_plugin(conf, "s3", "output")

    assert conf_lint.leer_setting(conf, entrada, "bucket") == "de-entrada"
    assert conf_lint.leer_setting(conf, salida, "bucket") == "de-salida"
    assert conf_lint.buscar_plugin(conf, "kafka", "input") is None


# ── Settings ────────────────────────────────────────────────────────────────
def test_leer_un_setting_atraviesa_el_sub_bloque():
    s3 = conf_lint.buscar_plugin(CONF_S3_ANIDADO, "s3", "input")

    assert conf_lint.leer_setting(CONF_S3_ANIDADO, s3, "bucket") == "demoscss"
    assert conf_lint.leer_setting(CONF_S3_ANIDADO, s3, "prefix") == "sp500-logs/"
    assert conf_lint.leer_setting(CONF_S3_ANIDADO, s3, "region") is None


def test_escribir_un_setting_cambia_solo_ese_valor():
    s3 = conf_lint.buscar_plugin(CONF_S3_ANIDADO, "s3", "input")
    nuevo = conf_lint.escribir_setting(CONF_S3_ANIDADO, s3, "bucket", "mi-tracker-cts")

    assert 'bucket => "mi-tracker-cts"' in nuevo
    assert "demoscss" not in nuevo
    # Todo lo demás queda intacto, incluidas las ediciones del paso 3.
    assert 'force_path_style => true' in nuevo
    assert 'prefix => "sp500-logs/"' in nuevo
    assert nuevo.count("filter {") == 1


def test_escribir_un_setting_que_no_estaba_lo_agrega():
    conf = 'input {\n  s3 {\n    bucket => "b"\n  }\n}\n'
    s3 = conf_lint.buscar_plugin(conf, "s3", "input")
    nuevo = conf_lint.escribir_setting(conf, s3, "prefix", "CloudTraces/")

    assert 'prefix => "CloudTraces/"' in nuevo
    assert 'bucket => "b"' in nuevo
    # Y el resultado se sigue pudiendo leer con las mismas funciones.
    s3b = conf_lint.buscar_plugin(nuevo, "s3", "input")
    assert conf_lint.leer_setting(nuevo, s3b, "prefix") == "CloudTraces/"


# ── El lint ─────────────────────────────────────────────────────────────────
# Lo único que se chequeaba del filter que devuelve el LLM era que fuera un
# string no vacío. Un .conf que Logstash no puede compilar deja la pipeline
# caída y no se ve desde acá: Terraform crea la configuración igual, el cluster
# queda vivo y vacío, y el único síntoma es que no entra un documento.
def _errores(problemas):
    return [str(p) for p in problemas if p.nivel == conf_lint.ERROR]


def test_un_conf_sano_no_tiene_nada_que_decir():
    assert conf_lint.lint(CONF_S3_ANIDADO, marcador_hosts=True) == []


def test_el_filter_sin_envolver_es_el_error_silencioso_del_llm():
    """Si el modelo devuelve el CUERPO (`grok { … }`) sin el `filter { }` que lo
    envuelve, el .conf resultante es inválido y Terraform lo acepta igual."""
    problemas = conf_lint.lint_filtro('grok { match => { "message" => "%{GREEDYDATA:m}" } }')

    assert any("no es una sección" in m for m in _errores(problemas)), problemas


def test_un_filter_bien_envuelto_pasa():
    assert conf_lint.lint_filtro('filter {\n  grok { match => { "message" => "%{IP:src}" } }\n}') == []


def test_las_llaves_y_las_comillas_desbalanceadas_cortan():
    assert any("sin cerrar" in m for m in _errores(conf_lint.lint('input { s3 { bucket => "b" }\n')))
    assert any("de más" in m for m in _errores(conf_lint.lint('input { } }\noutput { stdout {} }\n')))
    assert any("comilla sin cerrar" in m
               for m in _errores(conf_lint.lint('input { s3 { bucket => "b } }\noutput { stdout {} }\n')))


def test_un_plugin_que_no_existe_o_que_css_no_tiene():
    base = 'input { s3 { bucket => "b" } }\nfilter { %s }\noutput { stdout {} }\n'

    inventado = _errores(conf_lint.lint(base % 'parseame { }'))
    assert any("`parseame` no es un plugin de `filter`" in m for m in inventado), inventado

    # `translate` sí existe en Logstash, pero no está instalado en CSS: el deploy
    # muere al crear la configuración. Se descubrió desplegando.
    css = _errores(conf_lint.lint(base % 'translate { field => "a" }'))
    assert any("no está instalado en la Logstash de CSS" in m for m in css), css


def test_una_condicion_numerica_no_es_un_plugin():
    """`if [code] >= 400 {` termina en un número pegado a la llave. Leerlo como
    un bloque llamado `400` frenaba el deploy del caso SIEM, que lo usa."""
    conf = ('input { s3 { bucket => "b" } }\n'
            'filter {\n  if [code] >= 400 {\n    mutate { add_field => { "x" => "1" } }\n  }\n}\n'
            'output { stdout {} }\n')

    assert conf_lint.lint(conf) == []


def test_los_filtros_de_todos_los_casos_del_catalogo_pasan_el_lint():
    """La red que evita que la whitelist deje afuera algo que YA usamos: si
    agregar un plugin al catálogo de verticales rompe el lint, salta acá y no en
    un deploy de 10 minutos."""
    import verticals

    problemas = {v["slug"]: [str(p) for p in conf_lint.lint_filtro(v.get("filter_code", ""))]
                 for v in verticals.all_verticals()}

    assert not {k: v for k, v in problemas.items() if v}


def test_un_patron_grok_inexistente_avisa_sin_frenar():
    """Un patrón que no existe impide que la pipeline arranque, pero la lista de
    patrones del core puede quedarse corta: frenar un deploy bueno por eso sería
    peor. Avisa y deja seguir."""
    conf = ('input { s3 { bucket => "b" } }\n'
            'filter { grok { match => { "message" => "%{NOEXISTE:x}" } } }\n'
            'output { stdout {} }\n')

    problemas = conf_lint.lint(conf)

    assert _errores(problemas) == []
    assert any(p.nivel == conf_lint.AVISO and "NOEXISTE" in p.mensaje for p in problemas)


def test_un_patron_definido_ahi_mismo_no_avisa():
    conf = ('input { s3 { bucket => "b" } }\n'
            'filter { grok {\n'
            '  pattern_definitions => { "MIPAT" => "[0-9]+" }\n'
            '  match => { "message" => "%{MIPAT:n} %{GREEDYDATA:resto}" }\n'
            '} }\n'
            'output { stdout {} }\n')

    assert conf_lint.lint(conf) == []


def test_el_sprintf_del_indice_no_se_confunde_con_grok():
    """`index => "logs-%{+YYYY.MM}"` y `document_id => "%{trace_id}"` son sprintf
    de Logstash, no patrones grok: avisar por ellos sería ruido en cada deploy."""
    conf = ('input { s3 { bucket => "b" } }\nfilter { }\n'
            'output { elasticsearch { hosts => [] index => "logs-%{+YYYY.MM}" '
            'document_id => "%{trace_id}" } }\n')

    assert conf_lint.lint(conf, marcador_hosts=True) == []


def test_sin_el_marcador_de_hosts_la_pipeline_escribiria_en_otro_lado():
    """Terraform inyecta el cluster con un `replace` LITERAL de `hosts => []`.
    Si el texto exacto no está, el reemplazo no matchea y nadie se entera."""
    conf = ('input { s3 { bucket => "b" } }\nfilter { }\n'
            'output { elasticsearch { hosts => ["http://viejo:9200"] } }\n')

    assert any("marcador" in m for m in _errores(conf_lint.lint(conf, marcador_hosts=True)))
    # Sin pedir el marcador (ej. un preview), el mismo .conf no molesta.
    assert conf_lint.lint(conf) == []


def test_el_input_s3_sin_bucket_sigue_siendo_error():
    conf = 'input { s3 { bucket => "" } }\nfilter { }\noutput { stdout {} }\n'

    assert any("de qué bucket leer" in m for m in _errores(conf_lint.lint(conf)))


def test_escribir_escapa_las_comillas_del_valor():
    conf = 'input {\n  s3 {\n    bucket => "b"\n  }\n}\n'
    s3 = conf_lint.buscar_plugin(conf, "s3", "input")
    nuevo = conf_lint.escribir_setting(conf, s3, "bucket", 'raro"con"comillas')

    s3b = conf_lint.buscar_plugin(nuevo, "s3", "input")
    assert conf_lint.leer_setting(nuevo, s3b, "bucket") == 'raro\\"con\\"comillas'
    assert [b.nombre for b in conf_lint.secciones(nuevo)] == ["input"]
