"""
build_experiencias_dataset.py
=============================

Dataset sintético de una empresa de regalos de experiencias en Argentina: vende
boxes (gastronomía, bienestar, aventura, estadías, vinos, talleres) online, en
locales y a empresas; quien la recibe activa el código, reserva con uno de los
prestadores adheridos (restaurantes, spas, hoteles, bodegas, turismo aventura,
talleres) y la canjea. Cada fila es un evento de la plataforma de vouchers y
reservas, en castellano.

Todo es inventado (sin datos de nadie): clientes, beneficiarios, empresas,
prestadores, códigos, mails (dominios reservados example.*) y teléfonos salen de
un generador con semilla fija. Lo que trae, para que la demo cuente algo:

- reclamos de atención al cliente y reseñas en texto libre, con muchas formas de
  decir lo mismo → búsqueda híbrida ("no me llegó el mail" / "el regalo nunca
  apareció", "no hay fechas" / "siempre está todo ocupado");
- la estacionalidad de los regalos: Navidad y regalos empresariales, San
  Valentín, Hot Sale, Día del Padre, Día del Amigo, Día de la Madre y
  CyberMonday; los canjes, en fines de semana y en temporada (costa y Patagonia
  en verano, nieve en julio, vendimia en marzo) → pronósticos;
- eventos que se notan → detección de anomalías: una caída del procesador de
  pagos cuando arranca el Hot Sale, a la medianoche, una falla en el envío de los regalos digitales antes del Día del Padre,
  un ataque de fuerza bruta sobre los códigos, un spa que cierra y cancela todas
  sus reservas, y un prestador con mala atención que junta reclamos;
- coordenadas del prestador → mapa; mail, teléfono y código del voucher →
  enmascarado; prestador y beneficiario → perfil por entidad.

Formato: JSON por línea, un evento por línea, con el voucher, la box, el
prestador y la reserva anidados, como sale de una cola de eventos de la
plataforma.

Uso:   py build_experiencias_dataset.py [--desde 2025-11-24] [--hasta 2026-11-23] [--escala 0.65]
Sale:  datasets/regalos-de-experiencias.ndjson
"""
from __future__ import annotations

import argparse
import json
import math
import random
import string
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ART = timezone(timedelta(hours=-3))
SALIDA = Path(__file__).parent / "datasets" / "regalos-de-experiencias.ndjson"
R = random.Random(2611)

# ── Catálogo ────────────────────────────────────────────────────────────────
# (sku, nombre, categoría, precio en ARS al inicio, personas)
BOXES = [
    ("BX-GAS-01", "Sabores para Dos", "Gastronomía", 39900, 2),
    ("BX-GAS-02", "Cena Gourmet", "Gastronomía", 74900, 2),
    ("BX-GAS-03", "Parrilla y Vinos", "Gastronomía", 54900, 2),
    ("BX-BIE-01", "Día de Spa", "Bienestar", 59900, 1),
    ("BX-BIE-02", "Masajes para Dos", "Bienestar", 69900, 2),
    ("BX-BIE-03", "Relax Total", "Bienestar", 89900, 2),
    ("BX-AVE-01", "Aventura en la Naturaleza", "Aventura", 49900, 1),
    ("BX-AVE-02", "Adrenalina", "Aventura", 64900, 1),
    ("BX-AVE-03", "Vuelo de Bautismo", "Aventura", 189900, 1),
    ("BX-EST-01", "Noche Romántica", "Estadías", 119900, 2),
    ("BX-EST-02", "Escapada de Fin de Semana", "Estadías", 149900, 2),
    ("BX-EST-03", "Estadía Premium", "Estadías", 299900, 2),
    ("BX-VIN-01", "Cata para Dos", "Vinos", 44900, 2),
    ("BX-VIN-02", "Ruta del Vino", "Vinos", 84900, 2),
    ("BX-TAL-01", "Talleres Creativos", "Talleres", 34900, 1),
    ("BX-TAL-02", "Clases para Dos", "Talleres", 49900, 2),
    ("BX-MUL-01", "Multiexperiencia", "Multiexperiencia", 99900, 2),
    ("BX-MUL-02", "Experiencia Única", "Multiexperiencia", 199900, 2),
]
BOX = {b[0]: b for b in BOXES}
POR_CATEGORIA = defaultdict(list)
for _b in BOXES:
    POR_CATEGORIA[_b[2]].append(_b)

RUBROS_DE = {
    "Gastronomía": ["Restaurante", "Parrilla", "Cervecería"],
    "Bienestar": ["Spa", "Centro de masajes"],
    "Aventura": ["Turismo aventura"],
    "Estadías": ["Hotel", "Cabañas", "Hostería"],
    "Vinos": ["Bodega"],
    "Talleres": ["Taller"],
}
RUBROS_DE["Multiexperiencia"] = [r for c in ("Gastronomía", "Bienestar", "Aventura", "Vinos", "Talleres") for r in RUBROS_DE[c]]
CATEGORIA_DE_RUBRO = {r: c for c, rs in RUBROS_DE.items() if c != "Multiexperiencia" for r in rs}

# ── Dónde están los prestadores ─────────────────────────────────────────────
# (localidad, provincia, lat, lon, cantidad de prestadores, rubros típicos, destino turístico)
LUGARES = [
    ("Palermo", "CABA", -34.5889, -58.4306, 28, "Restaurante Parrilla Cervecería Spa Centro de masajes Taller", False),
    ("Recoleta", "CABA", -34.5875, -58.3974, 14, "Restaurante Spa Hotel Taller", False),
    ("San Telmo", "CABA", -34.6211, -58.3731, 10, "Restaurante Parrilla Cervecería Taller", False),
    ("Belgrano", "CABA", -34.5627, -58.4583, 10, "Restaurante Spa Centro de masajes Taller", False),
    ("Puerto Madero", "CABA", -34.6118, -58.3627, 8, "Restaurante Parrilla Hotel Spa", False),
    ("Villa Crespo", "CABA", -34.5990, -58.4380, 8, "Restaurante Cervecería Taller", False),
    ("Caballito", "CABA", -34.6195, -58.4400, 6, "Restaurante Parrilla Centro de masajes", False),
    ("Tigre", "Buenos Aires", -34.4264, -58.5797, 12, "Turismo aventura Restaurante Hotel Spa Cabañas", True),
    ("San Isidro", "Buenos Aires", -34.4713, -58.5286, 8, "Restaurante Spa Taller", False),
    ("Pilar", "Buenos Aires", -34.4587, -58.9142, 8, "Spa Hotel Restaurante Turismo aventura", False),
    ("Cañuelas", "Buenos Aires", -35.0517, -58.7603, 5, "Hotel Cabañas Turismo aventura Parrilla", True),
    ("Luján", "Buenos Aires", -34.5703, -59.1050, 4, "Turismo aventura Hostería Parrilla", True),
    ("Mar del Plata", "Buenos Aires", -38.0055, -57.5426, 14, "Restaurante Parrilla Hotel Spa Turismo aventura Cervecería", True),
    ("Pinamar", "Buenos Aires", -37.1079, -56.8613, 7, "Hotel Cabañas Restaurante Turismo aventura", True),
    ("Tandil", "Buenos Aires", -37.3217, -59.1332, 8, "Cabañas Hostería Turismo aventura Parrilla Cervecería", True),
    ("Sierra de la Ventana", "Buenos Aires", -38.1366, -61.7929, 4, "Cabañas Turismo aventura Hostería", True),
    ("La Plata", "Buenos Aires", -34.9214, -57.9545, 7, "Restaurante Cervecería Spa Taller", False),
    ("Córdoba", "Córdoba", -31.4201, -64.1888, 12, "Restaurante Parrilla Spa Taller Cervecería", False),
    ("Villa Carlos Paz", "Córdoba", -31.4241, -64.4978, 7, "Hotel Turismo aventura Restaurante Cabañas", True),
    ("Villa General Belgrano", "Córdoba", -31.9781, -64.5594, 6, "Cervecería Cabañas Hostería Restaurante", True),
    ("La Cumbrecita", "Córdoba", -31.8977, -64.7731, 3, "Hostería Turismo aventura", True),
    ("Rosario", "Santa Fe", -32.9442, -60.6505, 10, "Restaurante Parrilla Spa Taller Cervecería", False),
    ("Mendoza", "Mendoza", -32.8895, -68.8458, 9, "Restaurante Spa Hotel Taller", False),
    ("Luján de Cuyo", "Mendoza", -33.0368, -68.8798, 9, "Bodega Restaurante Hotel", True),
    ("Maipú", "Mendoza", -32.9846, -68.7811, 6, "Bodega Restaurante", True),
    ("Valle de Uco", "Mendoza", -33.5935, -69.2051, 7, "Bodega Hotel Restaurante", True),
    ("Potrerillos", "Mendoza", -32.9580, -69.1955, 4, "Turismo aventura Cabañas", True),
    ("Salta", "Salta", -24.7821, -65.4232, 7, "Restaurante Hotel Turismo aventura", True),
    ("Cafayate", "Salta", -26.0730, -65.9761, 5, "Bodega Hostería Turismo aventura", True),
    ("Purmamarca", "Jujuy", -23.7437, -65.4990, 3, "Hostería Turismo aventura", True),
    ("San Carlos de Bariloche", "Río Negro", -41.1335, -71.3103, 14, "Hotel Cabañas Turismo aventura Restaurante Cervecería Spa", True),
    ("Villa La Angostura", "Neuquén", -40.7616, -71.6464, 6, "Cabañas Hostería Spa Restaurante", True),
    ("San Martín de los Andes", "Neuquén", -40.1572, -71.3534, 7, "Cabañas Turismo aventura Hostería Restaurante", True),
    ("El Calafate", "Santa Cruz", -50.3379, -72.2648, 5, "Hotel Turismo aventura Restaurante", True),
    ("Ushuaia", "Tierra del Fuego", -54.8019, -68.3030, 6, "Hotel Turismo aventura Restaurante", True),
    ("Puerto Madryn", "Chubut", -42.7692, -65.0385, 5, "Turismo aventura Hotel Restaurante", True),
    ("Puerto Iguazú", "Misiones", -25.5972, -54.5786, 6, "Hotel Turismo aventura Restaurante", True),
    ("Merlo", "San Luis", -32.3436, -65.0123, 4, "Cabañas Spa Turismo aventura", True),
    ("San Miguel de Tucumán", "Tucumán", -26.8083, -65.2176, 4, "Restaurante Parrilla Taller", False),
]

# Nombres inventados por rubro: prefijo + nombre.
PREFIJOS = {
    "Restaurante": ["Bistró", "Casa", "Cocina", "Fonda", "Restó"], "Parrilla": ["Parrilla", "Asador", "Fogón"],
    "Cervecería": ["Cervecería", "Fábrica de Cerveza", "Bar"], "Spa": ["Spa", "Termas Urbanas", "Spa & Relax"],
    "Centro de masajes": ["Masajes", "Centro de Bienestar", "Estudio"], "Turismo aventura": ["Expediciones", "Aventura", "Excursiones"],
    "Hotel": ["Hotel", "Posada", "Lodge"], "Cabañas": ["Cabañas", "Complejo", "Refugio"], "Hostería": ["Hostería", "Posada", "Casa de Campo"],
    "Bodega": ["Bodega", "Finca", "Viñedos"], "Taller": ["Taller", "Estudio", "Escuela"],
}
NOMBRES = [
    "La Higuera", "Los Aromos", "El Remanso", "Siete Fuegos", "Lo de Tomás", "Doña Elvira", "El Mirador", "Las Lajas",
    "Cielo Abierto", "Los Cardones", "La Encina", "Tres Lunas", "El Ombú", "Piedra Blanca", "Monte Azul", "La Glorieta",
    "Agua Clara", "Los Teros", "El Alambique", "Raíces", "Bruma", "Arrayanes", "La Nonna", "El Faro", "Laurel", "Vertiente",
    "Las Moras", "Cumbre Alta", "El Quincho", "Nativa", "La Huella", "Paso Ancho", "Malvón", "Brasas del Sur",
    "La Calandria", "Ruta Escondida", "Mil Colores", "El Talar", "Terra Viva", "Nogales", "La Pausa", "Viento Norte",
    "Los Álamos", "Cerro Bayo", "Las Acacias", "El Molle", "Ceibo", "La Posta", "Puerto Viejo", "Altamira",
]


def _prestadores():
    lista, usados = [], set()
    n = 1000
    for loc, prov, lat, lon, cant, rubros, destino in LUGARES:
        rubros = rubros.split(" ")
        # "Centro de masajes" y "Turismo aventura" vienen partidos por el split.
        rubros = " ".join(rubros).replace("Centro de masajes", "Centro_de_masajes").replace("Turismo aventura", "Turismo_aventura").split()
        rubros = [r.replace("_", " ") for r in rubros]
        for i in range(cant):
            n += R.randint(1, 4)
            rubro = rubros[i % len(rubros)] if i < len(rubros) else R.choice(rubros)
            for _ in range(40):
                nombre = f"{R.choice(PREFIJOS[rubro])} {R.choice(NOMBRES)}"
                if nombre not in usados:
                    break
            else:
                nombre = f"{nombre} {loc}"
            usados.add(nombre)
            lista.append({
                "id": f"PRE-{n}", "nombre": nombre, "rubro": rubro, "localidad": loc, "provincia": prov,
                "lat": round(lat + R.uniform(-0.03, 0.03), 5), "lon": round(lon + R.uniform(-0.03, 0.03), 5),
                "destino": destino, "calidad": R.uniform(0.62, 0.98), "sin_cupo": R.uniform(0.03, 0.16),
                "popularidad": R.lognormvariate(0, 0.6),
            })
    return lista


PRESTADORES = _prestadores()
# Planteados: un spa grande de Palermo que cierra en julio, y una parrilla de Mar del Plata con mala atención.
SPA_QUE_CIERRA = next(p for p in PRESTADORES if p["rubro"] == "Spa" and p["localidad"] == "Palermo")
SPA_QUE_CIERRA["popularidad"] = 7.0
PARRILLA_MALA = next(p for p in PRESTADORES if p["rubro"] == "Parrilla" and p["localidad"] == "Mar del Plata")
PARRILLA_MALA.update(calidad=0.28, popularidad=3.5)
CIERRE_SPA = (date(2026, 7, 3), date(2026, 7, 20))
CAIDA_PAGOS = (datetime(2026, 5, 11, 0, 5, tzinfo=ART), datetime(2026, 5, 11, 2, 20, tzinfo=ART))
# Los eventos de venta online arrancan a la medianoche con una avalancha.
LANZAMIENTOS = {date(2025, 5, 12), date(2025, 11, 3), date(2026, 5, 11), date(2026, 11, 2)}
POR_RUBRO = defaultdict(list)
for _p in PRESTADORES:
    POR_RUBRO[_p["rubro"]].append(_p)

# ── Quién compra ────────────────────────────────────────────────────────────
PROVINCIAS = [("CABA", 23), ("Buenos Aires", 37), ("Córdoba", 9), ("Santa Fe", 8), ("Mendoza", 5), ("Tucumán", 2),
              ("Salta", 2), ("Neuquén", 2), ("Río Negro", 2), ("Entre Ríos", 2), ("Chubut", 1.5), ("Misiones", 1.5),
              ("San Luis", 1), ("Corrientes", 1), ("Jujuy", 1), ("Santa Cruz", 0.5), ("Tierra del Fuego", 0.5)]
CARACTERISTICA = {"CABA": "11", "Buenos Aires": "11", "Córdoba": "351", "Santa Fe": "341", "Mendoza": "261", "Tucumán": "381",
                  "Salta": "387", "Neuquén": "299", "Río Negro": "294", "Entre Ríos": "343", "Chubut": "280", "Misiones": "376",
                  "San Luis": "266", "Corrientes": "379", "Jujuy": "388", "Santa Cruz": "2966", "Tierra del Fuego": "2901"}
NOMBRES_PERSONA = ["sofia", "martina", "lucia", "valentina", "camila", "julieta", "agustina", "florencia", "mariana", "paula",
                   "carolina", "natalia", "gabriela", "romina", "laura", "juan", "mateo", "santiago", "tomas", "nicolas",
                   "martin", "facundo", "lucas", "diego", "pablo", "federico", "joaquin", "ignacio", "gonzalo", "andres"]
APELLIDOS = ["gonzalez", "rodriguez", "fernandez", "lopez", "martinez", "garcia", "perez", "romero", "sosa", "alvarez",
             "torres", "ruiz", "ramirez", "flores", "benitez", "acosta", "medina", "herrera", "suarez", "aguirre",
             "gimenez", "molina", "silva", "castro", "rojas", "ortiz", "luna", "juarez", "cabrera", "rios"]
DOMINIOS = ["example.com", "example.net", "example.org"]
RUBROS_EMPRESA = ["Banca y seguros", "Tecnología", "Consumo masivo", "Laboratorio", "Energía", "Retail", "Telecomunicaciones",
                  "Agroindustria", "Consultoría", "Automotriz"]
EMPRESAS = [{"id": f"EMP-{100 + i}", "rubro": R.choice(RUBROS_EMPRESA)} for i in range(140)]

_cli = _ben = 0


def persona(prefijo: str, provincia: str | None = None) -> dict:
    global _cli, _ben
    if prefijo == "CLI":
        _cli += 1
        n = _cli
    else:
        _ben += 1
        n = _ben
    prov = provincia or R.choices([p for p, _ in PROVINCIAS], [w for _, w in PROVINCIAS])[0]
    usuario = f"{R.choice(NOMBRES_PERSONA)}.{R.choice(APELLIDOS)}{R.choice(['', str(R.randint(1, 99))])}"
    return {"id": f"{prefijo}-{200000 + n * 7 + R.randint(0, 6)}", "email": f"{usuario}@{R.choice(DOMINIOS)}",
            "telefono": f"+54 9 {CARACTERISTICA[prov]} {R.randint(4000, 6999)}-{R.randint(1000, 9999)}", "provincia": prov}


# ── Sesiones web y app ──────────────────────────────────────────────────────
UA = [
    ("Android", "Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36"),
    ("iPhone", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1"),
    ("Windows", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
    ("Mac", "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15"),
]
UA_APP = [("Android", "AppExperiencias/4.12.0 (Android 14)"), ("iPhone", "AppExperiencias/4.12.0 (iOS 17.6)")]
REDES = ["181.4", "181.46", "181.165", "186.12", "186.130", "190.18", "190.55", "190.191", "200.45", "201.212", "152.168", "170.51"]


def sesion(canal: str) -> dict:
    disp, ua = R.choice(UA_APP if canal == "App" else UA)
    return {"ip": f"{R.choice(REDES)}.{R.randint(0, 255)}.{R.randint(1, 254)}", "dispositivo": disp, "user_agent": ua}


# ── Calendario de regalos ───────────────────────────────────────────────────
def tercer_domingo(anio: int, mes: int) -> date:
    d = date(anio, mes, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(days=14)


def ocasiones(anio: int) -> list[tuple[str, date, int, float, str]]:
    """(ocasión, fecha, días de rampa antes, pico, categorías que suben)."""
    return [
        ("Navidad", date(anio, 12, 24), 20, 4.2, "Gastronomía Estadías Multiexperiencia Bienestar"),
        ("Reyes", date(anio, 1, 6), 4, 1.3, "Talleres Aventura"),
        ("San Valentín", date(anio, 2, 14), 8, 2.6, "Estadías Gastronomía Bienestar"),
        ("Día de la Mujer", date(anio, 3, 8), 4, 1.5, "Bienestar"),
        ("Día del Padre", tercer_domingo(anio, 6), 9, 2.8, "Gastronomía Vinos Aventura"),
        ("Día del Amigo", date(anio, 7, 20), 4, 1.6, "Gastronomía Aventura Talleres"),
        ("Día del Niño", tercer_domingo(anio, 8), 6, 1.3, "Talleres Aventura"),
        ("Día de la Madre", tercer_domingo(anio, 10), 11, 3.6, "Bienestar Gastronomía Estadías"),
    ]


EVENTOS_ONLINE = {  # ventas online: (desde, hasta, multiplicador)
    "Hot Sale": [(date(2025, 5, 12), date(2025, 5, 14), 3.2), (date(2026, 5, 11), date(2026, 5, 13), 3.2)],
    "CyberMonday": [(date(2024, 11, 4), date(2024, 11, 6), 3.0), (date(2025, 11, 3), date(2025, 11, 5), 3.0),
                    (date(2026, 11, 2), date(2026, 11, 4), 3.0)],
    "Black Friday": [(date(2024, 11, 29), date(2024, 11, 29), 2.0), (date(2025, 11, 28), date(2025, 11, 28), 2.0)],
}
MES = {1: 0.75, 2: 0.85, 3: 0.8, 4: 0.85, 5: 0.95, 6: 1.0, 7: 0.95, 8: 0.9, 9: 0.95, 10: 1.1, 11: 1.0, 12: 1.3}
DIA_SEMANA = [0.95, 0.95, 1.0, 1.05, 1.15, 1.05, 0.85]


def factor_dia(d: date, inicio: date) -> tuple[float, str | None, str, float]:
    """Multiplicador de ventas del día, la ocasión que lo empuja, las categorías que suben y el extra online."""
    f = MES[d.month] * DIA_SEMANA[d.weekday()] * (1 + 0.15 * (d - inicio).days / 365)
    ocasion, cats, mejor = None, "", 0.0
    for anio in (d.year, d.year + 1):
        for nombre, fecha, rampa, pico, categorias in ocasiones(anio):
            dias = (fecha - d).days
            if 0 <= dias <= rampa:
                extra = (pico - 1) * (1 - dias / (rampa + 1)) ** 1.5
                if extra > mejor:
                    ocasion, cats, mejor = nombre, categorias, extra
    online = 1.0
    for nombre, tramos in EVENTOS_ONLINE.items():
        for a, b, m in tramos:
            if a <= d <= b:
                online, ocasion, cats = m, ocasion or nombre, cats or "Gastronomía Bienestar Estadías Aventura"
    return f * (1 + mejor), ocasion, cats, online


# ── Textos libres ───────────────────────────────────────────────────────────
RECLAMOS = {
    "No llegó el regalo": [
        "Compré una box digital y a la persona nunca le llegó el mail", "No me llegó el correo con el código del regalo",
        "El regalo no aparece en la casilla de mi mamá, revisamos spam y nada", "Pagué hace dos días y todavía no recibieron el voucher",
        "Me dicen que el mail rebotó, necesito que lo reenvíen", "La persona no recibió nada por WhatsApp ni por mail",
        "Necesito el regalo para el domingo y no llegó el código",
    ],
    "Envío demorado": [
        "La box física no llegó a tiempo para Navidad", "El envío a domicilio está demorado hace una semana",
        "Compré la caja para regalar y el correo no la entregó", "Me prometieron entrega en 48 horas y no llegó",
        "El paquete figura en camino desde hace días",
    ],
    "Sin disponibilidad": [
        "El prestador no tiene fechas disponibles hasta dentro de tres meses", "Siempre está todo ocupado los fines de semana",
        "Llamé varias veces al lugar y no me dan turno", "No hay cupo en ninguno de los spas de mi zona",
        "Quiero reservar y me rechazan todas las fechas", "Las opciones de mi provincia no tienen lugar",
    ],
    "Código no funciona": [
        "El código figura como usado y yo nunca lo usé", "Al activar el código me dice que ya fue activado por otra cuenta",
        "No puedo activar el voucher, dice código inválido", "Alguien usó mi regalo, yo no hice ninguna reserva",
        "El código de mi box aparece canjeado y no fui a ningún lado",
    ],
    "Reserva cancelada": [
        "Me cancelaron la reserva el día antes sin explicación", "El spa me canceló el turno y no me ofrecen otra fecha",
        "Tenía la reserva confirmada y el lugar cerró", "Me avisaron que el prestador cerró por refacciones, quiero otra opción",
        "Cancelaron mi experiencia y no sé cómo reprogramar",
    ],
    "Mala experiencia": [
        "Nos atendieron muy mal y nos hicieron esperar más de una hora", "La comida no era la del menú de la box",
        "Nos quisieron cobrar un adicional que no figuraba", "El lugar estaba cerrado cuando llegamos",
        "La experiencia no era como se describía en la página", "El masaje duró la mitad de lo prometido",
        "Llegamos con la reserva confirmada y no nos tenían anotados",
    ],
    "Cobro y facturación": [
        "Me cobraron dos veces la misma compra", "Necesito la factura A para la empresa", "El pago figura rechazado pero me lo debitaron",
        "Quiero saber por qué me cobraron las cuotas con interés", "No me llegó la factura de la compra",
    ],
    "Vencimiento": [
        "Se me venció el voucher y quiero extenderlo", "No sabía que tenía vencimiento, ¿se puede recuperar?",
        "Quiero pedir una prórroga porque estuve de viaje", "El regalo venció sin que lo pudiera usar por falta de fechas",
    ],
    "Cambio de box": [
        "Quiero cambiar la box por otra de mayor valor", "La experiencia que me regalaron no me sirve, ¿la puedo cambiar?",
        "Me gustaría cambiar la box de aventura por una de spa", "Quiero cambiar el regalo porque no hay prestadores cerca",
    ],
}
MEZCLA_RECLAMOS = {"No llegó el regalo": 12, "Envío demorado": 4, "Sin disponibilidad": 24, "Código no funciona": 6,
                   "Reserva cancelada": 10, "Mala experiencia": 14, "Cobro y facturación": 12, "Vencimiento": 10, "Cambio de box": 8}
CANALES_ATENCION = ["Chat", "WhatsApp", "Email", "Teléfono"]

RESENAS = {
    "Gastronomía": {
        "bien": ["La comida espectacular y la atención impecable", "Todo riquísimo, el vino que nos recomendaron excelente",
                 "Muy buena cena, el lugar hermoso y nos trataron de diez", "Las carnes en su punto, volveríamos sin dudas"],
        "mal": ["La comida llegó fría y tardaron muchísimo", "El menú de la box era muy limitado", "Nos hicieron esperar una hora para sentarnos",
                "Nos quisieron cobrar las bebidas aparte"],
    },
    "Bienestar": {
        "bien": ["Salimos renovadas, los masajes fueron lo mejor", "Un lugar muy tranquilo y las terapeutas excelentes",
                 "Impecable el circuito de aguas y la atención", "Me relajé como nunca, súper recomendable"],
        "mal": ["El masaje fue muy corto y el lugar estaba lleno", "El vestuario sucio y el agua de la pileta fría",
                "Nos atendieron apurados, no fue nada relajante", "Hacía frío en la sala y nadie se ocupó"],
    },
    "Aventura": {
        "bien": ["Una experiencia increíble, los guías muy profesionales", "Adrenalina pura, el paisaje es impresionante",
                 "Muy bien organizado y con todas las medidas de seguridad", "La pasamos bárbaro, ideal para hacer con amigos"],
        "mal": ["Suspendieron la actividad por el clima y no nos avisaron", "El equipo estaba en mal estado",
                "Esperamos dos horas a que saliera la excursión", "Fue mucho más corto de lo que decía la descripción"],
    },
    "Estadías": {
        "bien": ["La habitación hermosa y el desayuno buenísimo", "Una escapada soñada, la vista desde la cabaña increíble",
                 "Muy atentos en la recepción y todo muy limpio", "Ideal para desconectar, volveremos"],
        "mal": ["La habitación no era la de las fotos", "Había ruido toda la noche y el desayuno escaso",
                "Nos dieron una cabaña sin calefacción", "El check-in tardó una eternidad"],
    },
    "Vinos": {
        "bien": ["La degustación excelente y el sommelier un genio", "Hermosa bodega, aprendimos muchísimo de vinos",
                 "Los vinos y la picada muy buenos, la vista a la cordillera única", "Recorrido muy completo y bien explicado"],
        "mal": ["La visita fue muy apurada y con mucha gente", "Solo nos dieron dos copas, poca cosa",
                "El guía no sabía responder las preguntas", "Llegamos y la visita en castellano estaba completa"],
    },
    "Talleres": {
        "bien": ["El profe genial y nos llevamos todo lo que hicimos", "Muy divertido, aprendimos un montón",
                 "Grupo chico y muy buena onda", "Excelente taller, materiales de primera"],
        "mal": ["Éramos demasiados y casi no pudimos practicar", "Faltaban materiales y empezó tarde",
                "No era para principiantes como decía la descripción", "El lugar muy chico e incómodo"],
    },
}
NEUTRAS = ["Estuvo bien, nada del otro mundo", "Correcto, aunque esperábamos un poco más", "Lindo pero caro para lo que es",
           "Bien en general, la reserva costó bastante", "Aceptable, la atención podría mejorar"]

MOTIVOS_PAGO = [("Fondos insuficientes", 34), ("Tarjeta vencida o datos inválidos", 22), ("Rechazada por el banco emisor", 26),
                ("Sospecha de fraude", 10), ("Timeout del procesador de pagos", 2)]
MEDIOS_PAGO = [("Tarjeta de crédito", 58), ("Tarjeta de débito", 16), ("Billetera virtual", 18), ("Transferencia", 8)]
MOTIVOS_SIN_CUPO = ["Sin disponibilidad en la fecha", "Cupo completo para ese día", "Fecha bloqueada por el prestador",
                    "No trabaja ese día de la semana"]
MOTIVOS_CANCELA = ["Cambio de planes", "Problema de salud", "Mal clima", "Viaje", "Se superpuso con otro compromiso"]


# ── Simulación ──────────────────────────────────────────────────────────────
class Gen:
    def __init__(self, desde: date, hasta: date, escala: float):
        self.desde, self.hasta, self.escala = desde, hasta, escala
        self.t_desde = datetime.combine(desde, time(0), ART)
        self.t_hasta = datetime.combine(hasta + timedelta(days=1), time(0), ART)
        self.eventos: list[tuple[datetime, dict]] = []
        self.codigos: set[str] = set()
        self.seq = Counter()
        self.canjes_semana: dict[tuple[str, date], list[int]] = defaultdict(list)
        self.robables: list[dict] = []   # vouchers sin activar al momento del ataque

    # utilidades
    def ev(self, t: datetime, tipo: str, canal: str, **campos):
        if self.t_desde <= t < self.t_hasta:
            d = {"timestamp": t.isoformat(timespec="seconds"), "evento": {"tipo": tipo}, "canal": canal}
            d.update({k: v for k, v in campos.items() if v is not None})
            b = d.get("beneficiario")
            if b and tipo not in ("Activación", "Envío del regalo", "Reclamo", "Validación fallida"):
                d["beneficiario"] = {"id": b["id"], "provincia": b["provincia"]}
            self.eventos.append((t, d))

    def nro(self, pref: str, t: datetime) -> str:
        clave = f"{pref}{t:%y%m}"
        self.seq[clave] += 1
        return f"{pref}-{t:%y%m}-{self.seq[clave]:06d}"

    def codigo(self) -> str:
        while True:
            c = "XP-" + "".join(R.choices("ABCDEFGHJKMNPQRSTUVWXYZ23456789", k=4)) + "-" + "".join(R.choices("ABCDEFGHJKMNPQRSTUVWXYZ23456789", k=4))
            if c not in self.codigos:
                self.codigos.add(c)
                return c

    @staticmethod
    def precio(sku: str, t: datetime) -> int:
        meses = (t.year - 2025) * 12 + t.month - 11
        return int(round(BOX[sku][3] * (1.022 ** meses), -2))

    @staticmethod
    def box_dict(sku: str, precio: int) -> dict:
        _, nombre, cat, _, _ = BOX[sku]
        return {"sku": sku, "nombre": nombre, "categoria": cat, "precio_ars": precio}

    @staticmethod
    def prest_dict(p: dict) -> dict:
        return {"id": p["id"], "nombre": p["nombre"], "rubro": p["rubro"], "localidad": p["localidad"], "provincia": p["provincia"],
                "ubicacion": {"lat": p["lat"], "lon": p["lon"]}}

    @staticmethod
    def en_horario(d: date, desde_h: float, hasta_h: float) -> datetime:
        h = R.uniform(desde_h, hasta_h)
        return datetime.combine(d, time(0), ART) + timedelta(hours=h)

    # ventas
    def run(self):
        inicio = self.desde - timedelta(days=380)
        d = inicio
        while d <= self.hasta:
            f, ocasion, cats, online = factor_dia(d, inicio)
            n_b2c = self._poisson(26 * self.escala * f * (1 + 0.45 * (online - 1)))
            for _ in range(n_b2c):
                self.venta(d, ocasion, cats, online)
            # Empresas: fin de año y algún aniversario o incentivo durante el año.
            p_b2b = 0.10 * (7 if (d.month == 12 and d.day <= 20) or (d.month == 11 and d.day >= 20) else 1)
            if R.random() < p_b2b * self.escala:
                self.venta_empresa(d, R.choice(EMPRESAS), int(min(400, R.lognormvariate(3.2, 0.8))) + 5)
            if d == date(2025, 12, 4):   # el pedido grande de un banco para fin de año
                self.venta_empresa(d, EMPRESAS[7], int(1200 * self.escala), hora=11.2, sku="BX-MUL-01")
            d += timedelta(days=1)
        self.fraude()
        self.reclamos_sueltos()
        self.liquidaciones()
        self.validaciones_fallidas_comunes()

    def _poisson(self, lam: float) -> int:
        if lam > 30:
            return max(0, int(R.gauss(lam, math.sqrt(lam)) + 0.5))
        l, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= R.random()
            if p < l:
                return k
            k += 1

    def elegir_box(self, cats: str) -> str:
        pesos = {"Gastronomía": 26, "Bienestar": 22, "Aventura": 12, "Estadías": 14, "Vinos": 9, "Talleres": 7, "Multiexperiencia": 10}
        for c in cats.split():
            pesos[c] = pesos.get(c, 0) * 2.2
        cat = R.choices(list(pesos), list(pesos.values()))[0]
        return R.choice(POR_CATEGORIA[cat])[0]

    def venta(self, d: date, ocasion: str | None, cats: str, online: float):
        canal = R.choices(["Web", "App", "Tienda", "Marketplace"],
                          [48 * online, 22 * online, 18, 7])[0]
        h = R.choices([R.uniform(9, 13), R.uniform(13, 19), R.uniform(19, 24), R.uniform(0, 2)], [25, 40, 30, 5])[0]
        if canal == "Tienda":
            h = R.uniform(10, 21.5)
        elif d in LANZAMIENTOS and R.random() < 0.4:
            h = R.uniform(0, 2.5)
        t = datetime.combine(d, time(0), ART) + timedelta(hours=h)
        sku = self.elegir_box(cats)
        comprador = persona("CLI")
        pedido = self.nro("PED", t)
        pago_medio = R.choices([m for m, _ in MEDIOS_PAGO], [w for _, w in MEDIOS_PAGO])[0] if canal != "Tienda" else R.choice(["Tarjeta de crédito", "Tarjeta de débito", "Efectivo"])
        cuotas = R.choice([1, 1, 3, 3, 6, 12]) if pago_medio == "Tarjeta de crédito" else 1
        precio = self.precio(sku, t)
        oc = ocasion or R.choices(["Cumpleaños", "Aniversario", "Agradecimiento", "Sin ocasión", "Casamiento"], [45, 20, 10, 18, 7])[0]
        ses = sesion(canal) if canal in ("Web", "App") else None
        # Pagos rechazados: lo normal y la caída del procesador cuando arranca el Hot Sale.
        caida = CAIDA_PAGOS[0] <= t <= CAIDA_PAGOS[1] and canal in ("Web", "App", "Marketplace")
        if caida:
            # Cada cliente reintenta varias veces; algo más de la mitad vuelve después y compra.
            for k in range(R.choice([1, 2, 2, 3, 3, 4])):
                self.ev(t + timedelta(minutes=k * R.uniform(1, 6)), "Pago rechazado", canal,
                        pedido={"nro": pedido, "segmento": "Particular", "cantidad": 1, "ocasion": oc},
                        box=self.box_dict(sku, precio), comprador={k2: comprador[k2] for k2 in ("id", "email", "provincia")},
                        pago={"medio": pago_medio, "cuotas": cuotas, "estado": "Rechazado", "motivo_rechazo": "Timeout del procesador de pagos"},
                        monto_ars=precio, sesion=ses)
            if R.random() < 0.45:
                return
            t = CAIDA_PAGOS[1] + timedelta(minutes=R.uniform(5, 900))
        elif R.random() < 0.06:
            motivo = R.choices([m for m, _ in MOTIVOS_PAGO], [w for _, w in MOTIVOS_PAGO])[0]
            self.ev(t, "Pago rechazado", canal, pedido={"nro": pedido, "segmento": "Particular", "cantidad": 1, "ocasion": oc},
                    box=self.box_dict(sku, precio), comprador={k: comprador[k] for k in ("id", "email", "provincia")},
                    pago={"medio": pago_medio, "cuotas": cuotas, "estado": "Rechazado", "motivo_rechazo": motivo},
                    monto_ars=precio, sesion=ses)
            if R.random() < 0.35:
                return
            t += timedelta(minutes=R.uniform(1, 8))
        formato = "Física" if canal == "Tienda" or (canal == "Web" and R.random() < 0.22) else "Digital"
        codigo = self.codigo()
        vence = (t + timedelta(days=365)).date()
        self.ev(t, "Compra", canal, pedido={"nro": pedido, "segmento": "Particular", "cantidad": 1, "ocasion": oc},
                voucher={"codigo": codigo, "formato": formato, "vence": vence.isoformat()},
                box=self.box_dict(sku, precio), comprador={k: comprador[k] for k in ("id", "email", "provincia")},
                pago={"medio": pago_medio, "cuotas": cuotas, "estado": "Aprobado"}, monto_ars=precio, sesion=ses)
        prov_benef = comprador["provincia"] if R.random() < 0.8 else None
        benef = persona("BEN", prov_benef)
        self.ciclo(t, codigo, sku, precio, vence, formato, canal, benef, oc, b2b=False)

    def venta_empresa(self, d: date, emp: dict, cantidad: int, hora: float | None = None, sku: str | None = None):
        t = datetime.combine(d, time(0), ART) + timedelta(hours=hora or R.uniform(9.5, 18))
        sku = sku or R.choice(["BX-GAS-01", "BX-GAS-03", "BX-BIE-01", "BX-MUL-01", "BX-VIN-01", "BX-TAL-01"])
        precio = int(round(self.precio(sku, t) * 0.88, -2))   # precio corporativo
        pedido = self.nro("PED", t)
        self.ev(t, "Compra", "Empresas", pedido={"nro": pedido, "segmento": "Empresa", "cantidad": cantidad, "ocasion": "Regalo empresarial"},
                box=self.box_dict(sku, precio), empresa=emp,
                pago={"medio": "Transferencia", "cuotas": 1, "estado": "Aprobado"}, monto_ars=precio * cantidad)
        vence = (t + timedelta(days=365)).date()
        for _ in range(cantidad):
            codigo = self.codigo()
            benef = persona("BEN")
            envio_t = t + timedelta(hours=R.uniform(2, 72))
            self.ciclo(envio_t, codigo, sku, precio, vence, "Digital", "Empresas", benef, "Regalo empresarial", b2b=True)

    # el recorrido de un voucher
    def ciclo(self, t: datetime, codigo: str, sku: str, precio: int, vence: date, formato: str, canal: str, benef: dict,
              ocasion: str, b2b: bool):
        cat = BOX[sku][2]
        bdict = self.box_dict(sku, precio)
        voucher = {"codigo": codigo, "formato": formato, "vence": vence.isoformat()}
        ben_pub = {k: benef[k] for k in ("id", "email", "telefono", "provincia")}
        # Envío del regalo.
        if formato == "Digital":
            te = t + timedelta(minutes=R.uniform(1, 30)) if not b2b else t
            falla_mail = datetime(2026, 6, 17, 18, 0, tzinfo=ART) <= te <= datetime(2026, 6, 19, 13, 0, tzinfo=ART)
            medio = R.choices(["Email", "WhatsApp"], [70, 30])[0]
            if falla_mail and medio == "Email" and R.random() < 0.82:
                self.ev(te, "Envío del regalo", "Sistema", voucher=voucher, box=bdict, beneficiario=ben_pub,
                        envio={"medio": medio, "estado": "Rebotado", "detalle": "Error 451 del proveedor de correo: envío rechazado"})
                if R.random() < 0.3:
                    self.reclamo(te + timedelta(hours=R.uniform(2, 30)), "No llegó el regalo", voucher, bdict, ben_pub)
                te = datetime(2026, 6, 19, 13, 0, tzinfo=ART) + timedelta(minutes=R.uniform(0, 200))
                self.ev(te, "Envío del regalo", "Sistema", voucher=voucher, box=bdict, beneficiario=ben_pub,
                        envio={"medio": medio, "estado": "Entregado", "reintento": True})
            else:
                estado = "Rebotado" if R.random() < 0.012 else "Entregado"
                self.ev(te, "Envío del regalo", "Sistema", voucher=voucher, box=bdict, beneficiario=ben_pub,
                        envio={"medio": medio, "estado": estado})
                if estado == "Rebotado" and R.random() < 0.4:
                    self.reclamo(te + timedelta(hours=R.uniform(3, 48)), "No llegó el regalo", voucher, bdict, ben_pub)
        elif canal == "Web":
            dias = R.uniform(1, 4)
            demora = date(2025, 12, 15) <= t.date() <= date(2025, 12, 23) and R.random() < 0.3
            te = t + timedelta(days=dias + (R.uniform(3, 8) if demora else 0))
            self.ev(te, "Envío del regalo", "Sistema", voucher=voucher, box=bdict, beneficiario=ben_pub,
                    envio={"medio": "Envío a domicilio", "estado": "Entregado con demora" if demora else "Entregado"})
            if demora and R.random() < 0.45:
                self.reclamo(t + timedelta(days=dias + R.uniform(1, 3)), "Envío demorado", voucher, bdict, ben_pub)
        # Activación.
        if R.random() > (0.80 if b2b else 0.89):
            self.no_usado(t, codigo, sku, precio, vence, voucher, bdict, ben_pub)
            return
        demora = R.lognormvariate(math.log(8 if not b2b else 14), 0.9)
        ta = t + timedelta(days=demora, hours=R.uniform(-3, 3))
        ataque = datetime(2026, 8, 22, 2, 10, tzinfo=ART)
        if t < ataque < ta and R.random() < 0.25:
            self.robables.append({"codigo": codigo, "sku": sku, "precio": precio, "voucher": voucher, "box": bdict, "ben": ben_pub, "ta": ta})
            return
        canal_a = R.choice(["Web", "App", "App"])
        self.ev(ta, "Activación", canal_a, voucher=voucher, box=bdict, beneficiario=ben_pub, sesion=sesion(canal_a))
        # Cambio de box, a veces.
        if R.random() < 0.04:
            nuevo = R.choice([b for b in BOXES if b[0] != sku])[0]
            tc = ta + timedelta(days=R.uniform(0, 10))
            nuevo_precio = self.precio(nuevo, tc)
            self.ev(tc, "Cambio de box", canal_a, voucher=voucher, box=self.box_dict(nuevo, nuevo_precio), beneficiario=ben_pub,
                    cambio={"box_anterior": BOX[sku][1], "diferencia_ars": max(0, nuevo_precio - precio)}, monto_ars=max(0, nuevo_precio - precio))
            sku, precio, cat, bdict = nuevo, nuevo_precio, BOX[nuevo][2], self.box_dict(nuevo, nuevo_precio)
            ta = tc
        self.reservar(ta, codigo, sku, precio, vence, voucher, bdict, ben_pub, canal_a)

    def elegir_prestador(self, cat: str, provincia: str, d: date, excluir: str | None = None) -> dict:
        rubros = RUBROS_DE[cat]
        viaja = cat in ("Estadías", "Aventura", "Vinos") and R.random() < 0.55
        cands = [p for r in rubros for p in POR_RUBRO[r] if p["id"] != excluir]
        pesos = []
        for p in cands:
            w = p["popularidad"]
            cerca = p["provincia"] == provincia or (provincia in ("CABA", "Buenos Aires") and p["provincia"] in ("CABA", "Buenos Aires"))
            if viaja:
                w *= 2.5 if p["destino"] else 0.3
                mes = d.month
                if mes in (12, 1, 2) and p["localidad"] in ("Mar del Plata", "Pinamar", "San Carlos de Bariloche", "Villa La Angostura", "Puerto Madryn", "El Calafate", "Ushuaia"):
                    w *= 2.2
                if mes in (7, 8) and p["localidad"] in ("San Carlos de Bariloche", "San Martín de los Andes", "Ushuaia"):
                    w *= 2.8
                if mes in (2, 3, 4) and p["rubro"] == "Bodega":
                    w *= 2.0
            else:
                w *= 4.0 if cerca else 0.15
            if p is SPA_QUE_CIERRA and 6 <= d.month <= 7:
                w *= 1.8
            pesos.append(w)
        return R.choices(cands, pesos)[0]

    def reservar(self, ta, codigo, sku, precio, vence, voucher, bdict, ben_pub, canal_a, intento=0, excluir=None):
        cat = BOX[sku][2]
        limite = datetime.combine(vence, time(0), ART)
        if intento == 0 and R.random() > 0.86:
            self.no_usado(ta, codigo, sku, precio, vence, voucher, bdict, ben_pub)
            return
        ts = ta + timedelta(days=R.lognormvariate(math.log(20), 0.8) if intento == 0 else R.uniform(0.2, 4), hours=R.uniform(-4, 4))
        if ts >= limite:
            self.no_usado(ta, codigo, sku, precio, vence, voucher, bdict, ben_pub)
            return
        p = self.elegir_prestador(cat, ben_pub["provincia"], ts.date(), excluir)
        anticip = int(R.choice([R.uniform(2, 10), R.uniform(7, 30), R.uniform(20, 60)]))
        fecha = ts.date() + timedelta(days=anticip)
        # Las experiencias se hacen, sobre todo, de viernes a domingo.
        if R.random() < 0.7:
            fecha += timedelta(days=(4 - fecha.weekday()) % 7 + R.choice([0, 1, 1, 2]))
        personas = BOX[sku][4]
        nro = self.nro("RES", ts)
        reserva = {"nro": nro, "fecha_experiencia": fecha.isoformat(), "personas": personas, "anticipacion_dias": (fecha - ts.date()).days}
        pdict = self.prest_dict(p)
        self.ev(ts, "Reserva solicitada", canal_a, voucher=voucher, box=bdict, beneficiario=ben_pub, prestador=pdict, reserva=reserva,
                sesion=sesion(canal_a))
        tr = ts + timedelta(hours=R.uniform(0.5, 30))
        cerrado = p is SPA_QUE_CIERRA and CIERRE_SPA[0] <= fecha <= CIERRE_SPA[1] and ts.date() >= date(2026, 7, 2)
        alta_temporada = (fecha.month in (1, 2) and p["destino"]) or (fecha.month == 7 and p["destino"])
        p_sin = p["sin_cupo"] * (2.3 if alta_temporada else 1) * (1.4 if fecha.weekday() >= 5 else 1)
        if cerrado or R.random() < p_sin:
            motivo = "Prestador cerrado temporalmente" if cerrado else R.choice(MOTIVOS_SIN_CUPO)
            self.ev(tr, "Reserva rechazada", "Prestador", voucher=voucher, box=bdict, beneficiario=ben_pub, prestador=pdict, reserva=reserva,
                    motivo=motivo)
            if intento >= 2 and R.random() < 0.4:
                self.reclamo(tr + timedelta(hours=R.uniform(1, 48)), "Sin disponibilidad", voucher, bdict, ben_pub)
            if intento < 3:
                self.reservar(tr, codigo, sku, precio, vence, voucher, bdict, ben_pub, canal_a, intento + 1, p["id"] if cerrado else None)
            else:
                self.no_usado(tr, codigo, sku, precio, vence, voucher, bdict, ben_pub)
            return
        self.ev(tr, "Reserva confirmada", "Prestador", voucher=voucher, box=bdict, beneficiario=ben_pub, prestador=pdict, reserva=reserva)
        # El spa que cierra cancela todo lo de su cierre.
        if p is SPA_QUE_CIERRA and CIERRE_SPA[0] <= fecha <= CIERRE_SPA[1]:
            tc = datetime(2026, 7, 2, 9, 0, tzinfo=ART) + timedelta(minutes=R.uniform(0, 240))
            if tc > tr:
                self.ev(tc, "Reserva cancelada", "Prestador", voucher=voucher, box=bdict, beneficiario=ben_pub, prestador=pdict,
                        reserva=reserva, cancelada_por="Prestador", motivo="Cierre temporal por refacciones")
                if R.random() < 0.55:
                    self.reclamo(tc + timedelta(hours=R.uniform(1, 72)), "Reserva cancelada", voucher, bdict, ben_pub, prestador=pdict)
                self.reservar(tc + timedelta(hours=R.uniform(4, 96)), codigo, sku, precio, vence, voucher, bdict, ben_pub, canal_a, 1, p["id"])
                return
        if R.random() < (0.10 if p is PARRILLA_MALA else 0.015):
            tc = datetime.combine(fecha, time(0), ART) - timedelta(hours=R.uniform(6, 30))
            self.ev(tc, "Reserva cancelada", "Prestador", voucher=voucher, box=bdict, beneficiario=ben_pub, prestador=pdict,
                    reserva=reserva, cancelada_por="Prestador", motivo=R.choice(["Problema operativo", "Sobreventa de turnos", "Corte de luz"]))
            if R.random() < 0.5:
                self.reclamo(tc + timedelta(hours=R.uniform(1, 24)), "Reserva cancelada", voucher, bdict, ben_pub, prestador=pdict)
            self.reservar(tc, codigo, sku, precio, vence, voucher, bdict, ben_pub, canal_a, 1, p["id"])
            return
        if R.random() < 0.06:
            tc = tr + timedelta(hours=R.uniform(2, max(3, (datetime.combine(fecha, time(0), ART) - tr).total_seconds() / 3600 - 2)))
            self.ev(tc, "Reserva cancelada", canal_a, voucher=voucher, box=bdict, beneficiario=ben_pub, prestador=pdict, reserva=reserva,
                    cancelada_por="Beneficiario", motivo=R.choice(MOTIVOS_CANCELA))
            self.reservar(tc, codigo, sku, precio, vence, voucher, bdict, ben_pub, canal_a, 1)
            return
        # El día de la experiencia.
        rubro = p["rubro"]
        horas = {"Restaurante": (20, 22.5), "Parrilla": (12.5, 22), "Cervecería": (19, 22), "Spa": (10, 18), "Centro de masajes": (10, 19),
                 "Turismo aventura": (8.5, 15), "Hotel": (14, 18), "Cabañas": (14, 18), "Hostería": (14, 18), "Bodega": (10.5, 16),
                 "Taller": (17.5, 20)}[rubro]
        tx = self.en_horario(fecha, *horas)
        if R.random() < 0.03:
            self.ev(tx + timedelta(hours=2), "No show", "Prestador", voucher=voucher, box=bdict, beneficiario=ben_pub, prestador=pdict,
                    reserva=reserva)
            return
        liquida = int(precio * 0.65)
        self.ev(tx, "Canje", "Prestador", voucher=voucher, box=bdict, beneficiario=ben_pub, prestador=pdict, reserva=reserva,
                monto_ars=liquida)
        lunes = tx.date() - timedelta(days=tx.weekday())
        self.canjes_semana[(p["id"], lunes)].append(liquida)
        # Reseña y, si fue mala, reclamo.
        calidad = p["calidad"]
        if R.random() < 0.34:
            r = R.random()
            puntaje = 5 if r < calidad * 0.75 else 4 if r < calidad else 3 if r < calidad + 0.12 else R.choice([1, 2])
            pool = RESENAS[CATEGORIA_DE_RUBRO[rubro]]
            comentario = R.choice(pool["bien"]) if puntaje >= 4 else R.choice(NEUTRAS) if puntaje == 3 else R.choice(pool["mal"])
            self.ev(tx + timedelta(days=R.uniform(0.3, 6)), "Reseña", R.choice(["App", "Web"]), voucher=voucher, box=bdict,
                    beneficiario=ben_pub, prestador=pdict, resena={"puntaje": puntaje, "comentario": comentario})
        if R.random() < (0.18 if p is PARRILLA_MALA else 0.012):
            self.reclamo(tx + timedelta(hours=R.uniform(3, 72)), "Mala experiencia", voucher, bdict, ben_pub, prestador=pdict)

    def no_usado(self, t, codigo, sku, precio, vence, voucher, bdict, ben_pub):
        """El voucher no se usó: a veces se extiende, si no, vence."""
        tv = datetime.combine(vence, time(0), ART) + timedelta(minutes=R.uniform(5, 50))
        if R.random() < 0.22:
            te = tv - timedelta(days=R.uniform(1, 25))
            nuevo = vence + timedelta(days=90)
            cargo = int(precio * 0.15)
            v2 = dict(voucher, vence=nuevo.isoformat())
            self.ev(te, "Extensión de vigencia", R.choice(["Web", "App"]), voucher=v2, box=bdict, beneficiario=ben_pub,
                    monto_ars=cargo, extension={"vencimiento_anterior": vence.isoformat(), "dias": 90})
            if R.random() < 0.6:
                self.reservar(te, codigo, sku, precio, nuevo, v2, bdict, ben_pub, "App", 1)
                return
            tv = datetime.combine(nuevo, time(0), ART) + timedelta(minutes=R.uniform(5, 50))
            voucher = v2
        self.ev(tv, "Vencimiento", "Sistema", voucher=voucher, box=bdict, beneficiario=ben_pub, monto_ars=precio)
        if R.random() < 0.07:
            self.reclamo(tv + timedelta(days=R.uniform(1, 40)), "Vencimiento", voucher, bdict, ben_pub)

    def reclamo(self, t, categoria=None, voucher=None, bdict=None, ben_pub=None, prestador=None):
        categoria = categoria or R.choices(list(MEZCLA_RECLAMOS), list(MEZCLA_RECLAMOS.values()))[0]
        detalle = R.choice(RECLAMOS[categoria])
        if prestador and R.random() < 0.5:
            detalle += f". Fue en {prestador['nombre']}"
        estado = R.choices(["Resuelto", "En curso", "Abierto"], [70, 18, 12])[0]
        self.ev(t, "Reclamo", "Atención al cliente", voucher=voucher, box=bdict, beneficiario=ben_pub, prestador=prestador,
                reclamo={"nro": self.nro("REC", t), "categoria": categoria, "detalle": detalle,
                         "canal_atencion": R.choice(CANALES_ATENCION), "estado": estado})

    # lo planteado aparte
    def fraude(self):
        """Fuerza bruta sobre los códigos, la madrugada del 22/8: miles de intentos desde pocas IP y algunos aciertos."""
        t0 = datetime(2026, 8, 22, 2, 10, tzinfo=ART)
        ips = ["45.155.205.17", "45.155.205.91", "185.220.101.44", "193.32.162.8"]
        n = int(3300 * self.escala)
        for i in range(n):
            t = t0 + timedelta(seconds=i * (12600 / n) + R.uniform(0, 3))
            self.ev(t, "Validación fallida", "Web", voucher={"codigo": "XP-" + "".join(R.choices("ABCDEFGHJKMNPQRSTUVWXYZ23456789", k=4)) + "-" + "".join(R.choices("ABCDEFGHJKMNPQRSTUVWXYZ23456789", k=4))},
                    motivo="Código inexistente", sesion={"ip": R.choice(ips), "dispositivo": "Desconocido", "user_agent": "python-requests/2.31.0"})
        cuentas = [{"id": "BEN-990117", "email": "regalos.baratos@example.net", "telefono": "+54 9 11 5550-0101", "provincia": "CABA"},
                   {"id": "BEN-990243", "email": "canjes.rapidos@example.org", "telefono": "+54 9 11 5550-0177", "provincia": "CABA"}]
        destinos = [p for p in POR_RUBRO["Restaurante"] + POR_RUBRO["Spa"] if p["provincia"] == "CABA"][:3]
        robados = self.robables[:int(18 * self.escala) or 1]
        for v in robados:
            t = t0 + timedelta(seconds=R.uniform(0, 12600))
            cuenta = R.choice(cuentas)
            self.ev(t, "Activación", "Web", voucher=v["voucher"], box=v["box"], beneficiario=cuenta,
                    sesion={"ip": R.choice(ips), "dispositivo": "Desconocido", "user_agent": "python-requests/2.31.0"})
            p = R.choice(destinos)
            ts = t + timedelta(hours=R.uniform(3, 30))
            fecha = (ts + timedelta(days=R.uniform(1, 4))).date()
            reserva = {"nro": self.nro("RES", ts), "fecha_experiencia": fecha.isoformat(), "personas": BOX[v["sku"]][4],
                       "anticipacion_dias": (fecha - ts.date()).days}
            pdict = self.prest_dict(p)
            self.ev(ts, "Reserva solicitada", "Web", voucher=v["voucher"], box=v["box"], beneficiario=cuenta, prestador=pdict,
                    reserva=reserva, sesion={"ip": R.choice(ips), "dispositivo": "Desconocido", "user_agent": "python-requests/2.31.0"})
            self.ev(ts + timedelta(hours=R.uniform(1, 6)), "Reserva confirmada", "Prestador", voucher=v["voucher"], box=v["box"],
                    beneficiario=cuenta, prestador=pdict, reserva=reserva)
            tx = self.en_horario(fecha, 20, 22.5)
            self.ev(tx, "Canje", "Prestador", voucher=v["voucher"], box=v["box"], beneficiario=cuenta, prestador=pdict,
                    reserva=reserva, monto_ars=int(v["precio"] * 0.65))
            # El dueño real del regalo se entera cuando quiere activarlo.
            ta = max(v["ta"], tx + timedelta(days=1))
            self.ev(ta, "Validación fallida", R.choice(["Web", "App"]), voucher=v["voucher"], box=v["box"], beneficiario=v["ben"],
                    motivo="Código ya activado por otra cuenta", sesion=sesion("App"))
            if R.random() < 0.9:
                self.reclamo(ta + timedelta(hours=R.uniform(0.2, 20)), "Código no funciona", v["voucher"], v["box"], v["ben"])
        # Los que no se robaron siguen su vida normal.
        for v in self.robables[len(robados):]:
            canal_a = R.choice(["Web", "App"])
            self.ev(v["ta"], "Activación", canal_a, voucher=v["voucher"], box=v["box"], beneficiario=v["ben"], sesion=sesion(canal_a))
            self.reservar(v["ta"], v["codigo"], v["sku"], v["precio"], date.fromisoformat(v["voucher"]["vence"]), v["voucher"],
                          v["box"], v["ben"], canal_a)

    def reclamos_sueltos(self):
        codigos = sorted(self.codigos)
        d = self.desde
        while d <= self.hasta:
            for _ in range(self._poisson(3.2 * self.escala * (1.6 if d.month in (12, 1) else 1))):
                t = self.en_horario(d, 8.5, 22)
                ben = persona("BEN")
                self.reclamo(t, None, {"codigo": R.choice(codigos)}, None, {k: ben[k] for k in ("id", "email", "telefono", "provincia")})
            d += timedelta(days=1)

    def validaciones_fallidas_comunes(self):
        d = self.desde
        while d <= self.hasta:
            for _ in range(self._poisson(9 * self.escala)):
                canal = R.choice(["Web", "App"])
                t = self.en_horario(d, 8, 24)
                codigo = "XP-" + "".join(R.choices("ABCDEFGHJKMNPQRSTUVWXYZ23456789", k=4)) + "-" + "".join(R.choices("ABCDEFGHJKMNPQRSTUVWXYZ23456789", k=4))
                self.ev(t, "Validación fallida", canal, voucher={"codigo": codigo},
                        motivo=R.choices(["Código inexistente", "Código vencido", "Código ya activado"], [70, 20, 10])[0], sesion=sesion(canal))
            d += timedelta(days=1)

    def liquidaciones(self):
        for (pid, lunes), montos in self.canjes_semana.items():
            p = next(x for x in PRESTADORES if x["id"] == pid)
            t = datetime.combine(lunes + timedelta(days=8), time(10), ART) + timedelta(minutes=R.uniform(0, 90))
            self.ev(t, "Liquidación a prestador", "Sistema", prestador=self.prest_dict(p),
                    liquidacion={"semana": lunes.isoformat(), "canjes": len(montos), "comision_pct": 35}, monto_ars=sum(montos))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--desde", default="2025-11-24")
    ap.add_argument("--hasta", default="2026-11-23")
    ap.add_argument("--escala", type=float, default=0.65)
    a = ap.parse_args()
    g = Gen(date.fromisoformat(a.desde), date.fromisoformat(a.hasta), a.escala)
    g.run()
    g.eventos.sort(key=lambda x: x[0])
    SALIDA.parent.mkdir(exist_ok=True)
    with SALIDA.open("w", encoding="utf-8", newline="\n") as f:
        for _, e in g.eventos:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
    tipos = Counter(e["evento"]["tipo"] for _, e in g.eventos)
    print(f"{SALIDA.name}: {len(g.eventos):,} eventos, {SALIDA.stat().st_size / 1e6:.1f} MB")
    for t, n in tipos.most_common():
        print(f"  {t:<26} {n:>7,}")


if __name__ == "__main__":
    main()
