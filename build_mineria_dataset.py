"""
build_mineria_dataset.py
========================

Dataset sintético del sistema de gestión de flota de una mina de oro a cielo
abierto en la cordillera de San Juan: cada fila es un evento que registra el
despacho de la flota (un ciclo de acarreo completo, una demora o una alerta de
un camión), tal como se exporta de su base de datos. Para demos de mineras.

Lo que trae, para que la demo cuente algo:

- ciclos con sus tiempos (cola, carga, viaje cargado, descarga, retorno),
  toneladas, material, destino y ley de oro → pronósticos de producción; desde
  septiembre la pala entra en una zona de más ley;
- el comentario del despachador o del operador en texto libre → búsqueda
  híbrida ("se quedó dormido" / "microsueño del operador", "rueda pinchada" /
  "pinchadura en el neumático");
- eventos que se notan → detección de anomalías: un viento blanco que para la
  mina dos días, el chancador primario fuera de servicio, una racha de
  pinchaduras en la rampa sur y una semana de alertas de fatiga en el turno
  noche; la voladura del mediodía para la flota todos los días;
- coordenadas → mapa; legajo y DNI del operador → enmascarado; el camión →
  perfil por entidad.

Todo es inventado (camiones, operadores, DNI y la mina salen de un generador
con semilla fija).

Uso:   py build_mineria_dataset.py [--desde 2026-05-01] [--hasta 2026-10-12]
Sale:  datasets/ciclos-de-acarreo.csv
"""
from __future__ import annotations

import argparse
import csv
import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ART = timezone(timedelta(hours=-3))
SALIDA = Path(__file__).parent / "datasets" / "ciclos-de-acarreo.csv"

COLUMNAS = [
    "fecha_hora", "evento", "turno", "cuadrilla", "camion", "flota", "operador_legajo", "operador_dni", "equipo_carguio",
    "frente", "material", "destino", "toneladas", "ley_au_gpt", "distancia_km", "tiempo_cola_min", "tiempo_carga_min",
    "tiempo_viaje_cargado_min", "tiempo_descarga_min", "tiempo_retorno_min", "tiempo_ciclo_min", "velocidad_max_kmh",
    "combustible_l", "tipo_demora", "duracion_min", "alerta", "comentario", "latitud", "longitud",
]

# ── La mina ─────────────────────────────────────────────────────────────────
LAT, LON = -29.40, -69.88
# frente: (equipo de carguío, Δlat, Δlon, proporción de mineral, ley media en g/t)
FRENTES = {
    "Rajo Norte · Banco 4.210": ("PALA-01", 0.004, -0.003, 0.35, 0.78),
    "Rajo Norte · Banco 4.195": ("PALA-02", 0.003, -0.001, 0.45, 0.92),
    "Rajo Norte · Banco 4.180": ("PALA-02", 0.002, 0.001, 0.55, 1.05),
    "Rajo Sur · Banco 4.330": ("PALA-03", -0.006, 0.004, 0.25, 0.70),
    "Rajo Sur · Banco 4.315": ("CARG-01", -0.007, 0.006, 0.30, 0.74),
}
# destino: (Δlat, Δlon, km desde el rajo norte, km desde el rajo sur)
DESTINOS = {
    "Chancador primario": (0.020, -0.015, 3.2, 4.5),
    "Stockpile": (0.012, -0.020, 2.6, 3.8),
    "Botadero Norte": (0.030, 0.010, 2.4, 4.8),
    "Botadero Este": (-0.005, 0.035, 4.2, 3.0),
}
CAMIONES = [(f"CAM-{i:02d}", "Camión 220 t" if i <= 10 else "Camión 290 t") for i in range(1, 19)]

# ── Lo que anotan el despachador y los operadores ───────────────────────────
DEMORAS = {
    "Voladura": ["Voladura programada en el banco, flota detenida", "Equipos evacuados por tronadura",
                 "Espera por la voladura del mediodía", "Se detiene el acarreo hasta que liberen el área de voladura"],
    "Cambio de turno": ["Cambio de turno en el estacionamiento", "Relevo de operadores", "Charla de seguridad del inicio de turno"],
    "Abastecimiento de combustible": ["Carga de combustible en la estación de mina", "Camión en la isla de combustible",
                                      "Repostaje programado"],
    "Colación": ["Colación del operador", "Pausa para comer en el comedor de mina", "Relevo para la cena"],
    "Mantenimiento no programado": ["Pérdida de aceite hidráulico, va al taller", "Falla en el sistema de frenos, camión al taller",
                                    "Alarma de motor, se detiene para revisión", "Falla eléctrica en el tablero, se llama al electricista",
                                    "Se rompió un espejo retrovisor, se reemplaza", "Recalentamiento del motor en la subida",
                                    "Fuga en una manguera hidráulica de la tolva", "La dirección quedó dura, camión al taller"],
    "Neumático": ["Pinchadura en la rueda trasera derecha", "Se cambió un neumático por corte con roca filosa",
                  "Rueda reventada en la rampa sur", "Neumático con un tajo lateral, va a la gomería",
                  "Corte en la banda de rodamiento por piedras en el camino"],
    "Clima": ["Viento blanco, se suspende la operación", "Nevada intensa, caminos cerrados", "Niebla en la rampa, visibilidad nula",
              "Ráfagas fuertes, se detiene el carguío", "Camino congelado, se espera a la motoniveladora"],
    "Chancador detenido": ["Chancador primario detenido, se descarga en el stock", "Tolva del chancador llena, se espera",
                           "Chancador en mantenimiento, se desvía a la pila de acopio"],
}
ALERTAS = {
    "Fatiga del operador": ["El sistema detectó un microsueño del operador, se lo releva",
                            "Cabeceo del operador, se detiene el equipo y hace pausa activa",
                            "Alerta de somnolencia, se llama por radio al operador",
                            "Ojos cerrados por más de dos segundos, se reemplaza al conductor", "El operador se quedó dormido al volante"],
    "Exceso de velocidad": ["Bajada de la rampa por encima del límite", "Superó los 40 km/h cargado",
                            "Velocidad excesiva en la zona de cruce, se notifica al supervisor"],
    "Proximidad": ["Camioneta demasiado cerca del camión de extracción", "Aviso de proximidad con la motoniveladora",
                   "Dos camiones en el cruce sin distancia de seguridad"],
    "Presión de neumático baja": ["Presión baja en el neumático trasero izquierdo", "Pérdida de presión en una rueda, se revisa en el taller",
                                  "El sensor marca una goma desinflada"],
    "Temperatura de frenos alta": ["Frenos calientes en el descenso, se baja la velocidad", "Temperatura de frenos alta, parada de enfriamiento"],
}
# Lo que se agrega a veces en lo que no es rutina. Pocas, para que los textos distintos no exploten.
AGREGADOS = [", se informa al despacho", ", queda registrado en el parte del turno", ", aviso al supervisor",
             ", se coordina con mantenimiento mina"]
RUTINA = {"Voladura", "Cambio de turno", "Abastecimiento de combustible", "Colación"}
NOTAS_DE_CICLO = ["Material pegajoso, la tolva no descarga bien", "Carga desbalanceada, se reacomoda", "Cola larga en la pala",
                  "Camino con barro en la rampa", "Bolón grande en la carga, se avisa al chancador"]

# ── Lo que se nota (anomalías) ──────────────────────────────────────────────
VIENTO_BLANCO = (datetime(2026, 7, 20, 6, tzinfo=ART), datetime(2026, 7, 22, 14, tzinfo=ART))
CHANCADOR_FUERA = (datetime(2026, 8, 11, 6, tzinfo=ART), datetime(2026, 8, 12, 18, tzinfo=ART))
PINCHADURAS = ("Rajo Sur", datetime(2026, 6, 8, tzinfo=ART), datetime(2026, 6, 13, tzinfo=ART))
FATIGA_NOCHE = (datetime(2026, 9, 14, tzinfo=ART), datetime(2026, 9, 21, tzinfo=ART))
ZONA_DE_ALTA_LEY = ("Rajo Norte · Banco 4.180", date(2026, 9, 1), 1.6)


def _min(x: float) -> timedelta:
    return timedelta(minutes=x)


def _cuadrilla(d: date, noche: bool) -> str:
    """Roster 14×14: A y B dos semanas en la mina (una de día, una de noche), después C y D."""
    dias = (d - date(2026, 1, 5)).days % 28
    a, b = ("A", "B") if dias < 14 else ("C", "D")
    if dias % 14 >= 7:
        a, b = b, a
    return b if noche else a


def generar(desde: date, hasta: date, semilla: int = 20261015) -> tuple[list[dict], Counter]:
    r = random.Random(semilla)
    operadores = {(c, cam): (f"LG-{1000 + n * 13:04d}", str(r.randint(20_000_000, 42_999_999)))
                  for n, (c, (cam, _)) in enumerate((c, x) for c in "ABCD" for x in CAMIONES)}
    filas: list[dict] = []
    frentes = list(FRENTES)

    def cerca(dlat, dlon, radio=0.0015):
        return round(LAT + dlat + r.uniform(-radio, radio), 5), round(LON + dlon + r.uniform(-radio, radio), 5)

    def fila(ts, evento, turno, cam, flota, **kw):
        cuadrilla = _cuadrilla((ts - timedelta(hours=8)).date() if turno == "Noche" else ts.date(), turno == "Noche")
        legajo, dni = operadores[(cuadrilla, cam)]
        filas.append({"fecha_hora": ts.isoformat(timespec="seconds"), "evento": evento, "turno": turno, "cuadrilla": cuadrilla,
                      "camion": cam, "flota": flota, "operador_legajo": legajo, "operador_dni": dni, **kw})

    def demora(ts, turno, cam, flota, tipo, minutos, lugar, comentario=None):
        if comentario is None and r.random() < 0.75:
            comentario = r.choice(DEMORAS[tipo])
            if tipo not in RUTINA and r.random() < 0.35:
                comentario += r.choice(AGREGADOS)
        fila(ts, "Demora", turno, cam, flota, tipo_demora=tipo, duracion_min=round(minutos, 1), comentario=comentario or "",
             latitud=lugar[0], longitud=lugar[1])
        return ts + _min(minutos)

    d = desde
    while d <= hasta:
        for turno, inicio_h in (("Día", 8), ("Noche", 20)):
            inicio = datetime(d.year, d.month, d.day, inicio_h, tzinfo=ART)
            fin = inicio + timedelta(hours=12)
            invierno = d.month in (6, 7, 8)
            # Clima del turno: en invierno, a veces niebla o nieve para toda la flota un rato.
            clima = None
            if invierno and r.random() < 0.08:
                desde_c = inicio + _min(r.uniform(30, 500))
                clima = (desde_c, desde_c + _min(r.uniform(60, 200)))
            for n, (cam, flota) in enumerate(CAMIONES):
                capacidad = 220 if flota == "Camión 220 t" else 290
                frente = frentes[(n + d.toordinal() + (1 if turno == "Noche" else 0)) % len(frentes)]
                carguio, dlat, dlon, prop_mineral, ley_media = FRENTES[frente]
                rajo = frente.split(" · ")[0]
                estacionamiento = cerca(0.010, 0.005, 0.0008)
                t = inicio
                # Arranque del turno: el cambio de turno y, a la mitad, la colación y el combustible.
                t = demora(t, turno, cam, flota, "Cambio de turno", r.uniform(25, 40), estacionamiento)
                colacion = inicio + _min(r.uniform(230, 420))
                combustible = inicio + _min(r.uniform(120, 600))
                voladura = datetime(d.year, d.month, d.day, 13, tzinfo=ART) if turno == "Día" else None
                hechos = set()
                while t < fin - _min(20):
                    # Lo que para a toda la flota.
                    if VIENTO_BLANCO[0] <= t < VIENTO_BLANCO[1]:
                        if "viento" not in hechos:
                            hechos.add("viento")
                            demora(t, turno, cam, flota, "Clima", (VIENTO_BLANCO[1] - t).total_seconds() / 60, estacionamiento,
                                   comentario=r.choice(DEMORAS["Clima"][:2]))
                        t = VIENTO_BLANCO[1]
                        continue
                    if clima and clima[0] <= t < clima[1]:
                        t = demora(t, turno, cam, flota, "Clima", (clima[1] - t).total_seconds() / 60, cerca(dlat, dlon),
                                   comentario=r.choice(DEMORAS["Clima"][2:]))
                        continue
                    if voladura and t >= voladura and "voladura" not in hechos:
                        hechos.add("voladura")
                        t = demora(max(t, voladura), turno, cam, flota, "Voladura", r.uniform(35, 45), estacionamiento)
                        continue
                    if t >= colacion and "colacion" not in hechos:
                        hechos.add("colacion")
                        t = demora(t, turno, cam, flota, "Colación", r.uniform(30, 40), estacionamiento)
                        continue
                    if t >= combustible and "combustible" not in hechos:
                        hechos.add("combustible")
                        t = demora(t, turno, cam, flota, "Abastecimiento de combustible", r.uniform(15, 25), cerca(0.008, 0.008, 0.0005))
                        continue
                    # Lo que se rompe.
                    p_goma = 0.03 if rajo == PINCHADURAS[0] and PINCHADURAS[1] <= t < PINCHADURAS[2] else 0.0012
                    if r.random() < p_goma:
                        if r.random() < 0.6:
                            fila(t, "Alerta", turno, cam, flota, alerta="Presión de neumático baja",
                                 comentario=r.choice(ALERTAS["Presión de neumático baja"]), latitud=cerca(dlat, dlon)[0],
                                 longitud=cerca(dlat, dlon)[1])
                        t = demora(t, turno, cam, flota, "Neumático", r.uniform(60, 180), cerca(0.009, 0.010, 0.0005))
                        continue
                    if r.random() < 0.003:
                        t = demora(t, turno, cam, flota, "Mantenimiento no programado", r.uniform(40, 200), cerca(0.009, 0.010, 0.0005))
                        continue

                    # ── Un ciclo de acarreo ──
                    mineral = r.random() < prop_mineral
                    ley_frente = ZONA_DE_ALTA_LEY[2] if frente == ZONA_DE_ALTA_LEY[0] and d >= ZONA_DE_ALTA_LEY[1] else ley_media
                    ley = round(max(0.2, r.lognormvariate(0, 0.35) * ley_frente), 2) if mineral else ""
                    chancador_fuera = CHANCADOR_FUERA[0] <= t < CHANCADOR_FUERA[1]
                    if mineral:
                        destino = "Stockpile" if chancador_fuera else "Chancador primario"
                        material = "Mineral alta ley" if ley >= 1.0 else "Mineral baja ley"
                    else:
                        destino = "Botadero Norte" if rajo == "Rajo Norte" else "Botadero Este"
                        material = "Estéril"
                    ddlat, ddlon, km_norte, km_sur = DESTINOS[destino]
                    km = (km_norte if rajo == "Rajo Norte" else km_sur) * r.uniform(0.95, 1.08)
                    lento = 0.88 if invierno else 1.0
                    cola = max(0.0, r.gauss(2.5, 1.5)) + (r.uniform(4, 12) if chancador_fuera and mineral else 0)
                    carga = r.uniform(4, 6) if carguio == "CARG-01" else r.uniform(2.5, 4)
                    ida = km / (r.uniform(14, 18) * lento) * 60
                    descarga = r.uniform(1, 1.6)
                    vuelta = km / (r.uniform(26, 32) * lento) * 60
                    ciclo = cola + carga + ida + descarga + vuelta
                    t_descarga = t + _min(cola + carga + ida)
                    vel_max = r.uniform(32, 42) * lento
                    nota = ""
                    if chancador_fuera and mineral and r.random() < 0.25:
                        nota = r.choice(DEMORAS["Chancador detenido"])
                    elif r.random() < 0.01:
                        nota = r.choice(NOTAS_DE_CICLO)
                    lat, lon = cerca(ddlat, ddlon, 0.001)
                    fila(t_descarga, "Ciclo de acarreo", turno, cam, flota, equipo_carguio=carguio, frente=frente, material=material,
                         destino=destino, toneladas=round(capacidad * r.uniform(0.88, 1.04), 1), ley_au_gpt=ley,
                         distancia_km=round(km, 2), tiempo_cola_min=round(cola, 1), tiempo_carga_min=round(carga, 1),
                         tiempo_viaje_cargado_min=round(ida, 1), tiempo_descarga_min=round(descarga, 1),
                         tiempo_retorno_min=round(vuelta, 1), tiempo_ciclo_min=round(ciclo, 1),
                         velocidad_max_kmh=round(vel_max, 1),
                         combustible_l=round(ciclo / 60 * r.uniform(160, 210) * (1.15 if capacidad == 290 else 1), 1),
                         comentario=nota, latitud=lat, longitud=lon)
                    # Alertas del ciclo.
                    p_fatiga = 0.0015 * (6 if turno == "Noche" and FATIGA_NOCHE[0] <= t < FATIGA_NOCHE[1] else 1) * (
                        2 if turno == "Noche" and 3 <= (t.hour + 21) % 24 <= 8 else 1)
                    for alerta, p in (("Fatiga del operador", p_fatiga), ("Exceso de velocidad", 0.004), ("Proximidad", 0.003),
                                      ("Temperatura de frenos alta", 0.002)):
                        if r.random() < p:
                            cuando = t + _min(r.uniform(0, ciclo))
                            alat, alon = cerca((dlat + ddlat) / 2, (dlon + ddlon) / 2, 0.004)
                            fila(cuando, "Alerta", turno, cam, flota, alerta=alerta,
                                 velocidad_max_kmh=round(r.uniform(44, 52), 1) if alerta == "Exceso de velocidad" else "",
                                 comentario=(r.choice(ALERTAS[alerta]) + (r.choice(AGREGADOS) if r.random() < 0.35 else ""))
                                 if r.random() < 0.85 else "", latitud=alat, longitud=alon)
                    t += _min(ciclo)
        d += timedelta(days=1)
    filas.sort(key=lambda f: f["fecha_hora"])
    return filas, Counter(f["evento"] for f in filas)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--desde", default="2026-05-01")
    p.add_argument("--hasta", default="2026-10-12")
    a = p.parse_args()
    filas, cuenta = generar(date.fromisoformat(a.desde), date.fromisoformat(a.hasta))
    SALIDA.parent.mkdir(exist_ok=True)
    with SALIDA.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNAS)
        w.writerows([fila.get(c, "") for c in COLUMNAS] for fila in filas)
    print(f"{len(filas):,} eventos -> {SALIDA} ({SALIDA.stat().st_size / 1e6:.1f} MB)")
    print("  " + " · ".join(f"{k}: {v:,}" for k, v in cuenta.most_common()))


if __name__ == "__main__":
    main()
