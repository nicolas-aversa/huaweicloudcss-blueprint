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


def test_escribir_escapa_las_comillas_del_valor():
    conf = 'input {\n  s3 {\n    bucket => "b"\n  }\n}\n'
    s3 = conf_lint.buscar_plugin(conf, "s3", "input")
    nuevo = conf_lint.escribir_setting(conf, s3, "bucket", 'raro"con"comillas')

    s3b = conf_lint.buscar_plugin(nuevo, "s3", "input")
    assert conf_lint.leer_setting(nuevo, s3b, "bucket") == 'raro\\"con\\"comillas'
    assert [b.nombre for b in conf_lint.secciones(nuevo)] == ["input"]
