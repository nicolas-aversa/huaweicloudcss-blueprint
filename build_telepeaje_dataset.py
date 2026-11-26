"""
build_telepeaje_dataset.py
==========================

Dataset sintético de una empresa de peaje electrónico en Argentina. La empresa:

- emite el TAG que va en el parabrisas y lo manda por correo;
- le cobra a sus clientes, particulares y flotas, las pasadas por las autopistas
  y rutas adheridas;
- le liquida a cada concesionaria lo cobrado.

Cada fila es un evento de su plataforma, en castellano.

Todo es inventado (sin datos de nadie). Las cuentas, las patentes, los TAG, los
mails (dominios reservados example.*), los teléfonos y las concesionarias salen
de un generador con semilla fija. Las plazas llevan el nombre del lugar, con una
ubicación aproximada.

Lo que trae, para que la demo cuente algo:

- **Pasadas → pronósticos y perfil.** Cada una trae la lectura de la antena (TAG
  o patente), la categoría del TAG y la que mide la vía, la franja horaria y la
  tarifa. El tránsito tiene hora pico de lunes a viernes en el AMBA y escapadas en
  los fines de semana largos. Hay éxodo a la costa en enero, Semana Santa y
  vacaciones de invierno, más camiones a los puertos en la cosecha gruesa. Las
  tarifas suben cada trimestre.
- **Historias plantadas → anomalías y seguridad** (hora ARG):
  - 19/3, 06:40–09:15: las antenas de Hudson dejan de leer en hora pico.
    Todo se cobra por patente y llueven las alarmas de vía.
  - 20/4 al 24/4: paro del correo. Los TAG nuevos llegan tarde o vuelven.
  - 4/5 al 12/5: tras un mantenimiento, el sensor de ejes de Ituzaingó cobra
    autos como categoría 3. Hay reclamos y después devoluciones.
  - 1/8, 00:00–03:30: se cae el procesador de cobros el día del débito
    mensual. El sistema inhabilita por error los TAG de los rechazados y
    los rehabilita al otro día.
  - 14/9, 03:00–05:30: ataque de credenciales a la autogestión desde 4 IP
    (python-requests). En las cuentas tomadas cambian el domicilio y piden TAG
    adicionales que después pasan con otras patentes.
  - 5/10 al 21/10: un TAG clonado pasa por el sur mientras el auto del dueño
    está en zona norte. La patente que lee la cámara no es la del TAG.
- **Reclamos en texto libre → búsqueda híbrida.** Hay muchas formas de decir lo
  mismo: "me cobraron como camión" y "me facturaron categoría 3"; "no me leyó el
  TAG" y "la barrera no se levantó".
- **Mapa, enmascarado y perfil.** Las coordenadas de la plaza van al mapa. El
  mail, el teléfono y la patente se enmascaran. La cuenta sirve para el perfil
  por entidad.

Formato: JSON por línea, un evento por línea. La cuenta, el TAG, el vehículo y
la plaza van anidados, como salen de la cola de eventos de la plataforma.

Uso:   py build_telepeaje_dataset.py [--desde 2025-11-26] [--hasta 2026-11-25] [--escala 1.0]
Sale:  datasets/peaje-electronico.ndjson
"""
from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ART = timezone(timedelta(hours=-3))
SALIDA = Path(__file__).parent / "datasets" / "peaje-electronico.ndjson"
R = random.Random(2612)


def _t(y, m, d, h=0, mi=0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=ART)


# ── Historias planteadas ────────────────────────────────────────────────────
HUDSON = (_t(2026, 3, 19, 6, 40), _t(2026, 3, 19, 9, 15))          # antenas sin lectura
PARO_CORREO = (date(2026, 4, 17), date(2026, 4, 24))                # despachos que se demoran
SENSOR = (_t(2026, 5, 4, 0, 0), _t(2026, 5, 12, 15, 0))             # sensor de ejes de Ituzaingó
CAIDA_COBROS = (_t(2026, 8, 1, 0, 0), _t(2026, 8, 1, 3, 30))        # procesador de cobros caído
ATAQUE = _t(2026, 9, 14, 3, 0)                                       # credenciales, 2 h 30
CLON = (_t(2026, 10, 5, 6, 0), _t(2026, 10, 21, 11, 0))             # TAG clonado hasta la inhabilitación

# ── Plazas ──────────────────────────────────────────────────────────────────
# (id, nombre, ruta, concesionaria, localidad, provincia, lat, lon, urbana con hora pico, vías TAG, tarifa cat. 2 al inicio)
PLAZAS = [
    ("PZ-01", "Dellepiane", "Autopista Dellepiane", "Red Vial Metropolitana", "CABA", "CABA", -34.6523, -58.4669, True, 4, 1900),
    ("PZ-02", "Parque Avellaneda", "Autopista Perito Moreno", "Red Vial Metropolitana", "CABA", "CABA", -34.6436, -58.4776, True, 4, 1900),
    ("PZ-03", "Alberti", "Autopista 25 de Mayo", "Red Vial Metropolitana", "CABA", "CABA", -34.6258, -58.4003, True, 6, 1900),
    ("PZ-04", "Retiro", "Autopista Illia", "Red Vial Metropolitana", "CABA", "CABA", -34.5836, -58.3811, True, 4, 1900),
    ("PZ-05", "Debenedetti", "Panamericana", "Acceso Norte Concesiones", "Vicente López", "Buenos Aires", -34.5196, -58.4955, True, 8, 1700),
    ("PZ-06", "Márquez", "Panamericana, ramal Tigre", "Acceso Norte Concesiones", "San Isidro", "Buenos Aires", -34.4838, -58.5508, True, 4, 1700),
    ("PZ-07", "Escobar", "Panamericana, ramal Campana", "Acceso Norte Concesiones", "Escobar", "Buenos Aires", -34.3330, -58.8070, False, 6, 2100),
    ("PZ-08", "Pilar", "Panamericana, ramal Pilar", "Acceso Norte Concesiones", "Pilar", "Buenos Aires", -34.4520, -58.8890, True, 4, 1700),
    ("PZ-09", "Ituzaingó", "Acceso Oeste", "Conexión Oeste", "Ituzaingó", "Buenos Aires", -34.6497, -58.6790, True, 8, 1600),
    ("PZ-10", "General Rodríguez", "Acceso Oeste", "Conexión Oeste", "General Rodríguez", "Buenos Aires", -34.6100, -58.9500, False, 4, 1600),
    ("PZ-11", "Dock Sud", "Autopista Buenos Aires–La Plata", "Autovía Ribereña Sur", "Avellaneda", "Buenos Aires", -34.6531, -58.3528, True, 6, 1500),
    ("PZ-12", "Hudson", "Autopista Buenos Aires–La Plata", "Autovía Ribereña Sur", "Berazategui", "Buenos Aires", -34.7937, -58.1478, True, 8, 1500),
    ("PZ-13", "Riccheri", "Autopista Riccheri", "Accesos del Sudoeste", "Ezeiza", "Buenos Aires", -34.7460, -58.5300, True, 4, 1400),
    ("PZ-14", "Cañuelas", "Autopista Ezeiza–Cañuelas", "Accesos del Sudoeste", "Cañuelas", "Buenos Aires", -34.9750, -58.6800, False, 4, 1400),
    ("PZ-15", "Samborombón", "Ruta Nacional 2", "Corredor Atlántico", "Chascomús", "Buenos Aires", -35.3200, -58.0900, False, 6, 2600),
    ("PZ-16", "Maipú", "Ruta Nacional 2", "Corredor Atlántico", "Maipú", "Buenos Aires", -36.8650, -57.8770, False, 6, 2600),
    ("PZ-17", "Conesa", "Ruta Provincial 11", "Corredor Atlántico", "General Conesa", "Buenos Aires", -36.5170, -57.3330, False, 4, 2200),
    ("PZ-18", "Zárate", "Ruta Nacional 12", "Corredor Mesopotámico", "Zárate", "Buenos Aires", -34.0975, -59.0270, False, 4, 2400),
    ("PZ-19", "Villa Espil", "Ruta Nacional 7", "Corredor Pampeano", "San Andrés de Giles", "Buenos Aires", -34.4480, -59.4900, False, 4, 2300),
    ("PZ-20", "Malagueño", "Autopista Córdoba–Carlos Paz", "Autovía Serrana", "Malagueño", "Córdoba", -31.4650, -64.3580, False, 4, 1500),
    ("PZ-21", "Río Segundo", "Autopista Rosario–Córdoba", "Corredor Litoral Centro", "Río Segundo", "Córdoba", -31.6500, -63.9100, False, 4, 1800),
    ("PZ-22", "Carcarañá", "Autopista Rosario–Córdoba", "Corredor Litoral Centro", "Carcarañá", "Santa Fe", -32.8560, -61.1530, False, 4, 1800),
    ("PZ-23", "San Lorenzo", "Autopista Rosario–Santa Fe", "Corredor Litoral Centro", "San Lorenzo", "Santa Fe", -32.7450, -60.7400, False, 4, 1700),
]
PL = {p[0]: {"id": p[0], "nombre": p[1], "ruta": p[2], "concesionaria": p[3], "localidad": p[4], "provincia": p[5],
             "lat": p[6], "lon": p[7], "urbana": p[8], "vias": p[9], "base": p[10]} for p in PLAZAS}
PLAZA_PUB = {k: {"nombre": v["nombre"], "ruta": v["ruta"], "concesionaria": v["concesionaria"],
                 "ubicacion": {"lat": v["lat"], "lon": v["lon"]}} for k, v in PL.items()}
CONCESIONARIAS = sorted({p[3] for p in PLAZAS})

# Ajustes de tarifa (acumulativos) y categorías.
AJUSTES = [(date(2026, 1, 1), 1.15), (date(2026, 4, 1), 1.12), (date(2026, 7, 1), 1.10), (date(2026, 10, 1), 1.10)]
CAT_MULT = {1: 0.5, 2: 1.0, 3: 2.0, 4: 3.0, 5: 4.0, 6: 5.0}
CAT_NOMBRE = {1: "Moto", 2: "Auto o camioneta", 3: "Dos ejes de más de 2,1 m", 4: "Tres ejes", 5: "Cuatro ejes",
              6: "Cinco ejes o más"}
HACIA, DESDE = "Hacia la ciudad", "Desde la ciudad"

# Feriados nacionales (fines de semana largos).
FERIADOS = {date(2025, 11, 21), date(2025, 11, 24), date(2025, 12, 8), date(2025, 12, 25), date(2026, 1, 1),
            date(2026, 2, 16), date(2026, 2, 17), date(2026, 3, 23), date(2026, 3, 24), date(2026, 4, 2),
            date(2026, 4, 3), date(2026, 5, 1), date(2026, 5, 25), date(2026, 6, 15), date(2026, 6, 20),
            date(2026, 7, 9), date(2026, 7, 10), date(2026, 8, 17), date(2026, 10, 12), date(2026, 11, 23),
            date(2026, 12, 7), date(2026, 12, 8), date(2026, 12, 25)}


def libre(d: date) -> bool:
    return d.weekday() >= 5 or d in FERIADOS


# ── Dónde vive y por dónde anda cada uno ────────────────────────────────────
ZONAS = [("CABA", 20), ("Norte", 25), ("Oeste", 18), ("Sur", 25), ("Córdoba", 5), ("Rosario", 7)]
PROVINCIA_DE = {"CABA": "CABA", "Norte": "Buenos Aires", "Oeste": "Buenos Aires", "Sur": "Buenos Aires",
                "Córdoba": "Córdoba", "Rosario": "Santa Fe"}
CARACTERISTICA = {"CABA": "11", "Norte": "11", "Oeste": "11", "Sur": "11", "Córdoba": "351", "Rosario": "341"}
# Ida al trabajo: plazas en orden, hacia la ciudad.
COMMUTE = {
    "Norte": [(5, ["PZ-05"]), (3, ["PZ-06"]), (2, ["PZ-05", "PZ-04"]), (2, ["PZ-08", "PZ-05"])],
    "Oeste": [(6, ["PZ-09"]), (4, ["PZ-09", "PZ-02"]), (2, ["PZ-10", "PZ-09"])],
    "Sur": [(8, ["PZ-12", "PZ-11"]), (3, ["PZ-11"]), (3, ["PZ-13", "PZ-01"]), (2, ["PZ-14", "PZ-13"])],
    "CABA": [(4, ["PZ-03"]), (3, ["PZ-04"]), (2, ["PZ-01"]), (2, ["PZ-02"]), (2, ["PZ-11"])],
    "Córdoba": [(1, ["PZ-20"])],
    "Rosario": [(2, ["PZ-23"]), (1, ["PZ-22"])],
}
# Escapadas: (destino, plazas de ida, peso, peso por mes).
_AMBA = [
    ("Costa Atlántica", ["PZ-12", "PZ-15", "PZ-16"], 3.0, {1: 5, 2: 3, 12: 1.5, 3: 1.2, 4: 1.3}),
    ("Costa por la Ruta 11", ["PZ-12", "PZ-15", "PZ-17"], 2.0, {1: 5, 2: 3, 12: 1.5}),
    ("Litoral", ["PZ-05", "PZ-07", "PZ-18"], 1.5, {1: 1.5, 4: 1.3}),
    ("Rosario y el norte", ["PZ-05", "PZ-07"], 1.2, {}),
    ("Oeste por la Ruta 7", ["PZ-09", "PZ-10", "PZ-19"], 1.2, {7: 1.3}),
    ("Campo por Cañuelas", ["PZ-13", "PZ-14"], 1.0, {}),
    ("Delta y Tigre", ["PZ-06"], 1.0, {1: 1.3, 2: 1.3, 11: 1.3, 12: 1.3}),
]
ESCAPADAS = {
    "CABA": _AMBA, "Norte": _AMBA, "Oeste": _AMBA, "Sur": _AMBA,
    "Córdoba": [("Carlos Paz y Punilla", ["PZ-20"], 3.0, {1: 3, 2: 2, 7: 3}), ("Rosario", ["PZ-21", "PZ-22"], 1.0, {})],
    "Rosario": [("Córdoba", ["PZ-22", "PZ-21"], 1.5, {1: 1.5, 7: 2.5}), ("Santa Fe", ["PZ-23"], 2.0, {}),
                ("Buenos Aires", ["PZ-07", "PZ-05"], 1.5, {})],
}
# Camiones: (corredor, plazas de ida, va a puerto con la cosecha)
CORREDORES = [
    ("Buenos Aires–Rosario", ["PZ-05", "PZ-07"], True),
    ("Rosario–Córdoba", ["PZ-22", "PZ-21"], True),
    ("Puertos del Gran Rosario", ["PZ-23"], True),
    ("Buenos Aires–Mar del Plata", ["PZ-11", "PZ-12", "PZ-15", "PZ-16"], False),
    ("Buenos Aires–Mesopotamia", ["PZ-05", "PZ-07", "PZ-18"], False),
    ("Buenos Aires–Oeste", ["PZ-09", "PZ-10", "PZ-19"], True),
    ("Buenos Aires–Cañuelas", ["PZ-01", "PZ-13", "PZ-14"], False),
]
COSECHA = {3: 1.1, 4: 1.7, 5: 1.8, 6: 1.7, 7: 1.5, 8: 1.2}
REPARTO = [["PZ-03"], ["PZ-11", "PZ-03"], ["PZ-05", "PZ-04"], ["PZ-01"], ["PZ-02"], ["PZ-13", "PZ-01"], ["PZ-09", "PZ-02"]]

NOMBRES_PERSONA = ["sofia", "martina", "lucia", "valentina", "camila", "julieta", "agustina", "florencia", "mariana", "paula",
                   "carolina", "natalia", "gabriela", "romina", "laura", "juan", "mateo", "santiago", "tomas", "nicolas",
                   "martin", "facundo", "lucas", "diego", "pablo", "federico", "joaquin", "ignacio", "gonzalo", "andres"]
APELLIDOS = ["gonzalez", "rodriguez", "fernandez", "lopez", "martinez", "garcia", "perez", "romero", "sosa", "alvarez",
             "torres", "ruiz", "ramirez", "flores", "benitez", "acosta", "medina", "herrera", "suarez", "aguirre",
             "gimenez", "molina", "silva", "castro", "rojas", "ortiz", "luna", "juarez", "cabrera", "rios"]
DOMINIOS = ["example.com", "example.net", "example.org"]
RUBROS_CARGA = ["Transporte de cargas", "Agroindustria", "Distribución de alimentos", "Materiales de construcción"]
RUBROS_REPARTO = ["Logística y reparto", "Servicios técnicos", "Distribución de bebidas", "Mensajería"]
MEDIOS = [("Tarjeta de crédito", 55), ("Tarjeta de débito", 25), ("Billetera virtual", 20)]
RECHAZO = {"Tarjeta de crédito": 0.035, "Tarjeta de débito": 0.07, "Billetera virtual": 0.05, "Débito en cuenta": 0.015}
MOTIVOS_RECHAZO = {
    "Tarjeta de crédito": [("Tarjeta vencida", 30), ("Límite excedido", 35), ("Rechazada por el banco emisor", 35)],
    "Tarjeta de débito": [("Fondos insuficientes", 70), ("Rechazada por el banco emisor", 30)],
    "Billetera virtual": [("Saldo insuficiente", 75), ("Medio de pago desvinculado", 25)],
    "Débito en cuenta": [("Fondos insuficientes", 60), ("Cuenta inexistente o cerrada", 40)],
}
REDES = ["181.4", "181.46", "181.165", "186.12", "186.130", "190.18", "190.55", "190.191", "200.45", "201.212", "152.168", "170.51"]
UA_WEB = [("Android", "Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36"),
          ("iPhone", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1"),
          ("Windows", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
          ("Mac", "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15")]
UA_APP = [("Android", "AppTAG/3.8.1 (Android 14)"), ("iPhone", "AppTAG/3.8.1 (iOS 17.6)")]
IPS_ATAQUE = ["45.155.205.17", "45.155.205.91", "185.220.101.44", "193.32.162.8"]
UA_ATAQUE = "python-requests/2.31.0"


def sesion(canal: str) -> dict:
    disp, ua = R.choice(UA_APP if canal == "App" else UA_WEB)
    return {"ip": f"{R.choice(REDES)}.{R.randint(0, 255)}.{R.randint(1, 254)}", "dispositivo": disp, "user_agent": ua}


def sesion_ataque(navegador: bool = False) -> dict:
    if navegador:
        return {"ip": R.choice(IPS_ATAQUE), "dispositivo": "Windows", "user_agent": UA_WEB[2][1]}
    return {"ip": R.choice(IPS_ATAQUE), "dispositivo": "Desconocido", "user_agent": UA_ATAQUE}


# ── Textos libres ───────────────────────────────────────────────────────────
RECLAMOS = {
    "Pasada no reconocida": [
        "Me cobraron pasadas por peajes donde nunca estuve", "En el resumen aparecen cruces por el sur y yo vivo en zona norte",
        "Figura una pasada a la madrugada y el auto estaba guardado", "Tengo cobros de una autopista que no uso nunca",
        "Hay pasadas con una patente que no es la mía", "Me llegan notificaciones de peajes que no hice",
        "El auto estaba en el garage y me cobraron dos peajes",
    ],
    "Cobro duplicado": [
        "Me cobraron dos veces el mismo peaje", "La misma pasada figura repetida en el resumen", "Me debitaron doble este mes",
        "Pasé una sola vez y aparecen dos cobros con un minuto de diferencia", "En la tarjeta veo dos débitos por el mismo período",
    ],
    "Categoría mal cobrada": [
        "Me cobraron como camión y tengo un auto", "Me facturaron categoría 3 y mi auto es categoría 2",
        "El peaje me cobra el doble, como si fuera una camioneta grande", "Desde esta semana en el Acceso Oeste me cobran otra categoría",
        "Tengo un auto común y en el resumen dice dos ejes de más de 2,1 metros", "La tarifa que me cobran no corresponde a mi vehículo",
    ],
    "TAG no leído": [
        "La barrera no se levantó y tuve que esperar a que me habilitaran a mano", "El dispositivo no funciona en las vías exclusivas",
        "Pasé y no me leyó el TAG, ¿me van a cobrar igual?", "El peaje no detecta el TAG desde hace una semana",
        "Tuve que pasar por la vía de efectivo porque no me tomaba el TAG",
        "Se hizo una fila enorme en la vía de TAG porque no leía ningún dispositivo",
    ],
    "TAG inhabilitado": [
        "Me inhabilitaron el TAG y tengo los pagos al día", "Me bloquearon el dispositivo sin ningún aviso",
        "Dice que tengo deuda pero el débito figura pagado", "Me cobraron con recargo porque el TAG figuraba inhabilitado",
        "No entiendo por qué me suspendieron el TAG si la tarjeta tiene saldo",
    ],
    "No llegó el TAG": [
        "Pedí el dispositivo hace tres semanas y no llegó", "El correo dice que lo devolvieron y nadie vino a mi casa",
        "Sigo esperando el TAG, el seguimiento no se mueve", "Me dijeron que llegaba en 5 días y ya pasaron 15",
        "El envío figura entregado pero no recibí nada",
    ],
    "Medio de pago": [
        "No me toman la tarjeta para el débito automático", "Quiero cambiar el medio de pago y la página da error",
        "No puedo pagar con la billetera virtual", "Me rechazaron el débito y la tarjeta está bien", "Quiero pasar el cobro a otra tarjeta",
    ],
    "Facturación": [
        "Necesito la factura A a nombre de la empresa", "No me llega la factura por mail", "No entiendo el detalle del resumen del mes",
        "Quiero el detalle de pasadas por vehículo para rendir gastos", "La factura sale con el CUIT equivocado",
    ],
    "Cambio de vehículo o baja": [
        "Vendí el auto y quiero pasar el TAG al nuevo", "Quiero dar de baja el dispositivo",
        "Cambié de auto y no sé cómo actualizar la patente", "Necesito sumar un segundo vehículo a la cuenta",
    ],
    "Acceso a la cuenta": [
        "No puedo entrar a la autogestión, dice contraseña incorrecta", "Me cambiaron el domicilio de la cuenta y no fui yo",
        "Alguien pidió un TAG adicional a mi nombre", "Me llegó un mail de cambio de datos que yo no hice",
        "Me bloquearon el usuario después de varios intentos",
    ],
}
MEZCLA_RECLAMOS = {"Pasada no reconocida": 8, "Cobro duplicado": 9, "Categoría mal cobrada": 5, "TAG no leído": 16,
                   "TAG inhabilitado": 7, "No llegó el TAG": 8, "Medio de pago": 14, "Facturación": 16,
                   "Cambio de vehículo o baja": 12, "Acceso a la cuenta": 5}
CANALES_ATENCION = ["Chat", "WhatsApp", "Email", "Teléfono"]

ALARMAS = [  # (tipo, severidad, peso, detalles)
    ("Antena sin lectura", "Alta", 22, ["La antena de la vía {v} no responde (timeout del lector a 250 ms)",
                                        "Lector TAG de la vía {v} sin lecturas en los últimos 5 minutos",
                                        "Vía {v}: el lector reporta error de RF y no lee dispositivos"]),
    ("Barrera trabada", "Media", 24, ["La barrera de la vía {v} no baja", "Vía {v}: barrera abierta más de 3 minutos",
                                      "Falla del motor de la barrera en la vía {v}"]),
    ("Cámara de patentes sin imagen", "Media", 20, ["La cámara OCR de la vía {v} no entrega imágenes",
                                                    "Vía {v}: imágenes sin foco, el OCR no reconoce patentes"]),
    ("Sensor de ejes fuera de rango", "Media", 10, ["El sensor de ejes de la vía {v} reporta lecturas fuera de rango",
                                                    "Vía {v}: diferencias entre la categoría del TAG y la medida por la vía"]),
    ("Sin comunicación con el concentrador", "Alta", 12, ["La vía {v} perdió la comunicación con el concentrador de la plaza",
                                                          "Vía {v}: cola de tránsitos sin enviar al concentrador"]),
    ("Mantenimiento programado", "Baja", 8, ["Mantenimiento programado de la vía {v}", "Vía {v} cerrada por mantenimiento preventivo"]),
]


# ── Cuentas y vehículos ─────────────────────────────────────────────────────
class Vehiculo:
    def __init__(self, patente: str, cat: int, perfil: str, zona: str, tag: str):
        self.patente, self.cat, self.perfil, self.zona = patente, cat, perfil, zona
        self.tag = tag
        self.falla = R.uniform(0.18, 0.4) if R.random() < 0.05 else 0.006   # TAG mal pegado: lo lee la cámara
        self.commute = R.choices([r for _, r in COMMUTE[zona]], [w for w, _ in COMMUTE[zona]])[0]
        self.corredores: list = []
        self.cambios_tag: list[tuple[datetime, str]] = []   # (desde, nuevo TAG)
        self.sin_tag: list[tuple[datetime, datetime]] = []   # sin TAG que funcione: no pasa por las vías TAG
        self.activo_desde: datetime | None = None

    def tag_en(self, t: datetime) -> str | None:
        if any(a <= t < b for a, b in self.sin_tag):
            return None
        tag = self.tag
        for desde, nuevo in self.cambios_tag:
            if t >= desde:
                tag = nuevo
        return tag


class Cuenta:
    def __init__(self, cid: str, tipo: str, zona: str, rubro: str | None = None):
        self.id, self.tipo, self.zona, self.rubro = cid, tipo, zona, rubro
        self.provincia = PROVINCIA_DE[zona]
        usuario = f"{R.choice(NOMBRES_PERSONA)}.{R.choice(APELLIDOS)}{R.choice(['', str(R.randint(1, 99))])}"
        if tipo == "Empresa":
            usuario = f"pagos.{R.choice(['transportes', 'logistica', 'flota', 'administracion'])}{R.randint(10, 999)}"
        self.email = f"{usuario}@{R.choice(DOMINIOS)}"
        self.telefono = f"+54 9 {CARACTERISTICA[zona]} {R.randint(4000, 6999)}-{R.randint(1000, 9999)}"
        self.medio = "Débito en cuenta" if tipo == "Empresa" else R.choices([m for m, _ in MEDIOS], [w for _, w in MEDIOS])[0]
        self.vehiculos: list[Vehiculo] = []
        self.alta: datetime | None = None          # adhesión dentro de la ventana
        self.baja: datetime | None = None
        self.inhab: list[tuple[datetime, datetime]] = []
        self.acc: dict[tuple[int, int], list] = defaultdict(lambda: [0, 0])
        self.pub = {"id": cid, "tipo": tipo}
        if rubro:
            self.pub["rubro"] = rubro

    def titular(self) -> dict:
        return {"email": self.email, "telefono": self.telefono, "provincia": self.provincia}

    def inhabilitada(self, t: datetime) -> bool:
        return any(a <= t < b for a, b in self.inhab)


class Gen:
    def __init__(self, desde: date, hasta: date, escala: float):
        self.desde, self.hasta, self.escala = desde, hasta, escala
        self.t_desde = datetime.combine(desde, time(0), ART)
        self.t_hasta = datetime.combine(hasta + timedelta(days=1), time(0), ART)
        self.sim_desde = date(desde.year, desde.month, 1) - timedelta(days=1)
        self.sim_desde = date(self.sim_desde.year, self.sim_desde.month, 1)   # el mes anterior entero, para el primer cobro
        self.eventos: list[tuple[datetime, dict]] = []
        self.seq = Counter()
        self.patentes: set[str] = set()
        self.tags: set[str] = set()
        self.cuentas: list[Cuenta] = []
        self.liq: dict[tuple[str, date], list] = defaultdict(lambda: [0, 0])
        self.sensor: dict[str, list] = defaultdict(lambda: [0, 0])   # cuenta → [pasadas, diferencia]
        self.reclamo_sensor: set[str] = set()
        self.cuenta_de: dict[str, Cuenta] = {}

    # utilidades
    def ev(self, t: datetime, tipo: str, canal: str, **campos) -> bool:
        if not (self.t_desde <= t < self.t_hasta):
            return False
        d = {"timestamp": t.isoformat(timespec="seconds"), "evento": {"tipo": tipo}, "canal": canal}
        d.update({k: v for k, v in campos.items() if v is not None})
        self.eventos.append((t, d))
        return True

    def nro(self, pref: str, t: datetime) -> str:
        clave = f"{pref}{t:%y%m}"
        self.seq[clave] += 1
        return f"{pref}-{t:%y%m}-{self.seq[clave]:06d}"

    def patente(self) -> str:
        L = "ABCDEFGHJKLMNPRSTUVWXYZ"
        while True:
            if R.random() < 0.6:
                p = f"A{R.choice('ABCDEFG')} {R.randint(100, 999)} {R.choice(L)}{R.choice(L)}"
            else:
                p = f"{R.choice('FGHIJKLMNOP')}{R.choice(L)}{R.choice(L)} {R.randint(100, 999)}"
            if p not in self.patentes:
                self.patentes.add(p)
                return p

    def tag(self) -> str:
        while True:
            t = f"{R.randint(10, 99)}{R.randint(10_000_000, 99_999_999)}"
            if t not in self.tags:
                self.tags.add(t)
                return t

    @staticmethod
    def poisson(lam: float) -> int:
        if lam > 30:
            return max(0, int(R.gauss(lam, math.sqrt(lam)) + 0.5))
        l, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= R.random()
            if p < l:
                return k
            k += 1

    @staticmethod
    def a_la_hora(d: date, h: float) -> datetime:
        return datetime.combine(d, time(0), ART) + timedelta(hours=h)

    # ── Las cuentas ──
    def armar(self):
        n = 100000
        e = self.escala
        zonas, pesos = [z for z, _ in ZONAS], [w for _, w in ZONAS]
        perfiles = ["Diario"] * int(44 * e) + ["Frecuente"] * int(85 * e) + ["Ocasional"] * int(400 * e)
        R.shuffle(perfiles)
        i = 0
        while i < len(perfiles):
            n += R.randint(3, 17)
            zona = R.choices(zonas, pesos)[0]
            c = Cuenta(f"CTA-{n}", "Particular", zona)
            cuantos = 2 if R.random() < 0.12 and i + 1 < len(perfiles) else 1
            for k in range(cuantos):
                cat = R.choices([2, 1, 3], [90, 4, 6])[0]
                perfil = perfiles[i + k] if k == 0 else "Ocasional"
                c.vehiculos.append(Vehiculo(self.patente(), cat, perfil, zona, self.tag()))
            i += cuantos
            self.cuentas.append(c)
        # Flotas: camiones de carga y utilitarios de reparto.
        for k in range(max(4, int(9 * e))):
            n += R.randint(3, 17)
            zona = R.choices(["Norte", "Oeste", "Sur", "Rosario", "Córdoba"], [3, 2, 3, 3, 1])[0]
            c = Cuenta(f"CTA-{n}", "Empresa", zona, R.choice(RUBROS_CARGA))
            propios = [x for x in CORREDORES if (zona in ("Rosario", "Córdoba")) == (x[0] in ("Rosario–Córdoba", "Puertos del Gran Rosario"))]
            corredores = R.sample(propios, min(len(propios), R.choice([1, 2])))
            for _ in range(R.randint(2, 7)):
                v = Vehiculo(self.patente(), R.choices([4, 5, 6], [25, 30, 45])[0], "Camión", zona, self.tag())
                v.corredores = corredores
                c.vehiculos.append(v)
            self.cuentas.append(c)
        for k in range(max(3, int(7 * e))):
            n += R.randint(3, 17)
            zona = R.choices(["CABA", "Norte", "Oeste", "Sur"], [3, 2, 2, 3])[0]
            c = Cuenta(f"CTA-{n}", "Empresa", zona, R.choice(RUBROS_REPARTO))
            for _ in range(R.randint(2, 5)):
                v = Vehiculo(self.patente(), R.choices([3, 2], [75, 25])[0], "Reparto", zona, self.tag())
                c.vehiculos.append(v)
            self.cuentas.append(c)
        # Un cuarto de las cuentas se adhiere durante el año (más antes del verano y de las vacaciones de invierno).
        dias = [(self.desde + timedelta(days=k)) for k in range((self.hasta - self.desde).days - 20)]
        peso_mes = {12: 1.7, 1: 1.6, 2: 0.9, 3: 1.1, 4: 1.0, 5: 0.9, 6: 1.0, 7: 1.3, 8: 0.9, 9: 0.9, 10: 1.0, 11: 1.0}
        for c in self.cuentas:
            if R.random() < (0.27 if c.tipo == "Particular" else 0.15):
                d = R.choices(dias, [peso_mes[x.month] * (0.7 if libre(x) else 1) for x in dias])[0]
                c.alta = self.a_la_hora(d, R.uniform(8, 23.5))
        for c in self.cuentas:
            self.cuenta_de[c.id] = c
            if R.random() < 0.015:
                c.baja = self.t_desde + timedelta(days=R.uniform(30, 340))

    # ── Una pasada ──
    def franja(self, p: dict, t: datetime) -> str:
        if not p["urbana"]:
            return "Única"
        h = t.hour + t.minute / 60
        if not libre(t.date()) and (7 <= h < 10 or 17 <= h < 20):
            return "Pico"
        return "Fuera de pico"

    @staticmethod
    def tarifa(p: dict, t: datetime, cat: int, franja: str) -> int:
        f = 1.0
        for d, m in AJUSTES:
            if t.date() >= d:
                f *= m
        return int(round(p["base"] * f * CAT_MULT[cat] * (0.75 if franja == "Fuera de pico" else 1), -1))

    def pasada(self, c: Cuenta, pid: str, t: datetime, sentido: str, tag: str, patente: str, cat: int,
               falla: float = 0.006, patente_ocr: str | None = None, inhab: bool | None = None):
        p = PL[pid]
        det = cat
        if pid == "PZ-09" and SENSOR[0] <= t < SENSOR[1] and cat == 2 and R.random() < 0.65:
            det = 3
        elif cat == 2 and R.random() < 0.003:
            det = 3      # auto con acoplado: la vía mide bien
        franja = self.franja(p, t)
        monto = self.tarifa(p, t, det, franja)
        if det != cat and pid == "PZ-09" and SENSOR[0] <= t < SENSOR[1]:
            s = self.sensor[c.id]
            s[0] += 1
            s[1] += monto - self.tarifa(p, t, cat, franja)
            if c.id not in self.reclamo_sensor and R.random() < 0.12:
                self.reclamo_sensor.add(c.id)
                self.reclamo(t + timedelta(hours=R.uniform(2, 60)), c, "Categoría mal cobrada", plaza=pid)
        ocr = patente_ocr or patente
        if (pid == "PZ-12" and HUDSON[0] <= t < HUDSON[1]) or R.random() < falla:
            lectura = {"modo": "Patente (OCR)", "confianza_ocr": round(R.uniform(0.71, 0.99), 2), "patente_ocr": ocr}
        else:
            lectura = {"modo": "TAG", "ms": int(R.lognormvariate(3.4, 0.35))}
            if R.random() > 0.02:
                lectura["patente_ocr"] = ocr
        if inhab is None:
            inhab = c.inhabilitada(t)
        tarifa = {"franja": franja, "categoria": det}
        total = monto
        if inhab:
            tarifa["recargo_ars"] = monto
            total = 2 * monto
        acc = c.acc[(t.year, t.month)]
        acc[0] += 1
        acc[1] += total
        if self.ev(t, "Pasada", "Vía", cuenta=c.pub, tag={"id": tag, "estado": "Inhabilitado" if inhab else "Habilitado"},
                   vehiculo={"patente": patente, "categoria": cat, "tipo": CAT_NOMBRE[cat]}, plaza=PLAZA_PUB[pid],
                   via={"nro": R.randint(1, p["vias"]), "sentido": sentido, "categoria_detectada": det},
                   lectura=lectura, tarifa=tarifa, monto_ars=total):
            lunes = t.date() - timedelta(days=t.weekday())
            liq = self.liq[(p["concesionaria"], lunes)]
            liq[0] += 1
            liq[1] += total

    def recorrido(self, c: Cuenta, v: Vehiculo, plazas: list[str], t: datetime, sentido: str, minutos=(6, 25)):
        for pid in plazas:
            tag = v.tag_en(t)
            if tag is None:
                return
            self.pasada(c, pid, t, sentido, tag, v.patente, v.cat, v.falla)
            t += timedelta(minutes=R.uniform(*minutos), seconds=R.uniform(0, 59))

    # ── Un día de un vehículo ──
    def salida(self, d: date) -> tuple[str, date] | None:
        """Si d es el último día hábil antes de un fin de semana (largo o no), cuál y hasta cuándo."""
        if libre(d) or not libre(d + timedelta(days=1)):
            return None
        k = d + timedelta(days=1)
        while libre(k + timedelta(days=1)):
            k += timedelta(days=1)
        return ("largo" if (k - d).days >= 3 else "fin de semana"), k

    def escapada(self, c: Cuenta, v: Vehiculo, d: date, vuelta: date):
        opciones = ESCAPADAS[v.zona]
        dest = R.choices(opciones, [w * meses.get(d.month, 1) for _, _, w, meses in opciones])[0]
        _, plazas, _, _ = dest
        if R.random() < 0.6 and not libre(d):
            ti = self.a_la_hora(d, R.uniform(15.5, 22))
        else:
            ti = self.a_la_hora(d + timedelta(days=0 if libre(d) else 1), R.uniform(7, 12))
        self.recorrido(c, v, plazas, ti, DESDE, (25, 110))
        tv = self.a_la_hora(vuelta, R.uniform(13, 21.5))
        self.recorrido(c, v, list(reversed(plazas)), tv, HACIA, (25, 110))

    def dia(self, c: Cuenta, v: Vehiculo, d: date, ocupado: dict):
        if ocupado.get(id(v)) and d <= ocupado[id(v)]:
            return
        habil = not libre(d)
        if v.perfil == "Camión":
            p = (0.2 if habil else 0.07 if d.weekday() == 5 else 0.03)
            corr = R.choice(v.corredores)
            p *= COSECHA.get(d.month, 0.85) if corr[2] else (1.3 if d.month in (1, 2) and "Mar del Plata" in corr[0] else 1)
            if R.random() < p:
                h = R.choices([R.uniform(0, 6), R.uniform(6, 14), R.uniform(14, 24)], [40, 35, 25])[0]
                ti = self.a_la_hora(d, h)
                sentido = DESDE if c.zona not in ("Rosario", "Córdoba") else HACIA
                self.recorrido(c, v, corr[1], ti, sentido, (40, 120))
                vuelta = d + timedelta(days=R.choice([1, 1, 2]))
                tv = self.a_la_hora(vuelta, R.uniform(0, 24))
                self.recorrido(c, v, list(reversed(corr[1])), tv, HACIA if sentido == DESDE else DESDE, (40, 120))
                ocupado[id(v)] = vuelta
            return
        if v.perfil == "Reparto":
            mult = 1.3 if d.month == 12 else 1.0
            if habil and R.random() < 0.36 * mult:
                ruta = R.choice(REPARTO)
                self.recorrido(c, v, ruta, self.a_la_hora(d, R.uniform(8, 12)), HACIA)
                self.recorrido(c, v, list(reversed(ruta)), self.a_la_hora(d, R.uniform(13, 18.5)), DESDE)
            return
        # Particulares.
        s = self.salida(d)
        if s:
            tipo, vuelta = s
            p = {"Diario": 0.16, "Frecuente": 0.24, "Ocasional": 0.11}[v.perfil] * (2.2 if tipo == "largo" else 1)
            if d.month in (1, 2):
                p *= 1.6
            if R.random() < p:
                self.escapada(c, v, d, vuelta)
                ocupado[id(v)] = vuelta
                return
        # Vacaciones: en enero y febrero, y en julio, alguno se va una o dos semanas.
        vac = (date(d.year, 1, 2) <= d <= date(d.year, 2, 20)) or (date(d.year, 7, 6) <= d <= date(d.year, 7, 31))
        if vac and R.random() < {"Diario": 0.012, "Frecuente": 0.014, "Ocasional": 0.008}[v.perfil]:
            vuelta = d + timedelta(days=R.randint(5, 14))
            self.escapada(c, v, d, vuelta)
            ocupado[id(v)] = vuelta
            return
        p_ida = {"Diario": 0.8, "Frecuente": 0.2, "Ocasional": 0.012}[v.perfil] if habil else \
            {"Diario": 0.05, "Frecuente": 0.04, "Ocasional": 0.01}[v.perfil]
        if vac:
            p_ida *= 0.75
        if R.random() < p_ida:
            if v.perfil == "Diario" and habil:
                ida, vuelta_h = R.uniform(6.3, 9.4), R.uniform(16.8, 20.6)
            else:
                ida = R.uniform(8, 18)
                vuelta_h = min(23.8, ida + R.uniform(1.5, 6))
            self.recorrido(c, v, v.commute, self.a_la_hora(d, ida), HACIA)
            self.recorrido(c, v, list(reversed(v.commute)), self.a_la_hora(d, vuelta_h), DESDE)

    # ── El ciclo de una cuenta ──
    def adhesion(self, c: Cuenta):
        t = c.alta
        canal = "Empresas" if c.tipo == "Empresa" else R.choices(["Web", "App", "Billetera virtual"], [50, 25, 25])[0]
        if canal == "Billetera virtual":
            c.medio = "Billetera virtual"
        self.ev(t, "Adhesión", canal, cuenta=c.pub, titular=c.titular(), medio_pago=c.medio,
                adhesion={"tags": len(c.vehiculos), "vehiculos": [{"patente": v.patente, "categoria": v.cat} for v in c.vehiculos][:3]},
                sesion=sesion(canal) if canal in ("Web", "App") else None)
        entrega = self.envio(t + timedelta(hours=R.uniform(14, 40)), c, [v.tag for v in c.vehiculos])
        for v in c.vehiculos:
            ta = entrega + timedelta(hours=R.uniform(1, 30 if R.random() < 0.8 else 150))
            v.activo_desde = ta
            canal_a = R.choice(["App", "App", "Web"])
            self.ev(ta, "Activación del TAG", canal_a, cuenta=c.pub, tag={"id": v.tag, "estado": "Habilitado"},
                    vehiculo={"patente": v.patente, "categoria": v.cat, "tipo": CAT_NOMBRE[v.cat]}, sesion=sesion(canal_a))

    def envio(self, td: datetime, c: Cuenta, tags: list[str], localidad: str | None = None) -> datetime:
        """Despacho y entrega por correo; devuelve cuándo se entregó."""
        nro = self.nro("ENV", td)
        base = {"nro": nro, "tags": len(tags)}
        if localidad:
            base["localidad"] = localidad
        self.ev(td, "Envío del TAG", "Correo", cuenta=c.pub, envio=dict(base, estado="Despachado"))
        demora = R.uniform(2, 6)
        paro = PARO_CORREO[0] <= td.date() <= PARO_CORREO[1]
        if paro:
            demora += R.uniform(8, 13)
            if R.random() < 0.15:
                tv = td + timedelta(days=demora)
                self.ev(tv, "Envío del TAG", "Correo", cuenta=c.pub, envio=dict(base, estado="Devuelto al remitente", demora_dias=round(demora, 1)))
                if R.random() < 0.7:
                    self.reclamo(tv + timedelta(hours=R.uniform(2, 48)), c, "No llegó el TAG")
                td = tv + timedelta(days=R.uniform(1, 3))
                self.ev(td, "Envío del TAG", "Correo", cuenta=c.pub, envio=dict(base, estado="Despachado", reenvio=True))
                demora = R.uniform(2, 5)
            elif R.random() < 0.35:
                self.reclamo(td + timedelta(days=R.uniform(6, demora)), c, "No llegó el TAG")
        elif R.random() < 0.02:
            self.reclamo(td + timedelta(days=demora + R.uniform(0, 3)), c, "No llegó el TAG")
        te = td + timedelta(days=demora)
        te = te.replace(hour=R.randint(9, 18), minute=R.randint(0, 59))
        self.ev(te, "Envío del TAG", "Correo", cuenta=c.pub,
                envio=dict(base, estado="Entregado", demora_dias=round((te - td).total_seconds() / 86400, 1)))
        return te

    def login(self, t: datetime, c: Cuenta, fallido_antes: float = 0.05):
        canal = R.choice(["App", "App", "Web"])
        ses = sesion(canal)
        if R.random() < fallido_antes:
            self.ev(t - timedelta(seconds=R.uniform(20, 120)), "Inicio de sesión fallido", canal, cuenta=c.pub,
                    titular={"email": c.email}, motivo="Contraseña incorrecta", sesion=ses)
        self.ev(t, "Inicio de sesión", canal, cuenta=c.pub, titular={"email": c.email}, sesion=ses)

    def cobro(self, c: Cuenta, d: date):
        prev = d - timedelta(days=1)
        n, monto = c.acc.get((prev.year, prev.month), [0, 0])
        if n == 0:
            return
        t = self.a_la_hora(d, R.uniform(0.5, 5.5))
        comp = "Factura A" if c.tipo == "Empresa" else "Factura B"
        cobro = {"periodo": f"{prev.year}-{prev.month:02d}", "pasadas": n, "medio": c.medio, "comprobante": comp}
        if R.random() < 0.18:
            self.login(t + timedelta(days=R.uniform(0.3, 5)), c)
        if CAIDA_COBROS[0] <= t < CAIDA_COBROS[1]:
            self.ev(t, "Cobro rechazado", "Sistema", cuenta=c.pub, cobro=dict(cobro, motivo_rechazo="Timeout del procesador de pagos"),
                    monto_ars=monto)
            # El error: inhabilita a todos los rechazados esa misma mañana, en vez de esperar al día 11.
            ti = _t(2026, 8, 1, 10, 0) + timedelta(minutes=R.uniform(0, 40))
            tr = _t(2026, 8, 2, 11, 0) + timedelta(minutes=R.uniform(0, 90))
            c.inhab.append((ti, tr))
            self.ev(ti, "Inhabilitación del TAG", "Sistema", cuenta=c.pub, motivo="Cobro rechazado del período " + cobro["periodo"],
                    tags=len(c.vehiculos))
            if R.random() < 0.3:
                self.reclamo(ti + timedelta(hours=R.uniform(0.3, 24)), c, "TAG inhabilitado")
            if R.random() < 0.35:
                self.login(ti + timedelta(hours=R.uniform(0.2, 20)), c)
            self.ev(tr, "Rehabilitación del TAG", "Sistema", cuenta=c.pub,
                    motivo="Corrección: el cobro del 1/8 falló por el procesador de pagos", tags=len(c.vehiculos))
            t2 = _t(2026, 8, 2, 13, 0) + timedelta(minutes=R.uniform(0, 180))
            self.ev(t2, "Cobro", "Sistema", cuenta=c.pub, cobro=dict(cobro, reintento=True), monto_ars=monto)
            return
        if R.random() >= RECHAZO[c.medio]:
            self.ev(t, "Cobro", "Sistema", cuenta=c.pub, cobro=cobro, monto_ars=monto)
            return
        motivos = MOTIVOS_RECHAZO[c.medio]
        motivo = R.choices([m for m, _ in motivos], [w for _, w in motivos])[0]
        self.ev(t, "Cobro rechazado", "Sistema", cuenta=c.pub, cobro=dict(cobro, motivo_rechazo=motivo), monto_ars=monto)
        if R.random() < 0.06:
            self.reclamo(t + timedelta(hours=R.uniform(4, 60)), c, "Medio de pago")
        t2 = self.a_la_hora(d + timedelta(days=3), R.uniform(4, 6))
        if R.random() < 0.6:
            self.ev(t2, "Cobro", "Sistema", cuenta=c.pub, cobro=dict(cobro, reintento=True), monto_ars=monto)
            return
        self.ev(t2, "Cobro rechazado", "Sistema", cuenta=c.pub, cobro=dict(cobro, motivo_rechazo=motivo, reintento=True), monto_ars=monto)
        ti = self.a_la_hora(d + timedelta(days=10), R.uniform(10, 11))
        self.ev(ti, "Inhabilitación del TAG", "Sistema", cuenta=c.pub, motivo="Deuda impaga del período " + cobro["periodo"],
                tags=len(c.vehiculos))
        tp = ti + timedelta(days=min(20, R.lognormvariate(math.log(2.5), 0.9)))
        self.login(tp - timedelta(minutes=R.uniform(2, 10)), c)
        canal = R.choice(["App", "Web"])
        self.ev(tp, "Pago de deuda", canal, cuenta=c.pub, pago={"periodo": cobro["periodo"], "medio": R.choice(["Tarjeta de crédito", "Billetera virtual", "Pago con QR"])},
                monto_ars=monto)
        tr = tp + timedelta(minutes=R.uniform(2, 20))
        self.ev(tr, "Rehabilitación del TAG", "Sistema", cuenta=c.pub, motivo="Pago registrado", tags=len(c.vehiculos))
        c.inhab.append((ti, tr))
        if R.random() < 0.08:
            self.reclamo(ti + timedelta(hours=R.uniform(1, 72)), c, "TAG inhabilitado")

    def simular(self, c: Cuenta):
        if c.alta:
            self.adhesion(c)
        ocupado: dict = {}
        d = self.sim_desde
        while d <= self.hasta:
            if d.day == 1:
                self.cobro(c, d)
            if c.baja and d >= c.baja.date():
                if d == c.baja.date():
                    self.ev(c.baja, "Baja", R.choice(["Web", "App", "Atención al cliente"]), cuenta=c.pub,
                            motivo=R.choice(["Vendió el vehículo", "Ya no usa autopistas", "Se muda al exterior", "Disconforme con el servicio"]),
                            tags=len(c.vehiculos))
                d += timedelta(days=1)
                continue
            for v in c.vehiculos:
                if c.alta and (v.activo_desde is None or self.a_la_hora(d, 23.99) < v.activo_desde):
                    continue
                self.dia(c, v, d, ocupado)
            # Si un TAG mal pegado se lee siempre por cámara, a veces el cliente reclama.
            d += timedelta(days=1)
        for v in c.vehiculos:
            if v.falla > 0.1 and R.random() < 0.5:
                self.reclamo(self.t_desde + timedelta(days=R.uniform(10, 350), hours=R.uniform(9, 20)), c, "TAG no leído")

    # ── Historias ──
    def ataque(self):
        """Prueba de credenciales en la autogestión: miles de intentos desde 4 IP y algunas cuentas tomadas."""
        n = int(2800 * self.escala)
        victimas = [c for c in self.cuentas if c.tipo == "Particular" and not c.alta and not c.baja
                    and c.zona in ("CABA", "Norte", "Oeste", "Sur")]
        victimas = R.sample(victimas, max(6, int(16 * self.escala)))
        emails = [c.email for c in R.sample(self.cuentas, min(len(self.cuentas), 300))]
        for i in range(n):
            t = ATAQUE + timedelta(seconds=i * (9000 / n) + R.uniform(0, 2))
            existe = R.random() < 0.3
            mail = R.choice(emails) if existe else f"{R.choice(NOMBRES_PERSONA)}{R.randint(1, 9999)}@{R.choice(DOMINIOS)}"
            self.ev(t, "Inicio de sesión fallido", "Web", titular={"email": mail},
                    motivo="Contraseña incorrecta" if existe else "Usuario inexistente", sesion=sesion_ataque())
        patentes_atacante = [self.patente() for _ in range(4)]
        for c in victimas:
            tl = ATAQUE + timedelta(seconds=R.uniform(0, 9000))
            self.ev(tl, "Inicio de sesión", "Web", cuenta=c.pub, titular={"email": c.email}, sesion=sesion_ataque())
            localidad = R.choice(["Lanús", "Quilmes", "Lomas de Zamora", "Avellaneda"])
            tc = tl + timedelta(minutes=R.uniform(1, 4))
            self.ev(tc, "Cambio de datos", "Web", cuenta=c.pub, titular={"email": c.email},
                    cambio={"campo": "Domicilio de envío", "localidad_nueva": localidad}, sesion=sesion_ataque())
            nuevo = self.tag()
            pat = R.choice(patentes_atacante)
            tp = tc + timedelta(minutes=R.uniform(1, 3))
            self.ev(tp, "Pedido de TAG adicional", "Web", cuenta=c.pub, tag={"id": nuevo, "estado": "Pedido"},
                    vehiculo={"patente": pat, "categoria": 2, "tipo": CAT_NOMBRE[2]}, sesion=sesion_ataque())
            if R.random() < 0.35:   # el dueño ve el mail del cambio y avisa antes del envío
                tr = _t(2026, 9, 14, 7, 30) + timedelta(hours=R.uniform(0, 26))
                self.reclamo(tr, c, "Acceso a la cuenta", detalle=R.choice(["Me cambiaron el domicilio de la cuenta y no fui yo",
                                                                           "Me llegó un mail de cambio de datos que yo no hice",
                                                                           "Alguien pidió un TAG adicional a mi nombre"]))
                self.ev(tr + timedelta(hours=R.uniform(1, 3)), "Inhabilitación del TAG", "Atención al cliente", cuenta=c.pub,
                        tag={"id": nuevo, "estado": "Inhabilitado"}, motivo="TAG pedido por un tercero", tags=1)
                continue
            entrega = self.envio(_t(2026, 9, 15, 10, 0) + timedelta(hours=R.uniform(0, 5)), c, [nuevo], localidad)
            ta = entrega + timedelta(hours=R.uniform(2, 30))
            self.ev(ta, "Activación del TAG", "Web", cuenta=c.pub, tag={"id": nuevo, "estado": "Habilitado"},
                    vehiculo={"patente": pat, "categoria": 2, "tipo": CAT_NOMBRE[2]}, sesion=sesion_ataque(navegador=True))
            # Lo descubre con el cobro del 1/10.
            td = _t(2026, 10, 1, 6, 0) + timedelta(hours=R.uniform(2, 96))
            total = 0
            d = ta.date()
            while d <= td.date():
                if R.random() < 0.75:
                    for _ in range(R.choice([1, 2, 2, 3, 4])):
                        t = self.a_la_hora(d, R.uniform(6, 23))
                        if ta < t < td:
                            pid = R.choice(["PZ-11", "PZ-12", "PZ-03", "PZ-01", "PZ-13"])
                            antes = c.acc[(t.year, t.month)][1]
                            self.pasada(c, pid, t, R.choice([HACIA, DESDE]), nuevo, pat, 2, inhab=False)
                            total += c.acc[(t.year, t.month)][1] - antes
                d += timedelta(days=1)
            self.reclamo(td, c, "Pasada no reconocida", detalle=R.choice(["Hay pasadas con una patente que no es la mía",
                                                                         "Alguien pidió un TAG adicional a mi nombre y lo está usando",
                                                                         "Me cobraron pasadas por peajes donde nunca estuve"]))
            self.ev(td + timedelta(hours=R.uniform(1, 5)), "Inhabilitación del TAG", "Atención al cliente", cuenta=c.pub,
                    tag={"id": nuevo, "estado": "Inhabilitado"}, motivo="TAG pedido por un tercero", tags=1)
            if total:
                self.ev(_t(2026, 10, 14, 10, 0) + timedelta(hours=R.uniform(0, 6)), "Devolución", "Sistema", cuenta=c.pub,
                        motivo="Pasadas de un TAG pedido por un tercero", monto_ars=total)

    def clon(self):
        """Un TAG clonado: pasa por el sur mientras el auto del dueño anda por zona norte."""
        c = next(x for x in self.cuentas if x.tipo == "Particular" and not x.alta and not x.baja and x.zona == "Norte"
                 and len(x.vehiculos) == 1 and x.vehiculos[0].perfil == "Diario" and x.vehiculos[0].falla < 0.1)
        v = c.vehiculos[0]
        clon_pat = self.patente()
        total = 0
        d = CLON[0].date()
        intento_inhab = False
        while d <= CLON[1].date():
            if R.random() < 0.85 or d == CLON[1].date():
                for _ in range(R.choice([1, 2, 2, 3])):
                    t = self.a_la_hora(d, R.uniform(7, 22))
                    pid = R.choice(["PZ-11", "PZ-12", "PZ-13", "PZ-01", "PZ-15"])
                    inhab = t >= CLON[1]
                    if inhab and intento_inhab:
                        continue
                    intento_inhab |= inhab
                    antes = c.acc[(t.year, t.month)][1]
                    self.pasada(c, pid, t, R.choice([HACIA, DESDE]), v.tag, v.patente, 2, 0, patente_ocr=clon_pat, inhab=inhab)
                    total += c.acc[(t.year, t.month)][1] - antes
            d += timedelta(days=1)
        tr = CLON[1] - timedelta(minutes=R.uniform(70, 110))
        self.reclamo(tr, c, "Pasada no reconocida", detalle="Me llegan notificaciones de peajes en el sur y yo estuve todo el día en zona norte",
                     plaza="PZ-12")
        self.ev(CLON[1], "Inhabilitación del TAG", "Atención al cliente", cuenta=c.pub, tag={"id": v.tag, "estado": "Inhabilitado"},
                motivo="Sospecha de TAG clonado: la misma identificación en dos vehículos", tags=1)
        nuevo = self.tag()
        self.ev(CLON[1] + timedelta(minutes=R.uniform(15, 30)), "Reemplazo de TAG", "Atención al cliente", cuenta=c.pub,
                tag={"id": nuevo, "estado": "Pedido"}, vehiculo={"patente": v.patente, "categoria": v.cat, "tipo": CAT_NOMBRE[v.cat]},
                motivo="TAG clonado")
        entrega = self.envio(_t(2026, 10, 22, 11, 0), c, [nuevo])
        ta = entrega + timedelta(hours=R.uniform(10, 20))
        self.ev(ta, "Activación del TAG", "App", cuenta=c.pub, tag={"id": nuevo, "estado": "Habilitado"},
                vehiculo={"patente": v.patente, "categoria": v.cat, "tipo": CAT_NOMBRE[v.cat]}, sesion=sesion("App"))
        v.sin_tag.append((CLON[1], ta))
        v.cambios_tag.append((ta, nuevo))
        self.ev(_t(2026, 11, 4, 10, 0) + timedelta(hours=R.uniform(0, 5)), "Devolución", "Sistema", cuenta=c.pub,
                motivo="Pasadas de un TAG clonado", monto_ars=total)

    def reclamo(self, t: datetime, c: Cuenta | None, categoria: str | None = None, plaza: str | None = None, detalle: str | None = None):
        categoria = categoria or R.choices(list(MEZCLA_RECLAMOS), list(MEZCLA_RECLAMOS.values()))[0]
        detalle = detalle or R.choice(RECLAMOS[categoria])
        if plaza and R.random() < 0.5:
            detalle += f". Fue en el peaje {PL[plaza]['nombre']}"
        estado = R.choices(["Resuelto", "En curso", "Abierto"], [70, 18, 12])[0]
        self.ev(t, "Reclamo", "Atención al cliente", cuenta=c.pub if c else None, titular=c.titular() if c else None,
                plaza=PLAZA_PUB[plaza] if plaza else None,
                reclamo={"nro": self.nro("REC", t), "categoria": categoria, "detalle": detalle,
                         "canal_atencion": R.choice(CANALES_ATENCION), "estado": estado})

    def alarma(self, t: datetime, pid: str, tipo: str, via: int | None = None, detalle: str | None = None,
               severidad: str | None = None):
        a = next(x for x in ALARMAS if x[0] == tipo) if tipo != "Servicio restablecido" else None
        via = via or R.randint(1, PL[pid]["vias"])
        detalle = detalle or R.choice(a[3]).format(v=via)
        self.ev(t, "Alarma de vía", "Vía", plaza=PLAZA_PUB[pid],
                alarma={"tipo": tipo, "severidad": severidad or a[1], "via": via, "detalle": detalle})

    def alarmas(self):
        pids = list(PL)
        pesos = [PL[p]["vias"] for p in pids]
        d = self.desde
        while d <= self.hasta:
            for _ in range(self.poisson(3.2)):
                tipo = R.choices([a[0] for a in ALARMAS], [a[2] for a in ALARMAS])[0]
                h = R.uniform(0, 5) if tipo == "Mantenimiento programado" else R.uniform(0, 24)
                self.alarma(self.a_la_hora(d, h), R.choices(pids, pesos)[0], tipo)
            d += timedelta(days=1)
        # Hudson: las antenas dejan de leer en hora pico.
        self.alarma(HUDSON[0] - timedelta(minutes=2), "PZ-12", "Sin comunicación con el concentrador", via=4)
        for via in range(2, 8):
            t = HUDSON[0] + timedelta(minutes=R.uniform(0, 4))
            while t < HUDSON[1]:
                self.alarma(t, "PZ-12", "Antena sin lectura", via=via)
                t += timedelta(minutes=R.uniform(3, 7))
        self.alarma(HUDSON[1] + timedelta(minutes=3), "PZ-12", "Servicio restablecido", via=4, severidad="Info",
                    detalle="Se reiniciaron los lectores de las vías 2 a 7 y vuelven a leer los TAG")
        for _ in range(int(9 * self.escala) + 3):
            c = R.choice([x for x in self.cuentas if x.zona in ("Sur", "CABA") and x.tipo == "Particular"])
            self.reclamo(HUDSON[0] + timedelta(hours=R.uniform(0.5, 8)), c, "TAG no leído", plaza="PZ-12")
        # Ituzaingó: el sensor de ejes después del mantenimiento.
        self.alarma(_t(2026, 5, 3, 23, 30), "PZ-09", "Mantenimiento programado", via=1,
                    detalle="Recalibración de los sensores de ejes de las vías 1 a 8")
        d = SENSOR[0].date()
        while d <= SENSOR[1].date():
            for _ in range(R.randint(1, 3)):
                t = self.a_la_hora(d, R.uniform(6, 22))
                if t < SENSOR[1]:
                    self.alarma(t, "PZ-09", "Sensor de ejes fuera de rango")
            d += timedelta(days=1)
        self.alarma(SENSOR[1], "PZ-09", "Servicio restablecido", via=1, severidad="Info",
                    detalle="Sensores de ejes recalibrados; la categoría medida vuelve a coincidir con la del TAG")

    def devoluciones_sensor(self):
        for cid, (n, dif) in self.sensor.items():
            if dif > 0:
                self.ev(_t(2026, 5, 20, 10, 0) + timedelta(minutes=R.uniform(0, 150)), "Devolución", "Sistema",
                        cuenta=self.cuenta_de[cid].pub, motivo=f"Diferencia de categoría en Ituzaingó ({n} pasadas, 4 al 12 de mayo)",
                        monto_ars=dif)

    def sueltos(self):
        activas = [c for c in self.cuentas if not c.baja]
        d = self.desde
        while d <= self.hasta:
            for _ in range(self.poisson(2.6 * self.escala * (1.4 if d.month in (1, 12) else 1))):
                self.reclamo(self.a_la_hora(d, R.uniform(8, 22)), R.choice(activas))
            for _ in range(self.poisson(6 * self.escala)):
                self.login(self.a_la_hora(d, R.uniform(7, 24)), R.choice(activas), fallido_antes=0.08)
            for _ in range(self.poisson(1.5 * self.escala)):
                canal = R.choice(["App", "Web"])
                self.ev(self.a_la_hora(d, R.uniform(0, 24)), "Inicio de sesión fallido", canal,
                        titular={"email": f"{R.choice(NOMBRES_PERSONA)}.{R.choice(APELLIDOS)}@{R.choice(DOMINIOS)}"},
                        motivo=R.choice(["Usuario inexistente", "Contraseña incorrecta", "Usuario bloqueado por intentos"]),
                        sesion=sesion(canal))
            d += timedelta(days=1)

    def liquidaciones(self):
        for (conc, lunes), (n, monto) in sorted(self.liq.items()):
            if lunes < self.desde:
                continue
            t = self.a_la_hora(lunes + timedelta(days=9), R.uniform(10, 12))
            comision = 2.5
            self.ev(t, "Liquidación a concesionaria", "Sistema", concesionaria={"nombre": conc},
                    liquidacion={"semana": lunes.isoformat(), "pasadas": n, "comision_pct": comision,
                                 "bruto_ars": monto}, monto_ars=int(round(monto * (1 - comision / 100))))

    def run(self):
        self.armar()
        self.ataque()
        self.clon()
        for c in self.cuentas:
            self.simular(c)
        self.alarmas()
        self.devoluciones_sensor()
        self.sueltos()
        self.liquidaciones()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--desde", default="2025-11-26")
    ap.add_argument("--hasta", default="2026-11-25")
    ap.add_argument("--escala", type=float, default=1.0)
    a = ap.parse_args()
    g = Gen(date.fromisoformat(a.desde), date.fromisoformat(a.hasta), a.escala)
    g.run()
    g.eventos.sort(key=lambda x: x[0])
    SALIDA.parent.mkdir(exist_ok=True)
    with SALIDA.open("w", encoding="utf-8", newline="\n") as f:
        for _, e in g.eventos:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
    tipos = Counter(e["evento"]["tipo"] for _, e in g.eventos)
    print(f"{SALIDA.name}: {len(g.eventos):,} eventos, {SALIDA.stat().st_size / 1e6:.1f} MB, "
          f"{len(g.cuentas)} cuentas, {sum(len(c.vehiculos) for c in g.cuentas)} vehículos")
    for t, n in tipos.most_common():
        print(f"  {t:<30} {n:>7,}")


if __name__ == "__main__":
    main()
