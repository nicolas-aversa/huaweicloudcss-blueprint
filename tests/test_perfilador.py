"""Las filas reales deciden los tipos, y el .conf que sale de ahí anda.

Cada caso de este archivo es algo que el flujo "Dataset nuevo" hacía mal cuando
el LLM tipaba mirando solo el header: fechas que el template descartaba en
silencio, `12.000` leído como 12, códigos que perdían sus ceros, headers con
tildes convertidos en `column_N`. Y el corpus del final pasa los datasets reales
del repo por todo el camino —perfil, .conf, gramática de Logstash, verificación—
para que ninguno de esos arreglos rompa otro.
"""
import pathlib

import pytest

import conf_lint
import perfilador

_DATASETS = pathlib.Path(__file__).resolve().parent.parent / "datasets"

TELEMETRIA = """Fecha y Hora,ID Trabajo,Cliente,Prensa / Máquina,Tipo de Papel,Estado,Nivel Log,Páginas Totales,Páginas Impresas,Páginas Rechazadas,Velocidad (PPM),Consumo Tinta (ml),Código Error,Descripción Error
2026-09-18 11:04:12,TRB-00187,Editorial Sur,PRENSA-03,Couché 150g,COMPLETADO,INFO,12000,11980,20,142,865.5,,
2026-09-18 11:31:55,TRB-00188,Gráfica Andes,PRENSA-01,Obra 90g,FALLIDO,ERROR,8000,3120,41,118,240.0,E-0412,Atasco en unidad de plegado
2026-09-18 12:02:10,TRB-00189,Editorial Sur,PRENSA-03,Couché 150g,COMPLETADO,INFO,5000,5000,0,150,410.25,,
"""


def _perfil(texto: str):
    return perfilador.perfilar(texto.strip("\n").split("\n"))


def _col(perfil, nombre):
    c = perfil.columna(nombre)
    assert c is not None, [x.nombre for x in perfil.columnas]
    return c


def _conf(perfil) -> str:
    return ('input { s3 { bucket => "b" } }\n' + perfilador.armar_filter(perfil, "data") +
            'output { elasticsearch { hosts => [] index => "x" } }\n')


def _sin_errores(perfil):
    return [str(e) for e in conf_lint.lint(_conf(perfil), marcador_hosts=True)
            if e.nivel == conf_lint.ERROR]


# ── El caso que motivó todo ─────────────────────────────────────────────────
def test_el_csv_de_telemetria_sale_bien_tipado():
    p = _perfil(TELEMETRIA)

    assert p.formato == "delimitado" and p.separador == "," and p.header.startswith("Fecha y Hora")
    fecha = _col(p, "fecha_y_hora")
    assert fecha.tipo == "date" and fecha.formato_fecha == "yyyy-MM-dd HH:mm:ss"
    assert p.fecha_evento == "fecha_y_hora"
    for n in ("paginas_totales", "paginas_impresas", "paginas_rechazadas"):
        assert _col(p, n).tipo == "integer", n
    assert _col(p, "consumo_tinta_ml").tipo == "float"


def test_el_header_con_tildes_da_nombres_y_etiquetas():
    """"Páginas Totales" terminaba en `column_N` porque el detector viejo pedía
    ASCII puro. El nombre se sanea para el .conf; la etiqueta queda como la
    escribió la persona, y una unidad entre paréntesis se separa."""
    p = _perfil(TELEMETRIA)

    c = _col(p, "paginas_totales")
    assert c.etiqueta == "Páginas Totales"
    assert _col(p, "prensa_maquina").etiqueta == "Prensa / Máquina"
    vel = _col(p, "velocidad_ppm")
    assert vel.etiqueta == "Velocidad" and vel.unidad == "PPM"
    assert _col(p, "consumo_tinta_ml").unidad == "ml"


def test_el_conf_de_telemetria_compila_y_verifica():
    p = _perfil(TELEMETRIA)

    assert _sin_errores(p) == []
    filtro = perfilador.armar_filter(p, "data")
    assert 'match => ["[data][fecha_y_hora]", "yyyy-MM-dd HH:mm:ss"]' in filtro
    assert 'if [message] == "Fecha y Hora,ID Trabajo,' in filtro, "el header tiene que descartarse"
    assert perfilador.verificar(p).completa


# ── Números ─────────────────────────────────────────────────────────────────
PUNTO_Y_COMA = """fecha;sucursal;monto;cantidad;activo
18/09/2026 11:04;Palermo;1.234,56;12.000;true
19/09/2026 09:15;Belgrano;865,5;3;false
20/09/2026 18:40;Palermo;15.400,00;1;true
"""


def test_coma_decimal_y_punto_de_miles():
    p = _perfil(PUNTO_Y_COMA)

    assert p.separador == ";"
    monto = _col(p, "monto")
    assert monto.tipo == "float" and monto.decimal == "," and monto.limpiar_numero


def test_doce_mil_son_doce_mil():
    """`12.000` en una columna de enteros son miles. Leído valor por valor era
    12.0; y convertido sin limpiar, Logstash indexaba un 12."""
    p = _perfil(PUNTO_Y_COMA)

    cantidad = _col(p, "cantidad")
    assert cantidad.tipo == "integer"
    assert cantidad.limpiar_numero, "sin gsub, convert lee 12"
    filtro = perfilador.armar_filter(p, "data")
    assert '"[data][cantidad]", "[^0-9,-]", ""' in filtro
    assert perfilador.verificar(p).completa


def test_la_verificacion_simula_el_filter_y_no_el_perfil():
    """Comparar el perfil consigo mismo aprobaba el `12.000` que Logstash iba a
    indexar como 12. La verificación simula el gsub y el convert del .conf."""
    p = _perfil(PUNTO_Y_COMA)
    _col(p, "cantidad").limpiar_numero = False       # el bug, reproducido

    v = perfilador.verificar(p)

    assert not v.completa
    assert any("cantidad" in x for x in v.problemas), v.problemas


def test_el_limpiado_va_en_un_mutate_aparte_del_convert():
    """Dentro de un mismo `mutate`, Logstash corre `convert` ANTES que `gsub`."""
    filtro = perfilador.armar_filter(_perfil(PUNTO_Y_COMA), "data")

    arbol = conf_lint.parse('input { stdin {} }\n' + filtro + 'output { stdout {} }\n')
    mutates = [p for p, _ in arbol.plugins() if p.nombre == "mutate"]
    for m in mutates:
        nombres = {k for k, _ in m.atributos}
        assert not {"gsub", "convert"} <= nombres, "gsub y convert en el mismo mutate"
    assert filtro.index("gsub => [\n") < filtro.index("convert => {")


def test_simbolos_de_moneda_y_porcentaje():
    p = _perfil("item,precio,margen\na,$ 1.500,15%\nb,$ 250,8%\nc,$ 12.300,22%\n")

    precio = _col(p, "precio")
    assert precio.tipo == "integer" and precio.unidad == "$" and precio.limpiar_numero
    assert _col(p, "margen").unidad == "%"
    assert perfilador.verificar(p).completa


def test_un_codigo_con_ceros_adelante_no_es_un_numero():
    """`000` convertido a número es 0: se pierde el código."""
    p = _perfil("respuesta,monto\n000,10\n051,20\n000,30\n")

    assert _col(p, "respuesta").tipo == "string"


def test_los_ids_no_se_suman():
    p = _perfil("id_cliente,orden_id,nro_factura,monto\n1001,55,9001,10.5\n1002,56,9002,20.5\n1001,57,9003,30.5\n")

    for n in ("id_cliente", "orden_id", "nro_factura"):
        assert _col(p, n).tipo == "string", n
        assert not _col(p, n).dimension, n
    assert _col(p, "monto").tipo == "float"


def test_una_columna_que_mezcla_no_se_convierte():
    """Un tipo se asigna solo si TODOS los valores lo cumplen."""
    p = _perfil("a,b\n1,x\n2,y\ntres,z\n")

    assert _col(p, "a").tipo == "string"


def test_los_placeholders_no_vuelven_texto_a_una_columna_numerica():
    p = _perfil("a,b\n1,x\n-,y\nN/A,z\n4,w\n")

    assert _col(p, "a").tipo == "integer"
    assert '"n/a"' in perfilador.armar_filter(p, "data"), "el ruby tiene que borrarlos"


# ── Fechas ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("valores,esperado", [
    (["2026-09-18 11:04:12", "2026-09-19 00:00:01"], "yyyy-MM-dd HH:mm:ss"),
    (["2026-09-18T11:04:12Z", "2026-09-19T00:00:01.123-03:00"], "ISO8601"),
    (["18/09/2026 11:04", "19/09/2026 23:59"], "dd/MM/yyyy HH:mm"),
    (["01/02/2026", "13/02/2026"], "dd/MM/yyyy"),
    (["02/13/2026", "02/01/2026"], "MM/dd/yyyy"),          # un 13 en el mes lo desmiente
    (["1/2/2026 9:05", "13/12/2026 18:40"], "d/M/yyyy H:mm"),
    (["20260918110412", "20260919000001"], "yyyyMMddHHmmss"),
    (["2026-09-18", "2026-09-19"], "yyyy-MM-dd"),
])
def test_el_formato_de_fecha_sale_de_los_valores(valores, esperado):
    texto = "fecha,x\n" + "\n".join(f"{v},1" for v in valores) + "\n"

    c = _col(_perfil(texto), "fecha")

    assert c.tipo == "date" and c.formato_fecha == esperado


def test_dd_mm_gana_la_ambiguedad():
    """`01/02/2026` es 1 de febrero acá: la plataforma es de la-south-2."""
    c = _col(_perfil("fecha,x\n01/02/2026,1\n03/04/2026,2\n"), "fecha")
    assert c.formato_fecha == "dd/MM/yyyy"


def test_epoch_solo_con_un_nombre_que_lo_diga():
    """Diez dígitos pueden ser un epoch o cualquier otra cosa: sin un nombre que
    diga que es tiempo, no se afirma que sea una fecha."""
    con = _col(_perfil("ts,x\n1716120930,1\n1716120990,2\n"), "ts")
    sin = _col(_perfil("medida,x\n1716120930,1\n1716120990,2\n"), "medida")

    assert con.tipo == "date" and con.formato_fecha == "UNIX"
    assert sin.tipo == "integer"


def test_una_fecha_fuera_de_rango_no_es_una_fecha():
    """Catorce dígitos que "parsean" como el año 3000 son un número."""
    c = _col(_perfil("medida,x\n30001231235959,1\n30001231235958,2\n"), "medida")

    assert c.tipo == "integer"


def test_una_fecha_sin_zona_lleva_la_de_la_plataforma():
    filtro = perfilador.armar_filter(_perfil(TELEMETRIA), "data")
    assert f'timezone => "{perfilador.ZONA_POR_DEFECTO}"' in filtro

    iso = _perfil("ts,x\n2026-09-18T11:04:12Z,1\n2026-09-18T11:05:12Z,2\n")
    assert "timezone" not in perfilador.armar_filter(iso, "data"), "trae su zona"


def test_fecha_y_hora_en_columnas_separadas_se_juntan():
    """`date=2026-04-13 time=21:08:17`: sin juntarlas, @timestamp tendría
    precisión de día."""
    p = _perfil('date=2026-04-13 time=21:08:17 level="notice" srcip=10.0.0.1\n'
                'date=2026-04-14 time=09:05:03 level="warning" srcip=10.0.0.2\n')

    assert p.formato == "kv" and p.fecha_compuesta == ("date", "time")
    filtro = perfilador.armar_filter(p, "data")
    assert "%{[data][date]} %{[data][time]}" in filtro
    assert '"yyyy-MM-dd H:mm:ss"' in filtro
    assert _sin_errores(p) == [] and perfilador.verificar(p).completa


# ── Lo que llega del archivo tal cual ───────────────────────────────────────
def test_bom_y_fin_de_linea_de_windows():
    """El archivo se sube byte a byte a OBS: el BOM de Excel y el \\r de Windows
    llegan a Logstash. Sin limpiarlos, el header no se reconoce y entra como un
    documento más."""
    lineas = ["﻿fecha,monto\r", "2026-09-18 11:04:12,10\r", "2026-09-18 11:05:12,20\r"]
    p = perfilador.perfilar(lineas)

    assert p.header == "fecha,monto", repr(p.header)
    filtro = perfilador.armar_filter(p, "data")
    assert 'gsub => ["message", "^\\uFEFF", ""]' in filtro
    assert 'strip => ["message"]' in filtro


def test_un_campo_entre_comillas_con_el_separador_adentro():
    p = _perfil('nombre,monto,ciudad\n"Pérez, Juan",10,"Buenos Aires"\n"Gómez, Ana",20,Rosario\n')

    assert len(p.columnas) == 3
    assert _col(p, "monto").tipo == "integer"
    assert perfilador.verificar(p).completa


def test_separador_tab():
    p = _perfil("fecha\tmonto\n2026-09-18 11:04:12\t10\n2026-09-18 11:05:12\t20\n")

    assert p.separador == "\t"
    assert 'separator => "\t"' in perfilador.armar_filter(p, "data"), "el tab va literal"
    assert _sin_errores(p) == []


def test_un_header_con_comillas_se_descarta_igual():
    """Con `support_escapes` apagado, un `\\"` no se desescapa: el header con
    comillas se compara con un string entre comillas simples."""
    p = _perfil('"fecha","monto"\n"2026-09-18 11:04:12","10"\n"2026-09-18 11:05:12","20"\n')

    filtro = perfilador.armar_filter(p, "data")
    assert "if [message] == '\"fecha\",\"monto\"'" in filtro
    assert _sin_errores(p) == []


def test_un_csv_sin_header():
    p = _perfil("2026-09-18T11:04:12Z,emergency,129.16\n2026-09-18T11:05:12Z,ambulatory,69.5\n")

    assert not p.header
    assert [c.nombre for c in p.columnas] == ["columna_1", "columna_2", "columna_3"]
    assert _col(p, "columna_1").tipo == "date"
    assert "if [message] ==" not in perfilador.armar_filter(p, "data").split("drop {} }", 1)[1]


def test_solo_el_header_igual_arma_un_conf_valido():
    """El pegado de una línea: sin datos no hay tipos, pero el .conf compila."""
    p = _perfil("Fecha,Cliente,Monto")

    assert p.estructurado and all(c.tipo == "string" for c in p.columnas)
    assert _sin_errores(p) == []


# ── Lo que NO es una tabla ──────────────────────────────────────────────────
def test_un_log_con_envoltorio_va_al_llm():
    """`<fecha> - <host> a=1|b=2|…`: partido por `|` daba columnas "a=1", "b=2".
    Eso no es una tabla, y lo arma el LLM."""
    p = _perfil("20251113014806:726 - 7846:17874 operation_code=TRANSFER|message_type=210|response_code=000\n"
                "20251113014807:726 - 7846:17875 operation_code=PAYMENT|message_type=210|response_code=051\n")

    assert not p.estructurado


def test_un_json_indentado_no_se_toma_como_csv():
    """El input de OBS lo leería línea por línea, y `  "a": 1,` partido por
    comas da dos columnas prolijas en cada línea."""
    p = _perfil('{\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n')

    assert not p.estructurado


def test_texto_libre_no_es_estructurado():
    p = _perfil("Sep 18 11:04:12 host sshd[123]: Failed password for root from 10.0.0.1\n"
                "Sep 18 11:04:15 host sshd[124]: Accepted password for juan from 10.0.0.2\n")
    assert not p.estructurado


# ── JSON y clave=valor ──────────────────────────────────────────────────────
def test_jsonl_anidado_con_tipos_nativos():
    p = _perfil('{"ts":"2026-09-18T11:04:12Z","monto":10.5,"ok":true,"cliente":{"id":"C1","pais":"AR"}}\n'
                '{"ts":"2026-09-18T11:05:12Z","monto":20,"ok":false,"cliente":{"id":"C2","pais":"UY"}}\n')

    assert p.formato == "json"
    monto = _col(p, "monto")
    assert monto.tipo == "float" and monto.nativo, "un número nativo no necesita gsub ni convert"
    assert _col(p, "cliente_id").path == ("cliente", "id")
    assert _col(p, "cliente_id").tipo == "string"
    filtro = perfilador.armar_filter(p, "data")
    assert '"[data][monto]" =>' not in filtro, "un número nativo de JSON no se convierte"
    assert '"[data][ok]" =>' not in filtro, "un booleano nativo tampoco"
    assert _sin_errores(p) == [] and perfilador.verificar(p).completa


# ── Dimensiones ─────────────────────────────────────────────────────────────
def test_una_columna_casi_unica_no_es_dimension():
    filas = "\n".join(f"c{i},{'A' if i % 2 else 'B'}" for i in range(40))
    p = _perfil("nombre,grupo\n" + filas + "\n")

    assert not _col(p, "nombre").dimension, "40 valores distintos en 40 filas es un id"
    assert _col(p, "grupo").dimension


# ── El corpus: los datasets reales del repo ─────────────────────────────────
_ESTRUCTURADOS = ["encuentros-clinicos.log", "fortianalyzer.log", "fraud-detection.log",
                  "produccion-pozos.log", "streaming-ott.log", "ventas-ecommerce.log"]
_CON_ENVOLTORIO = ["transacciones-alyc.log", "transacciones-billetera.log"]


def _primeras(nombre, n=200):
    with (_DATASETS / nombre).open(encoding="utf-8") as fh:
        return [l for _, l in zip(range(n), fh)]


@pytest.mark.parametrize("nombre", _ESTRUCTURADOS)
def test_cada_dataset_del_repo_da_un_conf_que_compila_y_verifica(nombre):
    p = perfilador.perfilar(_primeras(nombre))

    assert p.estructurado, nombre
    assert _sin_errores(p) == [], nombre
    v = perfilador.verificar(p)
    assert v.completa, (nombre, v.ok, v.total, v.problemas)


@pytest.mark.parametrize("nombre", _CON_ENVOLTORIO)
def test_los_datasets_con_envoltorio_van_al_llm(nombre):
    assert not perfilador.perfilar(_primeras(nombre)).estructurado


@pytest.mark.parametrize("lineas", [
    ["2026-09-18 11:04:12,123 INFO [main] com.x.Y - arrancó, listo",
     "2026-09-18 11:04:13,456 WARN [main] com.x.Y - lento, 3s"],
    ["[2026-09-18T11:04:12.123] ERROR [pool-1] Bar - falló, reintento",
     "[2026-09-18T11:04:13.456] INFO [pool-1] Bar - ok, 2 filas"],
    ["CEF:0|Security|threatmanager|1.0|100|worm stopped|10|",
     "CEF:0|Security|threatmanager|1.0|101|worm started|5|"],
    ["<165>1 2026-09-18T11:04:12Z host app 1 ID47 - a|b|c",
     "<165>1 2026-09-18T11:04:13Z host app 1 ID48 - d|e|f"],
], ids=["log4j", "log4j-corchetes", "cef", "syslog"])
def test_una_linea_de_log_no_es_una_tabla(lineas):
    """Un separador las parte en columnas "consistentes" (la coma de los
    milisegundos, los `|` de CEF) y el .conf saldría con columnas absurdas."""
    assert not perfilador.perfilar(lineas).estructurado


def test_una_tabla_con_fecha_y_nivel_sigue_siendo_tabla():
    p = perfilador.perfilar(["fecha,nivel,mensaje",
                             "2026-09-18 11:04:12,INFO,arrancó",
                             "2026-09-18 11:04:13,WARN,lento"])
    assert p.estructurado and p.formato == "delimitado"


def test_los_campos_llevan_el_formato_de_fecha():
    """El formato tiene que viajar con el campo: es lo que le dice al index
    template cómo leer la fecha sin descartarla."""
    fs = perfilador.campos(_perfil(TELEMETRIA), "data")

    fecha = next(f for f in fs if f["field_path"] == "data.fecha_y_hora")
    assert fecha["type"] == "date" and fecha["date_format"] == "yyyy-MM-dd HH:mm:ss"
    assert fecha["role"] == "timestamp"
    tot = next(f for f in fs if f["field_path"] == "data.paginas_totales")
    assert tot["business_label"] == "Páginas Totales" and tot["date_format"] is None
