"""FIX: el protocolo con que una ALyC, un broker o un banco hablan con los
mercados. Lo escriben QuickFIX/J, QuickFIX/n, QuickFIX y cualquier otro motor.

Un mensaje FIX son pares `tag=valor` con el tag como número (`35=D` es una orden
nueva, `54=1` es compra), separados por SOH (el carácter 0x01) o, en los logs
que se exportan para leer, por `|`. El perfilador no lo toma como clave=valor (sus
claves empiezan con letra) y el LLM tendría que saberse el diccionario: acá va el
diccionario estándar, y de él salen las dos cosas que necesita un caso:

  - el bloque `ruby` de Logstash que decodifica cada línea (`fix.msg_type` =
    "Orden nueva", `fix.side` = "Compra", `fix.ord_status` = "Rechazada"…);
  - los campos que ve el paso 2, con su etiqueta, su tipo y su rol.

Tres envoltorios: SLF4J/logback (la línea de log con la sesión adelante,
`... quickfixj.msg.incoming - FIX.4.4:ALYC->BYMA: 8=FIX.4.4|9=...`), el FileLog
de QuickFIX (`20261021-13:45:12.345: 8=FIX.4.4...`) y el mensaje solo. En un log
de SLF4J también vienen los eventos de la sesión (logon, desconexión, salto de
secuencia): se clasifican.

Además del diccionario, lo que un mensaje dice sin decirlo: si entró o salió,
con qué mercado, si es de la sesión o del negocio, el motivo de un rechazo (venga
en el tipo de mensaje que venga), el monto ejecutado y la latencia (cuándo lo
mandó la contraparte, tag 52 en UTC, contra cuándo se recibió).

Los tags propios de un mercado quedan como `fix.tag_NNNN`: nada se pierde.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

# tag → (nombre, etiqueta, tipo): s = texto corto, t = texto libre, f = decimal,
# i = entero, d = fecha UTC de FIX (`yyyyMMdd-HH:mm:ss[.SSS]`).
TAGS: dict[str, tuple[str, str, str]] = {
    "1": ("account", "Cuenta (comitente)", "s"),
    "6": ("avg_px", "Precio promedio", "f"),
    "7": ("begin_seq_no", "Desde la secuencia", "i"),
    "8": ("begin_string", "Versión de FIX", "s"),
    "11": ("cl_ord_id", "ID de la orden", "s"),
    "14": ("cum_qty", "Cantidad ejecutada acumulada", "f"),
    "15": ("currency", "Moneda", "s"),
    "16": ("end_seq_no", "Hasta la secuencia", "i"),
    "17": ("id_ejecucion", "ID de la ejecución", "s"),
    "21": ("handl_inst", "Instrucción de manejo", "s"),
    "31": ("last_px", "Precio de la ejecución", "f"),
    "32": ("last_qty", "Cantidad de la ejecución", "f"),
    "34": ("msg_seq_num", "Número de secuencia", "i"),
    "35": ("msg_type", "Tipo de mensaje", "s"),
    "36": ("new_seq_no", "Nueva secuencia", "i"),
    "37": ("order_id", "ID de la orden en el mercado", "s"),
    "38": ("order_qty", "Cantidad de la orden", "f"),
    "39": ("ord_status", "Estado de la orden", "s"),
    "40": ("ord_type", "Tipo de orden", "s"),
    "41": ("orig_cl_ord_id", "ID de la orden original", "s"),
    "43": ("poss_dup_flag", "Reenvío", "s"),
    "44": ("price", "Precio", "f"),
    "45": ("ref_seq_num", "Secuencia rechazada", "i"),
    "48": ("security_id", "ID del instrumento", "s"),
    "49": ("sender_comp_id", "Emisor", "s"),
    "50": ("sender_sub_id", "Operador", "s"),
    "52": ("sending_time", "Hora de envío", "d"),
    "54": ("side", "Lado", "s"),
    "55": ("symbol", "Especie", "s"),
    "56": ("target_comp_id", "Destinatario", "s"),
    "57": ("target_sub_id", "Subdestinatario", "s"),
    "58": ("text", "Texto", "t"),
    "59": ("time_in_force", "Vigencia", "s"),
    "60": ("transact_time", "Hora de la transacción", "d"),
    "63": ("settl_type", "Plazo de liquidación", "s"),
    "97": ("poss_resend", "Posible reenvío", "s"),
    "98": ("encrypt_method", "Cifrado", "s"),
    "102": ("cxl_rej_reason", "Motivo del rechazo de cancelación", "s"),
    "103": ("ord_rej_reason", "Motivo del rechazo de la orden", "s"),
    "108": ("heart_bt_int", "Intervalo de heartbeat (s)", "i"),
    "112": ("test_req_id", "ID del TestRequest", "s"),
    "122": ("orig_sending_time", "Hora de envío original", "d"),
    "123": ("gap_fill_flag", "Relleno de huecos", "s"),
    "141": ("reset_seq_num_flag", "Reinicio de secuencias", "s"),
    "150": ("tipo_ejecucion", "Tipo de ejecución", "s"),
    "151": ("leaves_qty", "Cantidad pendiente", "f"),
    "167": ("security_type", "Tipo de instrumento", "s"),
    "207": ("security_exchange", "Mercado del instrumento", "s"),
    "371": ("ref_tag_id", "Tag rechazado", "s"),
    "372": ("ref_msg_type", "Tipo de mensaje rechazado", "s"),
    "373": ("session_reject_reason", "Motivo del rechazo de sesión", "s"),
    "380": ("business_reject_reason", "Motivo del rechazo de negocio", "s"),
    "434": ("cxl_rej_response_to", "Rechazo en respuesta a", "s"),
    "447": ("party_id_source", "Origen de la parte", "s"),
    "448": ("party_id", "Parte", "s"),
    "452": ("party_role", "Rol de la parte", "s"),
    "453": ("no_party_ids", "Cantidad de partes", "i"),
    "461": ("cfi_code", "Código CFI", "s"),
    "1128": ("appl_ver_id", "Versión de la aplicación", "s"),
    "1137": ("default_appl_ver_id", "Versión de la aplicación por defecto", "s"),
}
# Partes de la sesión que no dicen nada (largo del cuerpo y checksum).
IGNORAR = ("9", "10")

_SI_NO = {"Y": "Sí", "N": "No"}
ENUMS: dict[str, dict[str, str]] = {
    # En castellano, como el resto: el nombre estándar de FIX del reporte de
    # ejecución lleva `exec`, y CSS rechaza todo .conf que lo contenga (ver
    # conf_lint._VETADOS_CSS). El código original queda en `fix.msg_type_code`.
    "35": {"0": "Heartbeat", "1": "Pedido de prueba", "2": "Pedido de reenvío", "3": "Rechazo de sesión",
           "4": "Reinicio de secuencia", "5": "Logout", "A": "Logon", "D": "Orden nueva",
           "F": "Pedido de cancelación", "G": "Pedido de modificación", "8": "Reporte de ejecución",
           "9": "Rechazo de cancelación", "j": "Rechazo de negocio", "H": "Consulta de estado de orden",
           "q": "Cancelación masiva", "r": "Reporte de cancelación masiva", "V": "Pedido de market data",
           "W": "Market data (completo)", "X": "Market data (incremental)", "Y": "Rechazo de market data",
           "c": "Pedido de definición de instrumento", "d": "Definición de instrumento",
           "e": "Pedido de estado de instrumento", "f": "Estado de instrumento", "h": "Estado de la rueda",
           "x": "Pedido de lista de instrumentos", "y": "Lista de instrumentos", "AE": "Reporte de operación",
           "AR": "Acuse de reporte de operación", "BE": "Pedido de usuario", "BF": "Respuesta de usuario"},
    "39": {"0": "Nueva", "1": "Parcialmente ejecutada", "2": "Ejecutada", "3": "Terminada en el día",
           "4": "Cancelada", "5": "Reemplazada", "6": "Cancelación pendiente", "7": "Detenida", "8": "Rechazada",
           "9": "Suspendida", "A": "Alta pendiente", "B": "Calculada", "C": "Vencida", "E": "Reemplazo pendiente"},
    "150": {"0": "Nueva", "1": "Ejecución parcial", "2": "Ejecución total", "3": "Terminada en el día",
            "4": "Cancelada", "5": "Reemplazada", "6": "Cancelación pendiente", "7": "Detenida", "8": "Rechazada",
            "9": "Suspendida", "A": "Alta pendiente", "B": "Calculada", "C": "Vencida", "D": "Restablecida",
            "E": "Reemplazo pendiente", "F": "Operación", "G": "Corrección de operación",
            "H": "Anulación de operación", "I": "Estado de la orden"},
    "54": {"1": "Compra", "2": "Venta", "3": "Compra menos", "4": "Venta más", "5": "Venta en corto",
           "6": "Venta en corto exenta", "7": "No informado", "8": "Cruce", "9": "Cruce en corto"},
    "40": {"1": "Mercado", "2": "Límite", "3": "Stop", "4": "Stop límite", "K": "Mercado con resto límite"},
    "59": {"0": "Día", "1": "Hasta cancelar", "2": "En la apertura", "3": "Inmediata o cancelar",
           "4": "Todo o nada", "6": "Hasta la fecha", "7": "Al cierre"},
    "63": {"0": "Regular", "1": "Contado inmediato", "2": "24 horas", "3": "T+2", "4": "T+3", "5": "T+4",
           "6": "Fecha futura", "7": "Cuando se emita", "8": "Vendedor elige", "9": "T+5"},
    "102": {"0": "Demasiado tarde para cancelar", "1": "Orden desconocida", "2": "Opción del broker",
            "3": "Cancelación ya pendiente", "6": "ClOrdID duplicado", "99": "Otro"},
    "103": {"0": "Opción del broker", "1": "Instrumento desconocido", "2": "Mercado cerrado",
            "3": "Excede el límite de la orden", "4": "Demasiado tarde", "5": "Orden desconocida",
            "6": "Orden duplicada", "8": "Orden vieja", "11": "Característica no soportada",
            "13": "Cantidad incorrecta", "15": "Cuenta desconocida", "16": "Precio fuera de rango",
            "18": "Precio inválido", "99": "Otro"},
    "373": {"0": "Tag inválido", "1": "Falta un tag requerido", "2": "Tag no definido para el mensaje",
            "3": "Tag no definido", "4": "Tag sin valor", "5": "Valor incorrecto", "6": "Formato incorrecto",
            "9": "Problema de CompID", "10": "Problema de SendingTime", "11": "Tipo de mensaje inválido",
            "13": "Tag repetido", "99": "Otro"},
    "380": {"0": "Otro", "1": "ID desconocido", "2": "Instrumento desconocido", "3": "Tipo de mensaje no soportado",
            "4": "Aplicación no disponible", "5": "Falta un campo requerido", "6": "No autorizado"},
    "434": {"1": "Cancelación", "2": "Modificación"},
    "167": {"CS": "Acción", "GO": "Bono soberano", "CORP": "Bono corporativo", "FUT": "Futuro", "OPT": "Opción",
            "TB": "Letra", "CD": "Certificado de depósito", "MF": "Fondo"},
    "1128": {"6": "FIX44", "7": "FIX50", "8": "FIX50SP1", "9": "FIX50SP2"},
    "1137": {"6": "FIX44", "7": "FIX50", "8": "FIX50SP1", "9": "FIX50SP2"},
    "43": _SI_NO, "97": _SI_NO, "123": _SI_NO, "141": _SI_NO,
}
# Los mensajes de la sesión (no del negocio).
ADMIN = ("0", "1", "2", "3", "4", "5", "A")
# De qué tag sale el motivo de cada rechazo, si el texto (58) no vino.
_MOTIVO = {"8": "103", "9": "102", "3": "373", "j": "380"}

# Los eventos de QuickFIX/J (los de QuickFIX/n y C++ dicen lo mismo).
EVENTOS: list[tuple[str, str]] = [
    (r"MsgSeqNum too high", "secuencia_alta"),
    (r"MsgSeqNum too low", "secuencia_baja"),
    (r"Sent ResendRequest|Resending", "reenvio_pedido"),
    (r"Received ResendRequest", "reenvio_recibido"),
    (r"SequenceReset", "reinicio_de_secuencia"),
    (r"[Tt]imed out waiting for heartbeat|Heartbeat timeout", "heartbeat_vencido"),
    (r"Disconnect|disconnect", "desconexion"),
    (r"Initiated logon request|Received logon|Logon contains|[Ll]ogon response", "logon"),
    (r"[Ll]ogout", "logout"),
    (r"[Rr]eject|[Rr]echaz", "rechazo"),
    (r"Created session|Session .* schedule|Session state", "sesion"),
]

_SEP = re.compile(r"[|\x01]")
_HAY_FIX = re.compile(r"(?:^|[\s:|\x01])8=FIXT?\.\d")
# Los envoltorios: el primero que reconozca la mayoría de las líneas gana.
ENVOLTORIOS: dict[str, str] = {
    "slf4j": (r"^(?<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d{3})\s+(?<level>[A-Z]+)\s+\[(?<thread>[^\]]*)\]\s+"
              r"(?<logger>\S+)\s+-\s+(?:(?<session>FIXT?\.[\d.]+:[^\s:]+->[^\s:]+):\s+)?(?<payload>.*)$"),
    "filelog": r"^(?<ts>\d{8}-\d{2}:\d{2}:\d{2}(?:\.\d{3,9})?)\s*:\s+(?<payload>.*)$",
    "crudo": r"^(?<payload>8=FIXT?\..*)$",
}
# Python escribe los grupos con nombre como (?P<x>...); Ruby, como (?<x>...).
_PY = {k: re.compile(v.replace("(?<", "(?P<")) for k, v in ENVOLTORIOS.items()}
FORMATOS_DE_FECHA = {
    "slf4j": ['"yyyy-MM-dd HH:mm:ss.SSS"', '"yyyy-MM-dd HH:mm:ss,SSS"', '"yyyy-MM-dd\'T\'HH:mm:ss.SSS"'],
    "filelog": ['"yyyyMMdd-HH:mm:ss.SSS"', '"yyyyMMdd-HH:mm:ss"'],
}
# Un log de aplicación (SLF4J) va en la hora del servidor; FIX y su FileLog, en UTC.
ZONA_DEL_LOG = "America/Argentina/Buenos_Aires"


# ── Detección ───────────────────────────────────────────────────────────────
def envoltorio(lineas: list[str]) -> str:
    """Cuál de los tres envoltorios tienen la mayoría de las líneas, o ""."""
    datos = [l.rstrip("\r\n") for l in lineas if l.strip()][:500]
    if not datos:
        return ""
    votos = Counter(next((k for k, rx in _PY.items() if rx.match(l)), "") for l in datos)
    k, n = votos.most_common(1)[0]
    return k if k and n >= max(1, int(len(datos) * 0.8)) else ""


def es_fix(lineas: list[str]) -> bool:
    """Un log FIX: un envoltorio conocido y, en al menos un tercio de las
    líneas, un mensaje FIX (el resto pueden ser eventos de la sesión)."""
    datos = [l for l in lineas if l.strip()][:500]
    if not datos or not envoltorio(datos):
        return False
    return sum(1 for l in datos if _HAY_FIX.search(l) and "35=" in l) >= max(1, len(datos) // 3)


# ── Decodificación (la misma que hace el ruby, para armar los campos y probar) ─
def _fecha_fix(valor: str) -> str | None:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?$", valor or "")
    if not m:
        return None
    ms = (m.group(7) or "0").ljust(3, "0")[:3]
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}.{ms}Z"


def decodificar(linea: str, tipo_de_envoltorio: str) -> dict:
    """La línea como la deja el ruby (sin la latencia, que necesita @timestamp):
    `{"fix.msg_type": "Orden nueva", ...}`."""
    out: dict = {}
    m = _PY[tipo_de_envoltorio].match(linea.rstrip("\r\n")) if tipo_de_envoltorio in _PY else None
    if not m:
        return out
    g = m.groupdict()
    if g.get("level"):
        out["log.level"] = g["level"]
    if g.get("logger"):
        out["log.logger"] = g["logger"]
    if g.get("session"):
        out["fix.sesion"] = g["session"]
    payload = g.get("payload") or ""
    if not _HAY_FIX.search(" " + payload):
        out["fix.evento"] = payload
        out["fix.capa"] = "evento"
        if g.get("session"):
            # La sesión dice quién opera y con quién: un evento (una desconexión,
            # un salto de secuencia) también se reparte por mercado y participante.
            propio, _, contra = g["session"].split(":", 1)[-1].partition("->")
            out["fix.comp_id_propio"], out["fix.contraparte"] = propio, contra
        out["fix.tipo_evento"] = next((t for rx, t in EVENTOS if re.search(rx, payload)), "otro")
        if out["fix.tipo_evento"] == "rechazo":
            # Un rechazo que no viene en un mensaje FIX (el control de riesgo del
            # propio sistema) se cuenta y se busca junto con los del mercado.
            out["fix.rechazo_motivo"] = payload
        salto = re.search(r"expecting (\d+) but received (\d+)", payload, re.I)
        if salto:
            out["fix.salto_secuencia"] = int(salto.group(2)) - int(salto.group(1))
        return out
    pares: dict[str, str] = {}
    for parte in _SEP.split(payload[payload.index("8=FIX"):]):
        tag, _, valor = parte.partition("=")
        if tag.isdigit() and "=" in parte and tag not in pares:
            pares[tag] = valor
    for tag, valor in pares.items():
        if tag in IGNORAR:
            continue
        nombre, _, tipo = TAGS.get(tag, (f"tag_{tag}", "", "s"))
        if tag == "35":
            out["fix.msg_type_code"] = valor
        v = ENUMS.get(tag, {}).get(valor, valor)
        if tipo == "f":
            try:
                v = float(valor)
            except ValueError:
                continue
        elif tipo == "i":
            try:
                v = int(valor)
            except ValueError:
                continue
        elif tipo == "d":
            v = _fecha_fix(valor)
            if v is None:
                continue
        out[f"fix.{nombre}"] = v
    codigo = pares.get("35", "")
    out["fix.capa"] = "sesión" if codigo in ADMIN else "aplicación"
    nuestro = (g.get("session") or "").split(":", 1)[-1].split("->")[0] if g.get("session") else ""
    logger = g.get("logger") or ""
    if nuestro:
        out["fix.direccion"] = "saliente" if pares.get("49") == nuestro else "entrante"
    elif "incoming" in logger:
        out["fix.direccion"] = "entrante"
    elif "outgoing" in logger:
        out["fix.direccion"] = "saliente"
    if out.get("fix.direccion"):
        out["fix.contraparte"] = pares.get("56" if out["fix.direccion"] == "saliente" else "49", "")
        # Quién opera de este lado: en el log de una firma, siempre el mismo; en
        # el de un OMS que opera para muchos participantes, cada uno de ellos.
        out["fix.comp_id_propio"] = nuestro or pares.get("49" if out["fix.direccion"] == "saliente" else "56", "")
    if codigo in _MOTIVO and (codigo != "8" or pares.get("150") == "8"):
        motivo = pares.get("58") or ENUMS.get(_MOTIVO[codigo], {}).get(pares.get(_MOTIVO[codigo], ""), "")
        if motivo:
            out["fix.rechazo_motivo"] = motivo
    if "fix.last_qty" in out and "fix.last_px" in out and out["fix.last_qty"]:
        out["fix.monto_ejecutado"] = round(out["fix.last_qty"] * out["fix.last_px"], 2)
    demora = demora_ms(out.get("fix.sending_time", ""), out.get("fix.transact_time", ""))
    if demora is not None:
        out["fix.demora_ms"] = demora
    return out


def demora_ms(sending_iso: str, transact_iso: str) -> int | None:
    """Envío (tag 52) menos transacción (tag 60), en ms: lo que tardó el mensaje
    en salir desde que pasó lo que cuenta. En las órdenes que un OMS manda al
    mercado (con la hora en que la orden entró), es el tiempo adentro del OMS."""
    try:
        a = datetime.strptime(sending_iso, "%Y-%m-%dT%H:%M:%S.%fZ")
        b = datetime.strptime(transact_iso, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    ms = round((a - b).total_seconds() * 1000)
    return ms if -60_000 <= ms <= 60_000 else None


def latencia_ms(momento_utc: datetime, sending_time_iso: str) -> int | None:
    """Recibido menos enviado (tag 52), en ms; None si no es creíble (relojes
    desfasados más de un minuto, o un reenvío viejo)."""
    try:
        enviado = datetime.strptime(sending_time_iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    ms = round((momento_utc - enviado).total_seconds() * 1000)
    return ms if -60_000 <= ms <= 60_000 else None


# ── El .conf ────────────────────────────────────────────────────────────────
def _ruby_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ruby_init(tipo_de_envoltorio: str) -> str:
    tags = ", ".join(f"{_ruby_str(t)} => [{_ruby_str(n)}, {_ruby_str(k)}]" for t, (n, _, k) in TAGS.items())
    enums = ", ".join(f"{_ruby_str(t)} => {{" + ", ".join(f"{_ruby_str(c)} => {_ruby_str(v)}" for c, v in e.items()) + "}"
                      for t, e in ENUMS.items())
    eventos = ", ".join(f"[/{rx}/, {_ruby_str(t)}]" for rx, t in EVENTOS)
    return "\n".join([
        f"        FIX_TAGS = {{{tags}}}",
        f"        FIX_ENUMS = {{{enums}}}",
        f"        FIX_ADMIN = [{', '.join(_ruby_str(a) for a in ADMIN)}]",
        f"        FIX_MOTIVO = {{{', '.join(f'{_ruby_str(k)} => {_ruby_str(v)}' for k, v in _MOTIVO.items())}}}",
        f"        FIX_IGNORAR = [{', '.join(_ruby_str(i) for i in IGNORAR)}]",
        f"        FIX_EVENTOS = [{eventos}]",
        f"        FIX_LINEA = /{ENVOLTORIOS[tipo_de_envoltorio]}/",
    ])


# El cuerpo del ruby. Sin comillas simples: va dentro de `code => '...'`.
_RUBY_CODIGO = r"""
        m = FIX_LINEA.match(event.get("message").to_s.chomp)
        if m.nil?
          event.tag("_fix_sin_formato")
        else
          g = {}
          m.names.each { |n| g[n] = m[n] }
          event.set("[@metadata][ts]", g["ts"]) if g["ts"]
          event.set("[log][level]", g["level"]) if g["level"]
          event.set("[log][logger]", g["logger"]) if g["logger"]
          event.set("[fix][sesion]", g["session"]) if g["session"]
          payload = g["payload"].to_s
          desde = payload.index("8=FIX")
          if desde.nil?
            event.set("[fix][evento]", payload)
            event.set("[fix][capa]", "evento")
            if g["session"]
              lados = g["session"].split(":", 2)[1].to_s.split("->")
              event.set("[fix][comp_id_propio]", lados[0].to_s)
              event.set("[fix][contraparte]", lados[1].to_s)
            end
            tipo = "otro"
            FIX_EVENTOS.each do |par|
              if payload =~ par[0]
                tipo = par[1]
                break
              end
            end
            event.set("[fix][tipo_evento]", tipo)
            event.set("[fix][rechazo_motivo]", payload) if tipo == "rechazo"
            s = payload.match(/expecting (\d+) but received (\d+)/i)
            event.set("[fix][salto_secuencia]", s[2].to_i - s[1].to_i) if s
          else
            pares = {}
            payload[desde..-1].split(/[|\x01]/).each do |parte|
              i = parte.index("=")
              next if i.nil? || i == 0
              tag = parte[0...i]
              next unless tag =~ /\A\d+\z/
              pares[tag] = parte[(i + 1)..-1] unless pares.key?(tag)
            end
            pares.each do |tag, valor|
              next if FIX_IGNORAR.include?(tag)
              info = FIX_TAGS[tag] || ["tag_" + tag, "s"]
              event.set("[fix][msg_type_code]", valor) if tag == "35"
              v = (FIX_ENUMS[tag] && FIX_ENUMS[tag][valor]) || valor
              case info[1]
              when "f"
                v = Float(valor) rescue nil
              when "i"
                v = Integer(valor, 10) rescue nil
              when "d"
                f = valor.match(/\A(\d{4})(\d{2})(\d{2})-(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?\z/)
                v = f ? "#{f[1]}-#{f[2]}-#{f[3]}T#{f[4]}:#{f[5]}:#{f[6]}.#{(f[7] || "0").ljust(3, "0")[0, 3]}Z" : nil
              end
              event.set("[fix][" + info[0] + "]", v) unless v.nil?
            end
            codigo = pares["35"].to_s
            event.set("[fix][capa]", FIX_ADMIN.include?(codigo) ? "sesión" : "aplicación")
            direccion = nil
            nuestro = nil
            if g["session"]
              nuestro = g["session"].split(":", 2)[1].to_s.split("->")[0]
              direccion = pares["49"] == nuestro ? "saliente" : "entrante"
            elsif g["logger"].to_s.include?("incoming")
              direccion = "entrante"
            elsif g["logger"].to_s.include?("outgoing")
              direccion = "saliente"
            end
            if direccion
              event.set("[fix][direccion]", direccion)
              event.set("[fix][contraparte]", pares[direccion == "saliente" ? "56" : "49"].to_s)
              propio = g["session"] ? nuestro : pares[direccion == "saliente" ? "49" : "56"].to_s
              event.set("[fix][comp_id_propio]", propio)
            end
            if FIX_MOTIVO.key?(codigo) && (codigo != "8" || pares["150"] == "8")
              cod = pares[FIX_MOTIVO[codigo]]
              motivo = pares["58"] || (FIX_ENUMS[FIX_MOTIVO[codigo]] && FIX_ENUMS[FIX_MOTIVO[codigo]][cod.to_s])
              event.set("[fix][rechazo_motivo]", motivo) if motivo && !motivo.empty?
            end
            q = event.get("[fix][last_qty]")
            px = event.get("[fix][last_px]")
            event.set("[fix][monto_ejecutado]", (q * px).round(2)) if q.is_a?(Numeric) && px.is_a?(Numeric) && q > 0
          end
        end
"""

# Después del `date`: la latencia de lo que entra (recibido menos el tag 52).
_RUBY_LATENCIA = r"""
        enviado = event.get("[fix][sending_time]")
        hecho = event.get("[fix][transact_time]")
        if enviado && hecho
          a = enviado.match(/\A(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z\z/)
          b = hecho.match(/\A(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z\z/)
          if a && b
            ta = Time.utc(a[1].to_i, a[2].to_i, a[3].to_i, a[4].to_i, a[5].to_i, a[6].to_i, a[7].to_i * 1000)
            tb = Time.utc(b[1].to_i, b[2].to_i, b[3].to_i, b[4].to_i, b[5].to_i, b[6].to_i, b[7].to_i * 1000)
            d = ((ta - tb) * 1000).round
            event.set("[fix][demora_ms]", d) if d >= -60000 && d <= 60000
          end
        end
        if event.get("[fix][direccion]") == "entrante" && enviado
          f = enviado.match(/\A(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z\z/)
          if f
            t0 = Time.utc(f[1].to_i, f[2].to_i, f[3].to_i, f[4].to_i, f[5].to_i, f[6].to_i, f[7].to_i * 1000)
            ms = ((event.get("@timestamp").time - t0) * 1000).round
            event.set("[fix][latencia_ms]", ms) if ms >= -60000 && ms <= 60000
          end
        end
"""


def armar_filter(tipo_de_envoltorio: str) -> str:
    fecha = ""
    if tipo_de_envoltorio in FORMATOS_DE_FECHA:
        zona = ZONA_DEL_LOG if tipo_de_envoltorio == "slf4j" else "UTC"
        fecha = (f'  date {{ match => ["[@metadata][ts]", {", ".join(FORMATOS_DE_FECHA[tipo_de_envoltorio])}] '
                 f'timezone => "{zona}" target => "@timestamp" }}\n')
    else:
        # Un mensaje solo no trae la hora del log: la del envío (tag 52, UTC).
        fecha = '  date { match => ["[fix][sending_time]", "ISO8601"] target => "@timestamp" }\n'
    return (
        "filter {\n"
        "  ruby {\n"
        f"    init => '\n{_ruby_init(tipo_de_envoltorio)}\n      '\n"
        f"    code => '{_RUBY_CODIGO}      '\n"
        "  }\n"
        + fecha +
        "  ruby {\n"
        f"    code => '{_RUBY_LATENCIA}      '\n"
        "  }\n"
        "}\n"
    )


# ── Los campos del paso 2 ───────────────────────────────────────────────────
_DERIVADOS: dict[str, tuple[str, str]] = {
    "log.level": ("Nivel del log", "keyword"),
    "log.logger": ("Logger", "keyword"),
    "fix.sesion": ("Sesión FIX", "keyword"),
    "fix.msg_type_code": ("Código del tipo de mensaje", "keyword"),
    "fix.capa": ("Capa (sesión, aplicación o evento)", "keyword"),
    "fix.direccion": ("Dirección", "keyword"),
    "fix.contraparte": ("Mercado (contraparte)", "keyword"),
    "fix.rechazo_motivo": ("Motivo del rechazo", "text"),
    "fix.monto_ejecutado": ("Monto ejecutado", "float"),
    "fix.latencia_ms": ("Latencia (ms)", "long"),
    "fix.demora_ms": ("Demora desde la transacción (ms)", "long"),
    "fix.comp_id_propio": ("Participante (CompID propio)", "keyword"),
    "fix.evento": ("Evento de la sesión", "text"),
    "fix.tipo_evento": ("Tipo de evento", "keyword"),
    "fix.salto_secuencia": ("Salto de secuencia", "long"),
}
_TIPO_OS = {"s": "keyword", "t": "text", "f": "float", "i": "long", "d": "date"}
_ROLES = {
    "fix.account": "entity_id", "fix.symbol": "primary_dimension", "fix.ord_status": "success_indicator",
    "fix.rechazo_motivo": "critical_indicator", "fix.latencia_ms": "measure", "fix.monto_ejecutado": "measure",
    "fix.order_qty": "measure", "fix.last_qty": "measure", "fix.demora_ms": "measure",
}
_DIMENSIONES = {"fix.msg_type", "fix.side", "fix.ord_type", "fix.time_in_force", "fix.settl_type", "fix.currency",
                "fix.contraparte", "fix.sesion", "fix.direccion", "fix.capa", "fix.tipo_evento", "fix.tipo_ejecucion",
                "fix.ord_status", "fix.symbol", "fix.security_type", "fix.security_exchange", "log.level",
                "fix.begin_string", "fix.msg_type_code", "fix.comp_id_propio"}


# Lo que tiene cualquier log FIX de ruteo de órdenes, aunque la muestra (unas
# doscientas líneas) no lo haya mostrado: un rechazo o un plazo de liquidación
# pueden no caer en ella, y sin declararlos se pierden su etiqueta y su rol.
_NUCLEO = {"fix.msg_type", "fix.msg_type_code", "fix.capa", "fix.direccion", "fix.contraparte",
           "fix.sender_comp_id", "fix.target_comp_id", "fix.msg_seq_num", "fix.sending_time", "fix.poss_dup_flag",
           "fix.comp_id_propio"}
_NUCLEO_ORDENES = {"fix.cl_ord_id", "fix.orig_cl_ord_id", "fix.order_id", "fix.id_ejecucion", "fix.account", "fix.symbol",
                   "fix.side", "fix.order_qty", "fix.price", "fix.ord_type", "fix.time_in_force", "fix.tipo_ejecucion",
                   "fix.ord_status", "fix.last_qty", "fix.last_px", "fix.cum_qty", "fix.leaves_qty", "fix.avg_px",
                   "fix.text", "fix.ord_rej_reason", "fix.rechazo_motivo", "fix.monto_ejecutado", "fix.latencia_ms",
                   "fix.settl_type", "fix.currency", "fix.demora_ms", "fix.transact_time"}
_NUCLEO_SLF4J = {"fix.sesion", "fix.evento", "fix.tipo_evento", "fix.salto_secuencia", "log.level", "log.logger"}
_ORDENES = {"D", "8", "F", "G", "9"}


def campos(lineas: list[str], tipo_de_envoltorio: str) -> list[dict]:
    """Los campos de la muestra más el núcleo de FIX, en el orden del diccionario."""
    vistos: set[str] = set()
    propios: set[str] = set()
    for l in lineas[:3000]:
        d = decodificar(l, tipo_de_envoltorio)
        vistos |= set(d)
        if d.get("fix.comp_id_propio"):
            propios.add(d["fix.comp_id_propio"])
    # Con varios participantes (un OMS que opera para muchos), la entidad es el
    # participante; con uno solo (el log de una firma), la cuenta.
    entidad = "fix.comp_id_propio" if len(propios) >= 3 else "fix.account"
    vistos |= _NUCLEO
    if any(decodificar(l, tipo_de_envoltorio).get("fix.msg_type_code") in _ORDENES for l in lineas[:3000]):
        vistos |= _NUCLEO_ORDENES
    if tipo_de_envoltorio == "slf4j":
        vistos |= _NUCLEO_SLF4J
    por_nombre = {f"fix.{n}": (e, _TIPO_OS[k]) for n, e, k in TAGS.values()}
    salida = []
    orden = list(por_nombre) + list(_DERIVADOS) + sorted(v for v in vistos if v.startswith("fix.tag_"))
    for ruta in orden:
        if ruta not in vistos:
            continue
        etiqueta, tipo = por_nombre.get(ruta) or _DERIVADOS.get(ruta) or (f"Tag {ruta.split('_')[-1]}", "keyword")
        rol = _ROLES.get(ruta)
        if ruta in ("fix.account", "fix.comp_id_propio"):
            rol = "entity_id" if ruta == entidad else None
        if tipo == "date":
            rol = None             # la fecha del evento es @timestamp, no la del mensaje
        campo = {"raw_name": ruta.split(".", 1)[1], "field_path": ruta, "ecs_path": ruta, "normalized_path": ruta,
                 "type": tipo, "business_label": etiqueta,
                 "unit": "ms" if ruta in ("fix.latencia_ms", "fix.demora_ms") else None,
                 "dimension": ruta in _DIMENSIONES, "role": rol, "is_ecs": ruta.startswith("log."),
                 "ecs_type_official": None, "ecs_overlay_path": None}
        if ruta == entidad:
            campo["entity"] = True
        if ruta == "fix.account":
            campo["sensitive"] = True           # el comitente es un dato del cliente: va enmascarado
        salida.append(campo)
    return salida


# Lo primero que se ve en el paso 2, y el orden importa: la primera medida es
# la que se pronostica y vigila (sumada), así que va el monto ejecutado (el
# volumen del negocio) y no la latencia, que sumada no significa nada (el
# rollup sí la guarda con su promedio y su máximo por hora).
_PRIMERO = ["fix.msg_type", "fix.ord_status", "fix.tipo_ejecucion", "fix.symbol", "fix.side",
            "fix.comp_id_propio", "fix.account", "fix.monto_ejecutado", "fix.latencia_ms", "fix.demora_ms", "fix.last_qty", "fix.last_px", "fix.order_qty", "fix.price",
            "fix.rechazo_motivo", "fix.text", "fix.contraparte", "fix.direccion", "fix.capa", "fix.sesion",
            "fix.tipo_evento", "fix.evento", "fix.salto_secuencia", "fix.settl_type", "fix.currency", "fix.ord_type",
            "fix.time_in_force"]


def generar(lineas: list[str]) -> dict:
    """Lo que devuelve un generador del catálogo: filter_code + fields."""
    tipo = envoltorio(lineas)
    lista = campos(lineas, tipo)
    lista.sort(key=lambda f: _PRIMERO.index(f["field_path"]) if f["field_path"] in _PRIMERO else len(_PRIMERO))
    return {"filter_code": armar_filter(tipo), "fields": lista, "multiline_hint": False,
            "formato": f"fix_{tipo}"}
