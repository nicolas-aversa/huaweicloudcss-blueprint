"""
build_fix_dataset.py
====================

Dataset sintético del log de un OMS (sistema de gestión de órdenes) que rutea a
los mercados en nombre de 85 ALyCs (agentes de liquidación y compensación). Cada
ALyC opera en el mercado con su propio CompID, así que el OMS mantiene una sesión
FIX por ALyC y por mercado: `FIXT.1.1:AL017->A3` (derivados: dólar, índice,
agro) y, para las 30 más activas, `FIX.4.4:AL017->BYMA` (acciones, bonos y
CEDEARs).

Es el log de SLF4J/logback de QuickFIX/J tal como queda en el servidor del OMS:
una línea por mensaje o evento, con la hora local, el nivel, el hilo, el logger y
la sesión adelante. Con más de cien sesiones los heartbeats no se loguean
(`SLF4JLogHeartbeats=N`), pero consumen números de secuencia. El SOH va como `|`.
Además van las líneas del propio OMS (`oms.riesgo`, `oms.ruteo`, `oms.jvm`): el
control de riesgo previo rechaza órdenes antes de que lleguen al mercado.

Las órdenes llevan la ALyC como parte (453/448/452) y la hora en que entraron al
OMS (tag 60): el envío al mercado (tag 52) menos esa hora es el tiempo adentro
del OMS. Los mensajes son FIX válidos (BodyLength y CheckSum calculados). Todo es
inventado: ALyCs (AL001…AL085), cuentas, operadores, órdenes.

Historias para la demo:
  - 14/10 12:31 — el gateway de A3 se reinicia: se caen las 85 sesiones a la vez,
    reconectan, y las que perdieron mensajes piden reenvío (PossDupFlag=Y);
  - 15/10 10:58–11:18 — la sesión de AL033 con A3 queda trabada (MsgSeqNum too
    low tras un reinicio mal hecho): logon, logout, logon… y sus órdenes se rechazan;
  - 16/10 15:00–15:20 — una pausa de la JVM del OMS: la demora interna salta de
    milisegundos a cientos, para todas las ALyCs;
  - 19/10 14:10–14:40 — TXAR suspendida en BYMA: ola de rechazos del mercado;
  - 20/10 13:05–13:10 — el algoritmo de AL012 pega en el límite de mensajes por
    segundo de A3;
  - 13/10 11:30–11:45 — una integración nueva de AL058 manda órdenes sin cuenta
    (tag 1): rechazos de sesión.

Uso: py build_fix_dataset.py  → datasets/fix-oms.log
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

SEMILLA = 20261021
DIAS = [date(2026, 10, d) for d in (13, 14, 15, 16, 19, 20)]
UTC = timedelta(hours=3)                    # Argentina, sin horario de verano
SALIDA = Path(__file__).parent / "datasets" / "fix-oms.log"
N_ALYCS = 85
N_CON_BYMA = 30
HB = 30                                     # HeartBtInt (s): consume secuencias aunque no se loguee
ORDENES_POR_DIA = 5200

FUTUROS = {"DLR/NOV26": 1495.0, "DLR/DIC26": 1540.0, "DLR/ENE27": 1588.0, "RFX20/DIC26": 182000.0,
           "SOJ.ROS/MAY27": 312.5, "MAI.ROS/ABR27": 176.0, "TRI.ROS/ENE27": 201.0, "GGAL/DIC26": 6450.0}
ACCIONES = {"GGAL": 6200, "YPFD": 42000, "PAMP": 3600, "BMA": 9800, "TXAR": 1050, "ALUA": 980, "CEPU": 1450,
            "TGSU2": 6100, "LOMA": 2300, "BBAR": 7300, "SUPV": 2700, "CRES": 1450, "COME": 72, "EDN": 1950}
BONOS = {"AL30": 82000, "GD30": 86000, "AL35": 76000, "GD35": 79000, "AE38": 81000, "GD41": 74000}
BONOS_USD = {"AL30D": 62.5, "GD30D": 66.1, "AL35D": 57.9}
CEDEARS = {"AAPL": 17800, "MSFT": 21500, "KO": 18200, "MELI": 24500, "SPY": 39800, "NVDA": 9100}
BASE = {**FUTUROS, **ACCIONES, **BONOS, **BONOS_USD, **CEDEARS}

RECHAZOS_DEL_MERCADO = [   # (103, textos)
    ("16", ["Precio fuera de banda de fluctuación", "Price out of range", "El precio supera el límite de variación diaria"]),
    ("13", ["Cantidad menor al lote mínimo", "Invalid order quantity"]),
    ("2", ["Mercado cerrado para el instrumento", "Market closed"]),
    ("99", ["Excede el límite de posición del participante", "Position limit exceeded"]),
]
RECHAZOS_DE_RIESGO = [
    "Saldo insuficiente para la operación", "Fondos insuficientes en la cuenta comitente", "Insufficient buying power",
    "Excede el poder de compra disponible", "Sin tenencia suficiente para vender", "Tenencia insuficiente de la especie",
    "Excede el límite de riesgo por especie", "Orden supera el límite de exposición del comitente",
    "Cuenta comitente inhabilitada para operar", "Cuenta bloqueada por compliance",
    "Margen de garantía insuficiente para el futuro", "Garantías insuficientes en el mercado de derivados",
]


@dataclass
class Sesion:
    alyc: str
    mercado: str                      # "A3" | "BYMA"
    begin: str

    @property
    def nombre(self) -> str:
        return f"{self.begin}:{self.alyc}->{self.mercado}"


@dataclass
class Msg:
    sesion: Sesion
    saliente: bool
    t_envio: datetime                 # hora local en que lo mandó quien lo mandó
    t_log: datetime                   # hora local en que quedó en nuestro log
    campos: list                      # [(tag, valor)] del cuerpo, sin 8/9/34/49/52/56/10
    reenvio_de: datetime | None = None
    seq: int = 0
    perdido: bool = False             # se mandó con la sesión caída: llega después, reenviado

    @property
    def tipo(self) -> str:
        return dict(self.campos)["35"]


@dataclass
class Linea:
    t_log: datetime
    texto: str


@dataclass
class Dia:
    msgs: list = field(default_factory=list)
    lineas: list = field(default_factory=list)      # eventos de QuickFIX/J y líneas del OMS


def fix_ts(t_local: datetime) -> str:
    u = t_local + UTC
    return u.strftime("%Y%m%d-%H:%M:%S.") + f"{u.microsecond // 1000:03d}"


def armar(m: Msg) -> str:
    """El mensaje FIX completo, con BodyLength y CheckSum calculados sobre SOH."""
    s = m.sesion
    s49, s56 = (s.alyc, s.mercado) if m.saliente else (s.mercado, s.alyc)
    cab = [("35", m.tipo), ("49", s49), ("56", s56), ("34", str(m.seq))]
    if m.reenvio_de:
        cab.append(("43", "Y"))
    cab.append(("52", fix_ts(m.t_envio)))
    if m.reenvio_de:
        cab.append(("122", fix_ts(m.reenvio_de)))
    cuerpo = cab + [(t, v) for t, v in m.campos if t != "35"]
    texto = "".join(f"{t}={v}\x01" for t, v in cuerpo)
    cabeza = f"8={s.begin}\x019={len(texto.encode())}\x01"
    suma = sum((cabeza + texto).encode()) % 256
    return (cabeza + texto + f"10={suma:03d}\x01").replace("\x01", "|")


def evento(dia: Dia, sesion: Sesion | None, t: datetime, texto: str, error=False, hilo="QFJ Timer", logger=None):
    nivel = "ERROR" if error else "INFO "
    logger = logger or ("quickfixj.errorEvent" if error else "quickfixj.event")
    prefijo = f"{sesion.nombre}: " if sesion else ""
    dia.lineas.append(Linea(t, f"{nivel} [{hilo}] {logger} - {prefijo}{texto}"))


def curva(minuto: float, total: float) -> float:
    """Intensidad de la rueda: pico al abrir, valle al mediodía, pico al cierre."""
    x = minuto / total
    return (0.55 + 2.4 * math.exp(-x / 0.035) + 0.55 * math.exp(-((x - 0.97) / 0.05) ** 2)
            - 0.25 * math.exp(-((x - 0.42) / 0.12) ** 2))


class Rueda:
    def __init__(self, rnd: random.Random, dia: date, alycs, pesos, sesiones):
        self.r = rnd
        self.dia = dia
        self.alycs = alycs
        self.pesos = pesos
        self.sesiones = sesiones          # {(alyc, mercado): Sesion}
        self.out = Dia()
        self.n = 0
        self.precios = {k: float(v) for k, v in BASE.items()}
        self.cuentas = {a: [f"{rnd.randint(10000, 99999)}" for _ in range(rnd.randint(8, 60))] for a in alycs}
        self.operadores = {a: [f"{a}-OP{i:02d}" for i in range(1, rnd.randint(2, 6))] for a in alycs}
        self.pausa = None                 # (desde, hasta) de la pausa de la JVM
        self.trabada = None               # (alyc, desde, hasta): sesión con A3 que no levanta

    def t(self, h, m=0, s=0.0) -> datetime:
        return datetime.combine(self.dia, datetime.min.time()) + timedelta(hours=h, minutes=m, seconds=s)

    def precio(self, esp: str) -> float:
        p = self.precios[esp] * math.exp(self.r.gauss(0, 0.0012) + 0.02 * math.log(BASE[esp] / self.precios[esp]))
        self.precios[esp] = p
        return p

    def red(self, mercado) -> float:
        return (0.004 if mercado == "BYMA" else 0.006) * math.exp(self.r.gauss(0, 0.5))

    def adentro(self, t: datetime) -> float:
        """Lo que tarda la orden adentro del OMS (s): casi siempre ~1 ms."""
        if self.pausa and self.pausa[0] <= t <= self.pausa[1]:
            return self.r.uniform(0.08, 0.65)
        return 0.0006 * math.exp(self.r.gauss(0, 0.6))

    def msg(self, sesion, saliente, t_envio, campos, demora=0.0, **kw) -> Msg:
        m = Msg(sesion, saliente, t_envio, t_envio + timedelta(seconds=demora), campos, **kw)
        self.out.msgs.append(m)
        return m

    def oms(self, t, logger, texto, nivel="INFO ", hilo="ruteo-1"):
        self.out.lineas.append(Linea(t, f"{nivel} [{hilo}] {logger} - {texto}"))

    # ── Sesiones ──────────────────────────────────────────────────────────
    def abrir_y_cerrar(self, s: Sesion, desde: datetime, hasta: datetime):
        logon = [("35", "A"), ("98", "0"), ("108", str(HB)), ("141", "Y")] + ([("1137", "9")] if s.mercado == "A3" else [])
        evento(self.out, s, desde - timedelta(seconds=1), f"Session {s.nombre} schedule is daily, "
               f"{(desde + UTC):%H:%M:%S}-UTC - {(hasta + UTC):%H:%M:%S}-UTC")
        evento(self.out, s, desde, "Initiated logon request")
        self.msg(s, True, desde, logon)
        self.msg(s, False, desde + timedelta(seconds=0.015), logon, demora=self.red(s.mercado))
        evento(self.out, s, desde + timedelta(seconds=0.03), "Received logon")
        evento(self.out, s, hasta, "Initiated logout request")
        self.msg(s, True, hasta, [("35", "5")])
        self.msg(s, False, hasta + timedelta(seconds=0.02), [("35", "5")], demora=self.red(s.mercado))
        evento(self.out, s, hasta + timedelta(seconds=0.04), "Received logout response")
        evento(self.out, s, hasta + timedelta(seconds=0.05), "Disconnecting")

    # ── Órdenes ───────────────────────────────────────────────────────────
    def instrumento(self, mercado):
        if mercado == "A3":
            esp = self.r.choices(list(FUTUROS), weights=[30, 18, 8, 6, 10, 8, 6, 6])[0]
            return esp, "FUT", "ARS", None, max(1, int(self.r.lognormvariate(2.0, 1.0)))
        tipo = self.r.choices(["CS", "GO", "GOD", "CED"], weights=[55, 20, 10, 15])[0]
        plazo = "1" if self.r.random() < 0.25 else "2"
        if tipo == "CS":
            return self.r.choice(list(ACCIONES)), "CS", "ARS", plazo, max(1, int(self.r.lognormvariate(5.0, 1.3)))
        if tipo == "GO":
            return self.r.choice(list(BONOS)), "GO", "ARS", plazo, int(self.r.choice([1000, 5000, 10000, 50000]))
        if tipo == "GOD":
            return self.r.choice(list(BONOS_USD)), "GO", "USD", plazo, int(self.r.choice([100, 500, 1000, 5000]))
        return self.r.choice(list(CEDEARS)), "CS", "ARS", plazo, max(1, int(self.r.lognormvariate(3.0, 1.1)))

    def orden(self, t_entra: datetime, alyc: str, mercado: str, forzar: str | None = None):
        """Una orden que entra al OMS (de la pantalla o de la API de la ALyC) y
        todo lo que le pasa. `forzar`: "suspendida", "throttle", "sin_cuenta"."""
        s = self.sesiones[(alyc, mercado)]
        esp, tipo, moneda, plazo, qty = self.instrumento(mercado)
        if forzar == "suspendida":
            esp, tipo, moneda, plazo = "TXAR", "CS", "ARS", "2"
        cuenta = self.r.choice(self.cuentas[alyc])
        operador = self.r.choice(self.operadores[alyc])
        lado = "1" if self.r.random() < 0.52 else "2"
        px = self.precio(esp)
        mercado_ord = self.r.random() < 0.10 and forzar is None
        dec = 2 if px < 1000 else 1 if px < 10000 else 0
        px = round(px, dec)
        self.n += 1
        clid = f"{alyc}-{self.dia:%y%m%d}-{self.n:06d}"
        # 1) El control de riesgo del OMS, antes de que salga nada al mercado.
        p_riesgo = 0.03 + (0.03 if t_entra.hour == 11 and t_entra.minute < 15 else 0)
        if forzar is None and self.r.random() < p_riesgo:
            motivo = self.r.choice(RECHAZOS_DE_RIESGO)
            self.oms(t_entra + timedelta(seconds=0.0004), "oms.riesgo",
                     f"Orden {clid} rechazada por control de riesgo: {motivo} (participante {alyc}, "
                     f"comitente {cuenta}, {esp} {'compra' if lado == '1' else 'venta'} {qty})", hilo="riesgo-2")
            return
        if self.trabada and alyc == self.trabada[0] and mercado == "A3" and self.trabada[1] <= t_entra <= self.trabada[2]:
            self.oms(t_entra + timedelta(seconds=0.0003), "oms.ruteo",
                     f"Orden {clid} rechazada: la sesión {s.nombre} no está conectada (participante {alyc})",
                     nivel="WARN ")
            return
        # 2) Sale al mercado.
        t_sale = t_entra + timedelta(seconds=self.adentro(t_entra))
        d = [("35", "D"), ("11", clid)]
        if forzar != "sin_cuenta":
            d.append(("1", cuenta))
        d += [("453", "2"), ("448", alyc), ("447", "D"), ("452", "1"), ("448", operador), ("447", "D"), ("452", "11"),
              ("21", "1"), ("55", esp), ("207", "XBUE" if mercado == "BYMA" else "ROFX"), ("167", tipo), ("54", lado),
              ("38", str(qty)), ("40", "1" if mercado_ord else "2")]
        if not mercado_ord:
            d.append(("44", f"{px:.{dec}f}"))
        d += [("59", "0" if forzar else self.r.choices(["0", "3", "4"], weights=[90, 7, 3])[0]), ("60", fix_ts(t_entra))]
        if plazo:
            d.append(("63", plazo))
        d.append(("15", moneda))
        if mercado == "A3":
            d.append(("1128", "9"))
        m = self.msg(s, True, t_sale, d)
        t_ack = t_sale + timedelta(seconds=self.r.uniform(0.0015, 0.010))
        oid = f"{mercado}-{self.dia:%y%m%d}{self.n:07d}"
        comunes = [("37", oid), ("11", clid), ("1", cuenta), ("55", esp), ("54", lado), ("38", str(qty))]
        if not mercado_ord:
            comunes.append(("44", f"{px:.{dec}f}"))
        n_exec = iter(range(1, 99))

        def er(t_envio, extra, **kw):
            return self.msg(s, False, t_envio, [("35", "8")] + comunes + [("17", f"{oid}-{next(n_exec)}")] + extra,
                            demora=self.red(mercado), **kw)

        if forzar == "sin_cuenta":       # rechazo de sesión: falta un tag requerido
            self.msg(s, False, t_ack, [("35", "3"), ("45", "?"), ("371", "1"), ("372", "D"), ("373", "1"),
                                       ("58", "Required tag missing")], demora=self.red(mercado), )
            m.campos.append(("_ref", "rechazo_de_sesion"))
            return
        # 3) El mercado contesta.
        rechazo = None
        if forzar == "suspendida":
            rechazo = ("2", self.r.choice(["Instrumento suspendido por volatilidad", "Security halted - trading suspended",
                                           "TXAR en subasta de volatilidad"]))
        elif forzar == "throttle":
            rechazo = ("99", self.r.choice(["Throttle limit exceeded (50 msg/s)", "Límite de mensajes por segundo excedido"]))
        elif self.r.random() < 0.025:
            cod, textos = self.r.choice(RECHAZOS_DEL_MERCADO)
            rechazo = (cod, self.r.choice(textos))
        if rechazo:
            er(t_ack, [("150", "8"), ("39", "8"), ("103", rechazo[0]), ("58", rechazo[1]), ("151", "0"), ("14", "0"),
                       ("6", "0"), ("60", fix_ts(t_ack))])
            return
        er(t_ack, [("150", "0"), ("39", "0"), ("151", str(qty)), ("14", "0"), ("6", "0"), ("60", fix_ts(t_ack))])
        fin = self.t(17, 0)
        destino = self.r.choices(["total", "parcial_cancel", "cancel", "vence"], weights=[58, 9, 27, 6])[0]
        restante, acum, monto, t_ult = qty, 0, 0.0, t_ack
        if destino in ("total", "parcial_cancel"):
            partes = self.r.choices([1, 2, 3], weights=[65, 25, 10])[0]
            for i in range(partes):
                t_ult = t_ult + timedelta(seconds=self.r.expovariate(1 / (0.5 if mercado_ord else 25)))
                if t_ult >= fin:
                    break
                ultimo = i == partes - 1 and destino == "total"
                q = restante if ultimo else max(1, int(restante * self.r.uniform(0.2, 0.6)))
                lpx = round(self.precio(esp), dec)
                restante, acum, monto = restante - q, acum + q, monto + q * lpx
                er(t_ult, [("150", "F"), ("39", "2" if restante == 0 else "1"), ("32", str(q)),
                           ("31", f"{lpx:.{dec}f}"), ("151", str(restante)), ("14", str(acum)),
                           ("6", f"{monto / acum:.4f}"), ("60", fix_ts(t_ult))])
                if restante == 0:
                    return
            destino = "vence" if destino == "total" else "cancel"
        if destino == "cancel":
            t_cxl = t_ult + timedelta(seconds=self.r.expovariate(1 / 90))
            if t_cxl < fin:
                nuevo = f"{clid}-C"
                self.msg(s, True, t_cxl + timedelta(seconds=self.adentro(t_cxl)),
                         [("35", "F"), ("11", nuevo), ("41", clid), ("1", cuenta), ("55", esp), ("54", lado),
                          ("38", str(qty)), ("60", fix_ts(t_cxl))])
                t2 = t_cxl + timedelta(seconds=self.r.uniform(0.003, 0.012))
                if self.r.random() < 0.03:
                    self.msg(s, False, t2, [("35", "9"), ("37", oid), ("11", nuevo), ("41", clid), ("39", "2"),
                                            ("434", "1"), ("102", "0"), ("58", "Too late to cancel")],
                             demora=self.red(mercado))
                    return
                er(t2, [("150", "4"), ("39", "4"), ("41", clid), ("151", "0"), ("14", str(acum)), ("6", "0"),
                        ("60", fix_ts(t2))])
                return
        t_v = fin + timedelta(seconds=self.r.uniform(1, 20))
        er(t_v, [("150", "C"), ("39", "C"), ("151", "0"), ("14", str(acum)),
                 ("6", f"{(monto / acum) if acum else 0:.4f}"), ("60", fix_ts(t_v))])


def generar_dia(rnd: random.Random, dia: date, alycs, pesos, sesiones) -> Dia:
    r = Rueda(rnd, dia, alycs, pesos, sesiones)
    if dia == date(2026, 10, 16):
        r.pausa = (r.t(15, 0), r.t(15, 20))
        for k in range(9):
            r.oms(r.t(15, 0, 40 + k * 130), "oms.jvm", f"GC pause (G1 Evacuation Pause) {r.r.uniform(0.6, 2.4):.2f} s",
                  nivel="WARN ", hilo="jvm-monitor")
    if dia == date(2026, 10, 15):
        r.trabada = ("AL033", r.t(10, 58), r.t(11, 18))
    for s in sesiones.values():
        desde = r.t(10, 0) if s.mercado == "A3" else r.t(10, 30)
        r.abrir_y_cerrar(s, desde + timedelta(seconds=rnd.uniform(0, 4)), r.t(17, 15) + timedelta(seconds=rnd.uniform(0, 4)))
    actividad = rnd.uniform(0.85, 1.2)
    total_curva = sum(curva(m, 360) for m in range(360))
    for m in range(360):                      # de 11 a 17
        n = rnd.gauss(ORDENES_POR_DIA * actividad * curva(m, 360) / total_curva, 1.5)
        for _ in range(max(0, round(n))):
            t = r.t(11, m, rnd.uniform(0, 60))
            alyc = rnd.choices(alycs, weights=pesos)[0]
            mercado = "BYMA" if (alyc, "BYMA") in sesiones and rnd.random() < 0.55 else "A3"
            if dia == date(2026, 10, 14) and mercado == "A3" and r.t(12, 31, 5) <= t <= r.t(12, 32, 50):
                continue                      # A3 caído: el OMS no rutea
            r.orden(t, alyc, mercado)
    if dia == date(2026, 10, 19):
        for _ in range(160):
            alyc = rnd.choice([a for a in alycs if (a, "BYMA") in sesiones])
            r.orden(r.t(14, 10) + timedelta(seconds=rnd.uniform(0, 1800)), alyc, "BYMA", forzar="suspendida")
    if dia == date(2026, 10, 20):
        for _ in range(900):
            r.orden(r.t(13, 5) + timedelta(seconds=rnd.uniform(0, 300)), "AL012", "A3", forzar="throttle")
    if dia == date(2026, 10, 13):
        for _ in range(140):
            r.orden(r.t(11, 30) + timedelta(seconds=rnd.uniform(0, 900)), "AL058", "A3", forzar="sin_cuenta")
    if r.trabada:
        alyc, desde, hasta = r.trabada
        s = sesiones[(alyc, "A3")]
        t = desde
        while t < hasta:
            evento(r.out, s, t, "Initiated logon request")
            evento(r.out, s, t + timedelta(seconds=0.02), "MsgSeqNum too low, expecting 1520 but received 1", error=True)
            evento(r.out, s, t + timedelta(seconds=0.03), "Received logout request: MsgSeqNum too low, expecting 1520 but received 1")
            evento(r.out, s, t + timedelta(seconds=0.04), "Disconnecting")
            t += timedelta(seconds=30)
        evento(r.out, s, hasta, "Received SequenceReset FROM: 1 TO: 1520")
        evento(r.out, s, hasta + timedelta(seconds=0.01), "Received logon")
    return r.out


def secuencias(dia: Dia):
    """MsgSeqNum por sesión y dirección, en orden de envío. Cada logon reinicia, y
    cada intervalo sin mensajes consume uno por heartbeat (que no se loguea)."""
    por_flujo: dict = {}
    for m in dia.msgs:
        por_flujo.setdefault((id(m.sesion), m.saliente), []).append(m)
    for flujo in por_flujo.values():
        flujo.sort(key=lambda m: m.t_envio)
        seq, previo = 0, None
        for m in flujo:
            if m.tipo == "A":
                seq, previo = 0, None
            if previo is not None:
                seq += int((m.t_envio - previo).total_seconds() // HB)
            seq += 1
            m.seq, previo = seq, m.t_envio


def caida_de_a3(dia: Dia, sesiones, t_corte: datetime, t_vuelta: datetime, r: random.Random):
    """14/10: el gateway de A3 se reinicia. Todas las sesiones se cortan, vuelven
    escalonadas, y las que tenían algo en vuelo piden el reenvío."""
    for s in sesiones.values():
        if s.mercado != "A3":
            continue
        corte = t_corte + timedelta(seconds=r.uniform(0, 0.3))
        evento(dia, s, corte, "Disconnecting: Socket exception (Connection reset by peer)", error=True)
        vuelta = t_vuelta + timedelta(seconds=r.uniform(0, 25))
        evento(dia, s, corte + timedelta(seconds=30), "Initiated logon request")
        evento(dia, s, corte + timedelta(seconds=30.5), "Disconnecting: Socket exception (Connection refused)", error=True)
        entrantes = sorted((m for m in dia.msgs if m.sesion is s and not m.saliente), key=lambda m: m.t_envio)
        salientes = sorted((m for m in dia.msgs if m.sesion is s and m.saliente), key=lambda m: m.t_envio)
        perdidos = [m for m in entrantes if corte <= m.t_envio <= vuelta]
        for m in perdidos:
            m.perdido = True
        antes = [m for m in entrantes if m.t_envio < corte]
        esperado = (antes[-1].seq + 1) if antes else 1
        despues = [m for m in entrantes if m.t_envio > vuelta]
        # El logon de A3 trae la secuencia que sigue a lo que mandó mientras tanto.
        llegada = (max(m.seq for m in perdidos) + 1) if perdidos else esperado
        ult_sal = max((m.seq for m in salientes if m.t_envio < corte), default=0)
        evento(dia, s, vuelta, "Initiated logon request")
        dia.msgs.append(Msg(s, True, vuelta, vuelta, [("35", "A"), ("98", "0"), ("108", str(HB))], seq=ult_sal + 1))
        dia.msgs.append(Msg(s, False, vuelta + timedelta(seconds=0.02), vuelta + timedelta(seconds=0.03),
                            [("35", "A"), ("98", "0"), ("108", str(HB))], seq=llegada))
        evento(dia, s, vuelta + timedelta(seconds=0.031), "Received logon")
        corrimiento = 1
        if llegada > esperado:
            evento(dia, s, vuelta + timedelta(seconds=0.032),
                   f"MsgSeqNum too high, expecting {esperado} but received {llegada}", error=True)
            evento(dia, s, vuelta + timedelta(seconds=0.033), f"Sent ResendRequest FROM: {esperado} TO: infinity")
            dia.msgs.append(Msg(s, True, vuelta + timedelta(seconds=0.033), vuelta + timedelta(seconds=0.033),
                                [("35", "2"), ("7", str(esperado)), ("16", "0")], seq=ult_sal + 2))
            corrimiento = 2
            t_r = vuelta + timedelta(seconds=0.05)
            por_seq = {m.seq: m for m in perdidos}
            seq = esperado
            while seq < llegada:
                if seq in por_seq:
                    m = por_seq[seq]
                    m.reenvio_de, m.t_envio, m.t_log, m.perdido = m.t_envio, t_r, t_r + timedelta(seconds=0.004), False
                    seq += 1
                else:
                    hasta = seq
                    while hasta < llegada and hasta not in por_seq:
                        hasta += 1
                    dia.msgs.append(Msg(s, False, t_r, t_r + timedelta(seconds=0.004),
                                        [("35", "4"), ("123", "Y"), ("36", str(hasta))], reenvio_de=t_r, seq=seq))
                    seq = hasta
                t_r += timedelta(seconds=0.003)
            evento(dia, s, t_r + timedelta(seconds=0.01),
                   f"ResendRequest for messages FROM {esperado} TO {llegada - 1} has been satisfied.")
        for m in despues:
            m.seq += llegada - esperado + 1 if llegada > esperado else 1
        for m in salientes:
            if m.t_envio > vuelta:
                m.seq += corrimiento


def llegadas_en_orden(dia: Dia):
    """Una sesión FIX es una conexión TCP: lo que entra llega en el orden en que se mandó."""
    por_sesion: dict = {}
    for m in dia.msgs:
        if not m.saliente and not m.perdido and m.reenvio_de is None:
            por_sesion.setdefault(id(m.sesion), []).append(m)
    for flujo in por_sesion.values():
        previo = None
        for m in sorted(flujo, key=lambda m: (m.seq if m.tipo != "A" else -1, m.t_envio)):
            if m.tipo == "A":
                continue
            if previo is not None and m.t_log <= previo.t_log:
                m.t_log = previo.t_log + timedelta(microseconds=150)
            previo = m


def escribir(dia: Dia, f) -> int:
    filas = []
    for m in dia.msgs:
        if m.perdido or m.tipo == "0":
            continue
        if ("_ref", "rechazo_de_sesion") in m.campos:
            m.campos.remove(("_ref", "rechazo_de_sesion"))
        logger = "quickfixj.msg.outgoing" if m.saliente else "quickfixj.msg.incoming"
        filas.append((m.t_log, f"INFO  [QFJ Message Processor] {logger} - {m.sesion.nombre}: {armar(m)}"))
    filas += [(l.t_log, l.texto) for l in dia.lineas]
    filas.sort(key=lambda x: x[0])
    for t, texto in filas:
        f.write(f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond // 1000:03d} {texto}\n")
    return len(filas)


def rechazos_de_sesion(dia: Dia):
    """El 45 (RefSeqNum) de un rechazo de sesión es la secuencia de la orden rechazada."""
    ordenes = sorted((m for m in dia.msgs if m.saliente and ("_ref", "rechazo_de_sesion") in m.campos),
                     key=lambda m: m.t_envio)
    rechazos = sorted((m for m in dia.msgs if not m.saliente and m.tipo == "3"), key=lambda m: m.t_envio)
    for o, rj in zip(ordenes, rechazos):
        rj.campos = [(t, str(o.seq) if t == "45" else v) for t, v in rj.campos]


def main() -> None:
    rnd = random.Random(SEMILLA)
    alycs = [f"AL{i:03d}" for i in range(1, N_ALYCS + 1)]
    pesos = sorted((rnd.paretovariate(1.1) for _ in alycs), reverse=True)
    rnd.shuffle(pesos)
    activas = [a for _, a in sorted(zip(pesos, alycs), reverse=True)]
    for clave in ("AL012", "AL033", "AL058"):           # las de las historias operan bastante
        pesos[alycs.index(clave)] = max(pesos[alycs.index(clave)], sorted(pesos)[-20])
    sesiones = {(a, "A3"): Sesion(a, "A3", "FIXT.1.1") for a in alycs}
    sesiones.update({(a, "BYMA"): Sesion(a, "BYMA", "FIX.4.4") for a in activas[:N_CON_BYMA]})
    SALIDA.parent.mkdir(exist_ok=True)
    total = 0
    with SALIDA.open("w", encoding="utf-8", newline="\n") as f:
        for dia in DIAS:
            d = generar_dia(rnd, dia, alycs, pesos, sesiones)
            secuencias(d)
            rechazos_de_sesion(d)
            if dia == date(2026, 10, 14):
                base = datetime.combine(dia, datetime.min.time())
                caida_de_a3(d, sesiones, base + timedelta(hours=12, minutes=31, seconds=5),
                            base + timedelta(hours=12, minutes=32, seconds=50), random.Random(7))
            llegadas_en_orden(d)
            total += escribir(d, f)
    print(f"{SALIDA} · {total:,} líneas · {SALIDA.stat().st_size / 1e6:.1f} MB · {len(sesiones)} sesiones")


if __name__ == "__main__":
    main()
