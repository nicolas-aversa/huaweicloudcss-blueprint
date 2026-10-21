"""
build_fix_dataset.py
====================

Dataset sintético del log FIX de una ALyC (agente de liquidación y compensación)
que rutea órdenes a los mercados con QuickFIX/J. Es el log de SLF4J/logback de
QuickFIX/J tal como queda en un servidor: una línea por mensaje o evento, con la
hora local, el nivel, el hilo, el logger (`quickfixj.msg.incoming`,
`quickfixj.msg.outgoing`, `quickfixj.event`, `quickfixj.errorEvent`) y la sesión
adelante. El SOH de FIX va como `|` (el `%replace` de logback con que se suelen
exportar para leerlos).

Dos sesiones:
  - FIX.4.4:ALYC->BYMA — acciones, bonos y CEDEARs, plazo contado inmediato o
    24 horas, especies en pesos y en dólares (las `D`);
  - FIXT.1.1:ALYC->A3 — futuros de dólar (FIX 5.0 SP2).

Los mensajes son FIX de verdad: BodyLength y CheckSum calculados, secuencias por
sesión y dirección que arrancan en cada logon, heartbeats cada 30 s. Todo es
inventado (cuentas, operadores, órdenes): ningún dato de nadie.

Lo que trae para que la demo cuente algo:
  - la curva de una rueda (pico en la apertura, valle al mediodía, pico al cierre)
    y precios con ruido → pronósticos;
  - rechazos con motivos escritos de muchas formas, en castellano y en inglés
    («saldo insuficiente», «Insufficient buying power») → búsqueda por significado;
  - historias que se notan → anomalías, alertas y el perfil por cuenta:
      * 14/10 12:31 — BYMA deja de mandar heartbeats, se cae la sesión, al
        reconectar las secuencias saltan y QuickFIX/J pide el reenvío de las
        ejecuciones que se perdieron (llegan con PossDupFlag=Y);
      * 08/10 14:10–14:40 — TXAR suspendida: ola de rechazos;
      * 15 y 16/10 — una cuenta con patrón de layering: cientos de órdenes que
        se cancelan a los segundos;
      * 19/10 15:00–15:45 — latencia degradada con A3 (y TestRequests).

Uso: py build_fix_dataset.py  → datasets/fix-quickfixj.log
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

SEMILLA = 20261021
DIAS = [date(2026, 10, d) for d in (7, 8, 9, 13, 14, 15, 16, 19, 20)]   # el 12 es feriado
UTC = timedelta(hours=3)                    # Argentina, sin horario de verano
SALIDA = Path(__file__).parent / "datasets" / "fix-quickfixj.log"

BYMA = ("FIX.4.4:ALYC->BYMA", "FIX.4.4", "BYMA")
A3 = ("FIXT.1.1:ALYC->A3", "FIXT.1.1", "A3")

# especie → (precio base, tipo, moneda, lote típico)
ACCIONES = {"GGAL": 6200, "YPFD": 42000, "PAMP": 3600, "BMA": 9800, "TXAR": 1050, "ALUA": 980, "CEPU": 1450,
            "TGSU2": 6100, "LOMA": 2300, "BBAR": 7300, "SUPV": 2700, "CRES": 1450, "COME": 72, "EDN": 1950,
            "METR": 2100}
BONOS = {"AL30": 82000, "GD30": 86000, "AL35": 76000, "GD35": 79000, "AE38": 81000, "GD41": 74000}
BONOS_USD = {"AL30D": 62.5, "GD30D": 66.1, "AL35D": 57.9}
CEDEARS = {"AAPL": 17800, "MSFT": 21500, "KO": 18200, "MELI": 24500, "SPY": 39800, "NVDA": 9100}
FUTUROS = {"DLR/NOV26": 1495.0, "DLR/DIC26": 1540.0, "DLR/ENE27": 1588.0}

RECHAZOS = [   # (103, textos posibles)
    ("99", ["Saldo insuficiente para la operación", "Fondos insuficientes en la cuenta comitente",
            "Insufficient buying power", "Excede el poder de compra disponible", "Sin saldo en pesos para operar"]),
    ("99", ["Sin tenencia suficiente para vender", "Tenencia insuficiente de la especie",
            "Insufficient position to sell"]),
    ("16", ["Precio fuera de banda de fluctuación", "Price out of range", "El precio supera el límite de variación diaria"]),
    ("3", ["Excede límite de riesgo por especie", "Orden supera el límite de exposición del comitente",
           "Risk limit exceeded"]),
    ("13", ["Cantidad menor al lote mínimo", "Invalid order quantity", "La cantidad no respeta el lote de la especie"]),
    ("15", ["Cuenta comitente inhabilitada", "Cuenta bloqueada por compliance", "Unknown account"]),
    ("6", ["Orden duplicada (ClOrdID repetido)", "Duplicate ClOrdID"]),
]
PESOS_RECHAZO = [40, 14, 16, 10, 8, 7, 5]


@dataclass
class Msg:
    sesion: tuple
    saliente: bool
    t_envio: datetime                 # hora local en que lo mandó quien lo mandó
    t_log: datetime                   # hora local en que quedó en nuestro log
    campos: list                      # [(tag, valor)] del cuerpo, sin 8/9/34/49/52/56/10
    reenvio_de: datetime | None = None   # PossDup: la hora original
    seq: int = 0
    perdido: bool = False             # se mandó durante la caída: llega después, reenviado


@dataclass
class Evento:
    sesion: tuple
    t_log: datetime
    texto: str
    error: bool = False
    hilo: str = "QFJ Timer"


@dataclass
class Dia:
    msgs: list = field(default_factory=list)
    eventos: list = field(default_factory=list)


def fix_ts(t_local: datetime) -> str:
    u = t_local + UTC
    return u.strftime("%Y%m%d-%H:%M:%S.") + f"{u.microsecond // 1000:03d}"


def armar(m: Msg) -> str:
    """El mensaje FIX completo, con BodyLength y CheckSum calculados sobre SOH."""
    _, begin, contra = m.sesion
    s49, s56 = ("ALYC", contra) if m.saliente else (contra, "ALYC")
    cab = [("35", dict(m.campos)["35"]), ("49", s49), ("56", s56), ("34", str(m.seq))]
    if m.reenvio_de:
        cab.append(("43", "Y"))
    cab.append(("52", fix_ts(m.t_envio)))
    if m.reenvio_de:
        cab.append(("122", fix_ts(m.reenvio_de)))
    cuerpo = cab + [(t, v) for t, v in m.campos if t != "35"]
    texto = "".join(f"{t}={v}\x01" for t, v in cuerpo)
    cabeza = f"8={begin}\x019={len(texto.encode())}\x01"
    suma = sum((cabeza + texto).encode()) % 256
    return (cabeza + texto + f"10={suma:03d}\x01").replace("\x01", "|")


class Rueda:
    def __init__(self, rnd: random.Random, dia: date):
        self.r = rnd
        self.dia = dia
        self.out = Dia()
        self.n_orden = 0
        self.n_exec = 0
        self.precios = {**{k: float(v) for k, v in ACCIONES.items()}, **{k: float(v) for k, v in BONOS.items()},
                        **BONOS_USD, **{k: float(v) for k, v in CEDEARS.items()}, **FUTUROS}

    def t(self, h: int, m: int = 0, s: float = 0.0) -> datetime:
        return datetime.combine(self.dia, datetime.min.time()) + timedelta(hours=h, minutes=m, seconds=s)

    def precio(self, esp: str) -> float:
        """Paseo aleatorio con reversión: se mueve, pero no se escapa."""
        base = {**ACCIONES, **BONOS, **BONOS_USD, **CEDEARS, **FUTUROS}[esp]
        p = self.precios[esp]
        p *= math.exp(self.r.gauss(0, 0.0012) + 0.02 * math.log(base / p))
        self.precios[esp] = p
        return p

    def red(self, sesion) -> float:
        """Latencia de red + mercado en segundos (lognormal: casi siempre poca)."""
        base = 0.006 if sesion is BYMA else 0.011
        return base * math.exp(self.r.gauss(0, 0.55))

    def msg(self, sesion, saliente, t_envio, campos, demora=0.0, **kw) -> Msg:
        m = Msg(sesion, saliente, t_envio, t_envio + timedelta(seconds=demora), campos, **kw)
        self.out.msgs.append(m)
        return m

    def evento(self, sesion, t, texto, error=False, hilo="QFJ Timer"):
        self.out.eventos.append(Evento(sesion, t, texto, error, hilo))

    # ── Sesión ────────────────────────────────────────────────────────────
    def sesion(self, sesion, desde, hasta):
        self.evento(sesion, desde - timedelta(seconds=2), f"Session {sesion[0]} schedule is daily, "
                    f"{(desde + UTC).strftime('%H:%M:%S')}-UTC - {(hasta + UTC).strftime('%H:%M:%S')}-UTC")
        self.evento(sesion, desde, "Initiated logon request")
        logon = [("35", "A"), ("98", "0"), ("108", "30"), ("141", "Y")]
        if sesion is A3:
            logon.append(("1137", "9"))
        self.msg(sesion, True, desde, logon)
        self.msg(sesion, False, desde + timedelta(seconds=0.02), logon, demora=self.red(sesion))
        self.evento(sesion, desde + timedelta(seconds=0.04), "Received logon")
        t = desde + timedelta(seconds=30)
        while t < hasta:
            for saliente in (True, False):
                self.msg(sesion, saliente, t + timedelta(seconds=self.r.uniform(0, 0.4)), [("35", "0")],
                         demora=0 if saliente else self.red(sesion))
            t += timedelta(seconds=30)
        self.evento(sesion, hasta, "Initiated logout request")
        self.msg(sesion, True, hasta, [("35", "5")])
        self.msg(sesion, False, hasta + timedelta(seconds=0.03), [("35", "5")], demora=self.red(sesion))
        self.evento(sesion, hasta + timedelta(seconds=0.05), "Received logout response")
        self.evento(sesion, hasta + timedelta(seconds=0.06), "Disconnecting")

    # ── Órdenes ───────────────────────────────────────────────────────────
    def instrumento(self, sesion):
        if sesion is A3:
            esp = self.r.choice(list(FUTUROS))
            return esp, "FUT", "ARS", None, max(1, int(self.r.lognormvariate(2.2, 1.0)))
        tipo = self.r.choices(["CS", "GO", "GOD", "CED"], weights=[55, 20, 10, 15])[0]
        if tipo == "CS":
            esp = self.r.choice(list(ACCIONES))
            return esp, "CS", "ARS", self.plazo(), max(1, int(self.r.lognormvariate(5.0, 1.3)))
        if tipo == "GO":
            esp = self.r.choice(list(BONOS))
            return esp, "GO", "ARS", self.plazo(), int(self.r.choice([1000, 5000, 10000, 50000, 100000]))
        if tipo == "GOD":
            esp = self.r.choice(list(BONOS_USD))
            return esp, "GO", "USD", self.plazo(), int(self.r.choice([100, 500, 1000, 5000, 20000]))
        esp = self.r.choice(list(CEDEARS))
        return esp, "CS", "ARS", self.plazo(), max(1, int(self.r.lognormvariate(3.0, 1.1)))

    def plazo(self):
        return "1" if self.r.random() < 0.25 else "2"

    def cl(self):
        self.n_orden += 1
        return f"ALYC-{self.dia:%y%m%d}-{self.n_orden:06d}"

    def exec_id(self, contra):
        self.n_exec += 1
        return f"{contra}-E{self.dia:%y%m%d}{self.n_exec:07d}"

    def orden(self, sesion, t, cuenta, operador, forzar=None):
        """Una orden y todo lo que le pasa. `forzar`: "rechazo_suspendida" o
        "layering" (para las historias)."""
        esp, tipo, moneda, plazo, qty = self.instrumento(sesion)
        if forzar == "rechazo_suspendida":
            esp, tipo, moneda, plazo = "TXAR", "CS", "ARS", self.plazo()
        if forzar == "layering":
            esp, tipo, moneda, plazo, qty = "GGAL", "CS", "ARS", "2", self.r.choice([500, 800, 1000, 1500])
        lado = "1" if self.r.random() < 0.52 else "2"
        px = self.precio(esp)
        mercado = self.r.random() < 0.12 and forzar is None
        if forzar == "layering":
            lado = "1"
            px = px * (1 - self.r.uniform(0.003, 0.012))      # lejos del mercado, del lado comprador
        decimales = 2 if px < 1000 else 1 if px < 10000 else 0
        px = round(px, decimales)
        tif = "0" if forzar else self.r.choices(["0", "3", "4"], weights=[90, 7, 3])[0]
        contra = sesion[2]
        clid = self.cl()
        d = [("35", "D"), ("11", clid), ("1", cuenta), ("50", operador), ("21", "1"), ("55", esp),
             ("207", "XBUE" if sesion is BYMA else "ROFX"), ("167", tipo), ("54", lado), ("38", str(qty)),
             ("40", "1" if mercado else "2")]
        if not mercado:
            d.append(("44", f"{px:.{decimales}f}"))
        d += [("59", tif), ("60", fix_ts(t))]
        if plazo:
            d.append(("63", plazo))
        d.append(("15", moneda))
        if sesion is A3:
            d.append(("1128", "9"))
        self.msg(sesion, True, t, d)
        oid = f"{contra}-{self.dia:%y%m%d}{self.n_orden:06d}"
        comunes = [("37", oid), ("11", clid), ("1", cuenta), ("55", esp), ("54", lado), ("38", str(qty))]
        if not mercado:
            comunes.append(("44", f"{px:.{decimales}f}"))
        t_ack = t + timedelta(seconds=self.r.uniform(0.0015, 0.012))

        def er(t_envio, extra, **kw):
            return self.msg(sesion, False, t_envio, [("35", "8")] + comunes + extra, demora=self.red(sesion), **kw)

        # Rechazo
        p_rechazo = 0.035 + (0.03 if t.hour == 11 and t.minute < 20 else 0)
        if forzar == "rechazo_suspendida" or (forzar is None and self.r.random() < p_rechazo):
            if forzar == "rechazo_suspendida":
                cod, texto = "2", self.r.choice(["Instrumento suspendido por volatilidad", "Especie suspendida",
                                                 "Security halted - trading suspended", "TXAR en subasta de volatilidad"])
            else:
                cod, textos = self.r.choices(RECHAZOS, weights=PESOS_RECHAZO)[0]
                texto = self.r.choice(textos)
            er(t_ack, [("17", self.exec_id(contra)), ("150", "8"), ("39", "8"), ("103", cod), ("58", texto),
                       ("151", "0"), ("14", "0"), ("6", "0"), ("60", fix_ts(t_ack))])
            return
        er(t_ack, [("17", self.exec_id(contra)), ("150", "0"), ("39", "0"), ("151", str(qty)), ("14", "0"),
                   ("6", "0"), ("60", fix_ts(t_ack))])
        if forzar == "layering":
            t_cxl = t + timedelta(seconds=self.r.uniform(0.8, 3.5))
            self.cancelar(sesion, t_cxl, clid, comunes, qty, 0, contra, cuenta, operador, esp, lado)
            return
        # Qué le pasa después
        destino = self.r.choices(["total", "parcial_cancel", "cancel", "reemplazo", "vence"],
                                 weights=[55, 10, 25, 5, 5])[0]
        if mercado or tif in ("3", "4"):
            destino = "total" if self.r.random() < 0.85 else "cancel_ioc"
        fin_rueda = self.t(17, 0)
        restante, acum, monto = qty, 0, 0.0
        t_fill = t_ack
        if destino in ("total", "parcial_cancel"):
            partes = self.r.choices([1, 2, 3, 4], weights=[55, 25, 12, 8])[0]
            for i in range(partes):
                t_fill = t_fill + timedelta(seconds=self.r.expovariate(1 / (0.4 if mercado else 25)))
                if t_fill >= fin_rueda:
                    break
                ultimo = i == partes - 1 and destino == "total"
                q = restante if ultimo else max(1, int(restante * self.r.uniform(0.2, 0.6)))
                if destino == "parcial_cancel" and i == partes - 1:
                    q = max(1, int(restante * self.r.uniform(0.2, 0.5)))
                lpx = round(self.precio(esp), decimales)
                restante -= q
                acum += q
                monto += q * lpx
                er(t_fill, [("17", self.exec_id(contra)), ("150", "F"), ("39", "2" if restante == 0 else "1"),
                            ("32", str(q)), ("31", f"{lpx:.{decimales}f}"), ("151", str(restante)),
                            ("14", str(acum)), ("6", f"{monto / acum:.4f}"), ("60", fix_ts(t_fill))])
                if restante == 0:
                    return
            if destino == "total":
                destino = "vence"
        if destino in ("cancel", "parcial_cancel"):
            t_cxl = t_fill + timedelta(seconds=self.r.expovariate(1 / 90))
            if t_cxl < fin_rueda:
                self.cancelar(sesion, t_cxl, clid, comunes, restante, acum, contra, cuenta, operador, esp, lado)
                return
        if destino == "cancel_ioc":
            er(t_ack + timedelta(seconds=0.001), [("17", self.exec_id(contra)), ("150", "4"), ("39", "4"),
                                                   ("151", "0"), ("14", "0"), ("6", "0"),
                                                   ("58", "IOC sin contrapartida")])
            return
        if destino == "reemplazo":
            t_rep = t_ack + timedelta(seconds=self.r.expovariate(1 / 60))
            if t_rep < fin_rueda:
                nuevo = self.cl()
                nqty = max(1, int(qty * self.r.uniform(0.5, 1.5)))
                g = [("35", "G"), ("11", nuevo), ("41", clid), ("1", cuenta), ("50", operador), ("55", esp),
                     ("54", lado), ("38", str(nqty)), ("40", "2"), ("44", f"{round(self.precio(esp), decimales):.{decimales}f}"),
                     ("60", fix_ts(t_rep))]
                self.msg(sesion, True, t_rep, g)
                self.msg(sesion, False, t_rep + timedelta(seconds=0.004),
                         [("35", "8"), ("37", oid), ("11", nuevo), ("41", clid), ("1", cuenta), ("55", esp), ("54", lado),
                          ("38", str(nqty)), ("17", self.exec_id(contra)), ("150", "5"), ("39", "0"),
                          ("151", str(nqty)), ("14", "0"), ("6", "0"), ("60", fix_ts(t_rep))], demora=self.red(sesion))
                return
        # Lo que queda abierto vence al cierre (orden del día).
        t_venc = fin_rueda + timedelta(seconds=self.r.uniform(1, 20))
        er(t_venc, [("17", self.exec_id(contra)), ("150", "C"), ("39", "C"), ("151", "0"), ("14", str(acum)),
                    ("6", f"{(monto / acum) if acum else 0:.4f}"), ("60", fix_ts(t_venc))])

    def cancelar(self, sesion, t, clid, comunes, restante, acum, contra, cuenta, operador, esp, lado):
        nuevo = self.cl()
        self.msg(sesion, True, t, [("35", "F"), ("11", nuevo), ("41", clid), ("1", cuenta), ("50", operador),
                                   ("55", esp), ("54", lado), ("38", str(restante + acum)), ("60", fix_ts(t))])
        t2 = t + timedelta(seconds=self.r.uniform(0.002, 0.01))
        if self.r.random() < 0.03:     # llegó tarde: ya estaba ejecutada
            self.msg(sesion, False, t2, [("35", "9"), ("37", dict(comunes)["37"]), ("11", nuevo), ("41", clid),
                                         ("39", "2"), ("434", "1"), ("102", "0"),
                                         ("58", self.r.choice(["Too late to cancel", "Orden ya ejecutada",
                                                               "No se puede cancelar: orden finalizada"]))],
                     demora=self.red(sesion))
            return
        self.msg(sesion, False, t2, [("35", "8")] + [(k, v) for k, v in comunes if k != "11"]
                 + [("11", nuevo), ("41", clid), ("17", self.exec_id(contra)), ("150", "4"), ("39", "4"),
                    ("151", "0"), ("14", str(acum)), ("6", "0"), ("60", fix_ts(t2))], demora=self.red(sesion))


def curva(minuto: float) -> float:
    """Intensidad de la rueda (11 a 17): pico al abrir, valle al mediodía, pico al cierre."""
    x = minuto / 360
    return 0.55 + 2.4 * math.exp(-x / 0.035) + 0.55 * math.exp(-((x - 0.97) / 0.05) ** 2) - 0.25 * math.exp(-((x - 0.42) / 0.12) ** 2)


def generar_dia(rnd: random.Random, dia: date, cuentas, pesos_cuentas, operadores) -> Dia:
    r = Rueda(rnd, dia)
    r.sesion(BYMA, r.t(10, 30), r.t(17, 10))
    r.sesion(A3, r.t(10, 0), r.t(17, 10))
    actividad = rnd.uniform(0.85, 1.2)
    for sesion, por_dia, desde in ((BYMA, 2300, r.t(11, 0)), (A3, 480, r.t(10, 0))):
        minutos = 360 if sesion is BYMA else 420
        total_curva = sum(curva(m * 360 / minutos) for m in range(minutos))
        for m in range(minutos):
            n = rnd.gauss(por_dia * actividad * curva(m * 360 / minutos) / total_curva, 1.2)
            for _ in range(max(0, round(n))):
                t = desde + timedelta(minutes=m, seconds=rnd.uniform(0, 60))
                if dia == date(2026, 10, 14) and sesion is BYMA and r.t(12, 31, 10) <= t <= r.t(12, 33, 45):
                    continue                  # la sesión está caída: no sale nada
                cuenta = rnd.choices(cuentas, weights=pesos_cuentas)[0]
                r.orden(sesion, t, cuenta, rnd.choice(operadores))
    # Historia: TXAR suspendida (08/10, 14:10 a 14:40).
    if dia == date(2026, 10, 8):
        for _ in range(170):
            t = r.t(14, 10) + timedelta(seconds=rnd.uniform(0, 1800))
            r.orden(BYMA, t, rnd.choices(cuentas, weights=pesos_cuentas)[0], rnd.choice(operadores),
                    forzar="rechazo_suspendida")
    # Historia: layering de una cuenta (15 y 16/10).
    if dia in (date(2026, 10, 15), date(2026, 10, 16)):
        for _ in range(420):
            t = r.t(11, 30) + timedelta(seconds=rnd.uniform(0, 5 * 3600))
            r.orden(BYMA, t, "40217", "OPER07", forzar="layering")
    return r.out


def caida_de_byma(dia: Dia, r: random.Random, t_corte: datetime, t_vuelta: datetime):
    """14/10: los heartbeats de BYMA dejan de llegar, QuickFIX/J corta, reconecta
    y le faltan las secuencias de lo que BYMA mandó mientras tanto."""
    # Lo que la ALyC quiso mandar con la sesión caída (cancelaciones,
    # modificaciones) sale después de reconectar, y su respuesta también.
    corridos: dict[str, datetime] = {}
    for m in sorted(dia.msgs, key=lambda m: m.t_envio):
        if (m.sesion is BYMA and m.saliente and dict(m.campos)["35"] != "0"
                and t_corte <= m.t_envio <= t_vuelta):
            nuevo = t_vuelta + timedelta(seconds=0.3 + 0.012 * len(corridos))
            corridos[dict(m.campos).get("11", "")] = nuevo
            m.t_envio = m.t_log = nuevo
    for m in dia.msgs:
        clid = dict(m.campos).get("11", "")
        if m.sesion is BYMA and not m.saliente and clid in corridos and t_corte <= m.t_envio <= t_vuelta:
            m.t_envio = corridos[clid] + timedelta(seconds=0.006)
            m.t_log = m.t_envio + timedelta(seconds=r.uniform(0.004, 0.012))
    for m in dia.msgs:
        if m.sesion is BYMA and t_corte - timedelta(seconds=40) <= m.t_envio <= t_vuelta:
            if m.saliente and dict(m.campos)["35"] == "0":
                m.perdido = True            # no se mandan: la conexión no existe
            elif not m.saliente:
                if dict(m.campos)["35"] == "0" and m.t_envio >= t_corte - timedelta(seconds=40):
                    m.perdido = True        # justamente los heartbeats que no llegaron
                elif m.t_envio >= t_corte:
                    m.perdido = True        # ejecuciones mandadas con la sesión caída
    dia.eventos.append(Evento(BYMA, t_corte, "Timed out waiting for heartbeat", error=True))
    dia.eventos.append(Evento(BYMA, t_corte + timedelta(seconds=0.01), "Disconnecting: Timed out waiting for heartbeat"))
    for i in range(1, 5):
        dia.eventos.append(Evento(BYMA, t_corte + timedelta(seconds=30 * i),
                                  "Initiated logon request", hilo="QFJ Timer"))
        dia.eventos.append(Evento(BYMA, t_corte + timedelta(seconds=30 * i + 5),
                                  "Disconnecting: Socket exception (Connection refused)", error=True))


def asignar_secuencias(dia: Dia, t_vuelta: datetime | None):
    """MsgSeqNum por sesión y dirección, en orden de envío; cada logon reinicia.
    Lo perdido en la caída vuelve después de la reconexión con PossDupFlag."""
    nuevos, eventos = [], []
    for sesion in (BYMA, A3):
        for saliente in (True, False):
            propios = sorted((m for m in dia.msgs if m.sesion is sesion and m.saliente is saliente
                              and not (m.perdido and m.saliente)), key=lambda m: m.t_envio)
            seq = 0
            for m in propios:
                if dict(m.campos)["35"] == "A":
                    seq = 0
                seq += 1
                m.seq = seq
    if t_vuelta is None:
        return
    # La reconexión: logon de los dos lados; el de BYMA trae una secuencia más alta.
    r = random.Random(7)
    entrantes = sorted((m for m in dia.msgs if m.sesion is BYMA and not m.saliente), key=lambda m: m.t_envio)
    perdidos = [m for m in entrantes if m.perdido and dict(m.campos)["35"] != "0"]
    hb_perdidos = [m for m in entrantes if m.perdido and dict(m.campos)["35"] == "0"]
    for m in hb_perdidos:
        dia.msgs.remove(m)                  # un heartbeat no se reenvía: va como SequenceReset
    salientes = sorted((m for m in dia.msgs if m.sesion is BYMA and m.saliente and not m.perdido and m.t_envio <= t_vuelta),
                       key=lambda m: m.t_envio)
    ultimo_entrante = max((m.seq for m in entrantes if not m.perdido and m.t_envio < t_vuelta), default=0)
    esperado = ultimo_entrante + 1
    llegada = max((m.seq for m in entrantes if m.perdido), default=esperado - 1) + 1
    t = t_vuelta
    eventos.append(Evento(BYMA, t, "Initiated logon request"))
    logon_sal = Msg(BYMA, True, t, t, [("35", "A"), ("98", "0"), ("108", "30")],
                    seq=(salientes[-1].seq + 1) if salientes else 1)
    logon_ent = Msg(BYMA, False, t + timedelta(seconds=0.02), t + timedelta(seconds=0.03),
                    [("35", "A"), ("98", "0"), ("108", "30")], seq=llegada)
    nuevos += [logon_sal, logon_ent]
    eventos.append(Evento(BYMA, t + timedelta(seconds=0.031), "Received logon"))
    eventos.append(Evento(BYMA, t + timedelta(seconds=0.032),
                          f"MsgSeqNum too high, expecting {esperado} but received {llegada}", error=True))
    eventos.append(Evento(BYMA, t + timedelta(seconds=0.033),
                          f"Sent ResendRequest FROM: {esperado} TO: infinity"))
    pedido = Msg(BYMA, True, t + timedelta(seconds=0.033), t + timedelta(seconds=0.033),
                 [("35", "2"), ("7", str(esperado)), ("16", "0")], seq=logon_sal.seq + 1)
    nuevos.append(pedido)
    # Los salientes que siguen se corren dos lugares (logon y ResendRequest).
    for m in dia.msgs:
        if m.sesion is BYMA and m.saliente and not m.perdido and m.t_envio > t_vuelta:
            m.seq += 2
    t_r = t + timedelta(seconds=0.05)
    # BYMA reenvía: los mensajes de negocio con PossDup; los huecos de heartbeats, con un SequenceReset.
    por_seq = {m.seq: m for m in perdidos}
    seq = esperado
    while seq < llegada:
        if seq in por_seq:
            m = por_seq[seq]
            m.reenvio_de = m.t_envio
            m.t_envio = t_r
            m.t_log = t_r + timedelta(seconds=0.004)      # en orden: BYMA reenvía uno detrás del otro
            m.perdido = False
            seq += 1
        else:
            hasta = seq
            while hasta < llegada and hasta not in por_seq:
                hasta += 1
            nuevos.append(Msg(BYMA, False, t_r, t_r + timedelta(seconds=0.004),
                              [("35", "4"), ("123", "Y"), ("36", str(hasta))], reenvio_de=t_r, seq=seq))
            seq = hasta
        t_r += timedelta(seconds=0.003)
    eventos.append(Evento(BYMA, t_r + timedelta(seconds=0.01), f"ResendRequest for messages FROM {esperado} TO {llegada - 1} has been satisfied."))
    # El resto de lo entrante de BYMA corre su secuencia después del logon.
    for m in dia.msgs:
        if m.sesion is BYMA and not m.saliente and not m.perdido and m.t_envio > t_vuelta and m.reenvio_de is None:
            m.seq += 1
    dia.msgs += nuevos
    dia.eventos += eventos


def latencia_a3(dia: Dia, desde: datetime, hasta: datetime, r: random.Random):
    """19/10: A3 tarda en contestar (la red o su gateway)."""
    for m in dia.msgs:
        if m.sesion is A3 and not m.saliente and desde <= m.t_envio <= hasta:
            m.t_log = m.t_envio + timedelta(seconds=r.uniform(0.4, 1.8))
    t = desde + timedelta(minutes=3)
    while t < hasta:
        dia.eventos.append(Evento(A3, t, "Sent TestRequest: heartbeat interval exceeded"))
        t += timedelta(minutes=r.uniform(4, 9))


def llegadas_en_orden(dia: Dia):
    """Una sesión FIX es una conexión TCP: lo que entra llega en el orden en que
    se mandó, aunque la latencia de cada mensaje varíe. (El único salto es el de
    la reconexión: el logon con la secuencia alta y después los reenvíos.)"""
    for sesion in (BYMA, A3):
        previo = None
        for m in sorted((m for m in dia.msgs if m.sesion is sesion and not m.saliente and not m.perdido
                         and m.reenvio_de is None), key=lambda m: m.seq + (0 if m.seq else 10 ** 9)):
            if previo is not None and m.t_log <= previo.t_log and m.t_envio >= previo.t_envio:
                m.t_log = previo.t_log + timedelta(microseconds=200)
            previo = m


def lineas(dia: Dia) -> list[tuple[datetime, str]]:
    salida = []
    for m in dia.msgs:
        if m.perdido:
            continue
        logger = "quickfixj.msg.outgoing" if m.saliente else "quickfixj.msg.incoming"
        hilo = "QFJ Message Processor" if dict(m.campos)["35"] != "0" else "QFJ Timer"
        salida.append((m.t_log, f"INFO  [{hilo}] {logger} - {m.sesion[0]}: {armar(m)}"))
    for e in dia.eventos:
        nivel, logger = ("ERROR", "quickfixj.errorEvent") if e.error else ("INFO ", "quickfixj.event")
        salida.append((e.t_log, f"{nivel} [{e.hilo}] {logger} - {e.sesion[0]}: {e.texto}"))
    salida.sort(key=lambda x: x[0])
    return [(t, f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond // 1000:03d} {texto}") for t, texto in salida]


def main() -> None:
    rnd = random.Random(SEMILLA)
    cuentas = [f"{n:05d}" for n in rnd.sample(range(10000, 99999), 180)] + ["40217"]
    pesos = [rnd.paretovariate(1.3) for _ in cuentas[:-1]] + [1.0]
    operadores = [f"OPER{i:02d}" for i in range(1, 13)]
    SALIDA.parent.mkdir(exist_ok=True)
    total = 0
    with SALIDA.open("w", encoding="utf-8", newline="\n") as f:
        for dia in DIAS:
            d = generar_dia(rnd, dia, cuentas, pesos, operadores)
            t_vuelta = None
            if dia == date(2026, 10, 14):
                base = datetime.combine(dia, datetime.min.time())
                t_corte, t_vuelta = base + timedelta(hours=12, minutes=31, seconds=10), base + timedelta(hours=12, minutes=33, seconds=45)
                caida_de_byma(d, rnd, t_corte, t_vuelta)
            if dia == date(2026, 10, 19):
                base = datetime.combine(dia, datetime.min.time())
                latencia_a3(d, base + timedelta(hours=15), base + timedelta(hours=15, minutes=45), rnd)
            asignar_secuencias(d, t_vuelta)
            llegadas_en_orden(d)
            for _, linea in lineas(d):
                f.write(linea + "\n")
                total += 1
    print(f"{SALIDA} · {total:,} líneas · {SALIDA.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
