"""
build_emergencias_dataset.py
============================

Dataset sintético de un servicio de emergencias médicas prehospitalarias en el
AMBA (CABA y conurbano): cada fila es un incidente, de la llamada al cierre, en
castellano. Para demos de un prestador de emergencias, urgencias, médico a
domicilio y videoconsulta.

Todo es inventado (sin datos de nadie): afiliados, DNI, teléfonos, móviles y
médicos salen de un generador con semilla fija. Lo que trae, para que la demo
cuente algo:

- el motivo de la llamada en texto libre, como lo anota el operador, con muchas
  formas de decir lo mismo → búsqueda híbrida ("falta de aire" / "disnea");
- volumen con ritmo diario y semanal, y estacionalidad (invierno: respiratorio
  y pediatría; verano: calor y gastro) → pronósticos;
- eventos que se notan → detección de anomalías: una ola de calor, una
  tormenta que inunda el sur del conurbano y estira los tiempos de llegada,
  una semana de gripe y una caída del sistema de despacho;
- coordenadas → mapa; DNI y teléfono → enmascarado; afiliado → perfil por
  entidad (hay pacientes crónicos que llaman seguido).

Uso:   py build_emergencias_dataset.py [--desde 2026-01-01] [--hasta 2026-10-12] [--por-dia 200]
Sale:  datasets/emergencias-medicas.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ART = timezone(timedelta(hours=-3))
SALIDA = Path(__file__).parent / "datasets" / "emergencias-medicas.csv"

COLUMNAS = [
    "fecha_hora", "nro_incidente", "canal", "segmento", "plan", "afiliado_id", "dni_titular", "telefono_contacto",
    "edad_paciente", "sexo_paciente", "codigo", "tipo_servicio", "categoria", "motivo", "localidad", "zona",
    "latitud", "longitud", "base_operativa", "tipo_movil", "movil", "operador_id", "medico_id",
    "tiempo_asignacion_seg", "tiempo_respuesta_min", "sla_min", "cumple_sla", "tiempo_atencion_min",
    "resultado", "centro_derivacion", "costo_servicio_ars", "satisfaccion",
]

# ── Dónde ───────────────────────────────────────────────────────────────────
# (localidad, zona, lat, lon, base operativa, peso)
LOCALIDADES = [
    ("Palermo", "CABA", -34.5889, -58.4306, "Base Palermo", 9), ("Belgrano", "CABA", -34.5627, -58.4583, "Base Palermo", 7),
    ("Recoleta", "CABA", -34.5875, -58.3974, "Base Palermo", 6), ("Núñez", "CABA", -34.5456, -58.4636, "Base Palermo", 4),
    ("Villa Urquiza", "CABA", -34.5733, -58.4877, "Base Palermo", 4), ("Caballito", "CABA", -34.6195, -58.4400, "Base Caballito", 7),
    ("Almagro", "CABA", -34.6098, -58.4210, "Base Caballito", 4), ("Flores", "CABA", -34.6280, -58.4637, "Base Caballito", 5),
    ("Villa Devoto", "CABA", -34.6011, -58.5133, "Base Caballito", 3), ("Mataderos", "CABA", -34.6590, -58.5030, "Base Caballito", 2),
    ("San Telmo", "CABA", -34.6211, -58.3731, "Base Caballito", 2), ("Barracas", "CABA", -34.6460, -58.3820, "Base Caballito", 2),
    ("Vicente López", "GBA Norte", -34.5260, -58.4790, "Base San Isidro", 6), ("San Isidro", "GBA Norte", -34.4713, -58.5286, "Base San Isidro", 6),
    ("San Fernando", "GBA Norte", -34.4422, -58.5598, "Base San Isidro", 3), ("Tigre", "GBA Norte", -34.4264, -58.5797, "Base San Isidro", 4),
    ("Pilar", "GBA Norte", -34.4587, -58.9142, "Base Pilar", 3),
    ("Morón", "GBA Oeste", -34.6534, -58.6198, "Base Morón", 5), ("Ramos Mejía", "GBA Oeste", -34.6431, -58.5651, "Base Morón", 4),
    ("Haedo", "GBA Oeste", -34.6440, -58.5940, "Base Morón", 2), ("Castelar", "GBA Oeste", -34.6519, -58.6440, "Base Morón", 3),
    ("Ituzaingó", "GBA Oeste", -34.6582, -58.6684, "Base Morón", 2),
    ("Avellaneda", "GBA Sur", -34.6624, -58.3650, "Base Lomas de Zamora", 3), ("Lanús", "GBA Sur", -34.7068, -58.3916, "Base Lomas de Zamora", 4),
    ("Lomas de Zamora", "GBA Sur", -34.7609, -58.4061, "Base Lomas de Zamora", 5), ("Banfield", "GBA Sur", -34.7445, -58.3960, "Base Lomas de Zamora", 3),
    ("Adrogué", "GBA Sur", -34.7998, -58.3906, "Base Lomas de Zamora", 2), ("Quilmes", "GBA Sur", -34.7206, -58.2543, "Base Quilmes", 5),
]
HOSPITALES = {
    "CABA": ["Hospital Fernández", "Hospital Durand", "Hospital Pirovano", "Hospital Argerich", "Hospital Álvarez"],
    "GBA Norte": ["Hospital Central de San Isidro", "Hospital Houssay"],
    "GBA Oeste": ["Hospital Posadas", "Hospital de Morón"],
    "GBA Sur": ["Hospital Gandulfo", "Hospital Fiorito", "Hospital Iriarte"],
}
DEMORA_POR_ZONA = {"CABA": 1.0, "GBA Norte": 1.1, "GBA Oeste": 1.15, "GBA Sur": 1.25}

# ── Qué pasó: el motivo, como lo anota el operador ──────────────────────────
# categoría → código → frases. Muchas formas de decir lo mismo, a propósito.
MOTIVOS = {
    "Respiratorio": {
        "Rojo": ["dificultad respiratoria severa, labios morados", "no puede respirar, se ahoga", "crisis asmática que no cede con el broncodilatador",
                 "disnea súbita en reposo"],
        "Amarillo": ["falta de aire al caminar", "le cuesta respirar desde anoche", "tos con silbido en el pecho", "broncoespasmo",
                     "agitado y con tos persistente", "respira con dificultad y tiene fiebre"],
        "Verde": ["tos y mocos hace tres días", "dolor de garganta y tos seca", "congestión nasal y tos", "catarro con fiebre baja",
                  "control de bronquitis", "tos que no se va hace una semana"],
    },
    "Cardiovascular": {
        "Rojo": ["dolor de pecho opresivo que se irradia al brazo izquierdo", "dolor precordial intenso con sudoración",
                 "paro cardiorrespiratorio", "se desvaneció y no responde", "dolor en el pecho, antecedente de infarto"],
        "Amarillo": ["palpitaciones y mareo", "presión alta 18/10 con dolor de cabeza", "dolor en el pecho al respirar hondo",
                     "taquicardia que no se va", "le late muy rápido el corazón"],
        "Verde": ["control de presión arterial", "presión un poco alta, sin síntomas", "ajuste de medicación para la presión"],
    },
    "Neurológico": {
        "Rojo": ["pérdida de fuerza en un brazo y la boca torcida", "habla arrastrada de golpe", "convulsión que no para",
                 "inconsciente después de convulsionar"],
        "Amarillo": ["dolor de cabeza muy intenso, el peor de su vida", "mareos y vértigo con vómitos", "desorientado, no reconoce a la familia",
                     "hormigueo en la cara y en la mano"],
        "Verde": ["dolor de cabeza que no calma con analgésicos", "migraña de siempre", "vértigo leve al levantarse"],
    },
    "Traumatismo": {
        "Rojo": ["accidente de moto con golpe en la cabeza", "caída de altura, no se puede mover", "herida de arma blanca", "atropellado por un auto"],
        "Amarillo": ["caída de la escalera, dolor en la cadera", "corte profundo en la mano que sangra mucho", "golpe en la cabeza y vomitó",
                     "fractura de muñeca, se ve deformada"],
        "Verde": ["esguince de tobillo jugando al fútbol", "golpe en la rodilla, puede caminar", "dolor de espalda después de levantar peso",
                  "lumbalgia", "se torció la muñeca"],
    },
    "Gastrointestinal": {
        "Amarillo": ["dolor abdominal fuerte del lado derecho", "vómitos con sangre", "diarrea con deshidratación, casi no orina",
                     "dolor de panza intenso que no cede"],
        "Verde": ["vómitos desde anoche", "diarrea y dolor de estómago", "acidez y ardor en el estómago", "náuseas y malestar general"],
    },
    "Infeccioso": {
        "Amarillo": ["fiebre de 40 con escalofríos", "fiebre alta y manchas en la piel", "fiebre y ardor al orinar"],
        "Verde": ["fiebre y dolor de cuerpo", "síntomas de gripe", "dolor de garganta con fiebre", "fiebre de 38 desde ayer",
                  "dolor de oído con fiebre"],
    },
    "Pediátrico": {
        "Rojo": ["bebé que no respira bien, se pone morado", "convulsión con fiebre en un nene"],
        "Amarillo": ["bebé de 8 meses con fiebre de 39", "nene con vómitos que no tolera líquidos", "bronquiolitis, el bebé respira agitado"],
        "Verde": ["nena con dolor de oído", "chico con tos y mocos", "fiebre en un nene de 4 años", "control de un bebé con catarro",
                  "erupción en la piel de un nene"],
    },
    "Salud mental": {
        "Amarillo": ["crisis de angustia, dice que se quiere morir", "agitación psicomotriz", "intoxicación con alcohol"],
        "Verde": ["ataque de pánico", "crisis de ansiedad, siente que le falta el aire", "no puede dormir hace días"],
    },
    "Calor": {
        "Amarillo": ["golpe de calor, mareado y con vómitos", "deshidratación de un adulto mayor por el calor"],
        "Verde": ["agotamiento por el calor", "insolación leve"],
    },
    "Control clínico": {
        "Verde": ["receta de medicación habitual", "certificado médico para el trabajo", "control de glucemia, es diabético",
                  "consulta por resultados de análisis", "erupción en la piel con picazón", "dolor muscular", "control posoperatorio"],
    },
    "Traslado": {
        "Programado": ["traslado a sesión de diálisis", "traslado para estudio programado", "alta de internación a domicilio",
                       "traslado a rehabilitación kinesiológica"],
    },
}
# Lo que el operador agrega a veces. Pocas, para que los textos distintos no exploten.
AGREGADOS = [", adulto mayor que vive solo", ", es diabético", ", con antecedentes cardíacos", ", embarazada", " en la vía pública",
             " en el trabajo"]

# El código decide el servicio, el móvil, el SLA y el costo.
SERVICIO = {"Rojo": "Emergencia", "Amarillo": "Urgencia", "Verde": "Médico a domicilio", "Programado": "Traslado programado"}
MOVIL = {"Emergencia": ("UTIM", "UTIM"), "Urgencia": ("Móvil de urgencias", "URG"), "Médico a domicilio": ("Auto médico", "AM"),
         "Videoconsulta": ("Videoconsulta", "TELE"), "Traslado programado": ("Ambulancia de traslado", "TRA")}
SLA = {"Emergencia": 15, "Urgencia": 30, "Médico a domicilio": 120, "Videoconsulta": 15, "Traslado programado": 30}
COSTO = {"Emergencia": (185_000, 45_000), "Urgencia": (92_000, 18_000), "Médico a domicilio": (48_000, 9_000),
         "Videoconsulta": (16_000, 3_000), "Traslado programado": (118_000, 22_000)}
CATEGORIAS_DE_VIDEO = {"Respiratorio", "Infeccioso", "Pediátrico", "Salud mental", "Control clínico", "Gastrointestinal"}

# ── Lo que se nota (anomalías) ──────────────────────────────────────────────
OLA_DE_CALOR = (date(2026, 2, 9), date(2026, 2, 12))
SEMANA_DE_GRIPE = (date(2026, 7, 6), date(2026, 7, 12))
TORMENTA = date(2026, 5, 20)            # el sur del conurbano, inundado
CAIDA_DEL_DESPACHO = (datetime(2026, 9, 3, 18, tzinfo=ART), datetime(2026, 9, 3, 21, 30, tzinfo=ART))

# Llamados por hora del día (relativo).
POR_HORA = [0.35, 0.28, 0.24, 0.22, 0.24, 0.32, 0.55, 0.85, 1.1, 1.25, 1.3, 1.25, 1.15, 1.05, 1.0, 1.0, 1.05, 1.15,
            1.25, 1.4, 1.45, 1.35, 1.05, 0.65]


def _en(d: date, rango: tuple[date, date]) -> bool:
    return rango[0] <= d <= rango[1]


def _estacion(d: date) -> str:
    return "invierno" if d.month in (6, 7, 8) else "verano" if d.month in (12, 1, 2) else "media"


def _peso_de_categoria(cat: str, d: date) -> float:
    est = _estacion(d)
    base = {"Respiratorio": 14, "Cardiovascular": 9, "Neurológico": 6, "Traumatismo": 12, "Gastrointestinal": 10,
            "Infeccioso": 12, "Pediátrico": 11, "Salud mental": 5, "Calor": 0, "Control clínico": 14}[cat]
    if cat in ("Respiratorio", "Pediátrico", "Infeccioso"):
        base *= 1.9 if est == "invierno" else 1.3 if d.month in (5, 9) else 0.75 if est == "verano" else 1
        if _en(d, SEMANA_DE_GRIPE):
            base *= 1.8
    if cat == "Gastrointestinal" and est == "verano":
        base *= 1.4
    if cat == "Calor" and est == "verano":
        base = 3 * (7 if _en(d, OLA_DE_CALOR) else 1)
    return base


def _volumen(d: date, por_dia: int, r: random.Random) -> int:
    v = por_dia
    v *= {5: 1.15, 6: 1.18, 0: 1.05}.get(d.weekday(), 1.0)
    v *= 1.25 if _estacion(d) == "invierno" else 0.95 if _estacion(d) == "verano" else 1.0
    if _en(d, OLA_DE_CALOR):
        v *= 1.5
    if _en(d, SEMANA_DE_GRIPE):
        v *= 1.6
    if d == TORMENTA:
        v *= 1.2
    return max(10, int(r.gauss(v, v * 0.07)))


def _afiliados(r: random.Random, n: int = 22_000) -> list[dict]:
    """El padrón: titulares con su plan y su domicilio. Unos pocos (crónicos) llaman mucho."""
    pesos = [l[5] for l in LOCALIDADES]
    padron = []
    for i in range(n):
        seg = r.choices(["Socio particular", "Convenio obra social/prepaga", "Área protegida (empresa)"], [58, 27, 15])[0]
        edad = int(min(95, max(18, r.gauss(52, 18))))
        plan = ("Área protegida" if seg.startswith("Área") else r.choice(["Convenio prepaga", "Convenio obra social"]) if seg.startswith("Convenio")
                else "Plan Senior" if edad >= 68 and r.random() < 0.7 else r.choices(["Plan Integral", "Plan Familiar"], [55, 45])[0])
        padron.append({
            "id": f"AF-{4_100_000 + i * 7 + r.randint(0, 6):07d}", "segmento": seg, "plan": plan, "edad": edad,
            "sexo": r.choices(["F", "M", "X"], [54, 45, 1])[0], "dni": str(r.randint(11_000_000, 45_999_999)),
            "telefono": f"11-{r.randint(4000, 6999)}-{r.randint(1000, 9999)}",
            "loc": r.choices(LOCALIDADES, pesos)[0], "familia": plan == "Plan Familiar" or (plan.startswith("Convenio") and r.random() < 0.4),
            # Zipf: los crónicos (adultos mayores, cardíacos) llaman mucho más.
            "peso": (1 / (1 + i % 900)) ** 0.6 * (3 if edad >= 70 else 1),
        })
    return padron


def generar(desde: date, hasta: date, por_dia: int, semilla: int = 20261013) -> list[list]:
    r = random.Random(semilla)
    padron = _afiliados(r)
    pesos_padron = [a["peso"] for a in padron]
    filas: list[list] = []
    n = 0
    d = desde
    while d <= hasta:
        for _ in range(_volumen(d, por_dia, r)):
            hora = r.choices(range(24), POR_HORA)[0]
            ts = datetime(d.year, d.month, d.day, hora, r.randint(0, 59), r.randint(0, 59), tzinfo=ART)
            filas.append(_incidente(r, ts, padron, pesos_padron))
            n += 1
        d += timedelta(days=1)
    filas.sort(key=lambda f: f[0])
    for i, f in enumerate(filas):
        f[1] = f"EM-{f[0][2:4]}-{i + 1:07d}"
    return filas


def _incidente(r: random.Random, ts: datetime, padron: list[dict], pesos: list[float]) -> list:
    d = ts.date()
    a = r.choices(padron, pesos)[0]
    # Código primero (mezcla del servicio), después la categoría que lo explica.
    codigo = r.choices(["Rojo", "Amarillo", "Verde", "Programado"], [9, 26, 59, 6])[0]
    if codigo == "Programado":
        cat = "Traslado"
        ts = ts.replace(hour=r.randint(7, 18))      # los programados, en horario de atención
    else:
        cats = [c for c, m in MOTIVOS.items() if codigo in m]
        cat = r.choices(cats, [_peso_de_categoria(c, d) for c in cats])[0]
    servicio = SERVICIO[codigo]
    if codigo == "Verde" and cat in CATEGORIAS_DE_VIDEO and r.random() < 0.32:
        servicio = "Videoconsulta"
    motivo = r.choice(MOTIVOS[cat][codigo])
    if cat not in ("Pediátrico", "Traslado") and r.random() < 0.3:
        motivo += r.choice(AGREGADOS if a["segmento"].startswith("Área") else AGREGADOS[:4] + AGREGADOS[4:5])
    # El paciente: el titular o, en un plan familiar, un hijo.
    if cat == "Pediátrico":
        edad, sexo = r.choice([r.randint(0, 2), r.randint(1, 12)]), r.choice(["F", "M"])
    elif a["familia"] and r.random() < 0.3:
        edad, sexo = r.randint(14, 30), r.choice(["F", "M"])
    else:
        edad, sexo = a["edad"], a["sexo"]
    if ", embarazada" in motivo:
        sexo, edad = "F", min(edad, r.randint(19, 42))
    # Dónde: la casa del titular o, en área protegida, la empresa (el mismo partido).
    loc, zona, lat, lon, base, _ = a["loc"]
    lat, lon = round(lat + r.uniform(-0.012, 0.012), 5), round(lon + r.uniform(-0.012, 0.012), 5)
    canal = ("Línea área protegida" if a["segmento"].startswith("Área") and r.random() < 0.6
             else r.choices(["App", "WhatsApp"], [55, 45])[0] if servicio == "Videoconsulta"
             else r.choices(["Teléfono 0810", "WhatsApp", "App"], [70, 18, 12] if servicio == "Médico a domicilio" else [88, 4, 8])[0])
    tipo_movil, prefijo = MOVIL[servicio]
    movil = f"{prefijo}-{r.randint(1, 14 if servicio == 'Emergencia' else 30):03d}" if servicio != "Videoconsulta" else "TELE"

    # Tiempos: según el código, la zona, la hora y lo que pasó ese día.
    asignacion = {"Emergencia": 25, "Urgencia": 70, "Médico a domicilio": 240, "Videoconsulta": 90, "Traslado programado": 0}[servicio]
    asignacion = int(asignacion * r.lognormvariate(0, 0.45)) if asignacion else 0
    respuesta = {"Emergencia": 11, "Urgencia": 22, "Médico a domicilio": 70, "Videoconsulta": 7, "Traslado programado": 4}[servicio]
    factor = DEMORA_POR_ZONA[zona] if servicio != "Videoconsulta" else 1.0
    if 17 <= ts.hour <= 20 and servicio not in ("Videoconsulta", "Traslado programado"):
        factor *= 1.25
    if _estacion(d) == "invierno":
        factor *= 1.12
    if _en(d, SEMANA_DE_GRIPE):
        factor *= 1.45
    if _en(d, OLA_DE_CALOR):
        factor *= 1.3
    if d == TORMENTA and servicio != "Videoconsulta":
        factor *= 2.6 if zona == "GBA Sur" else 1.4
    if CAIDA_DEL_DESPACHO[0] <= ts <= CAIDA_DEL_DESPACHO[1]:
        asignacion = int(asignacion * 9 + 600)
        factor *= 1.8
    respuesta = max(1.0, r.lognormvariate(math.log(respuesta * factor), 0.35)) + asignacion / 60
    sla = SLA[servicio]
    cumple = "Sí" if respuesta <= sla else "No"
    atencion = max(4, round(r.gauss({"Emergencia": 35, "Urgencia": 26, "Médico a domicilio": 18, "Videoconsulta": 12,
                                     "Traslado programado": 45}[servicio], 6), 0))

    resultado = r.choices(*{
        "Emergencia": (["Trasladado a centro asistencial", "Atendido en domicilio", "Fallecido", "Falsa alarma", "Cancelado por el socio",
                        "Paciente no localizado"], [62, 26, 2, 4, 4, 2]),
        "Urgencia": (["Atendido en domicilio", "Trasladado a centro asistencial", "Cancelado por el socio", "Domicilio no encontrado"],
                     [64, 27, 6, 3]),
        "Médico a domicilio": (["Atendido en domicilio", "Derivado a consultorio", "Cancelado por el socio"], [88, 5, 7]),
        "Videoconsulta": (["Resuelto por videoconsulta", "Derivado a médico a domicilio", "Derivado a guardia"], [82, 12, 6]),
        "Traslado programado": (["Traslado realizado", "Reprogramado"], [96, 4]),
    }[servicio])[0]
    if cumple == "No" and resultado == "Atendido en domicilio" and r.random() < 0.25:
        resultado = "Cancelado por el socio"      # se cansó de esperar
    centro = ""
    if resultado in ("Trasladado a centro asistencial", "Derivado a guardia"):
        centro = r.choice(HOSPITALES[zona] + ["Sanatorio de la red prestadora"])
    elif servicio == "Traslado programado":
        centro = r.choice(["Centro de diálisis de la red", "Centro de diagnóstico por imágenes", "Instituto de rehabilitación"])

    media, desvio = COSTO[servicio]
    costo = int(round(max(media * 0.4, r.gauss(media, desvio)) / 100) * 100)
    if resultado.startswith("Cancelado") or resultado in ("Falsa alarma", "Domicilio no encontrado", "Paciente no localizado", "Reprogramado"):
        costo = int(costo * 0.35 / 100) * 100
    satisfaccion = ""
    if r.random() < 0.55 and not resultado.startswith(("Cancelado", "Fallecido")):
        nota = r.gauss(8.6 if cumple == "Sí" else 5.4, 1.4)
        satisfaccion = str(int(min(10, max(0, round(nota)))))
    medico = "" if servicio == "Traslado programado" else f"MED-{r.randint(1, 60 if servicio == 'Videoconsulta' else 260):03d}"
    return [
        ts.isoformat(timespec="seconds"), "", canal, a["segmento"], a["plan"], a["id"], a["dni"], a["telefono"],
        edad, sexo, codigo, servicio, cat, motivo, loc, zona, lat, lon, base, tipo_movil, movil, f"OP-{r.randint(1, 45):02d}",
        medico, asignacion, round(respuesta, 1), sla, cumple, int(atencion), resultado, centro, costo, satisfaccion,
    ]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--desde", default="2026-01-01")
    p.add_argument("--hasta", default="2026-10-12")
    p.add_argument("--por-dia", type=int, default=200)
    a = p.parse_args()
    filas = generar(date.fromisoformat(a.desde), date.fromisoformat(a.hasta), a.por_dia)
    SALIDA.parent.mkdir(exist_ok=True)
    with SALIDA.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNAS)
        w.writerows(filas)
    print(f"{len(filas):,} incidentes -> {SALIDA} ({SALIDA.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
