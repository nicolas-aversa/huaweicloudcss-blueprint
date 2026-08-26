"""
build_vertical_datasets.py
==========================

Genera los datasets `.log` de los 3 verticales nuevos (~100k registros cada
uno, como fintech) a partir de fuentes publicas, re-fechados a la MISMA ventana
que fintech (2025-07-01 -> 2026-07-01) para que los 4 verticales sean
consistentes en Kibana y los forecasts tengan historia densa.

Cada vertical tiene su PROPIO formato de log y su propio filter de Logstash
(no comparten el de fintech):

- **oil-gas** (kv espacio/=, estilo SCADA) <- Volve production data (Equinor,
  xlsx oficial): reportes diarios REALES por pozo (presion, temperatura, horas
  on-stream, volumenes). Se elige la ventana de 12 meses de mayor produccion
  del campo, se desplaza al anio destino y se upsamplea a telemetria (~36 min
  por pozo): el volumen diario real se reparte entre lecturas y las presiones/
  temperaturas llevan jitter alrededor del promedio diario real.
- **media-retail-ecommerce** (JSON por linea) <- sample data de OpenSearch
  Dashboards (ndjson): las 4.675 ordenes reales del sample cicladas a lo largo
  del anio con estacionalidad semanal + picos (Black Friday, Navidad) y un
  campo `status` agregado (con rachas de cancelaciones para que la serie
  tenga eventos visibles). Pasa por Logstash como cualquier log (filtro
  json); NO usa la API de sample data de Dashboards.
- **health** (CSV, como lo entrega Synthea) <- Synthea
  (encounters.csv sintetico, sin PHI real): consultas re-muestreadas a ~100k,
  re-fechadas al anio con patron de horario de atencion + flu season + picos
  de emergencias plantados.

Uso:
    py build_vertical_datasets.py --sources <dir con volve_production.xlsx,
                                              ecommerce.ndjson, synthea/csv/*>

Salida: datasets/produccion-pozos.log, datasets/ventas-ecommerce.log,
        datasets/encuentros-clinicos.log  (un evento por linea; luego se
        pre-cargan a OBS segun datasets/README.md).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

WINDOW_START = datetime(2025, 7, 1)
WINDOW_END = datetime(2026, 7, 1)   # exclusivo
WINDOW_DAYS = (WINDOW_END - WINDOW_START).days
TARGET_LINES = 100_000

rng = random.Random(42)


def _q(v: str) -> str:
    """Quotea un valor kv si tiene espacios."""
    s = str(v)
    return f'"{s}"' if (" " in s or s == "") else s


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── oil-gas (Volve) ──────────────────────────────────────────────────────────

# Lecturas SCADA por pozo por dia. En la ventana elegida reportan ~4 pozos
# promedio (no los 7 de todo el campo): ~1.450 pozo-dias x 68 lecturas ~ 100k.
READINGS_PER_DAY = 68


def build_oil_gas(xlsx_path: Path, out_path: Path) -> None:
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    ws = wb["Daily Production Data"]
    rows = ws.iter_rows(min_row=1, values_only=True)
    header = [str(c) for c in next(rows)]
    idx = {name: i for i, name in enumerate(header)}

    def col(r, name, default=0.0):
        v = r[idx[name]]
        return default if v is None else v

    data = [r for r in rows if isinstance(r[idx["DATEPRD"]], datetime)]

    # Ventana de 12 meses con mayor produccion total de oil (la mejor cara del campo).
    daily_oil: dict[datetime, float] = defaultdict(float)
    for r in data:
        daily_oil[r[idx["DATEPRD"]].replace(hour=0)] += float(col(r, "BORE_OIL_VOL"))
    days = sorted(daily_oil)
    best_start, best_sum = days[0], -1.0
    for start in days:
        end = start + timedelta(days=WINDOW_DAYS)
        s = sum(v for d, v in daily_oil.items() if start <= d < end)
        if s > best_sum:
            best_start, best_sum = start, s

    shift = WINDOW_START - best_start
    step_min = (24 * 60) // READINGS_PER_DAY
    lines = []
    for r in data:
        d = r[idx["DATEPRD"]]
        if not (best_start <= d < best_start + timedelta(days=WINDOW_DAYS)):
            continue
        well = str(col(r, "WELL_BORE_CODE", "")).replace("NO ", "")
        well_type = str(col(r, "WELL_TYPE", ""))          # OP / WI
        flow_kind = str(col(r, "FLOW_KIND", ""))          # production / injection
        hrs = float(col(r, "ON_STREAM_HRS"))
        if well_type == "WI":
            status = "INJECTING" if hrs > 0 else "DOWN"
        else:
            status = "FLOWING" if hrs > 0 else "DOWN"
        day0 = (d + shift).replace(hour=0, minute=0, second=0)
        # Promedios diarios reales -> N lecturas intra-dia: volumen repartido
        # (la suma diaria se conserva ~igual a la real) y presiones con jitter.
        oil_d = float(col(r, "BORE_OIL_VOL"))
        gas_d = float(col(r, "BORE_GAS_VOL"))
        wat_d = float(col(r, "BORE_WAT_VOL"))
        wi_d = float(col(r, "BORE_WI_VOL") or 0.0)
        dhp = float(col(r, "AVG_DOWNHOLE_PRESSURE"))
        dht = float(col(r, "AVG_DOWNHOLE_TEMPERATURE"))
        whp = float(col(r, "AVG_WHP_P"))
        wht = float(col(r, "AVG_WHT_P"))
        choke = float(col(r, "AVG_CHOKE_SIZE_P"))

        def jit(v: float, pct: float = 0.03) -> float:
            return round(v * (1 + rng.uniform(-pct, pct)), 1)

        def share(v: float) -> float:
            return round(v / READINGS_PER_DAY * (1 + rng.uniform(-0.15, 0.15)), 2)

        for k in range(READINGS_PER_DAY):
            ts = day0 + timedelta(minutes=k * step_min + rng.randint(0, 3))
            kv = [
                ("ts", _iso(ts)),
                ("well", _q(well)),
                ("well_type", well_type),
                ("flow_kind", flow_kind),
                ("status", status),
                ("on_stream_hrs", round(hrs, 1)),
                ("avg_dhp", jit(dhp)),
                ("avg_dht", jit(dht, 0.01)),
                ("avg_whp", jit(whp)),
                ("avg_wht", jit(wht, 0.02)),
                ("choke_size", jit(choke, 0.01)),
                ("oil_vol", share(oil_d)),
                ("gas_vol", share(gas_d)),
                ("wat_vol", share(wat_d)),
                ("wi_vol", share(wi_d)),
            ]
            # downtime SOLO cuando el pozo esta caido -> value_count() del campo
            # cuenta lecturas de caida (mismo truco que funnel.failed_at_code).
            if status == "DOWN":
                kv.append(("downtime", 1))
            lines.append((ts, " ".join(f"{k2}={v2}" for k2, v2 in kv)))

    lines.sort(key=lambda t: t[0])
    out_path.write_text("\n".join(l for _, l in lines) + "\n", encoding="utf-8")
    print(f"oil-gas: {len(lines)} lineas ({_iso(lines[0][0])} -> {_iso(lines[-1][0])})")


# ── media-retail-ecommerce (OpenSearch sample) ───────────────────────────────

CANCEL_REASONS = ["PAYMENT_FAILED", "CUSTOMER_REQUEST", "OUT_OF_STOCK"]
# Rachas de cancelaciones (historia: caida de pasarela de pagos) — eventos
# visibles en la serie de status/cancel_reason.
CANCEL_BURSTS = {datetime(2025, 9, 14).date(), datetime(2026, 2, 3).date(),
                 datetime(2026, 5, 20).date()}
HOLIDAY_BOOSTS = {  # fecha -> factor de volumen
    datetime(2025, 11, 27).date(): 3.0,   # Black Friday
    datetime(2025, 11, 28).date(): 2.2,
    datetime(2025, 12, 22).date(): 2.0,   # Navidad
    datetime(2025, 12, 23).date(): 2.4,
    datetime(2026, 5, 11).date(): 1.8,    # Hot Sale
    datetime(2026, 5, 12).date(): 1.8,
}
DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# ~100k en el anio: base ~255/dia + finde + feriados.
ECOM_BASE_PER_DAY = 255


def _slim_order(doc: dict) -> dict:
    """Reduce la orden del sample a los campos que la demo usa (linea manejable)."""
    geo = (doc.get("geoip") or {})
    return {
        "order_id": doc.get("order_id"),
        "order_date": doc.get("order_date"),
        "status": "COMPLETED",
        "customer_id": doc.get("customer_id"),
        "customer_full_name": doc.get("customer_full_name"),
        "customer_gender": doc.get("customer_gender"),
        "category": doc.get("category") or [],
        "manufacturer": doc.get("manufacturer") or [],
        "total_quantity": doc.get("total_quantity"),
        "taxful_total_price": doc.get("taxful_total_price"),
        "currency": doc.get("currency"),
        "day_of_week": doc.get("day_of_week"),
        "geo": {
            "city": geo.get("city_name"), "region": geo.get("region_name"),
            "country": geo.get("country_iso_code"),
        },
        "products": [
            {"product_id": p.get("product_id"), "sku": p.get("sku"),
             "category": p.get("category"), "price": p.get("taxful_price") or p.get("base_price"),
             "quantity": p.get("quantity")}
            for p in (doc.get("products") or [])[:4]
        ],
    }


def build_ecommerce(ndjson_path: Path, out_path: Path) -> None:
    orders = [_slim_order(json.loads(l)) for l in ndjson_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    lines = []
    order_id = 100000
    day = WINDOW_START
    oi = 0
    while day < WINDOW_END:
        dow = day.weekday()
        base = ECOM_BASE_PER_DAY + (60 if dow >= 5 else 0) + rng.randint(-25, 25)   # finde mas fuerte
        n = int(base * HOLIDAY_BOOSTS.get(day.date(), 1.0))
        burst = day.date() in CANCEL_BURSTS
        for _ in range(n):
            o = dict(orders[oi % len(orders)])
            oi += 1
            # Horario de compra: sesgo 10-23h.
            ts = day + timedelta(hours=rng.choices(range(24),
                weights=[1, 1, 1, 1, 1, 2, 3, 4, 5, 6, 8, 9, 9, 8, 8, 8, 9, 10, 11, 12, 12, 10, 6, 3])[0],
                minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
            order_id += 1
            o["order_id"] = order_id
            o["order_date"] = ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            o["day_of_week"] = DOW_NAMES[dow]
            p_cancel = 0.25 if burst else 0.02
            roll = rng.random()
            if roll < p_cancel:
                o["status"] = "CANCELLED"
                # En la racha, la causa dominante es la pasarela caida.
                o["cancel_reason"] = ("PAYMENT_FAILED" if burst and rng.random() < 0.85
                                      else rng.choice(CANCEL_REASONS))
            elif roll < p_cancel + 0.04:
                o["status"] = "RETURNED"
            lines.append((ts, json.dumps(o, ensure_ascii=False, separators=(",", ":"))))
        day += timedelta(days=1)

    lines.sort(key=lambda t: t[0])
    out_path.write_text("\n".join(l for _, l in lines) + "\n", encoding="utf-8")
    print(f"media-retail-ecommerce: {len(lines)} lineas ({_iso(lines[0][0])} -> {_iso(lines[-1][0])})")


# ── health (Synthea) ───────────────────────────────────────────

# Picos de emergencias plantados (ej. ola de calor / brote) — eventos visibles
# en la serie de triage/emergencias.
ER_SPIKES = {datetime(2026, 1, 19).date(), datetime(2026, 1, 20).date(),
             datetime(2025, 12, 8).date()}

# Columnas del CSV de salida (el filter csv de Logstash las declara igual):
# ts,class,code,desc,patient,city,cost,claim,covered,reason,triage
HEALTH_COLUMNS = ["ts", "class", "code", "desc", "patient", "city",
                  "cost", "claim", "covered", "reason", "triage"]


def _csv_line(values: list) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(values)
    return buf.getvalue()


def build_health(synthea_csv_dir: Path, out_path: Path) -> None:
    # Demografia (ciudad) por paciente, para el sabor public-sector.
    city_by_patient: dict[str, str] = {}
    with (synthea_csv_dir / "patients.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            city_by_patient[row["Id"]] = row.get("CITY", "") or ""

    with (synthea_csv_dir / "encounters.csv").open(encoding="utf-8") as f:
        pool = list(csv.DictReader(f))

    # Re-muestreo con reposicion hasta ~100k (los pacientes repiten visitas).
    encounters = [rng.choice(pool) for _ in range(TARGET_LINES)]

    # Invierno (flu season) mas cargado: peso por mes jul-2025..jun-2026.
    month_w = {7: 0.8, 8: 0.8, 9: 0.9, 10: 1.0, 11: 1.1, 12: 1.3,
               1: 1.4, 2: 1.3, 3: 1.1, 4: 1.0, 5: 0.9, 6: 0.9}
    day_pool, day_weights = [], []
    d = WINDOW_START
    while d < WINDOW_END:
        w = month_w[d.month] * (0.55 if d.weekday() >= 5 else 1.0)   # finde: solo guardia
        if d.date() in ER_SPIKES:
            w *= 1.15
        day_pool.append(d)
        day_weights.append(w)
        d += timedelta(days=1)

    lines = []
    for e in encounters:
        cls = e["ENCOUNTERCLASS"]
        day = rng.choices(day_pool, weights=day_weights)[0]
        spike = day.date() in ER_SPIKES
        # En dias de pico, parte de las consultas se vuelven emergencias.
        if spike and cls in ("ambulatory", "wellness", "outpatient") and rng.random() < 0.35:
            cls = "emergency"
        if cls == "emergency":
            hour = rng.randint(0, 23)                      # guardia 24/7
        else:
            hour = rng.choices(range(8, 19), weights=[6, 9, 10, 10, 8, 6, 7, 9, 9, 7, 4])[0]
        ts = day + timedelta(hours=hour, minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
        # triage SOLO en emergencias/urgencias -> value_count(triage) = casos criticos
        # (mismo truco que downtime / funnel.failed_at_code). En CSV el campo va
        # vacio y el filter lo elimina cuando esta vacio.
        triage = (rng.choices(["RED", "YELLOW", "GREEN"], weights=[2, 5, 3])[0]
                  if cls in ("emergency", "urgentcare") else "")
        lines.append((ts, _csv_line([
            _iso(ts), cls, e["CODE"], e["DESCRIPTION"], e["PATIENT"][:8],
            city_by_patient.get(e["PATIENT"], ""), e["BASE_ENCOUNTER_COST"],
            e["TOTAL_CLAIM_COST"], e["PAYER_COVERAGE"],
            e.get("REASONDESCRIPTION", ""), triage,
        ])))

    lines.sort(key=lambda t: t[0])
    out_path.write_text("\n".join(l for _, l in lines) + "\n", encoding="utf-8")
    print(f"health: {len(lines)} lineas ({_iso(lines[0][0])} -> {_iso(lines[-1][0])})")


# ── alyc (Fintech · ALyC argentina — mercado de capitales) ───────────────────
# Órdenes, ejecuciones y liquidación de un Agente de Liquidación y Compensación:
# BYMA/MAE, especies reales, rueda 11-17 ART solo días hábiles. Sintético (no hay
# dataset público de una ALyC), mismo seed determinístico que el resto.

# Feriados AR (aprox) dentro de la ventana — la rueda no opera.
_AR_HOLIDAYS = {
    datetime(2025, 7, 9).date(), datetime(2025, 8, 15).date(),
    datetime(2025, 10, 13).date(), datetime(2025, 11, 24).date(),
    datetime(2025, 12, 8).date(), datetime(2025, 12, 25).date(),
    datetime(2026, 1, 1).date(), datetime(2026, 2, 16).date(),
    datetime(2026, 2, 17).date(), datetime(2026, 3, 24).date(),
    datetime(2026, 4, 2).date(), datetime(2026, 4, 3).date(),
    datetime(2026, 5, 1).date(), datetime(2026, 5, 25).date(),
    datetime(2026, 6, 20).date(),
}

# (ticker, precio_base, moneda). Precios plausibles mediados de 2025; los ARS
# llevan drift anual (crawling peg) + ruido.
_ALYC_ESPECIES = [
    ("AL30", 68_500.0, "ARS"), ("AL30D", 58.2, "USD"), ("GD30", 71_200.0, "ARS"),
    ("GD35", 46_800.0, "ARS"), ("GGAL", 6_850.0, "ARS"), ("YPFD", 47_300.0, "ARS"),
    ("PAMP", 3_720.0, "ARS"), ("BMA", 11_450.0, "ARS"), ("S31O25", 132.5, "ARS"),
]

_ALYC_EVENTS = [
    ("ORDER_NEW", 34), ("ORDER_FILL", 28), ("PARTIAL_FILL", 8), ("ORDER_CANCEL", 10),
    ("ORDER_REJECT", 4), ("SETTLEMENT_OK", 14), ("SETTLEMENT_FAIL", 2),
]
_ALYC_REJECT_REASONS = ["SALDO_INSUFICIENTE", "LIMITE_EXCEDIDO", "ESPECIE_SUSPENDIDA"]
_ALYC_FAIL_REASONS = ["FALTA_ESPECIES", "FALTA_FONDOS", "CONTRAPARTE"]
# Pico en apertura (11h) y cierre (16-17h): pesos por hora 11..16.
_ALYC_HOUR_W = [24, 14, 10, 10, 14, 28]


def build_alyc(out_path: Path) -> None:
    # Comitentes: 800 cuentas, las primeras concentran el volumen (mesa/algorítmicos).
    comitentes = [f"C{10000 + i}" for i in range(800)]
    com_w = [40 if i < 30 else (8 if i < 150 else 1) for i in range(800)]
    ev_names = [e for e, _ in _ALYC_EVENTS]
    ev_w = [w for _, w in _ALYC_EVENTS]

    lines = []
    d = WINDOW_START
    while d < WINDOW_END:
        if d.weekday() >= 5 or d.date() in _AR_HOLIDAYS:
            d += timedelta(days=1)
            continue
        # Drift anual de precios ARS (inflación/crawling): +45% punta a punta.
        t = (d - WINDOW_START).days / max(1, WINDOW_DAYS)
        n_day = 400 + rng.randint(-60, 60)
        seq = 0
        for _ in range(n_day):
            hour = rng.choices(range(11, 17), weights=_ALYC_HOUR_W)[0]
            ts = d + timedelta(hours=hour, minutes=rng.randint(0, 59),
                               seconds=rng.randint(0, 59), microseconds=rng.randint(0, 999) * 1000)
            evt = rng.choices(ev_names, weights=ev_w)[0]
            ticker, base, curr = rng.choice(_ALYC_ESPECIES)
            drift = (1 + 0.45 * t) if curr == "ARS" else (1 + 0.06 * t)
            price = round(base * drift * (1 + rng.uniform(-0.03, 0.03)), 2)
            qty = rng.choice([10, 25, 50, 100, 150, 200, 500, 1000])
            notional = round(price * qty, 2)
            seq += 1
            kv = [
                ("evt", evt),
                ("order_id", f"O-{ts.strftime('%Y%m%d')}-{seq:06d}"),
                ("comitente", rng.choices(comitentes, weights=com_w)[0]),
                ("especie", ticker),
                ("side", rng.choice(["BUY", "SELL"])),
                ("qty", qty),
                ("price", price),
                ("notional", notional),
                ("currency", curr),
                ("plazo", rng.choices(["CI", "T+1"], weights=[3, 7])[0]),
                ("channel", rng.choices(["WEB", "API", "MOBILE", "MESA"], weights=[4, 3, 2, 1])[0]),
            ]
            if evt == "ORDER_REJECT":
                kv.append(("status", "REJECTED"))
                kv.append(("reject_reason", rng.choice(_ALYC_REJECT_REASONS)))
            elif evt == "SETTLEMENT_FAIL":
                kv.append(("status", "FAILED"))
                kv.append(("fail_reason", rng.choice(_ALYC_FAIL_REASONS)))
            elif evt == "SETTLEMENT_OK":
                kv.append(("status", "SETTLED"))
            elif evt in ("ORDER_FILL", "PARTIAL_FILL"):
                kv.append(("status", "FILLED" if evt == "ORDER_FILL" else "PARTIAL"))
                kv.append(("fee", round(notional * 0.0015, 2)))
            else:
                kv.append(("status", "NEW" if evt == "ORDER_NEW" else "CANCELLED"))
            market = rng.choices(["BYMA", "MAE"], weights=[85, 15])[0]
            envelope = ts.strftime("%Y%m%d-%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"
            lines.append((ts, f"{envelope} {market} " + " ".join(f"{k}={v}" for k, v in kv)))
        d += timedelta(days=1)

    lines.sort(key=lambda x: x[0])
    out_path.write_text("\n".join(l for _, l in lines) + "\n", encoding="utf-8")
    print(f"alyc: {len(lines)} lineas ({_iso(lines[0][0])} -> {_iso(lines[-1][0])})")


# ── media-streaming (Media · OTT) ────────────────────────────────────────────
# Sesiones de reproducción de una plataforma de video: JSON por línea. Prime-time
# 20-24, finde más fuerte, estrenos con pico y un día de burst de errores en un
# pop del CDN (eventos visibles en las series/forecasts).

_MS_GENRES = ["Drama", "Comedia", "Thriller", "Documental", "Infantil", "Deporte", "Noticias"]
_MS_TITLES = (
    [(f"m{i:03d}", t, "MOVIE") for i, t in enumerate([
        "La Sombra del Puerto", "Medianoche en Retiro", "El Último Asado", "Cordillera Adentro",
        "Ruta 40", "La Deuda", "Viento Sur", "El Archivo", "Tango Rojo", "La Escollera",
        "Furia de Río", "El Contador de Cuentos", "Malbec", "La Firma", "Estación Norte"])]
    + [(f"s{i:03d}", t, "SERIES") for i, t in enumerate([
        "Barrio Chino", "Los de Afuera", "Código Patagonia", "La Mesa Chica", "Puerto Espera",
        "Familia de Papel", "El Anexo", "Doble Turno", "Las Grutas", "Km 0",
        "La Torre", "Cuentas Claras", "Bajo Llave", "El Semillero", "Zona Franca"])]
    + [(f"d{i:03d}", t, "DOCUMENTARY") for i, t in enumerate([
        "Gigantes del Sur", "Memoria del Fuego", "Pampa Infinita", "Los Glaciares", "Vinos de Altura"])]
    + [(f"l{i:03d}", t, "LIVE") for i, t in enumerate([
        "Canal Noticias 24", "Fútbol en Vivo HD", "Música en Vivo", "Cocina en Casa", "Debate Abierto"])]
)
_MS_EVENTS = [("PLAY_START", 32), ("PLAY_END", 32), ("REBUFFER", 20),
              ("BITRATE_SWITCH", 12), ("PLAYBACK_ERROR", 4)]
_MS_ERRORS = ["E_DRM", "E_NETWORK", "E_DECODE", "E_CDN_TIMEOUT"]
_MS_DEVICES = [("SMART_TV", 35), ("MOBILE_ANDROID", 25), ("MOBILE_IOS", 15), ("WEB", 15), ("TV_STICK", 10)]
_MS_POPS = ["EZE1", "GRU2", "SCL1", "BOG1", "MIA1"]
_MS_COUNTRIES = [("AR", 40), ("BR", 25), ("CL", 12), ("CO", 12), ("UY", 6), ("MX", 5)]
_MS_BITRATES = [800, 1600, 2400, 3600, 4800, 6400, 8000]
# Hora del día (0-23): valle madrugada, pico prime-time 20-24.
_MS_HOUR_W = [3, 2, 1, 1, 1, 1, 2, 3, 4, 5, 6, 7, 8, 8, 8, 9, 10, 11, 13, 16, 20, 22, 20, 10]
# Estrenos: ese día el título nuevo se lleva gran parte del tráfico (pico).
_MS_PREMIERES = {
    datetime(2025, 9, 5).date(): "s010",   # "La Torre"
    datetime(2025, 12, 19).date(): "m013", # "La Firma"
    datetime(2026, 3, 13).date(): "s014",  # "Zona Franca"
}
_MS_ERROR_BURST = datetime(2026, 2, 10).date()   # GRU2 degradado ese día


def build_media_streaming(out_path: Path) -> None:
    users = [f"u{100000 + i}" for i in range(5000)]
    user_w = [20 if i < 500 else (5 if i < 2000 else 1) for i in range(5000)]
    genre_by_title = {cid: rng.choice(_MS_GENRES[:-2]) if ctype != "LIVE" else rng.choice(["Noticias", "Deporte"])
                      for cid, _t, ctype in _MS_TITLES}
    ev_names = [e for e, _ in _MS_EVENTS]
    ev_w = [w for _, w in _MS_EVENTS]
    dev_names = [d for d, _ in _MS_DEVICES]
    dev_w = [w for _, w in _MS_DEVICES]
    c_names = [c for c, _ in _MS_COUNTRIES]
    c_w = [w for _, w in _MS_COUNTRIES]

    lines = []
    d = WINDOW_START
    while d < WINDOW_END:
        premiere = _MS_PREMIERES.get(d.date())
        base = 250 + (80 if d.weekday() >= 5 else 0) + rng.randint(-30, 30)
        if premiere:
            base = int(base * 1.8)
        for _ in range(base):
            hour = rng.choices(range(24), weights=_MS_HOUR_W)[0]
            ts = d + timedelta(hours=hour, minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
            if premiere and rng.random() < 0.4:
                cid, title, ctype = next(x for x in _MS_TITLES if x[0] == premiere)
            else:
                cid, title, ctype = rng.choice(_MS_TITLES)
            evt = rng.choices(ev_names, weights=ev_w)[0]
            pop = rng.choice(_MS_POPS)
            # Burst: GRU2 degradado ese día → más errores concentrados ahí.
            if d.date() == _MS_ERROR_BURST and pop == "GRU2" and rng.random() < 0.35:
                evt = "PLAYBACK_ERROR"
            doc = {
                "ts": _iso(ts),
                "event": evt,
                "session_id": f"s-{rng.getrandbits(40):010x}",
                "user_id": rng.choices(users, weights=user_w)[0],
                "content_id": cid,
                "title": title,
                "content_type": ctype,
                "genre": genre_by_title[cid],
                "device": rng.choices(dev_names, weights=dev_w)[0],
                "cdn_pop": pop,
                "country": rng.choices(c_names, weights=c_w)[0],
                "bitrate_kbps": rng.choice(_MS_BITRATES),
            }
            # Campos condicionales: presentes SOLO cuando aplican (truco value_count).
            if evt == "PLAY_END":
                doc["watch_seconds"] = rng.randint(120, 7200 if ctype != "LIVE" else 10800)
            elif evt == "REBUFFER":
                doc["buffering_ms"] = rng.randint(150, 8000)
            elif evt == "PLAYBACK_ERROR":
                doc["error_code"] = rng.choice(_MS_ERRORS)
            lines.append((ts, json.dumps(doc, ensure_ascii=False, separators=(",", ":"))))
        d += timedelta(days=1)

    lines.sort(key=lambda x: x[0])
    out_path.write_text("\n".join(l for _, l in lines) + "\n", encoding="utf-8")
    print(f"media-streaming: {len(lines)} lineas ({_iso(lines[0][0])} -> {_iso(lines[-1][0])})")


# ── fortianalyzer (FortiGate nativo) ─────────────────────────────────────────

def build_fortianalyzer(src_path: Path, out_path: Path) -> None:
    """El dataset ES el FortiGate multi-type real (`datasets/firewall.log`), que ya
    está en la ventana. Se copia bajo el slug propio (el bundling resuelve por
    nombre de archivo) verificando la ventana; si algún día el origen se corre de
    fechas, esto avisa en vez de propagarlo en silencio."""
    text = src_path.read_text(encoding="utf-8", errors="replace")
    dates = re.findall(r"^date=(\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
    lo, hi = min(dates), max(dates)
    # WINDOW_END inclusive acá: el origen tiene líneas fechadas justo en el día límite.
    if not (WINDOW_START.strftime("%Y-%m-%d") <= lo and hi <= WINDOW_END.strftime("%Y-%m-%d")):
        print(f"[fortianalyzer] ADVERTENCIA: {src_path.name} fuera de ventana ({lo} -> {hi}) — revisar")
    out_path.write_text(text, encoding="utf-8")
    n = text.count("\n")
    print(f"fortianalyzer: {n} lineas (copiado de {src_path.name}, {lo} -> {hi})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", default="", help="dir con volve_production.xlsx, ecommerce.ndjson y "
                    "synthea/csv/ — OPCIONAL: sin esto solo se generan los datasets sin fuentes "
                    "externas (alyc, media-streaming, fortianalyzer)")
    ap.add_argument("--out", default="datasets", help="dir de salida (default: datasets/)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    if args.sources:
        src = Path(args.sources)
        build_oil_gas(src / "volve_production.xlsx", out / "produccion-pozos.log")
        build_ecommerce(src / "ecommerce.ndjson", out / "ventas-ecommerce.log")
        build_health(src / "synthea" / "csv", out / "encuentros-clinicos.log")
    build_alyc(out / "transacciones-alyc.log")
    build_media_streaming(out / "streaming-ott.log")
    build_fortianalyzer(out / "firewall.log", out / "fortianalyzer.log")


if __name__ == "__main__":
    main()
