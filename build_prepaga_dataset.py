"""
build_prepaga_dataset.py
========================

Dataset sintético del sistema de autorizaciones de una prepaga u obra social
en Argentina: cada fila es una transacción que manda un prestador (el
validador del consultorio, la farmacia, la guardia, la web de prestadores o la
app del socio) para validar la credencial y autorizar una práctica. Sale como
lo exporta el sistema: `.txt` separado por `|`, con fechas `dd/mm/aaaa` y coma
decimal.

Todo es inventado (sin datos de nadie): socios, DNI, prestadores y diagnósticos
salen de un generador con semilla fija. Lo que trae, para que la demo cuente
algo:

- el diagnóstico como lo escribe el médico y la observación del auditor, en
  texto libre, con muchas formas de decir lo mismo → búsqueda híbrida ("dolor
  de espalda" / "lumbalgia", "presión alta" / "hipertensión");
- volumen con ritmo diario y semanal y estacionalidad (dengue en otoño,
  respiratorio en invierno, campaña antigripal) → pronósticos;
- eventos que se notan → detección de anomalías: una caída del validador que
  dispara los tiempos de respuesta, una ola de bronquiolitis, rechazos por mora
  después de un aumento de cuota y un centro de kinesiología que factura
  sesiones de más (sobreprestación);
- coordenadas del prestador → mapa; DNI y diagnóstico del socio → enmascarado;
  el socio → perfil por entidad.

Uso:   py build_prepaga_dataset.py [--desde 2026-04-01] [--hasta 2026-10-12] [--por-dia 560]
Sale:  datasets/autorizaciones-prepaga.txt
"""
from __future__ import annotations

import argparse
import random
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

SALIDA = Path(__file__).parent / "datasets" / "autorizaciones-prepaga.txt"

COLUMNAS = [
    "fecha_hora", "nro_transaccion", "canal", "tipo_solicitud", "segmento", "plan", "afiliado_id", "dni_afiliado", "edad",
    "sexo", "prestador_id", "prestador", "tipo_prestador", "localidad", "provincia", "latitud", "longitud", "especialidad",
    "codigo_practica", "practica", "diagnostico_cie10", "diagnostico", "resultado", "motivo_rechazo", "observacion_auditor",
    "horas_de_auditoria", "tiempo_respuesta_ms", "importe_ars", "cobertura_pct", "copago_ars",
]

# ── Dónde ───────────────────────────────────────────────────────────────────
# (localidad, provincia, latitud, longitud, peso)
LOCALIDADES = [
    ("Palermo", "CABA", -34.5889, -58.4306, 9), ("Belgrano", "CABA", -34.5627, -58.4583, 8), ("Recoleta", "CABA", -34.5875, -58.3974, 7),
    ("Caballito", "CABA", -34.6195, -58.4400, 6), ("Microcentro", "CABA", -34.6037, -58.3816, 5), ("Villa Urquiza", "CABA", -34.5733, -58.4877, 3),
    ("Vicente López", "Buenos Aires", -34.5260, -58.4790, 5), ("San Isidro", "Buenos Aires", -34.4713, -58.5286, 5),
    ("Pilar", "Buenos Aires", -34.4587, -58.9142, 4), ("Tigre", "Buenos Aires", -34.4264, -58.5797, 3),
    ("Morón", "Buenos Aires", -34.6534, -58.6198, 4), ("Lomas de Zamora", "Buenos Aires", -34.7609, -58.4061, 4),
    ("Quilmes", "Buenos Aires", -34.7206, -58.2543, 3), ("La Plata", "Buenos Aires", -34.9214, -57.9545, 4),
    ("Mar del Plata", "Buenos Aires", -38.0055, -57.5426, 3), ("Rosario", "Santa Fe", -32.9442, -60.6505, 5),
    ("Córdoba", "Córdoba", -31.4201, -64.1888, 6), ("Mendoza", "Mendoza", -32.8895, -68.8458, 4),
    ("San Miguel de Tucumán", "Tucumán", -26.8083, -65.2176, 2), ("Neuquén", "Neuquén", -38.9516, -68.0591, 2),
    ("Salta", "Salta", -24.7821, -65.4232, 2),
]
PLANES = {  # plan: (peso, copago de una consulta, cobertura de medicamentos)
    "Básico": (30, 4_500, 40), "Intermedio": (34, 3_000, 40), "Superior": (24, 0, 50), "Premium": (12, 0, 70),
}

# ── Qué se pide: diagnóstico (como lo escribe el médico) y práctica ─────────
# CIE-10 → (especialidad, frases). Muchas formas de decir lo mismo, a propósito.
DIAGNOSTICOS = {
    "M54.5": ("Traumatología", ["lumbalgia", "dolor lumbar con irradiación a pierna izquierda", "dolor de espalda baja desde hace una semana",
                                "contractura lumbar", "lumbociatalgia derecha"]),
    "M17.1": ("Traumatología", ["gonartrosis de rodilla derecha", "dolor de rodilla al subir escaleras", "artrosis de rodilla",
                                "rodilla inflamada con dolor crónico"]),
    "S83.5": ("Traumatología", ["esguince de rodilla jugando al fútbol", "lesión de ligamento cruzado anterior", "torcedura de rodilla con derrame"]),
    "I10": ("Cardiología", ["hipertensión arterial", "presión alta en los controles", "HTA en tratamiento", "tensión arterial elevada"]),
    "I48": ("Cardiología", ["fibrilación auricular", "palpitaciones con pulso irregular", "arritmia en control"]),
    "R51": ("Clínica médica", ["cefalea intensa", "dolor de cabeza fuerte desde ayer", "jaqueca", "migraña recurrente"]),
    "J11": ("Clínica médica", ["síndrome gripal", "fiebre, tos y dolor de cuerpo", "gripe", "cuadro viral con fiebre alta"]),
    "A90": ("Clínica médica", ["sospecha de dengue", "fiebre alta y dolor detrás de los ojos", "dengue sin signos de alarma"]),
    "E11": ("Clínica médica", ["diabetes tipo 2", "glucemia alta en ayunas", "control de diabetes"]),
    "Z00": ("Clínica médica", ["control de salud", "chequeo anual", "examen periódico"]),
    "J21": ("Pediatría", ["bronquiolitis", "bebé con dificultad para respirar y silbido en el pecho", "lactante con tos y agitación"]),
    "H66": ("Pediatría", ["otitis media aguda", "dolor de oído con fiebre en un nene", "infección de oído"]),
    "E66": ("Nutrición", ["obesidad", "sobrepeso con indicación de plan alimentario", "índice de masa corporal elevado"]),
    "F41.1": ("Salud mental", ["trastorno de ansiedad generalizada", "ataques de pánico frecuentes", "ansiedad que no lo deja dormir"]),
    "F32": ("Salud mental", ["episodio depresivo", "tristeza persistente y desgano", "depresión moderada"]),
    "N39.0": ("Urología", ["infección urinaria", "ardor al orinar", "cistitis"]),
    "Z34": ("Obstetricia", ["control de embarazo", "embarazo de 20 semanas, control"]),
    "O80": ("Obstetricia", ["parto normal", "embarazo a término en trabajo de parto"]),
    "H52": ("Oftalmología", ["control de agudeza visual", "miopía, cambio de anteojos", "ve borroso de lejos"]),
    "L70": ("Dermatología", ["acné", "granos en la cara", "acné inflamatorio"]),
    "K29": ("Gastroenterología", ["gastritis", "ardor de estómago", "dolor en la boca del estómago"]),
    "K80": ("Gastroenterología", ["litiasis vesicular", "cálculos en la vesícula", "cólico biliar a repetición"]),
    "C50": ("Oncología", ["cáncer de mama en tratamiento", "carcinoma de mama con quimioterapia"]),
    "K02": ("Odontología", ["caries", "dolor de muela", "arreglo de caries"]),
}
AGREGADOS = [", control", ", derivado por guardia", ", segunda consulta", ", según estudios previos"]

# tipo de solicitud → [(código, práctica, importe, especialidades que la piden o "*", requiere auditoría)]
PRACTICAS = {
    "Validación de consulta": [("420101", "Consulta médica en consultorio", 24_000, "*", False)],
    "Consulta de guardia": [("420301", "Consulta de guardia", 38_000, "*", False)],
    "Práctica ambulatoria": [
        ("250101", "Kinesiología (sesión)", 12_000, {"Traumatología"}, False),
        ("660001", "Laboratorio de rutina", 26_000, "*", False),
        ("180104", "Ecografía abdominal", 39_000, {"Gastroenterología", "Clínica médica", "Urología"}, False),
        ("340201", "Radiografía de columna", 22_000, {"Traumatología"}, False),
        ("170101", "Electrocardiograma", 15_000, {"Cardiología", "Clínica médica"}, False),
        ("230101", "Sesión de psicoterapia", 14_000, {"Salud mental"}, False),
        ("010101", "Obturación de caries", 30_000, {"Odontología"}, False),
        ("300101", "Control obstétrico con ecografía", 42_000, {"Obstetricia"}, False)],
    "Estudio de alta complejidad": [
        ("341101", "Resonancia magnética de columna lumbar", 185_000, {"Traumatología"}, True),
        ("341105", "Resonancia magnética de rodilla", 175_000, {"Traumatología"}, True),
        ("341201", "Tomografía de tórax", 140_000, {"Clínica médica", "Oncología"}, True),
        ("170201", "Ecocardiograma Doppler", 76_000, {"Cardiología"}, True),
        ("180501", "Videoendoscopía digestiva alta", 160_000, {"Gastroenterología"}, True)],
    "Internación programada": [
        ("120801", "Artroscopía de rodilla", 2_800_000, {"Traumatología"}, True),
        ("100401", "Colecistectomía laparoscópica", 2_200_000, {"Gastroenterología"}, True),
        ("110101", "Parto", 1_600_000, {"Obstetricia"}, True)],
    "Internación de urgencia": [("430101", "Internación clínica (día)", 650_000, {"Clínica médica", "Pediatría", "Cardiología"}, False)],
    "Medicamento": [
        ("MED-AMB", "Medicamento ambulatorio", 32_000, "*", False),
        ("MED-CRO", "Medicamento para tratamiento crónico", 58_000, {"Cardiología", "Clínica médica", "Salud mental"}, False),
        ("MED-OBE", "Medicamento para el tratamiento de la obesidad", 420_000, {"Nutrición"}, True),
        ("MED-ONC", "Medicamento oncológico de alto costo", 3_500_000, {"Oncología"}, True),
        ("VAC-GRI", "Vacuna antigripal", 18_000, {"Clínica médica", "Pediatría"}, False)],
}
TIPOS = {  # tipo de solicitud: (peso, canales, tipo de prestador)
    "Validación de consulta": (44, {"Validador online en consultorio": 80, "App del socio": 20}, ["Consultorio", "Clínica", "Centro médico"]),
    "Consulta de guardia": (5, {"Validador online en guardia": 100}, ["Sanatorio", "Clínica"]),
    "Práctica ambulatoria": (22, {"Validador online en consultorio": 55, "Web de prestadores": 45},
                             ["Centro de diagnóstico", "Laboratorio", "Centro de kinesiología", "Centro médico"]),
    "Estudio de alta complejidad": (6, {"Web de prestadores": 70, "Mesa de autorizaciones": 30}, ["Centro de diagnóstico", "Sanatorio"]),
    "Internación programada": (1.5, {"Web de prestadores": 60, "Mesa de autorizaciones": 40}, ["Sanatorio", "Clínica"]),
    "Internación de urgencia": (1.5, {"Validador online en guardia": 100}, ["Sanatorio", "Clínica"]),
    "Medicamento": (20, {"Farmacia": 100}, ["Farmacia"]),
}
RECHAZOS = {  # motivo: peso
    "Afiliado en mora": 22, "Práctica no cubierta por el plan": 20, "Falta orden médica": 18, "Prestador fuera de cartilla": 12,
    "Carencia vigente": 10, "Supera el tope anual": 8, "Credencial inválida": 7, "Transacción duplicada": 3,
}
# Lo que escribe el auditor médico. Pocas, con variantes.
AUDITOR = {
    "Aprobada tras auditoría": ["Se autoriza con la documentación presentada", "Autorizado por única vez", "Se aprueba según historia clínica",
                                "Autorizado con cobertura parcial, el resto a cargo del socio", "Se autoriza, presentar informe al finalizar"],
    "Rechazada por auditoría": ["No se justifica con los estudios presentados", "Falta historia clínica y estudios previos",
                                "Se sugiere tratamiento conservador antes del estudio", "Indicación fuera de protocolo",
                                "Medicamento no incluido en el vademécum del plan"],
    "Sobreprestación": ["Volumen de sesiones atípico, se solicita auditoría en terreno",
                        "El mismo socio con sesiones diarias, se pide justificación al prestador",
                        "Prestador con facturación fuera de lo habitual, se suspende hasta auditar"],
}

# ── Lo que se nota (anomalías) ──────────────────────────────────────────────
CAIDA_DEL_VALIDADOR = (datetime(2026, 6, 17, 10, 0), datetime(2026, 6, 17, 12, 30))
BRONQUIOLITIS = (date(2026, 6, 22), date(2026, 7, 5))
MORA = (date(2026, 5, 4), date(2026, 5, 22))       # después del aumento de cuota
SOBREPRESTACION = (date(2026, 8, 3), date(2026, 8, 28))
POR_HORA = [0.04, 0.03, 0.02, 0.02, 0.02, 0.04, 0.15, 0.55, 1.2, 1.5, 1.6, 1.5, 1.2, 1.0, 1.15, 1.35, 1.45, 1.4, 1.25, 1.0,
            0.7, 0.4, 0.2, 0.08]


def _en(d: date, rango: tuple) -> bool:
    return rango[0] <= d <= rango[1]


def _ar(numero: float) -> str:
    """Coma decimal, como sale de un sistema en castellano."""
    return f"{numero:.2f}".replace(".", ",")


def _coord(x: float) -> str:
    return f"{x:.5f}".replace(".", ",")


def _afiliados(r: random.Random, n: int = 30_000) -> list[dict]:
    pesos = [l[4] for l in LOCALIDADES]
    padron = []
    for i in range(n):
        plan = r.choices(list(PLANES), [p[0] for p in PLANES.values()])[0]
        segmento = r.choices(["Individual", "Corporativo", "Derivación de aportes"], [38, 37, 25])[0]
        edad = int(min(92, max(0, r.gauss(41, 21))))
        padron.append({"id": f"{60_000_000 + i * 31 + r.randint(0, 30):08d}-{r.randint(0, 3):02d}", "plan": plan, "segmento": segmento,
                       "edad": edad, "sexo": r.choices(["F", "M"], [53, 47])[0], "dni": str(r.randint(8_000_000, 58_999_999)),
                       "loc": r.choices(LOCALIDADES, pesos)[0],
                       "peso": (1 / (1 + i % 700)) ** 0.5 * (2.5 if edad >= 65 else 1)})   # los crónicos usan más
    return padron


def _prestadores(r: random.Random) -> dict[tuple[str, str], list[dict]]:
    """Por (localidad, tipo): los prestadores de cartilla."""
    out: dict[tuple[str, str], list[dict]] = {}
    n = 0
    for loc, prov, lat, lon, peso in LOCALIDADES:
        for tipo in ["Consultorio", "Clínica", "Centro médico", "Sanatorio", "Centro de diagnóstico", "Laboratorio",
                     "Centro de kinesiología", "Farmacia"]:
            for k in range(1, 2 + int(peso // 4) + (2 if tipo in ("Consultorio", "Farmacia") else 0)):
                n += 1
                out.setdefault((loc, tipo), []).append({
                    "id": f"PR-{n:04d}", "nombre": f"{tipo} {loc} {k}", "tipo": tipo, "loc": loc, "prov": prov,
                    "lat": lat + r.uniform(-0.01, 0.01), "lon": lon + r.uniform(-0.01, 0.01)})
    return out


def _peso_diagnostico(cie: str, d: date) -> float:
    base = {"J11": 6, "J21": 2, "A90": 0, "Z00": 9, "M54.5": 8, "I10": 8}.get(cie, 4)
    if cie in ("J11", "H66", "J21") and d.month in (6, 7, 8):
        base *= 2.5
    if cie == "J21" and _en(d, BRONQUIOLITIS):
        base *= 4
    if cie == "A90" and d < date(2026, 5, 20):
        base = 9 * max(0.0, (date(2026, 5, 20) - d).days / 49)   # el dengue del otoño se apaga
    return base


def _posibles(tipo: str, a: dict, d: date) -> list[str]:
    """Los diagnósticos que pueden pedir ese tipo de solicitud para ese socio (por edad y sexo), con peso ese día."""
    posibles = [c for c in DIAGNOSTICOS if any(p[3] == "*" or DIAGNOSTICOS[c][0] in p[3] for p in PRACTICAS[tipo])]
    if a["edad"] >= 14:
        posibles = [c for c in posibles if DIAGNOSTICOS[c][0] != "Pediatría"]
    else:
        posibles = [c for c in posibles if DIAGNOSTICOS[c][0] in ("Pediatría", "Clínica médica", "Oftalmología", "Odontología")]
    if a["sexo"] == "M" or not 18 <= a["edad"] <= 45:
        posibles = [c for c in posibles if DIAGNOSTICOS[c][0] != "Obstetricia"]
    return [c for c in posibles if _peso_diagnostico(c, d) > 0]


def generar(desde: date, hasta: date, por_dia: int, semilla: int = 20261016) -> tuple[list[list], Counter]:
    r = random.Random(semilla)
    padron = _afiliados(r)
    pesos_padron = [a["peso"] for a in padron]
    prestadores = _prestadores(r)
    abusador = prestadores[("Morón", "Centro de kinesiología")][0]
    socios_del_abuso = r.sample([a for a in padron if a["loc"][0] in ("Morón", "Lomas de Zamora", "Caballito")], 25)
    filas: list[list] = []
    d = desde
    while d <= hasta:
        v = por_dia * {5: 0.45, 6: 0.3}.get(d.weekday(), 1.0) * (1.15 if d.month in (6, 7, 8) else 1.0)
        v *= 1.25 if _en(d, BRONQUIOLITIS) else 1.0
        for _ in range(max(0, int(r.gauss(v, v * 0.06)))):
            filas.append(_transaccion(r, d, padron, pesos_padron, prestadores))
        if _en(d, SOBREPRESTACION) and d.weekday() < 6:      # el centro que factura de más
            for _ in range(r.randint(35, 50)):
                filas.append(_transaccion(r, d, padron, pesos_padron, prestadores, abuso=(abusador, r.choice(socios_del_abuso))))
        d += timedelta(days=1)
    filas.sort(key=lambda f: f[0])
    cuenta = Counter(f[22] for f in filas)
    for i, f in enumerate(filas):
        f[1] = f"AUT-{f[0]:%y%m}-{i + 1:07d}"
        f[0] = f[0].strftime("%d/%m/%Y %H:%M:%S")
    return filas, cuenta


def _transaccion(r: random.Random, d: date, padron, pesos_padron, prestadores, abuso=None) -> list:
    if abuso:
        prest, a = abuso
        tipo = "Práctica ambulatoria"
        cie = "M54.5"
        especialidad = "Traumatología"
        practica = PRACTICAS[tipo][0]                       # kinesiología
        canal = "Web de prestadores"
    else:
        a = r.choices(padron, pesos_padron)[0]
        tipo = r.choices(list(TIPOS), [t[0] for t in TIPOS.values()])[0]
        if tipo == "Medicamento" and d.month in (4, 5) and r.random() < 0.3:
            cie, especialidad = "Z00", "Clínica médica"
            practica = next(p for p in PRACTICAS["Medicamento"] if p[0] == "VAC-GRI")
        else:
            posibles = _posibles(tipo, a, d)
            if not posibles:                                # nada de ese tipo le corresponde: es una consulta
                tipo = "Validación de consulta"
                posibles = _posibles(tipo, a, d)
            cie = r.choices(posibles, [_peso_diagnostico(c, d) for c in posibles])[0]
            especialidad = DIAGNOSTICOS[cie][0]
            practica = r.choice([p for p in PRACTICAS[tipo] if p[3] == "*" or especialidad in p[3]])
        canales = TIPOS[tipo][1]
        canal = r.choices(list(canales), list(canales.values()))[0]
        tipo_prest = r.choice(TIPOS[tipo][2])
        if practica[0] == "250101":
            tipo_prest = "Centro de kinesiología"
        loc = a["loc"] if r.random() < 0.85 else r.choice(LOCALIDADES)
        prest = r.choice(prestadores.get((loc[0], tipo_prest)) or prestadores[(loc[0], "Clínica")])
    # Cuándo: guardia y farmacia todo el día; lo demás, en horario de consultorio.
    if canal in ("Validador online en guardia", "Farmacia"):
        hora = r.choices(range(24), [max(0.25, h) for h in POR_HORA])[0]
    else:
        hora = r.choices(range(24), POR_HORA)[0]
        hora = min(20, max(7, hora))
    ts = datetime(d.year, d.month, d.day, hora, r.randint(0, 59), r.randint(0, 59))

    codigo, nombre, importe, _, auditable = practica
    importe = round(importe * r.uniform(0.9, 1.15), 2)
    plan = a["plan"]
    diagnostico = r.choice(DIAGNOSTICOS[cie][1]) + (r.choice(AGREGADOS) if r.random() < 0.25 else "")
    respuesta = int(r.lognormvariate(5.8, 0.35))                      # ~330 ms
    resultado, motivo, obs, horas_aud = "Aprobada", "", "", ""

    # ¿Se rechaza en línea?
    p_rechazo = 0.06 + (0.10 if a["segmento"] == "Individual" and _en(d, MORA) else 0)
    if practica[0] == "MED-OBE":
        p_rechazo = 0.55                                               # casi ningún plan lo cubre
    if r.random() < p_rechazo:
        resultado = "Rechazada"
        if a["segmento"] == "Individual" and _en(d, MORA) and r.random() < 0.7:
            motivo = "Afiliado en mora"
        elif practica[0] == "MED-OBE":
            motivo = "Práctica no cubierta por el plan"
        else:
            motivo = r.choices(list(RECHAZOS), list(RECHAZOS.values()))[0]
    elif auditable:
        horas_aud = round(r.lognormvariate(3.0, 0.6), 1)               # ~20 h
        if r.random() < 0.82:
            resultado = "Aprobada tras auditoría"
        else:
            resultado, motivo = "Rechazada por auditoría", "Rechazo del auditor médico"
        obs = r.choice(AUDITOR[resultado]) if r.random() < 0.85 else ""
    if abuso and d >= SOBREPRESTACION[0] + timedelta(days=12) and r.random() < 0.35:
        resultado, motivo = "Pendiente de auditoría", ""
        obs = r.choice(AUDITOR["Sobreprestación"])
    if CAIDA_DEL_VALIDADOR[0] <= ts <= CAIDA_DEL_VALIDADOR[1] and canal.startswith("Validador"):
        respuesta = int(r.uniform(8_000, 30_000))
        if r.random() < 0.6:
            resultado, motivo, obs, horas_aud = "Error de comunicación", "Tiempo de espera agotado", "", ""

    # Cuánto paga el socio.
    if tipo == "Medicamento":
        cobertura = 100 if codigo in ("MED-ONC", "VAC-GRI") else PLANES[plan][2] if codigo != "MED-CRO" else 70
    elif tipo in ("Validación de consulta", "Consulta de guardia", "Práctica ambulatoria"):
        cobertura = 100
    else:
        cobertura = 100 if plan in ("Superior", "Premium") else 90
    copago = importe * (100 - cobertura) / 100
    if tipo in ("Validación de consulta", "Consulta de guardia"):
        copago = PLANES[plan][1] * (1.5 if tipo == "Consulta de guardia" else 1)
    if resultado.startswith("Rechazada") or resultado in ("Error de comunicación", "Pendiente de auditoría"):
        copago = 0.0
    return [
        ts, "", canal, tipo, a["segmento"], plan, a["id"], a["dni"], a["edad"], a["sexo"], prest["id"], prest["nombre"], prest["tipo"],
        prest["loc"], prest["prov"], _coord(prest["lat"]), _coord(prest["lon"]), especialidad, codigo, nombre, cie, diagnostico,
        resultado, motivo, obs, "" if horas_aud == "" else _ar(horas_aud).rstrip("0").rstrip(","), respuesta, _ar(importe), cobertura,
        _ar(copago),
    ]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--desde", default="2026-04-01")
    p.add_argument("--hasta", default="2026-10-12")
    p.add_argument("--por-dia", type=int, default=560)
    a = p.parse_args()
    filas, cuenta = generar(date.fromisoformat(a.desde), date.fromisoformat(a.hasta), a.por_dia)
    SALIDA.parent.mkdir(exist_ok=True)
    with SALIDA.open("w", encoding="utf-8", newline="") as f:
        f.write("|".join(COLUMNAS) + "\n")
        f.writelines("|".join(str(x) for x in fila) + "\n" for fila in filas)
    print(f"{len(filas):,} transacciones -> {SALIDA} ({SALIDA.stat().st_size / 1e6:.1f} MB)")
    print("  " + " · ".join(f"{k}: {v:,}" for k, v in cuenta.most_common()))


if __name__ == "__main__":
    main()
