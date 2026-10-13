"""
build_logistica_dataset.py
==========================

Dataset sintético del seguimiento de envíos de un operador logístico de
paquetería en Argentina: cada fila es un evento del recorrido de un envío
(admisión, ingreso a un centro de distribución, viaje troncal, salida a reparto,
entrega, visita fallida, retiro en sucursal, devolución, incidencia), en
castellano. Para demos de correos privados y operadores de última milla.

Todo es inventado (sin datos de nadie): cuentas, envíos, DNI, teléfonos y
repartidores salen de un generador con semilla fija. Lo que trae, para que la
demo cuente algo:

- la nota del repartidor o del centro en texto libre, con muchas formas de decir
  lo mismo → búsqueda híbrida ("no había nadie" / "cliente ausente", "llegó
  roto" / "caja aplastada");
- volumen con ritmo diario y semanal, crecimiento y los picos del comercio
  electrónico (CyberMonday, Black Friday, fiestas, Hot Sale, Día del Padre, Día
  del Niño) y la temporada en la costa → pronósticos (capacidad);
- eventos que se notan → detección de anomalías: un paro de transporte, una
  falla de la clasificadora en un centro, nevadas que cortan la ruta a la
  cordillera y una cinta que rompe paquetes;
- coordenadas → mapa; DNI y teléfono del destinatario → enmascarado; cuenta del
  remitente → perfil por entidad (pocos vendedores grandes mandan mucho).

Formato: por defecto, JSON por línea (un evento por línea, con el envío, el
remitente, el destino y la ubicación anidados), que es como sale un evento de
seguimiento de un sistema de transporte: por la API, por webhooks a los clientes
o por una cola. `--formato csv` saca lo mismo plano, como un export del sistema.

Uso:   py build_logistica_dataset.py [--formato json|csv] [--desde 2025-10-13] [--hasta 2026-10-12] [--por-dia N]
Sale:  datasets/seguimiento-de-envios.ndjson (o .csv)
"""
from __future__ import annotations

import argparse
import bisect
import csv
import itertools
import json
import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ART = timezone(timedelta(hours=-3))
SALIDA = Path(__file__).parent / "datasets" / "seguimiento-de-envios"

COLUMNAS = [
    "fecha_hora", "nro_envio", "evento", "tipo_servicio", "canal_admision", "segmento_cliente", "cuenta_remitente",
    "rubro", "origen_localidad", "origen_provincia", "destino_localidad", "destino_provincia", "region_destino",
    "centro_distribucion", "latitud", "longitud", "vehiculo", "repartidor_id", "intento", "bultos", "peso_kg",
    "valor_declarado_ars", "costo_envio_ars", "sla_horas", "horas_desde_admision", "cumple_sla", "motivo",
    "observacion", "recibio", "dni_destinatario", "telefono_destinatario", "calificacion",
]

# En JSON, cada columna va dentro de su objeto (como lo arma el sistema de transporte).
ANIDADO = {
    "fecha_hora": ("timestamp",), "evento": ("evento", "tipo"), "motivo": ("evento", "motivo"),
    "observacion": ("evento", "observacion"), "intento": ("evento", "intento"),
    "horas_desde_admision": ("evento", "horas_desde_admision"), "cumple_sla": ("evento", "cumple_sla"),
    "nro_envio": ("envio", "nro"), "tipo_servicio": ("envio", "servicio"), "canal_admision": ("envio", "canal_admision"),
    "sla_horas": ("envio", "sla_horas"), "bultos": ("envio", "bultos"), "peso_kg": ("envio", "peso_kg"),
    "valor_declarado_ars": ("envio", "valor_declarado_ars"), "costo_envio_ars": ("envio", "costo_ars"),
    "segmento_cliente": ("remitente", "segmento"), "cuenta_remitente": ("remitente", "cuenta"), "rubro": ("remitente", "rubro"),
    "origen_localidad": ("origen", "localidad"), "origen_provincia": ("origen", "provincia"),
    "destino_localidad": ("destino", "localidad"), "destino_provincia": ("destino", "provincia"),
    "region_destino": ("destino", "region"), "dni_destinatario": ("destinatario", "dni"),
    "telefono_destinatario": ("destinatario", "telefono"), "centro_distribucion": ("ubicacion", "centro"),
    "latitud": ("ubicacion", "lat"), "longitud": ("ubicacion", "lon"), "vehiculo": ("transporte", "vehiculo"),
    "repartidor_id": ("transporte", "repartidor"), "recibio": ("entrega", "recibio"), "calificacion": ("entrega", "calificacion"),
}


def a_json(fila: dict) -> str:
    """Una fila plana como el evento JSON del sistema: anidado y sin los campos vacíos."""
    evento: dict = {}
    for columna in COLUMNAS:
        valor = fila.get(columna, "")
        if valor == "" or columna == "canal_admision" and fila["evento"] != "Admitido":   # dato de la admisión
            continue
        *padres, hoja = ANIDADO[columna]
        destino = evento
        for padre in padres:
            destino = destino.setdefault(padre, {})
        destino[hoja] = valor
    return json.dumps(evento, ensure_ascii=False, separators=(",", ":"))


# ── Dónde ───────────────────────────────────────────────────────────────────
# Centro de distribución: (latitud, longitud, horas de troncal desde Buenos Aires,
# código de sus repartidores, cuántos tiene, característica telefónica de la zona)
CENTROS = {
    "CD Buenos Aires": (-34.4600, -58.6400, 0, "BA", 260, "11"),
    "CD Mar del Plata": (-37.9900, -57.6000, 6, "MDP", 30, "223"),
    "CD Rosario": (-32.9800, -60.7100, 4.5, "ROS", 45, "341"),
    "CD Córdoba": (-31.3600, -64.2300, 9.5, "COR", 55, "351"),
    "CD Mendoza": (-32.9300, -68.8000, 14, "MZA", 30, "261"),
    "CD Tucumán": (-26.8300, -65.1800, 15, "TUC", 35, "381"),
    "CD Resistencia": (-27.4800, -58.9500, 13, "RES", 30, "362"),
    "CD Neuquén": (-38.9300, -68.1000, 15, "NQN", 25, "299"),
    "CD Bahía Blanca": (-38.7000, -62.2300, 9, "BHI", 30, "291"),
}

# (localidad, provincia, región, latitud, longitud, centro, horas desde el centro hasta la sucursal, peso como destino)
LOCALIDADES = [
    ("Palermo", "CABA", "AMBA", -34.5889, -58.4306, "CD Buenos Aires", 0, 6),
    ("Caballito", "CABA", "AMBA", -34.6195, -58.4400, "CD Buenos Aires", 0, 5),
    ("Belgrano", "CABA", "AMBA", -34.5627, -58.4583, "CD Buenos Aires", 0, 5),
    ("Flores", "CABA", "AMBA", -34.6280, -58.4637, "CD Buenos Aires", 0, 4),
    ("Recoleta", "CABA", "AMBA", -34.5875, -58.3974, "CD Buenos Aires", 0, 4),
    ("Villa Urquiza", "CABA", "AMBA", -34.5733, -58.4877, "CD Buenos Aires", 0, 3),
    ("Barracas", "CABA", "AMBA", -34.6460, -58.3820, "CD Buenos Aires", 0, 2),
    ("Vicente López", "Buenos Aires", "AMBA", -34.5260, -58.4790, "CD Buenos Aires", 0, 3),
    ("San Isidro", "Buenos Aires", "AMBA", -34.4713, -58.5286, "CD Buenos Aires", 0, 3),
    ("Tigre", "Buenos Aires", "AMBA", -34.4264, -58.5797, "CD Buenos Aires", 0, 3),
    ("Pilar", "Buenos Aires", "AMBA", -34.4587, -58.9142, "CD Buenos Aires", 0, 3),
    ("San Miguel", "Buenos Aires", "AMBA", -34.5432, -58.7123, "CD Buenos Aires", 0, 3),
    ("Morón", "Buenos Aires", "AMBA", -34.6534, -58.6198, "CD Buenos Aires", 0, 4),
    ("San Justo", "Buenos Aires", "AMBA", -34.6833, -58.5500, "CD Buenos Aires", 0, 5),
    ("Merlo", "Buenos Aires", "AMBA", -34.6653, -58.7275, "CD Buenos Aires", 0, 3),
    ("Lanús", "Buenos Aires", "AMBA", -34.7068, -58.3916, "CD Buenos Aires", 0, 4),
    ("Lomas de Zamora", "Buenos Aires", "AMBA", -34.7609, -58.4061, "CD Buenos Aires", 0, 4),
    ("Quilmes", "Buenos Aires", "AMBA", -34.7206, -58.2543, "CD Buenos Aires", 0, 4),
    ("Avellaneda", "Buenos Aires", "AMBA", -34.6624, -58.3650, "CD Buenos Aires", 0, 3),
    ("Florencio Varela", "Buenos Aires", "AMBA", -34.8272, -58.3956, "CD Buenos Aires", 0, 3),
    ("La Plata", "Buenos Aires", "AMBA", -34.9214, -57.9545, "CD Buenos Aires", 1.5, 4),
    ("Mar del Plata", "Buenos Aires", "Costa Atlántica", -38.0055, -57.5426, "CD Mar del Plata", 0, 4),
    ("Pinamar", "Buenos Aires", "Costa Atlántica", -37.1079, -56.8614, "CD Mar del Plata", 2, 1),
    ("Villa Gesell", "Buenos Aires", "Costa Atlántica", -37.2639, -56.9731, "CD Mar del Plata", 2, 1),
    ("Necochea", "Buenos Aires", "Costa Atlántica", -38.5545, -58.7396, "CD Mar del Plata", 2, 1),
    ("Tandil", "Buenos Aires", "Centro", -37.3217, -59.1332, "CD Mar del Plata", 3, 1.5),
    ("Rosario", "Santa Fe", "Centro", -32.9442, -60.6505, "CD Rosario", 0, 5),
    ("Santa Fe", "Santa Fe", "Centro", -31.6333, -60.7000, "CD Rosario", 2, 3),
    ("Paraná", "Entre Ríos", "Centro", -31.7413, -60.5115, "CD Rosario", 2.5, 2),
    ("Rafaela", "Santa Fe", "Centro", -31.2503, -61.4867, "CD Rosario", 3, 1),
    ("Venado Tuerto", "Santa Fe", "Centro", -33.7456, -61.9688, "CD Rosario", 3, 1),
    ("San Nicolás", "Buenos Aires", "Centro", -33.3350, -60.2253, "CD Rosario", 1, 1),
    ("Córdoba", "Córdoba", "Centro", -31.4201, -64.1888, "CD Córdoba", 0, 6),
    ("Villa María", "Córdoba", "Centro", -32.4075, -63.2402, "CD Córdoba", 2, 1.5),
    ("Río Cuarto", "Córdoba", "Centro", -33.1307, -64.3499, "CD Córdoba", 3, 1.5),
    ("Villa Carlos Paz", "Córdoba", "Centro", -31.4241, -64.4978, "CD Córdoba", 1, 1),
    ("San Francisco", "Córdoba", "Centro", -31.4280, -62.0827, "CD Córdoba", 3, 1),
    ("Mendoza", "Mendoza", "Cuyo", -32.8895, -68.8458, "CD Mendoza", 0, 4),
    ("San Rafael", "Mendoza", "Cuyo", -34.6177, -68.3301, "CD Mendoza", 3, 1),
    ("San Juan", "San Juan", "Cuyo", -31.5375, -68.5364, "CD Mendoza", 2, 2),
    ("San Luis", "San Luis", "Cuyo", -33.2950, -66.3356, "CD Mendoza", 4, 1),
    ("San Miguel de Tucumán", "Tucumán", "NOA", -26.8083, -65.2176, "CD Tucumán", 0, 3),
    ("Salta", "Salta", "NOA", -24.7821, -65.4232, "CD Tucumán", 4, 2.5),
    ("San Salvador de Jujuy", "Jujuy", "NOA", -24.1858, -65.2995, "CD Tucumán", 5, 1.5),
    ("Santiago del Estero", "Santiago del Estero", "NOA", -27.7951, -64.2615, "CD Tucumán", 3, 1.5),
    ("Catamarca", "Catamarca", "NOA", -28.4696, -65.7795, "CD Tucumán", 4, 1),
    ("La Rioja", "La Rioja", "NOA", -29.4131, -66.8558, "CD Tucumán", 6, 0.8),
    ("Resistencia", "Chaco", "NEA", -27.4606, -58.9839, "CD Resistencia", 0, 2),
    ("Corrientes", "Corrientes", "NEA", -27.4692, -58.8306, "CD Resistencia", 0.5, 2),
    ("Posadas", "Misiones", "NEA", -27.3671, -55.8961, "CD Resistencia", 5, 2),
    ("Oberá", "Misiones", "NEA", -27.4871, -55.1199, "CD Resistencia", 6, 0.6),
    ("Formosa", "Formosa", "NEA", -26.1775, -58.1781, "CD Resistencia", 3, 1),
    ("Neuquén", "Neuquén", "Patagonia", -38.9516, -68.0591, "CD Neuquén", 0, 3),
    ("Cipolletti", "Río Negro", "Patagonia", -38.9339, -67.9903, "CD Neuquén", 0.5, 1),
    ("General Roca", "Río Negro", "Patagonia", -39.0333, -67.5833, "CD Neuquén", 1, 0.8),
    ("San Carlos de Bariloche", "Río Negro", "Patagonia", -41.1335, -71.3103, "CD Neuquén", 8, 2.5),
    ("San Martín de los Andes", "Neuquén", "Patagonia", -40.1579, -71.3534, "CD Neuquén", 7, 0.6),
    ("Bahía Blanca", "Buenos Aires", "Centro", -38.7183, -62.2663, "CD Bahía Blanca", 0, 2.5),
    ("Santa Rosa", "La Pampa", "Centro", -36.6203, -64.2906, "CD Bahía Blanca", 5, 1),
    ("Viedma", "Río Negro", "Patagonia", -40.8135, -62.9967, "CD Bahía Blanca", 5, 0.6),
    ("Trelew", "Chubut", "Patagonia", -43.2489, -65.3051, "CD Bahía Blanca", 14, 1),
    ("Puerto Madryn", "Chubut", "Patagonia", -42.7692, -65.0385, "CD Bahía Blanca", 13, 1),
    ("Comodoro Rivadavia", "Chubut", "Patagonia", -45.8641, -67.4966, "CD Bahía Blanca", 22, 1.5),
    ("Río Gallegos", "Santa Cruz", "Patagonia", -51.6230, -69.2168, "CD Bahía Blanca", 36, 0.8),
    ("Ushuaia", "Tierra del Fuego", "Patagonia", -54.8019, -68.3030, "CD Bahía Blanca", 48, 0.8),
]
POR_NOMBRE = {l[0]: l for l in LOCALIDADES}
# De dónde despachan los vendedores: sus depósitos, casi todos en el AMBA.
ORIGENES = [(l, l[7]) for l in LOCALIDADES if l[2] == "AMBA" and l[0] != "La Plata"] + [
    (POR_NOMBRE["Rosario"], 8), (POR_NOMBRE["Córdoba"], 8), (POR_NOMBRE["Mendoza"], 4), (POR_NOMBRE["San Miguel de Tucumán"], 2),
    (POR_NOMBRE["La Plata"], 2)]

SLA = {  # horas prometidas (corridas, con el fin de semana adentro), por servicio y región de destino
    "Estándar": {"AMBA": 96, "Costa Atlántica": 120, "Centro": 120, "Cuyo": 144, "NOA": 144, "NEA": 144, "Patagonia": 168},
    "Prioritario": {"AMBA": 48, "Costa Atlántica": 72, "Centro": 72, "Cuyo": 96, "NOA": 96, "NEA": 96, "Patagonia": 120},
}
COSTO_BASE = {"AMBA": 5_800, "Costa Atlántica": 8_900, "Centro": 9_600, "Cuyo": 11_800, "NOA": 12_500, "NEA": 12_200,
              "Patagonia": 15_800}
POR_SERVICIO = {"Estándar": 1.0, "Prioritario": 1.45, "En el día": 1.6, "Retiro en sucursal": 0.82, "Logística inversa": 0.9}

# rubro: (peso medio en kg, desvío, valor declarado medio, peso entre los vendedores)
RUBROS = {
    "Indumentaria y calzado": (0.9, 0.5, 45_000, 22), "Electrónica": (2.2, 2.0, 180_000, 14),
    "Hogar y deco": (4.5, 4.0, 60_000, 10), "Cosmética y perfumería": (0.6, 0.3, 30_000, 9),
    "Farmacia": (0.5, 0.3, 22_000, 5), "Libros": (0.8, 0.4, 25_000, 4), "Autopartes": (3.5, 3.0, 70_000, 5),
    "Deportes": (2.0, 1.8, 65_000, 7), "Juguetes": (1.5, 1.2, 35_000, 6), "Alimentos y bebidas": (3.0, 2.0, 28_000, 4),
    "Documentación y tarjetas": (0.08, 0.03, 0, 0),
}
RUBROS_DE_VENTA = [k for k, v in RUBROS.items() if v[3]]

# ── Lo que anota el repartidor o el centro ──────────────────────────────────
# motivo → frases. Muchas formas de decir lo mismo, a propósito.
NOTAS = {
    "Destinatario ausente": [
        "Nadie atendió en el domicilio, se dejó aviso de visita", "Cliente ausente, se tocó timbre varias veces",
        "No había nadie en la casa", "No responde el timbre ni el celular", "El destinatario no se encontraba, se reprograma la visita",
        "Domicilio cerrado, sin moradores", "Se llamó al teléfono de contacto y no atendió", "Ausente, se deja aviso debajo de la puerta",
        "Nadie en la vivienda al momento de la entrega", "El cliente no estaba, el vecino dice que vuelve a la noche",
        "Toqué timbre y no salió nadie", "Casa sin gente, persiana baja"],
    "Domicilio incorrecto o incompleto": [
        "Falta el número de departamento", "La dirección no existe", "Numeración inexistente en la cuadra",
        "No se encuentra la calle en el barrio", "Dirección incompleta, no figura el piso ni el depto",
        "En el domicilio no conocen al destinatario", "La calle cambió de nombre y no figura en el mapa",
        "El número de puerta no coincide con el de la etiqueta", "Es un edificio y falta la unidad",
        "Domicilio en zona rural sin referencias"],
    "Rechazado por el destinatario": [
        "El destinatario no quiso recibir el paquete", "Rechazó el envío, dice que no hizo la compra",
        "No acepta el pedido porque llegó tarde", "Rechaza porque no es el producto que pidió", "Se negó a firmar la recepción",
        "El cliente canceló la compra y no lo recibe"],
    "Zona de acceso restringido": [
        "Barrio cerrado, la guardia no autorizó el ingreso", "Zona de riesgo, no se ingresa sin custodia",
        "No se pudo entrar al country, no figura en la lista de autorizados", "Calle de tierra intransitable para el utilitario",
        "Asentamiento sin acceso para el vehículo", "Edificio sin portero y nadie abre la puerta de calle"],
    "Comercio u oficina cerrada": [
        "Oficina cerrada al momento de la visita", "El local estaba cerrado", "Comercio sin atención por la tarde",
        "Empresa fuera del horario de recepción"],
    "Entrega solo al titular": [
        "La tarjeta solo se entrega al titular y no estaba", "Atendió un familiar pero la entrega es personal",
        "El titular no presentó el documento", "Documentación con entrega en mano, titular ausente"],
    "Condiciones climáticas": [
        "Nevada, ruta cortada, no se pudo llegar", "Calles con nieve y hielo, se suspende el reparto",
        "Camino de montaña cerrado por la nieve", "Temporal de nieve en la zona, se reprograma"],
    "Paquete dañado": [
        "La caja llegó aplastada", "Envoltorio roto, contenido a la vista", "Paquete mojado", "El producto llegó roto",
        "Caja golpeada en una esquina", "Bulto con la cinta abierta, se vuelve a embalar", "Se rompió el envase en el traslado",
        "Caja deformada, se revisa el contenido", "Paquete abollado al salir de la cinta", "Etiqueta despegada y caja rota"],
    "Demora en ruta": [
        "Demora en ruta por accidente en la autopista", "Corte de ruta por protesta, el camión quedó parado",
        "Falla mecánica del camión, se trasborda la carga", "Se atrasó la salida del troncal",
        "Control de carga en ruta, demora de varias horas", "Neumático pinchado del semirremolque"],
    "Paro de transporte": [
        "Paro de transporte, no salen camiones", "Sin reparto por medida de fuerza", "Operación detenida por el paro, se reprograma"],
    "Extravío": [
        "No se encuentra el bulto en el centro de distribución", "Paquete no escaneado en el ingreso, se busca",
        "Envío extraviado, se inicia el reclamo", "No aparece en el inventario del centro"],
    "Vencido el plazo de retiro": [
        "Vencido el plazo de retiro, vuelve al remitente", "Nadie lo retiró de la sucursal, se devuelve",
        "No se pudo entregar, se devuelve al vendedor"],
    "Devolución del comprador": [
        "El comprador devuelve por talle equivocado", "Devolución por arrepentimiento de compra",
        "Devuelve porque el producto vino fallado", "Cambio por otro color", "El producto no era lo que esperaba",
        "Llegó incompleto, lo devuelve"],
}
# La demora en un centro: por volumen (eventos comerciales) o por la falla de la clasificadora.
POR_VOLUMEN = ["Exceso de volumen por evento comercial, se procesa con demora", "Paquete pendiente de clasificar por saturación del centro",
               "Centro saturado, el envío sale en el próximo turno"]
POR_FALLA = ["Demora por falla operativa en el centro de distribución", "La clasificadora está fuera de servicio, se separa a mano",
             "Atraso en el procesamiento por falla de la cinta"]
# Lo que el repartidor agrega a veces. Pocas, para que los textos distintos no exploten.
AGREGADOS = [", segunda visita", ", se envió SMS al destinatario", ", se reprograma para mañana", ", se dejó constancia con foto"]
RECIBIO = {  # quién recibió → cómo lo anota el repartidor
    "Titular": ["Entregado en mano al titular", "Recibió el destinatario en persona", "Entregado al cliente, firmó la recepción"],
    "Familiar": ["Lo recibió un familiar", "Recibió la hija del destinatario", "Entregado a la pareja del titular"],
    "Portería": ["Recibió la portería del edificio", "Dejado en la recepción del edificio", "Entregado al encargado"],
    "Vecino autorizado": ["Se entregó al vecino con autorización", "Lo recibió la vecina de al lado, con permiso del cliente"],
    "Guardia de barrio cerrado": ["Entregado en la guardia del barrio cerrado", "Quedó en la garita del country"],
    "Depósito del remitente": ["Ingresó al depósito del vendedor", "Recibido en el depósito, control de devolución"],
}
# Lo que anota el chofer del troncal cuando la nieve corta la ruta a la cordillera.
POR_LA_NIEVE = ["Ruta cortada por nevada, el camión espera en el puesto de control", "Paso cerrado por la nieve, el troncal no puede avanzar",
                "Vialidad corta la ruta por hielo, se espera la apertura"]
DANADO_AL_ENTREGAR = ["Entregado con la caja dañada, el cliente deja constancia", "Se entregó con el embalaje roto, firmó con observaciones"]
DISPONIBLE = ["Disponible para retirar en sucursal", "Se avisó por SMS que puede retirarlo", "Queda en sucursal a disposición del cliente"]

# ── Lo que se nota (picos y anomalías) ──────────────────────────────────────
CYBERMONDAY = (date(2025, 11, 3), date(2025, 11, 5))
BLACK_FRIDAY = (date(2025, 11, 28), date(2025, 12, 1))
FIESTAS = (date(2025, 12, 9), date(2025, 12, 22))
TEMPORADA_EN_LA_COSTA = (date(2026, 1, 2), date(2026, 2, 15))
PARO = date(2026, 4, 9)
PARO_DESDE, PARO_HASTA = datetime(2026, 4, 9, tzinfo=ART), datetime(2026, 4, 10, tzinfo=ART)
HOT_SALE = (date(2026, 5, 11), date(2026, 5, 13))
CINTA_QUE_ROMPE = ("CD Rosario", datetime(2026, 6, 1, tzinfo=ART), datetime(2026, 6, 11, tzinfo=ART))
DIA_DEL_PADRE = (date(2026, 6, 15), date(2026, 6, 19))
NEVADAS = ({"San Carlos de Bariloche", "San Martín de los Andes"}, datetime(2026, 7, 13, tzinfo=ART), datetime(2026, 7, 17, tzinfo=ART))
DIA_DEL_NINO = (date(2026, 8, 10), date(2026, 8, 14))
FALLA_DE_LA_CLASIFICADORA = ("CD Córdoba", datetime(2026, 8, 19, 5, tzinfo=ART), datetime(2026, 8, 21, 12, tzinfo=ART))
# Los centros se saturan después de los eventos comerciales: (desde, hasta, intensidad)
SATURACION = [(datetime(2025, 11, 3, 12, tzinfo=ART), datetime(2025, 11, 11, tzinfo=ART), 1.0),
              (datetime(2025, 11, 28, 12, tzinfo=ART), datetime(2025, 12, 3, tzinfo=ART), 0.5),
              (datetime(2025, 12, 12, tzinfo=ART), datetime(2025, 12, 23, tzinfo=ART), 0.35),
              (datetime(2026, 5, 11, 12, tzinfo=ART), datetime(2026, 5, 18, tzinfo=ART), 0.9)]
FERIADOS = {date(2025, 12, 25), date(2026, 1, 1)}
MEDIO_DIA = {date(2025, 12, 24), date(2025, 12, 31)}


def _en(x, rango) -> bool:
    return rango[0] <= x <= rango[1]


def _h(horas: float) -> timedelta:
    return timedelta(hours=horas)


def _cerca(lat: float, lon: float, r: random.Random, radio: float) -> tuple[float, float]:
    return round(lat + r.uniform(-radio, radio), 5), round(lon + r.uniform(-radio, radio), 5)


def _volumen(d: date, desde: date, hasta: date, por_dia: int, r: random.Random) -> int:
    v = por_dia * (0.9 + 0.2 * (d - desde).days / max(1, (hasta - desde).days))       # el negocio crece
    v *= {0: 1.25, 1: 1.15, 2: 1.05, 3: 1.0, 4: 0.95, 5: 0.3, 6: 0.04}[d.weekday()]
    if d.month == 1:
        v *= 0.85                                                                       # vacaciones
    if _en(d, CYBERMONDAY):
        v *= 2.6
    elif _en(d, BLACK_FRIDAY):
        v *= 1.7
    elif _en(d, FIESTAS):
        v *= 1.3 + 0.4 * (d - FIESTAS[0]).days / (FIESTAS[1] - FIESTAS[0]).days
    elif _en(d, HOT_SALE):
        v *= 2.4
    elif _en(d, DIA_DEL_PADRE):
        v *= 1.15
    elif _en(d, DIA_DEL_NINO):
        v *= 1.35
    if d in FERIADOS:
        v *= 0.03
    elif d in MEDIO_DIA:
        v *= 0.45
    elif d == PARO:
        v *= 0.15                       # sin colectas: solo lo que se despacha en sucursal
    return max(0, int(r.gauss(v, v * 0.07)))


def _cuentas(r: random.Random, n: int = 1_800) -> list[dict]:
    """Los clientes con cuenta: pocos vendedores grandes mandan mucho (Zipf)."""
    cuentas = []
    for i in range(n):
        seg = r.choices(["E-commerce", "Pyme", "Corporativo"], [62, 28, 10])[0]
        rubro = (r.choices(["Documentación y tarjetas", "Farmacia", "Electrónica"], [70, 20, 10])[0] if seg == "Corporativo"
                 else r.choices(RUBROS_DE_VENTA, [RUBROS[k][3] for k in RUBROS_DE_VENTA])[0])
        cuentas.append({"id": f"CTA-{100_000 + i * 37 + r.randint(0, 36):06d}", "segmento": seg, "rubro": rubro,
                        "origen": r.choices([o for o, _ in ORIGENES], [p for _, p in ORIGENES])[0],
                        "peso": (1 / (1 + i)) ** 0.85 * (2 if seg == "Corporativo" else 1), "chica": i > 300})
    return cuentas


def _impulso(c: dict, d: date) -> float:
    """Cuánto más manda una cuenta ese día (los eventos comerciales son del e-commerce)."""
    f = 1.0
    venta = c["segmento"] == "E-commerce"
    if venta and _en(d, CYBERMONDAY):
        f *= 2.2
    if venta and (_en(d, BLACK_FRIDAY) or _en(d, HOT_SALE)):
        f *= 1.8
    if c["rubro"] in ("Juguetes", "Electrónica") and _en(d, FIESTAS):
        f *= 1.5
    if c["rubro"] in ("Deportes", "Electrónica") and _en(d, DIA_DEL_PADRE):
        f *= 1.6
    if c["rubro"] == "Juguetes" and _en(d, DIA_DEL_NINO):
        f *= 3.0
    if c["segmento"] == "Corporativo" and d.weekday() >= 5:
        f *= 0.0
    return f


def _envio(r: random.Random, d: date, cuentas: list[dict], acumulado: list[float], destinos: list[tuple], pesos_destino: list[float]) -> dict:
    """Un envío nuevo: quién lo manda, a dónde, con qué servicio y cuándo se admite."""
    particular = r.random() < 0.08 or d == PARO and r.random() < 0.6
    c = None if particular else cuentas[bisect.bisect(acumulado, r.random() * acumulado[-1])]
    seg = "Particular" if particular else c["segmento"]
    rubro = r.choice(RUBROS_DE_VENTA) if particular else c["rubro"]
    if particular:
        servicio = r.choices(["Estándar", "Retiro en sucursal", "Prioritario"], [55, 35, 10])[0]
        canal = "Sucursal"
    elif seg == "Corporativo":
        servicio = r.choices(["Prioritario", "Estándar"], [50, 50])[0]
        canal = "Colecta en depósito"
    elif seg == "Pyme":
        servicio = r.choices(["Estándar", "Prioritario", "Retiro en sucursal"], [65, 15, 20])[0]
        canal = r.choices(["Colecta en depósito", "Sucursal"], [45, 55])[0]
    else:
        servicio = r.choices(["Estándar", "Prioritario", "En el día", "Retiro en sucursal", "Logística inversa"],
                             [55, 18, 7, 12, 8])[0]
        canal = "Punto de despacho" if c["chica"] and r.random() < 0.6 else "Colecta en depósito"
    destino = r.choices(destinos, pesos_destino)[0]
    origen = r.choices([o for o, _ in ORIGENES], [p for _, p in ORIGENES])[0] if particular else c["origen"]
    if servicio == "Logística inversa":            # el comprador le devuelve al vendedor
        origen, destino = destino, c["origen"]
        canal = r.choices(["Punto de despacho", "Sucursal"], [60, 40])[0]
    if servicio == "En el día" and not (origen[2] == destino[2] == "AMBA"):
        servicio = "Prioritario"
    if d.weekday() >= 5 or d == PARO:              # sin colectas
        canal = "Sucursal" if canal == "Colecta en depósito" else canal
    if d.weekday() == 6:                           # el domingo solo reciben los puntos de despacho
        canal = "Punto de despacho"
    if canal == "Sucursal" and servicio == "En el día":
        servicio = "Prioritario"
    # Cuándo: la colecta pasa a la tarde (la del día, a la mañana); la sucursal abre 9 a 19.
    if servicio == "En el día":
        hora = r.uniform(8, 12.5)
    elif canal == "Colecta en depósito":
        hora = r.uniform(13, 19)
    elif d.weekday() == 5:
        hora = r.uniform(9, 13.5)
    else:
        hora = r.uniform(9, 19.5) if canal == "Sucursal" else r.uniform(10, 20)
    if d in MEDIO_DIA:
        hora = min(hora, r.uniform(9, 13))
    t0 = datetime(d.year, d.month, d.day, tzinfo=ART) + _h(hora)
    region = destino[2]
    media, desvio, valor, _ = RUBROS[rubro]
    peso = round(max(0.05, r.gauss(media, desvio)), 2)
    bultos = 1 if rubro != "Hogar y deco" or r.random() < 0.7 else r.randint(2, 3)
    costo = COSTO_BASE[region] * POR_SERVICIO[servicio] + max(0.0, peso - 1) * 1_100 * COSTO_BASE[region] / COSTO_BASE["AMBA"]
    costo *= 0.7 if seg == "Corporativo" else 0.85 if seg == "E-commerce" and not c["chica"] else 1.0
    sla = 12 if servicio == "En el día" else 120 if servicio == "Logística inversa" else SLA[
        "Prioritario" if servicio == "Prioritario" else "Estándar"][region]
    tel_area = CENTROS[destino[5]][5]
    return {
        "t0": t0, "servicio": servicio, "canal": canal, "segmento": seg, "cuenta": "" if particular else c["id"], "rubro": rubro,
        "origen": origen, "destino": destino, "peso": peso, "bultos": bultos, "costo": int(round(costo / 10) * 10),
        "valor": "" if not valor else int(round(max(valor * 0.2, r.gauss(valor, valor * 0.5)) / 100) * 100), "sla": sla,
        "dni": "" if servicio == "Logística inversa" else str(r.randint(14_000_000, 46_999_999)),
        "tel": "" if servicio == "Logística inversa" else (
            f"11-{r.randint(4000, 6999)}-{r.randint(1000, 9999)}" if tel_area == "11"
            else f"{tel_area}-{r.randint(400, 699)}-{r.randint(1000, 9999)}"),
    }


# ── El recorrido ────────────────────────────────────────────────────────────
def _salida_troncal(t: datetime, r: random.Random) -> tuple[datetime, bool]:
    """El troncal sale de noche (22 a 1), salvo el sábado, los feriados y el paro."""
    d, paro = t.date(), False
    while True:
        salida = datetime(d.year, d.month, d.day, 22, tzinfo=ART) + _h(r.uniform(0, 3))
        if salida >= t + _h(1) and d.weekday() != 5 and d not in FERIADOS:
            if PARO_DESDE <= salida < PARO_HASTA:
                paro = True
            else:
                return salida, paro
        d += timedelta(days=1)


def _reparto(t: datetime, r: random.Random, prioritario: bool = False) -> tuple[datetime, bool]:
    """Sale a reparto a la mañana siguiente (o esa misma, si llegó de madrugada),
    de lunes a sábado; el sábado con media dotación, que sale con lo prioritario."""
    d, paro = (t.date() if t.hour < 7 else t.date() + timedelta(days=1)), False
    while True:
        if d == PARO:
            paro = True
        elif d.weekday() != 6 and d not in FERIADOS and not (d.weekday() == 5 and not prioritario and r.random() < 0.5):
            return datetime(d.year, d.month, d.day, 7, 30, tzinfo=ART) + _h(r.uniform(0, 2.5)), paro
        d += timedelta(days=1)


def _en_horario_de_sucursal(t: datetime, r: random.Random) -> datetime:
    while t.weekday() == 6 or t.date() in FERIADOS or t.date() == PARO or not 9 <= t.hour < 19:
        t = datetime(t.year, t.month, t.day, tzinfo=ART) + timedelta(days=1 if t.hour >= 9 else 0) + _h(r.uniform(9, 18.5))
    return t


def _procesar(r: random.Random, t: datetime, centro: str, servicio: str) -> tuple[datetime, list]:
    """Cuándo queda listo en el centro lo que ingresó en `t`, y las incidencias del camino."""
    base = {"En el día": (0.4, 1.0), "Prioritario": (1, 4)}.get(servicio, (3, 14))
    listo = t + _h(r.uniform(*base))
    extra = []
    if servicio not in ("En el día", "Prioritario") and r.random() < 0.12:
        listo += _h(24)                                         # queda para el turno del día siguiente
    if centro == "CD Buenos Aires":
        for desde, hasta, intensidad in SATURACION:
            if desde <= t <= hasta:
                listo += _h(intensidad * r.uniform(6, 30))
                if r.random() < 0.12:
                    extra.append((t + _h(r.uniform(0.5, 4)), "Incidencia", "Demora en el centro de distribución", r.choice(POR_VOLUMEN)))
    nombre, desde, hasta = FALLA_DE_LA_CLASIFICADORA
    if centro == nombre and desde <= listo <= hasta:
        cuando = max(t, desde) + _h(r.uniform(0.3, 3))
        listo = hasta + _h(r.uniform(1, 10))
        if r.random() < 0.6 and cuando < hasta:
            extra.append((cuando, "Incidencia", "Demora en el centro de distribución", r.choice(POR_FALLA)))
    nombre, desde, hasta = CINTA_QUE_ROMPE
    if r.random() < (0.3 if centro == nombre and desde <= t < hasta else 0.012):
        extra.append((t + _h(r.uniform(0.2, 3)), "Incidencia", "Paquete dañado", r.choice(NOTAS["Paquete dañado"])))
    return listo, extra


def _recorrido(r: random.Random, e: dict) -> list[dict]:
    ev: list[dict] = []
    o, dst = e["origen"], e["destino"]
    co, cd = o[5], dst[5]
    cabe = dst[1] == "CABA"

    def fila(ts, evento, centro, lugar, **kw):
        ev.append({"ts": ts, "evento": evento, "centro": centro, "lugar": lugar, **kw})

    def en_centro(c):
        return _cerca(CENTROS[c][0], CENTROS[c][1], r, 0.003)

    def en_destino():
        return _cerca(dst[3], dst[4], r, 0.012 if dst[2] == "AMBA" else 0.03)

    def final(ts):
        return {"horas_desde_admision": round((ts - e["t0"]).total_seconds() / 3600, 1)}

    adm = {"bultos": e["bultos"], "peso_kg": e["peso"], "valor_declarado_ars": e["valor"], "costo_envio_ars": e["costo"],
           "sla_horas": e["sla"], "vehiculo": "Utilitario de colecta" if e["canal"] == "Colecta en depósito" else ""}
    if e["servicio"] == "Logística inversa":
        adm.update(motivo="Devolución del comprador", observacion=r.choice(NOTAS["Devolución del comprador"]))
    fila(e["t0"], "Admitido", co, _cerca(o[3], o[4], r, 0.012 if o[2] == "AMBA" else 0.03), **adm)

    def incidencias(extra, centro):
        for ts, evento, motivo, nota in extra:
            fila(ts, evento, centro, en_centro(centro), motivo=motivo, observacion=nota)
        return any(m == "Paquete dañado" for _, _, m, _ in extra)

    # Al centro de origen y, si el destino es de otro centro, el troncal.
    t = e["t0"] + _h(r.uniform(1, 2) if e["servicio"] == "En el día" else r.uniform(2, 6))
    if t.date() == PARO and e["canal"] == "Colecta en depósito":
        t = PARO_HASTA + _h(r.uniform(7, 12))
    fila(t, "Ingreso a centro de distribución", co, en_centro(co))
    if r.random() < 0.0025:                                     # se pierde en el centro
        ts = t + _h(r.uniform(24, 96))
        fila(ts, "Extraviado", co, en_centro(co), motivo="Extravío", observacion=r.choice(NOTAS["Extravío"]), **final(ts))
        return ev
    t, extra = _procesar(r, t, co, e["servicio"])
    danado = incidencias(extra, co)
    if cd != co:
        salida, paro = _salida_troncal(t, r)
        if paro and r.random() < 0.3:
            fila(max(t, PARO_DESDE + _h(r.uniform(6, 11))), "Incidencia", co, en_centro(co), motivo="Paro de transporte",
                 observacion=r.choice(NOTAS["Paro de transporte"]))
        horas_o, horas_d = CENTROS[co][2], CENTROS[cd][2]
        horas = (horas_d if co == "CD Buenos Aires" else horas_o if cd == "CD Buenos Aires" else (horas_o + horas_d) * 0.75)
        horas *= r.lognormvariate(0, 0.08)
        fila(salida, "En viaje troncal", co, en_centro(co), vehiculo=r.choices(["Camión troncal", "Semirremolque"], [70, 30])[0])
        if r.random() < 0.008:                                  # algo pasa en la ruta
            avance = r.uniform(0.3, 0.7)
            lat = CENTROS[co][0] + (CENTROS[cd][0] - CENTROS[co][0]) * avance
            lon = CENTROS[co][1] + (CENTROS[cd][1] - CENTROS[co][1]) * avance
            fila(salida + _h(horas * avance), "Incidencia", co, _cerca(lat, lon, r, 0.02), motivo="Demora en ruta",
                 observacion=r.choice(NOTAS["Demora en ruta"]))
            horas += r.uniform(10, 30)
        llegada = salida + _h(horas)
        if cd == "CD Neuquén" and NEVADAS[1] <= llegada < NEVADAS[2]:
            if r.random() < 0.6:
                lat = CENTROS[co][0] + (CENTROS[cd][0] - CENTROS[co][0]) * 0.85
                lon = CENTROS[co][1] + (CENTROS[cd][1] - CENTROS[co][1]) * 0.85
                fila(llegada - _h(r.uniform(1, 4)), "Incidencia", co, _cerca(lat, lon, r, 0.02), motivo="Condiciones climáticas",
                     observacion=r.choice(POR_LA_NIEVE))
            llegada = NEVADAS[2] + _h(r.uniform(2, 14))
        fila(llegada, "Ingreso a centro de distribución", cd, en_centro(cd))
        t, extra = _procesar(r, llegada, cd, e["servicio"])
        danado = incidencias(extra, cd) or danado
    # Hasta la sucursal de la localidad (las lejanas, horas de camión más). Con las
    # nevadas, la cordillera queda cortada.
    t += _h(dst[6])
    if dst[0] in NEVADAS[0] and NEVADAS[1] <= t < NEVADAS[2]:
        t += _h(r.uniform(30, 60))

    cumple = lambda ts: "Sí" if (ts - e["t0"]).total_seconds() / 3600 <= e["sla"] else "No"  # noqa: E731

    def a_sucursal(ts, intento_previo=False):
        ts = _en_horario_de_sucursal(ts, r)
        kw = final(ts) if not intento_previo else {}
        if not intento_previo:
            kw["cumple_sla"] = cumple(ts)
        fila(ts, "Disponible en sucursal", cd, en_destino(), observacion=r.choice(DISPONIBLE) if r.random() < 0.4 else "", **kw)
        if r.random() < (0.85 if not intento_previo else 0.7):
            ts = _en_horario_de_sucursal(ts + _h(r.uniform(3, 110)), r)
            fila(ts, "Retirado en sucursal", cd, en_destino())
        else:
            ts = ts + _h(r.uniform(160, 240))
            fila(ts, "Devuelto al remitente", cd, en_centro(cd), motivo="Vencido el plazo de retiro",
                 observacion=r.choice(NOTAS["Vencido el plazo de retiro"]), **final(ts))

    if e["servicio"] == "Retiro en sucursal":
        a_sucursal(t)
        return ev

    # Reparto: hasta dos visitas; después, a la sucursal.
    codigo, cuantos = CENTROS[cd][3], CENTROS[cd][4]
    repartidor = f"REP-{codigo}-{r.randint(1, cuantos):03d}"
    if e["servicio"] == "Logística inversa" or e["rubro"] == "Documentación y tarjetas":
        vehiculo = "Moto" if e["rubro"] == "Documentación y tarjetas" and r.random() < 0.7 else "Utilitario"
    else:
        vehiculo = r.choices(["Moto", "Bicicleta", "Utilitario"], [35, 10, 55])[0] if cabe else (
            r.choices(["Utilitario", "Moto"], [75, 25] if dst[2] == "AMBA" else [80, 20])[0])
    for intento in (1, 2):
        if e["servicio"] == "En el día" and intento == 1:
            salida, paro_aca = t + _h(r.uniform(0.5, 1.5)), False
        else:
            salida, paro_aca = _reparto(t, r, prioritario=e["servicio"] == "Prioritario")
        if paro_aca and r.random() < 0.3:
            fila(max(t, PARO_DESDE + _h(r.uniform(7, 12))), "Incidencia", cd, en_centro(cd), motivo="Paro de transporte",
                 observacion=r.choice(NOTAS["Paro de transporte"]))
        comun = {"vehiculo": vehiculo, "repartidor_id": repartidor, "intento": intento}
        fila(salida, "En distribución", cd, en_centro(cd) if dst[6] == 0 else en_destino(), **comun)
        ts = salida + _h(r.uniform(0.8, 9))
        if ts.hour >= 21 or (ts.hour == 20 and ts.minute > 30):
            ts = datetime(ts.year, ts.month, ts.day, 20, tzinfo=ART) + _h(r.uniform(0, 0.5))
        # ¿Se pudo entregar?
        nevada = dst[0] in NEVADAS[0] and NEVADAS[1] <= ts < NEVADAS[2]
        p = 0.02 if e["servicio"] == "Logística inversa" else 0.22 if e["rubro"] == "Documentación y tarjetas" else 0.08
        p *= 1.25 if dst[2] == "AMBA" and not cabe else 1.0
        p *= 1.3 if ts.weekday() == 5 else 1.0
        p *= 1.15 if _en(ts.date(), FIESTAS) else 1.0
        p *= 0.8 if intento == 2 else 1.0
        if nevada or r.random() < p:
            if nevada and r.random() < 0.8:
                motivo = "Condiciones climáticas"
            elif e["rubro"] == "Documentación y tarjetas":
                motivo = r.choices(["Entrega solo al titular", "Destinatario ausente", "Domicilio incorrecto o incompleto"], [60, 30, 10])[0]
            else:
                motivo = r.choices(["Destinatario ausente", "Domicilio incorrecto o incompleto", "Rechazado por el destinatario",
                                    "Zona de acceso restringido", "Comercio u oficina cerrada"],
                                   [55, 16, 8, 10 * (2 if dst[2] == "AMBA" and not cabe else 1), 4])[0]
            nota = r.choice(NOTAS[motivo])
            if motivo not in ("Condiciones climáticas", "Rechazado por el destinatario") and r.random() < 0.3:
                nota += r.choice(AGREGADOS[1:] if intento == 1 else AGREGADOS)
            fila(ts, "Visita fallida", cd, en_destino(), motivo=motivo, observacion=nota, **comun)
            if motivo == "Rechazado por el destinatario":
                ts = ts + _h(r.uniform(20, 60))
                fila(ts, "Devuelto al remitente", cd, en_centro(cd), motivo=motivo,
                     observacion="Rechazado por el destinatario, retorna al origen", **final(ts))
                return ev
            t = ts + _h(1)
            continue
        recibio = ("Depósito del remitente" if e["servicio"] == "Logística inversa"
                   else "Titular" if e["rubro"] == "Documentación y tarjetas" and r.random() < 0.9
                   else r.choices(["Titular", "Familiar", "Portería", "Vecino autorizado", "Guardia de barrio cerrado"],
                                  [52, 20, 18 if cabe else 6, 5, 9 if dst[2] == "AMBA" and not cabe else 1])[0])
        nota = r.choice(DANADO_AL_ENTREGAR) if danado else r.choice(RECIBIO[recibio]) if r.random() < 0.25 else ""
        a_tiempo = cumple(ts)
        calificacion = ""
        if e["servicio"] != "Logística inversa" and r.random() < (0.7 if danado else 0.35):
            nota_media = 1.4 if danado else 4.5 if a_tiempo == "Sí" else 2.9
            calificacion = int(min(5, max(1, round(r.gauss(nota_media, 0.7)))))
        fila(ts, "Entregado", cd, en_destino(), recibio=recibio, observacion=nota, cumple_sla=a_tiempo,
             calificacion=calificacion, **comun, **final(ts))
        return ev
    a_sucursal(t, intento_previo=True)
    return ev


def generar(desde: date, hasta: date, por_dia: int, semilla: int = 20261013) -> tuple[list[dict], Counter]:
    r = random.Random(semilla)
    cuentas = _cuentas(r)
    envios: list[dict] = []
    d = desde - timedelta(days=12)                    # para que el primer día ya haya envíos en camino
    while d <= hasta:
        acumulado = list(itertools.accumulate(c["peso"] * _impulso(c, d) for c in cuentas))
        pesos_destino = [l[7] * (2.5 if l[2] == "Costa Atlántica" and _en(d, TEMPORADA_EN_LA_COSTA) else 1) for l in LOCALIDADES]
        for _ in range(_volumen(d, desde, hasta, por_dia, r)):
            envios.append(_envio(r, d, cuentas, acumulado, LOCALIDADES, pesos_destino))
        d += timedelta(days=1)
    envios.sort(key=lambda e: e["t0"])
    inicio = datetime(desde.year, desde.month, desde.day, tzinfo=ART)
    fin = datetime(hasta.year, hasta.month, hasta.day, tzinfo=ART) + timedelta(days=1)
    filas: list[dict] = []
    eventos: Counter = Counter()
    for i, e in enumerate(envios):
        nro = f"ENV-{e['t0']:%y%m}-{i + 1:06d}"
        for x in _recorrido(r, e):
            if not inicio <= x["ts"] < fin:
                continue
            eventos[x["evento"]] += 1
            o, dst = e["origen"], e["destino"]
            lat, lon = x["lugar"]
            fila = {
                "fecha_hora": x["ts"].isoformat(timespec="seconds"), "nro_envio": nro, "evento": x["evento"],
                "tipo_servicio": e["servicio"], "canal_admision": e["canal"], "segmento_cliente": e["segmento"],
                "cuenta_remitente": e["cuenta"], "rubro": e["rubro"], "origen_localidad": o[0], "origen_provincia": o[1],
                "destino_localidad": dst[0], "destino_provincia": dst[1], "region_destino": dst[2],
                "centro_distribucion": x["centro"], "latitud": lat, "longitud": lon,
                "dni_destinatario": e["dni"], "telefono_destinatario": e["tel"],
            }
            fila.update({k: v for k, v in x.items() if k not in ("ts", "evento", "centro", "lugar")})
            filas.append({c: fila.get(c, "") for c in COLUMNAS})
    filas.sort(key=lambda f: f["fecha_hora"])
    return filas, eventos


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--formato", choices=["json", "csv"], default="json")
    p.add_argument("--desde", default="2025-10-13")
    p.add_argument("--hasta", default="2026-10-12")
    # En JSON, menos envíos por día: repite los nombres de los campos y el Builder acepta hasta 50 MB.
    p.add_argument("--por-dia", type=int, default=None, help="envíos por día (55 en CSV, 45 en JSON)")
    a = p.parse_args()
    por_dia = a.por_dia or (45 if a.formato == "json" else 55)
    filas, eventos = generar(date.fromisoformat(a.desde), date.fromisoformat(a.hasta), por_dia)
    salida = SALIDA.with_suffix(".ndjson" if a.formato == "json" else ".csv")
    salida.parent.mkdir(exist_ok=True)
    with salida.open("w", encoding="utf-8", newline="") as f:
        if a.formato == "json":
            f.writelines(a_json(fila) + "\n" for fila in filas)
        else:
            w = csv.writer(f)
            w.writerow(COLUMNAS)
            w.writerows([fila[c] for c in COLUMNAS] for fila in filas)
    envios = len({f["nro_envio"] for f in filas})
    print(f"{len(filas):,} eventos de {envios:,} envíos -> {salida} ({salida.stat().st_size / 1e6:.1f} MB)")
    print("  " + " · ".join(f"{k}: {v:,}" for k, v in eventos.most_common()))


if __name__ == "__main__":
    main()
