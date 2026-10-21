"""FIX como formato conocido: el log de QuickFIX/J (o de cualquier motor FIX)
entra por "Dataset nuevo" y sale decodificado, sin LLM.

El ruby que decodifica en Logstash se probó contra esta misma decodificación en
Python sobre 4.800 líneas (con `|`, con SOH y en FileLog): coinciden campo por
campo. Acá se fija la decodificación, la detección, los campos y el `.conf`.
"""
import re

import pytest

import build_fix_dataset as gen
import conf_lint
import fix_formato as ff
import log_format_catalog
import perfilador

CAB = "INFO  [QFJ Message Processor] quickfixj.msg"
ORDEN = (f"2026-10-07 11:00:00.512 {CAB}.outgoing - FIX.4.4:ALYC->BYMA: 8=FIX.4.4|9=200|35=D|49=ALYC|56=BYMA|34=12|"
         "52=20261007-14:00:00.512|11=ALYC-261007-000001|1=12345|50=OPER01|21=1|55=GGAL|207=XBUE|167=CS|54=1|"
         "38=500|40=2|44=6200|59=0|60=20261007-14:00:00.512|63=2|15=ARS|10=123|")
EJECUCION = (f"2026-10-07 11:00:03.020 {CAB}.incoming - FIX.4.4:ALYC->BYMA: 8=FIX.4.4|9=250|35=8|49=BYMA|56=ALYC|"
             "34=40|52=20261007-14:00:03.011|37=BYMA-1|11=ALYC-261007-000001|1=12345|55=GGAL|54=1|38=500|44=6200|"
             "17=BYMA-E1|150=F|39=2|32=500|31=6198.5|151=0|14=500|6=6198.5|60=20261007-14:00:03.011|10=001|")
RECHAZO_SIN_TEXTO = (f"2026-10-07 11:00:04.020 {CAB}.incoming - FIX.4.4:ALYC->BYMA: 8=FIX.4.4|9=150|35=8|49=BYMA|"
                     "56=ALYC|34=41|52=20261007-14:00:04.010|37=BYMA-2|11=X|17=E2|150=8|39=8|103=16|10=002|")
RECHAZO_CANCELACION = (f"2026-10-07 11:00:05.020 {CAB}.incoming - FIX.4.4:ALYC->BYMA: 8=FIX.4.4|9=150|35=9|49=BYMA|"
                       "56=ALYC|34=42|52=20261007-14:00:05.010|37=BYMA-1|11=Y|41=X|39=2|434=1|102=0|"
                       "58=Too late to cancel|10=003|")
EVENTO_SALTO = ("2026-10-14 12:33:45.032 ERROR [QFJ Timer] quickfixj.errorEvent - FIX.4.4:ALYC->BYMA: "
                "MsgSeqNum too high, expecting 2146 but received 2160")
EVENTO_HB = ("2026-10-14 12:31:10.000 ERROR [QFJ Timer] quickfixj.errorEvent - FIX.4.4:ALYC->BYMA: "
             "Timed out waiting for heartbeat")
HEARTBEAT = (f"2026-10-07 11:00:30.000 INFO  [QFJ Timer] quickfixj.msg.outgoing - FIX.4.4:ALYC->BYMA: "
             "8=FIX.4.4|9=60|35=0|49=ALYC|56=BYMA|34=13|52=20261007-14:00:30.000|10=090|")
LOG_SLF4J = [ORDEN, EJECUCION, RECHAZO_SIN_TEXTO, RECHAZO_CANCELACION, EVENTO_SALTO, HEARTBEAT, EVENTO_HB] * 5


def _filelog(linea):
    m = ff._PY["slf4j"].match(linea)
    return "20261007-14:00:00.512: " + m.group("payload").replace("|", "\x01")


# ── Detección ───────────────────────────────────────────────────────────────
def test_reconoce_los_tres_envoltorios_con_pipe_o_soh():
    assert ff.envoltorio(LOG_SLF4J) == "slf4j" and ff.es_fix(LOG_SLF4J)
    assert ff.es_fix([l.replace("|", "\x01") for l in LOG_SLF4J])
    filelog = [_filelog(l) for l in (ORDEN, EJECUCION, HEARTBEAT) * 4]
    assert ff.envoltorio(filelog) == "filelog" and ff.es_fix(filelog)
    crudo = [ff._PY["slf4j"].match(l).group("payload") for l in (ORDEN, EJECUCION) * 4]
    assert ff.envoltorio(crudo) == "crudo" and ff.es_fix(crudo)


@pytest.mark.parametrize("lineas", [
    ["2026-10-07 11:00:00,512 INFO  [main] com.acme.App - Arrancó el servicio"] * 10,
    ['{"ts": "2026-10-07T11:00:00Z", "msg": "8=FIX.4.4 no es esto"}'] * 10,
    ["fecha,especie,cantidad", "2026-10-07,GGAL,500", "2026-10-07,YPFD,10"],
])
def test_no_confunde_otros_formatos(lineas):
    assert not ff.es_fix(lineas)


def test_el_catalogo_lo_toma_antes_que_log4j_y_el_perfilador_no_lo_toma():
    """Un log de QuickFIX/J por SLF4J empieza como un log4j: sin FIX primero,
    log4j se lo llevaba sin decodificar un tag."""
    assert log_format_catalog.CATALOG[0].name == "fix"
    assert log_format_catalog.try_match(LOG_SLF4J)["formato"] == "fix_slf4j"
    assert not perfilador.perfilar(LOG_SLF4J).estructurado


# ── Decodificación ──────────────────────────────────────────────────────────
def test_una_orden_nueva():
    d = ff.decodificar(ORDEN, "slf4j")
    assert d["fix.msg_type"] == "Orden nueva" and d["fix.msg_type_code"] == "D"
    assert d["fix.side"] == "Compra" and d["fix.ord_type"] == "Límite" and d["fix.time_in_force"] == "Día"
    assert d["fix.settl_type"] == "24 horas" and d["fix.currency"] == "ARS" and d["fix.account"] == "12345"
    assert d["fix.order_qty"] == 500.0 and d["fix.price"] == 6200.0 and d["fix.msg_seq_num"] == 12
    assert d["fix.sending_time"] == "2026-10-07T14:00:00.512Z"
    assert (d["fix.direccion"], d["fix.contraparte"], d["fix.capa"]) == ("saliente", "BYMA", "aplicación")
    assert d["log.logger"] == "quickfixj.msg.outgoing" and d["fix.sesion"] == "FIX.4.4:ALYC->BYMA"
    assert "fix.body_length" not in d and not any(k.startswith("fix.tag_9") or k == "fix.tag_10" for k in d)


def test_una_ejecucion_trae_el_monto():
    d = ff.decodificar(EJECUCION, "slf4j")
    assert d["fix.tipo_ejecucion"] == "Operación" and d["fix.ord_status"] == "Ejecutada"
    assert d["fix.monto_ejecutado"] == round(500 * 6198.5, 2)
    assert d["fix.direccion"] == "entrante" and d["fix.contraparte"] == "BYMA"
    assert "fix.rechazo_motivo" not in d


def test_el_motivo_del_rechazo_sale_del_texto_o_del_codigo():
    assert ff.decodificar(RECHAZO_SIN_TEXTO, "slf4j")["fix.rechazo_motivo"] == "Precio fuera de rango"
    cxl = ff.decodificar(RECHAZO_CANCELACION, "slf4j")
    assert cxl["fix.msg_type"] == "Rechazo de cancelación" and cxl["fix.rechazo_motivo"] == "Too late to cancel"
    assert cxl["fix.cxl_rej_reason"] == "Demasiado tarde para cancelar"


def test_los_eventos_de_la_sesion_se_clasifican():
    salto = ff.decodificar(EVENTO_SALTO, "slf4j")
    assert salto["fix.tipo_evento"] == "secuencia_alta" and salto["fix.salto_secuencia"] == 14
    assert salto["fix.capa"] == "evento" and salto["log.level"] == "ERROR"
    assert ff.decodificar(EVENTO_HB, "slf4j")["fix.tipo_evento"] == "heartbeat_vencido"
    assert ff.decodificar(HEARTBEAT, "slf4j")["fix.capa"] == "sesión"


def test_un_tag_desconocido_no_se_pierde_y_el_repetido_toma_el_primero():
    linea = ORDEN.replace("|10=123|", "|5975=XYZ|448=ALYC|448=OTRO|10=123|")
    d = ff.decodificar(linea, "slf4j")
    assert d["fix.tag_5975"] == "XYZ" and d["fix.party_id"] == "ALYC"


def test_la_latencia_descarta_relojes_desfasados():
    from datetime import datetime, timezone
    t = datetime(2026, 10, 7, 14, 0, 3, 20000, tzinfo=timezone.utc)
    assert ff.latencia_ms(t, "2026-10-07T14:00:03.011Z") == 9
    assert ff.latencia_ms(t, "2026-10-07T13:50:00.000Z") is None


# ── Los campos ──────────────────────────────────────────────────────────────
def test_los_campos_traen_el_nucleo_aunque_la_muestra_no_lo_muestre():
    """Doscientas líneas pueden no traer un rechazo ni un plazo: igual se declaran."""
    campos = {f["field_path"]: f for f in ff.generar([ORDEN, HEARTBEAT] * 5)["fields"]}
    for ruta in ("fix.rechazo_motivo", "fix.ord_rej_reason", "fix.monto_ejecutado", "fix.latencia_ms",
                 "fix.tipo_evento", "fix.salto_secuencia", "fix.poss_dup_flag"):
        assert ruta in campos, ruta
    assert campos["fix.account"]["entity"] and campos["fix.account"]["sensitive"]
    assert campos["fix.sending_time"]["role"] is None, "la fecha del evento es @timestamp, no la del mensaje"
    assert campos["fix.rechazo_motivo"]["role"] == "critical_indicator"
    assert campos["fix.ord_status"]["role"] == "success_indicator"


def test_la_primera_medida_es_el_monto_y_no_la_latencia():
    """La primera medida es la que se pronostica y vigila, sumada: sumar
    latencias no significa nada."""
    campos = ff.generar(LOG_SLF4J)["fields"]
    medidas = [f["field_path"] for f in campos if f.get("role") == "measure"]
    assert medidas[0] == "fix.monto_ejecutado"


def test_sin_ordenes_no_declara_los_campos_de_ordenes():
    """Un log de market data o solo de sesión no tiene cuentas ni montos."""
    campos = {f["field_path"] for f in ff.generar([HEARTBEAT, EVENTO_HB] * 5)["fields"]}
    assert "fix.account" not in campos and "fix.monto_ejecutado" not in campos
    assert "fix.msg_type" in campos and "fix.tipo_evento" in campos


# ── El .conf ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tipo", ["slf4j", "filelog", "crudo"])
def test_el_conf_pasa_el_lint_y_el_ruby_no_rompe_las_comillas(tipo):
    conf = ff.armar_filter(tipo)
    assert not [p for p in conf_lint.lint_filtro(conf) if p.nivel == conf_lint.ERROR]
    for cuerpo in re.findall(r"(?:init|code) => '(.*?)'\n", conf, re.S):
        assert "'" not in cuerpo, "una comilla simple cierra el `code => '...'` de Logstash"
    assert f"FIX_LINEA = /{ff.ENVOLTORIOS[tipo]}/" in conf
    assert all(f'"{t}" => ["{n}"' in conf for t, (n, _, _) in ff.TAGS.items())


def test_la_zona_del_log_y_la_de_fix():
    """El log de la aplicación va en la hora del servidor; FIX, en UTC."""
    assert 'timezone => "America/Argentina/Buenos_Aires"' in ff.armar_filter("slf4j")
    assert 'timezone => "UTC"' in ff.armar_filter("filelog")
    assert '"[fix][sending_time]", "ISO8601"' in ff.armar_filter("crudo")


# ── El dataset ──────────────────────────────────────────────────────────────
def test_el_generador_arma_mensajes_fix_validos():
    """BodyLength y CheckSum calculados sobre SOH, como los valida un motor FIX."""
    from datetime import datetime
    sesion = gen.Sesion("AL017", "BYMA", "FIX.4.4")
    m = gen.Msg(sesion, True, datetime(2026, 10, 7, 11), datetime(2026, 10, 7, 11),
                [("35", "D"), ("11", "X"), ("55", "GGAL")], seq=7)
    texto = gen.armar(m).replace("|", "\x01")
    cuerpo = texto.split("\x019=", 1)[1].split("\x01", 1)[1].rsplit("10=", 1)[0]
    assert int(re.search(r"\x019=(\d+)\x01", texto).group(1)) == len(cuerpo.encode())
    sin_suma = texto.rsplit("10=", 1)[0]
    assert int(texto.rsplit("10=", 1)[1].strip("\x01")) == sum(sin_suma.encode()) % 256
    assert "|34=7|" in gen.armar(m) and "|52=20261007-14:00:00.000|" in gen.armar(m)
    assert "|49=AL017|56=BYMA|" in gen.armar(m) and sesion.nombre == "FIX.4.4:AL017->BYMA"


def test_los_heartbeats_no_logueados_igual_consumen_secuencias():
    """Con `SLF4JLogHeartbeats=N` no se ven, pero cada intervalo sin mensajes
    consume un número: los saltos de a varios son normales, no un problema."""
    from datetime import datetime, timedelta
    s = gen.Sesion("AL001", "A3", "FIXT.1.1")
    t0 = datetime(2026, 10, 13, 10)
    dia = gen.Dia(msgs=[gen.Msg(s, True, t0, t0, [("35", "A")]),
                        gen.Msg(s, True, t0 + timedelta(seconds=5), t0, [("35", "D")]),
                        gen.Msg(s, True, t0 + timedelta(seconds=95), t0, [("35", "D")])])
    gen.secuencias(dia)
    assert [m.seq for m in dia.msgs] == [1, 2, 6]       # 90 s sin mensajes = 3 heartbeats


# ── Un OMS que opera para muchos participantes ──────────────────────────────
def _de(participante, linea=ORDEN):
    return linea.replace("ALYC", participante)


def test_el_participante_propio_sale_de_la_sesion_o_de_la_direccion():
    assert ff.decodificar(_de("AL017"), "slf4j")["fix.comp_id_propio"] == "AL017"
    assert ff.decodificar(_de("AL017", EJECUCION), "slf4j")["fix.comp_id_propio"] == "AL017"
    filelog = _filelog(_de("AL017"))
    sin_sesion = ff.decodificar(filelog.replace("20261007-14:00:00.512: ", ""), "crudo")
    assert "fix.comp_id_propio" not in sin_sesion, "sin sesión ni logger no se sabe de qué lado está"


def test_con_varios_participantes_la_entidad_es_el_participante():
    """El log de una firma tiene un solo CompID propio: la entidad es la cuenta.
    El de un OMS tiene muchos: la entidad es el participante (la ALyC)."""
    una = {f["field_path"]: f for f in ff.generar(LOG_SLF4J)["fields"]}
    assert una["fix.account"].get("entity") and una["fix.account"]["role"] == "entity_id"
    assert not una["fix.comp_id_propio"].get("entity")
    oms = [_de(f"AL{i:03d}", l) for i in range(1, 6) for l in (ORDEN, EJECUCION)]
    varias = {f["field_path"]: f for f in ff.generar(oms)["fields"]}
    assert varias["fix.comp_id_propio"].get("entity") and varias["fix.comp_id_propio"]["role"] == "entity_id"
    assert not varias["fix.account"].get("entity") and varias["fix.account"]["role"] is None
    assert varias["fix.account"]["sensitive"], "el comitente se enmascara igual"


def test_la_demora_es_el_envio_menos_la_transaccion():
    """En la orden que un OMS manda al mercado, 60 es cuándo entró la orden: la
    demora es el tiempo adentro del OMS."""
    linea = ORDEN.replace("|60=20261007-14:00:00.512|", "|60=20261007-14:00:00.130|")
    assert ff.decodificar(linea, "slf4j")["fix.demora_ms"] == 382
    assert ff.demora_ms("2026-10-07T14:00:00.512Z", "2026-10-07T12:00:00.000Z") is None   # una hora: no es creíble
    assert "fix.demora_ms" not in ff.decodificar(HEARTBEAT, "slf4j")


def test_un_rechazo_del_oms_en_castellano_es_un_rechazo():
    linea = ("2026-10-13 11:00:01.000 INFO  [riesgo-2] oms.riesgo - Orden AL017-1 rechazada por control de riesgo: "
             "Saldo insuficiente")
    d = ff.decodificar(linea, "slf4j")
    assert d["fix.tipo_evento"] == "rechazo" and d["log.logger"] == "oms.riesgo"


def test_el_rechazo_del_oms_se_cuenta_con_los_del_mercado():
    """El motivo del rechazo es el indicador crítico y el campo de la búsqueda:
    los del control de riesgo propio (una línea de texto) tienen que estar."""
    linea = ("2026-10-13 11:00:01.000 INFO  [riesgo-2] oms.riesgo - Orden AL017-1 rechazada por control de riesgo: "
             "Saldo insuficiente")
    assert "Saldo insuficiente" in ff.decodificar(linea, "slf4j")["fix.rechazo_motivo"]
    assert "fix.rechazo_motivo" not in ff.decodificar(EVENTO_HB, "slf4j")
