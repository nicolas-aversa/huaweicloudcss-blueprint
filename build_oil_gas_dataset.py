"""
build_oil_gas_dataset.py
========================

Dataset sintético de la operación de pozos no convencionales en Vaca Muerta
(Neuquén): lo que publica el SCADA de cada pad por MQTT, en JSON, un mensaje por
línea. Para demos de operadoras de petróleo y gas.

Dos tipos de mensaje, como en los tópicos del broker:

- `telemetria`: cada 2 horas, por pozo: presiones, choke, caudales de petróleo,
  gas y agua y, según cómo se extrae, las variables de la bomba;
- `alarma`: lo que dispara el SCADA, con su ciclo (activa → reconocida →
  normalizada) y el comentario que deja el operador del centro de control al
  reconocerla, en texto libre → búsqueda híbrida ("se congeló el choke" /
  "hidratos en el orificio").

Lo que trae, para que la demo cuente algo:

- curvas de declinación de pozos de shale y un pad nuevo que entra en
  producción → pronósticos; el gas produce más en invierno (demanda);
- eventos que se notan → detección de anomalías: la interferencia de la
  fractura del pad vecino (frac hit), un corte de energía en un bloque, una ola
  polar que congela los chokes de gas, un bloqueo de rutas que llena los
  tanques de un bloque sin oleoducto y una bomba electrosumergible que se va
  calentando hasta romperse;
- coordenadas de los pads → mapa; el pozo → perfil por entidad.

Todo es inventado (pozos, pads, bloques y operadores salen de un generador con
semilla fija).

Uso:   py build_oil_gas_dataset.py [--desde 2026-03-15] [--hasta 2026-10-12]
Sale:  datasets/telemetria-pozos.ndjson
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ART = timezone(timedelta(hours=-3))
SALIDA = Path(__file__).parent / "datasets" / "telemetria-pozos.ndjson"
CADA_HORAS = 2

# ── Dónde ───────────────────────────────────────────────────────────────────
# bloque: (latitud, longitud del centro, prefijo de los pozos, cómo se evacúa el crudo)
BLOQUES = {
    "Añelo Norte": (-38.27, -68.80, "ANN", "Oleoducto"),
    "Añelo Sur": (-38.43, -68.70, "ANS", "Oleoducto"),
    "Rincón Este": (-37.52, -68.88, "RIE", "Camión"),       # sin oleoducto: el crudo sale en camión
}
# (pad, bloque, tipo de pozo, extracción, pozos, puesta en producción, meses produciendo al empezar)
PADS = [
    ("PAD-AN-12", "Añelo Norte", "Petrolífero", "Gas lift", 4, None, 14),
    ("PAD-AN-22", "Añelo Norte", "Petrolífero", "Surgente", 4, None, 7),
    ("PAD-AN-31", "Añelo Norte", "Petrolífero", "Surgente", 4, date(2026, 5, 5), 0),
    ("PAD-AS-05", "Añelo Sur", "Gasífero", "Surgente", 4, None, 10),
    ("PAD-AS-14", "Añelo Sur", "Petrolífero", "Electrosumergible", 4, None, 18),
    ("PAD-RE-03", "Rincón Este", "Petrolífero", "Bombeo mecánico", 4, None, 30),
    ("PAD-RE-07", "Rincón Este", "Petrolífero", "Electrosumergible", 4, None, 20),
]

# ── Las alarmas del SCADA ───────────────────────────────────────────────────
ALARMAS = {
    "PCAS-H": ("Alta presión en casing", "Alta"),
    "PBOCA-L": ("Presión de boca baja", "Media"),
    "COM": ("Falla de comunicación con la RTU", "Baja"),
    "PWR": ("Parada por falta de energía", "Alta"),
    "ESP-TM": ("Alta temperatura de motor de la bomba electrosumergible", "Media"),
    "ESP-SC": ("Parada de bomba electrosumergible por sobrecorriente", "Alta"),
    "HID": ("Posible formación de hidratos en el choke", "Media"),
    "TQ-H": ("Nivel alto en tanque de almacenamiento", "Alta"),
    "H2S": ("Detección de H2S en la locación", "Crítica"),
    "BM-LL": ("Bajo llenado de bomba", "Baja"),
    "FUGA": ("Pérdida en línea de conducción", "Alta"),
    "SEP-H": ("Nivel alto en separador de ensayo", "Media"),
}
# Lo que escribe el operador al reconocerla. Muchas formas de decir lo mismo, a propósito.
COMENTARIOS = {
    "PCAS-H": ["Interferencia por la fractura del pad vecino, se cierra el pozo preventivamente",
               "Subió la presión de casing por el frac del pad de al lado", "Posible frac hit, se coordina con completación",
               "Comunicación con la fractura vecina, pozo en cierre", "Aumento brusco de presión en el anular, se avisa a ingeniería"],
    "PBOCA-L": ["Caída de presión en boca, se revisa el choke", "Presión baja en cabeza de pozo, se ajusta el orificio",
                "El pozo viene perdiendo presión, se evalúa pasar a extracción artificial",
                "Baja presión de surgencia, se programa cambio de choke"],
    "COM": ["Sin señal de la RTU, se envía recorredor", "Se cayó el enlace de radio del pad", "RTU sin comunicación, se reinicia el gateway",
            "Pérdida de telemetría, el panel solar no carga", "La RTU quedó con batería baja, sin datos"],
    "PWR": ["Corte de energía en la subestación, pozos parados", "Sin tensión en el pad, se pide grupo electrógeno",
            "Caída de la línea de media tensión, se espera a la distribuidora", "Apagón en el bloque, bombas detenidas"],
    "ESP-TM": ["Temperatura del motor en aumento, se baja la frecuencia", "Motor de la electrosumergible caliente, se monitorea",
               "Sube la temperatura de fondo, posible desgaste de la bomba", "Se reduce la velocidad del variador por temperatura alta"],
    "ESP-SC": ["La bomba electrosumergible disparó por sobrecorriente, se pide equipo de pulling",
               "La ESP se paró por alta corriente, posible bomba trabada", "Falla de la bomba de fondo, pozo sin producción",
               "Sobrecorriente en el variador, no rearranca"],
    "HID": ["Se inyectó metanol para disolver hidratos", "Choke congelado por el frío, se calefacciona la línea",
            "Hidratos en el orificio, se baja el caudal", "Escarcha en la línea de conducción, se aplica inhibidor"],
    "TQ-H": ["Tanque casi lleno, los camiones no pueden entrar por el bloqueo", "No hay retiro de crudo por el corte de ruta, se cierra el pozo",
             "Nivel de tanque al límite, se reduce la producción", "Sin camiones por el conflicto gremial, pozos en cierre"],
    "H2S": ["Detector de H2S en alarma, se evacua la locación y se verifica",
            "Lectura de ácido sulfhídrico, se revisa con equipo autónomo", "Falsa alarma de H2S, sensor descalibrado"],
    "BM-LL": ["Bajo llenado de la bomba, se ajusta el régimen de golpes", "Golpe de fluido en el aparato de bombeo, se baja la velocidad",
              "La bomba trabaja con poco llenado, se programa ensayo"],
    "FUGA": ["Pérdida en la brida del cabezal, se ajustó", "Mancha de crudo en la línea de conducción, se aísla el tramo",
             "Goteo en la válvula del manifold, se cambia la empaquetadura"],
    "SEP-H": ["Separador con nivel alto, se drena manualmente", "Se trabó la válvula de descarga del separador",
              "Nivel alto en el separador, se ajusta el controlador"],
}
# Lo que el operador agrega a veces. Pocas, para que los textos distintos no exploten.
AGREGADOS = [", se deja asentado en el parte diario", ", se avisa al supervisor de turno", ", queda en observación",
             ", se coordina con mantenimiento"]
OPERADORES = [f"OP-{2000 + i * 17}" for i in range(22)]   # el centro de control, por turnos

# ── Lo que se nota (anomalías) ──────────────────────────────────────────────
FRAC_HIT = (datetime(2026, 4, 12, tzinfo=ART), datetime(2026, 4, 24, tzinfo=ART))   # se fractura el AN-31; lo siente el AN-22
CORTE_DE_ENERGIA = ("Añelo Sur", datetime(2026, 6, 3, 2, tzinfo=ART), datetime(2026, 6, 3, 11, tzinfo=ART))
OLA_POLAR = (datetime(2026, 7, 7, tzinfo=ART), datetime(2026, 7, 11, tzinfo=ART))
BLOQUEO = ("Rincón Este", datetime(2026, 8, 25, 6, tzinfo=ART), datetime(2026, 8, 27, 18, tzinfo=ART))
ESP_QUE_SE_ROMPE = ("RIE-1061h", datetime(2026, 9, 8, tzinfo=ART), datetime(2026, 9, 19, 14, tzinfo=ART),
                    datetime(2026, 9, 30, 10, tzinfo=ART))   # empieza a calentar, se rompe, vuelve después del pulling


def _h(horas: float) -> timedelta:
    return timedelta(hours=horas)


def _pozos(r: random.Random) -> list[dict]:
    pozos = []
    for n_pad, (pad, bloque, tipo, extraccion, cuantos, alta, meses) in enumerate(PADS):
        lat0, lon0, prefijo, _ = BLOQUES[bloque]
        lat, lon = lat0 + r.uniform(-0.05, 0.05), lon0 + r.uniform(-0.05, 0.05)
        for i in range(cuantos):
            gas = tipo == "Gasífero"
            pozos.append({
                "id": f"{prefijo}-{1000 + n_pad * 10 + i + 1}h", "pad": pad, "bloque": bloque, "tipo": tipo, "extraccion": extraccion,
                "lat": round(lat + r.uniform(-0.0004, 0.0004), 5), "lon": round(lon + r.uniform(-0.0004, 0.0004), 5),
                "alta": alta, "meses": meses,
                # Caudal inicial (petróleo en m3/d; en los de gas, miles de m3/d de gas) y su declinación.
                "qi": r.uniform(380, 520) if gas else r.uniform(140, 230), "di": 0.12 if gas else 0.18,
                "gor": r.uniform(180, 260), "corte_agua": r.uniform(0.08, 0.28), "condensado": r.uniform(8, 15),
                "choke": r.randint(28, 40) if gas else r.randint(18, 32),
            })
    return pozos


def _declinacion(p: dict, ts: datetime) -> float:
    """Fracción del caudal inicial a esa fecha (declinación hiperbólica de shale)."""
    if p["alta"]:
        meses = (ts.date() - p["alta"]).days / 30.4
    else:
        meses = p["meses"] + (ts.date() - date(2026, 3, 15)).days / 30.4
    return 1 / (1 + 1.1 * p["di"] * max(0.0, meses)) ** (1 / 1.1)


def _invierno(ts: datetime) -> float:
    """El gas se abre más en invierno: la demanda manda."""
    return {5: 1.1, 6: 1.25, 7: 1.3, 8: 1.2, 9: 1.05}.get(ts.month, 0.95)


def generar(desde: date, hasta: date, semilla: int = 20261014) -> tuple[list[dict], Counter]:
    r = random.Random(semilla)
    pozos = _pozos(r)
    mensajes: list[tuple[datetime, dict]] = []
    n_alarma = [0]
    abiertas: dict[tuple[str, str], datetime] = {}       # (pozo, código) → hasta cuándo sigue activa

    def activo(p):
        return {"id": p["id"], "pad": p["pad"], "bloque": p["bloque"], "tipo": p["tipo"], "extraccion": p["extraccion"],
                "lat": p["lat"], "lon": p["lon"]}

    def alarma(p, ts, codigo, dura_h, valor=None, umbral=None, comentario_seguro=False):
        """El ciclo de una alarma: activa, reconocida (con el comentario) y normalizada."""
        if abiertas.get((p["id"], codigo), ts - _h(1)) > ts:
            return                                            # ya está activa: el SCADA no la repite
        abiertas[(p["id"], codigo)] = ts + _h(dura_h)
        n_alarma[0] += 1
        descripcion, severidad = ALARMAS[codigo]
        base = {"id": f"ALM-{n_alarma[0]:06d}", "codigo": codigo, "descripcion": descripcion, "severidad": severidad}
        if valor is not None:
            base.update(valor=round(valor, 1), umbral=umbral)
        gw = f"GW-{p['pad']}"
        t1 = ts + timedelta(minutes=r.uniform(2, 25))
        mensajes.append((ts, {"tipo": "alarma", "gateway": gw, "pozo": activo(p), "alarma": {**base, "estado": "Activa"}}))
        reconocida = {"tipo": "alarma", "gateway": gw, "pozo": activo(p), "alarma": {**base, "estado": "Reconocida"},
                      "operador": r.choice(OPERADORES)}
        if comentario_seguro or r.random() < 0.8:
            reconocida["comentario"] = r.choice(COMENTARIOS[codigo]) + (r.choice(AGREGADOS) if r.random() < 0.3 else "")
        mensajes.append((t1, reconocida))
        if dura_h:
            mensajes.append((ts + _h(dura_h), {"tipo": "alarma", "gateway": gw, "pozo": activo(p),
                                                "alarma": {**base, "estado": "Normalizada"}}))

    # Los golpes de la fractura vecina: a cada pozo del AN-22 le llega en un momento.
    golpes = {p["id"]: FRAC_HIT[0] + _h(r.uniform(0, (FRAC_HIT[1] - FRAC_HIT[0]).total_seconds() / 3600 - 72))
              for p in pozos if p["pad"] == "PAD-AN-22"}
    # El tanque de cada pad sin oleoducto: se llena con la producción y lo vacían los camiones.
    tanque = {pad: 40.0 for pad, bloque, *_ in PADS if BLOQUES[bloque][3] == "Camión"}

    t = datetime(desde.year, desde.month, desde.day, tzinfo=ART)
    fin = datetime(hasta.year, hasta.month, hasta.day, tzinfo=ART) + timedelta(days=1)
    while t < fin:
        bloqueado = BLOQUEO[1] <= t < BLOQUEO[2]
        recien_liberado = BLOQUEO[2] <= t < BLOQUEO[2] + _h(18)   # los camiones demorados entran todos juntos
        for pad in tanque:                                    # 2 h de producción entran; los camiones retiran de día
            if recien_liberado:
                tanque[pad] = max(15.0, tanque[pad] - r.uniform(15, 25))
            elif not bloqueado and 8 <= t.hour <= 18:
                tanque[pad] = max(15.0, tanque[pad] - r.uniform(8, 16))
            tanque[pad] = min(98.0, tanque[pad] + r.uniform(2.5, 4.0) * (1.6 if bloqueado else 1))
        for p in pozos:
            if p["alta"] and t.date() < p["alta"]:
                continue
            ts = t + timedelta(seconds=r.randint(0, 90))     # el gateway publica cuando puede
            gas = p["tipo"] == "Gasífero"
            q = p["qi"] * _declinacion(p, ts) * r.lognormvariate(0, 0.04)
            corte_agua = p["corte_agua"]
            if p["alta"] and (ts.date() - p["alta"]).days < 12:   # flowback: el pozo nuevo limpia y sube
                avance = (ts.date() - p["alta"]).days / 12
                q *= 0.4 + 0.6 * avance
                corte_agua = 0.6 - (0.6 - corte_agua) * avance
            if gas:
                q *= _invierno(ts)
            m = {"choke_64": p["choke"]}
            cerrado, sin_dato, extra = False, False, {}

            # ── Lo que pasa ──
            golpe = golpes.get(p["id"])
            if golpe and golpe <= ts < golpe + _h(72):
                avance = (ts - golpe).total_seconds() / 3600 / 72
                q *= 0.25 + 0.6 * avance
                corte_agua = 0.6 - 0.3 * avance
                extra["presion_casing_psi"] = r.uniform(2600, 3400) * (1 - 0.4 * avance)
                if ts < golpe + _h(18):
                    cerrado = r.random() < 0.6
                alarma(p, golpe, "PCAS-H", r.uniform(20, 40), valor=r.uniform(2700, 3300), umbral=2200, comentario_seguro=True)
            elif golpe and ts >= golpe + _h(72):
                q *= 0.85                                     # no vuelve del todo
            bloque, desde_c, hasta_c = CORTE_DE_ENERGIA
            if p["bloque"] == bloque and desde_c <= ts < hasta_c:
                if p["extraccion"] in ("Electrosumergible", "Gas lift", "Bombeo mecánico"):
                    cerrado = True
                    alarma(p, desde_c + timedelta(minutes=r.uniform(0, 8)), "PWR", (hasta_c - desde_c).total_seconds() / 3600 + 0.5,
                           comentario_seguro=True)
                if ts >= desde_c + _h(4):                     # la RTU se queda sin batería
                    sin_dato = True
                    alarma(p, desde_c + _h(r.uniform(4, 4.5)), "COM", (hasta_c - desde_c).total_seconds() / 3600 - 3.5)
            if gas and OLA_POLAR[0] <= ts < OLA_POLAR[1]:
                q *= r.uniform(0.5, 0.75)
                extra["presion_boca_psi"] = r.uniform(900, 2600)   # oscila: se tapa y se destapa el orificio
                if r.random() < 0.35:
                    alarma(p, ts, "HID", r.uniform(3, 10))
            if p["pad"] in tanque and tanque[p["pad"]] >= 95:
                cerrado = True
                alarma(p, ts, "TQ-H", max(4.0, (BLOQUEO[2] - ts).total_seconds() / 3600) + r.uniform(2, 6),
                       valor=tanque[p["pad"]], umbral=90, comentario_seguro=True)
            pozo_esp, empieza, rompe, vuelve = ESP_QUE_SE_ROMPE
            calentamiento = 0.0
            if p["id"] == pozo_esp and empieza <= ts < rompe:
                calentamiento = (ts - empieza).total_seconds() / (rompe - empieza).total_seconds()
                q *= 1 - 0.15 * calentamiento
                if calentamiento > 0.55 and r.random() < 0.3:
                    alarma(p, ts, "ESP-TM", r.uniform(4, 12), valor=122 + 45 * calentamiento, umbral=145)
            if p["id"] == pozo_esp and rompe <= ts < vuelve:
                cerrado = True
                alarma(p, rompe, "ESP-SC", (vuelve - rompe).total_seconds() / 3600, valor=r.uniform(96, 110), umbral=85,
                       comentario_seguro=True)
            # Lo de todos los días: comunicación, bombeo, alguna pérdida.
            if r.random() < 0.002:
                sin_dato = True
                alarma(p, ts, "COM", r.uniform(1, 6))
            if r.random() < 0.002:
                alarma(p, ts, "SEP-H", r.uniform(0.5, 3), valor=r.uniform(82, 95), umbral=80)
            if p["extraccion"] == "Bombeo mecánico" and r.random() < 0.01:
                alarma(p, ts, "BM-LL", r.uniform(1, 5), valor=r.uniform(45, 58), umbral=60)
            if p["extraccion"] == "Surgente" and not gas and _declinacion(p, ts) < 0.45 and r.random() < 0.008:
                alarma(p, ts, "PBOCA-L", r.uniform(2, 8), valor=r.uniform(380, 440), umbral=450)
            if r.random() < 0.0008:
                alarma(p, ts, "FUGA", r.uniform(2, 10))
            if r.random() < 0.00008:
                alarma(p, ts, "H2S", r.uniform(0.5, 2))
            if sin_dato:
                continue

            # ── La medición ──
            if cerrado:
                q = 0.0
            if gas:
                m["gas_mm3d"] = round(q, 1)
                m["petroleo_m3d"] = round(0 if cerrado else p["condensado"] * _declinacion(p, ts) * r.lognormvariate(0, 0.05), 1)
                m["agua_m3d"] = round(0 if cerrado else r.uniform(3, 9), 1)
            else:
                m["petroleo_m3d"] = round(q, 1)
                m["gas_mm3d"] = round(q * p["gor"] / 1000 * r.lognormvariate(0, 0.04), 1)
                m["agua_m3d"] = round(q * corte_agua / (1 - corte_agua), 1)
            fraccion = 0 if cerrado else _declinacion(p, ts)
            if p["extraccion"] == "Surgente":
                boca = (2600 if cerrado else 350 + (2200 if gas else 1800) * fraccion ** 0.8) * r.lognormvariate(0, 0.03)
                m["presion_boca_psi"] = round(extra.get("presion_boca_psi", boca), 1)
                m["presion_casing_psi"] = round(extra.get("presion_casing_psi", boca * r.uniform(1.05, 1.2)), 1)
            else:
                m["presion_boca_psi"] = round(r.uniform(120, 350) if not cerrado else r.uniform(40, 90), 1)
                m["presion_casing_psi"] = round(r.uniform(300, 700), 1)
            m["temperatura_boca_c"] = round((18 if cerrado else 35 + 25 * fraccion) + r.uniform(-2, 2) - (6 if ts.month in (6, 7, 8) else 0), 1)
            if p["extraccion"] == "Electrosumergible":
                parado = cerrado
                m["frecuencia_hz"] = 0.0 if parado else round(r.uniform(52, 58), 1)
                m["corriente_a"] = 0.0 if parado else round(r.uniform(55, 70) * (1 + 0.3 * calentamiento * r.uniform(0.5, 1.5)), 1)
                m["temperatura_motor_c"] = round(r.uniform(60, 75) if parado else r.uniform(118, 128) + 45 * calentamiento, 1)
            elif p["extraccion"] == "Bombeo mecánico":
                m["golpes_por_minuto"] = 0.0 if cerrado else round(r.uniform(5, 7), 1)
                m["llenado_bomba_pct"] = 0.0 if cerrado else round(r.uniform(72, 96), 1)
            elif p["extraccion"] == "Gas lift":
                m["inyeccion_gas_mm3d"] = 0.0 if cerrado else round(r.uniform(25, 45), 1)
            if p["pad"] in tanque:
                m["nivel_tanque_pct"] = round(tanque[p["pad"]], 1)
            mensajes.append((ts, {"tipo": "telemetria", "gateway": f"GW-{p['pad']}", "pozo": activo(p), "medicion": m}))
        t += _h(CADA_HORAS)

    inicio = datetime(desde.year, desde.month, desde.day, tzinfo=ART)
    mensajes = sorted(((ts, m) for ts, m in mensajes if inicio <= ts < fin), key=lambda x: x[0])
    salida = [{"ts": ts.isoformat(timespec="seconds"), **m} for ts, m in mensajes]
    return salida, Counter(m["tipo"] if m["tipo"] == "telemetria" else m["alarma"]["codigo"] for m in salida)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--desde", default="2026-03-15")
    p.add_argument("--hasta", default="2026-10-12")
    a = p.parse_args()
    mensajes, cuenta = generar(date.fromisoformat(a.desde), date.fromisoformat(a.hasta))
    SALIDA.parent.mkdir(exist_ok=True)
    with SALIDA.open("w", encoding="utf-8", newline="") as f:
        f.writelines(json.dumps(m, ensure_ascii=False, separators=(",", ":")) + "\n" for m in mensajes)
    print(f"{len(mensajes):,} mensajes -> {SALIDA} ({SALIDA.stat().st_size / 1e6:.1f} MB)")
    print("  " + " · ".join(f"{k}: {v:,}" for k, v in cuenta.most_common()))


if __name__ == "__main__":
    main()
