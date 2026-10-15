"""
build_retail_dataset.py
=======================

Dataset sintético de los pedidos omnicanal de una cadena argentina de
electrodomésticos y tecnología, con sucursales en todo el país, tienda web,
app, televenta y marketplace (vendedores externos), y crédito propio. Cada fila
es un evento del pedido: cada vez que cambia de estado.

Por qué así: en el retail de electro y tecnología el e-commerce, la venta en
sucursal y el marketplace suelen correr sobre una plataforma de comercio
(VTEX es la más usada en la región). Su gestor de pedidos (OMS) avisa cada
cambio de estado de un pedido con un JSON —por un webhook o por su cola de
eventos— y la integración del retailer lo completa con el detalle del pedido y
lo deja en una cola o en un archivo. `pedido.estado_oms` trae el estado como lo
nombra ese OMS (`payment-pending`, `payment-approved`, `handling`, `invoiced`,
`canceled`…); la entrega y la posventa no son estados del OMS y van vacíos.

Todo es inventado (sin datos de nadie): clientes, DNI, correos, teléfonos y
vendedores salen de un generador con semilla fija. Lo que trae, para que la
demo cuente algo:

- el comentario del cliente en la posventa (reclamos, devoluciones, cancelaciones)
  en texto libre, con muchas formas de decir lo mismo → búsqueda por significado
  («llegó roto» / «la puerta vino abollada», «me cobraron dos veces»);
- volumen con ritmo diario y semanal, y los picos del retail: CyberMonday, Black
  Friday, Navidad, Hot Sale, Día del Padre, el aguinaldo y la previa del Mundial
  (televisores); y la temporada de cada rubro (aires y ventiladores en verano,
  estufas en invierno) → pronósticos;
- eventos que se notan → detección de anomalías: una caída del procesador de
  pagos, un ataque de fraude con tarjetas una madrugada, el sobrestock vendido de
  un televisor en el Hot Sale (cancelaciones sin stock), una ola de calor y
  demoras de entrega en el NEA;
- coordenadas del destino → mapa; DNI, correo y teléfono → enmascarado; el
  cliente → perfil por entidad; total, cuotas y días de demora → medidas.

Formato: JSON por línea (un evento por línea, con el pedido, el cliente, el
destino, el producto, el pago, la entrega y la posventa anidados).

Uso:   py build_retail_dataset.py [--desde 2025-10-15] [--hasta 2026-10-14] [--por-dia N]
Sale:  datasets/pedidos-omnicanal.ndjson
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from build_logistica_dataset import LOCALIDADES

ART = timezone(timedelta(hours=-3))
SALIDA = Path(__file__).parent / "datasets" / "pedidos-omnicanal.ndjson"

# columna → dónde va en el JSON
ANIDADO = {
    "fecha_hora": ("timestamp",), "evento": ("evento",),
    "nro_pedido": ("pedido", "id"), "estado_oms": ("pedido", "estado_oms"), "canal": ("pedido", "canal"),
    "vendedor": ("pedido", "vendedor"), "campania": ("pedido", "campania"), "seguro": ("pedido", "seguro_extendido"),
    "cliente_id": ("cliente", "id"), "tipo_cliente": ("cliente", "tipo"), "dni": ("cliente", "dni"),
    "email": ("cliente", "email"), "telefono": ("cliente", "telefono"),
    "localidad": ("destino", "localidad"), "provincia": ("destino", "provincia"), "region": ("destino", "region"),
    "lat": ("destino", "lat"), "lon": ("destino", "lon"),
    "sku": ("producto", "sku"), "producto": ("producto", "nombre"), "categoria": ("producto", "categoria"),
    "marca": ("producto", "marca"), "cantidad": ("producto", "cantidad"), "precio": ("producto", "precio_unitario_ars"),
    "total": ("importe", "total_ars"), "envio": ("importe", "envio_ars"),
    "medio": ("pago", "medio"), "tarjeta": ("pago", "tarjeta"), "cuotas": ("pago", "cuotas"),
    "interes": ("pago", "interes"), "estado_pago": ("pago", "estado"), "motivo_rechazo": ("pago", "motivo_rechazo"),
    "tipo_entrega": ("entrega", "tipo"), "sucursal": ("entrega", "sucursal"), "transportista": ("entrega", "transportista"),
    "fecha_prometida": ("entrega", "fecha_prometida"), "dias_demora": ("entrega", "dias_de_demora"),
    "motivo": ("posventa", "motivo"), "comentario": ("posventa", "comentario"), "calificacion": ("posventa", "calificacion"),
}


def a_json(fila: dict) -> str:
    evento: dict = {}
    for columna, ruta in ANIDADO.items():
        valor = fila.get(columna, "")
        if valor == "" or valor is None:
            continue
        *padres, hoja = ruta
        destino = evento
        for padre in padres:
            destino = destino.setdefault(padre, {})
        destino[hoja] = valor
    return json.dumps(evento, ensure_ascii=False, separators=(",", ":"))


# ── Qué se vende ────────────────────────────────────────────────────────────
# (sku, nombre, categoría, marca, precio en ARS, peso en las ventas, temporada, voluminoso)
PRODUCTOS = [
    ("TV-32-HD", 'Smart TV 32" HD', "Televisores", "Noblex", 320_000, 6, "tv", False),
    ("TV-43-FHD", 'Smart TV 43" Full HD', "Televisores", "Samsung", 520_000, 7, "tv", False),
    ("TV-50-4K", 'Smart TV 50" 4K', "Televisores", "LG", 760_000, 6, "tv", False),
    ("TV-55-4K", 'Smart TV 55" 4K', "Televisores", "Samsung", 920_000, 5, "tv", True),
    ("TV-65-4K", 'Smart TV 65" 4K', "Televisores", "LG", 1_450_000, 2, "tv", True),
    ("CEL-MOTO-128", "Celular Moto G 128 GB", "Celulares", "Motorola", 420_000, 9, "", False),
    ("CEL-GAL-A128", "Celular Galaxy A 128 GB", "Celulares", "Samsung", 480_000, 9, "", False),
    ("CEL-GAL-S256", "Celular Galaxy S 256 GB", "Celulares", "Samsung", 1_650_000, 3, "", False),
    ("CEL-REDMI-128", "Celular Redmi Note 128 GB", "Celulares", "Xiaomi", 390_000, 5, "", False),
    ("NB-15-I5", 'Notebook 15" Core i5 8 GB', "Notebooks", "Lenovo", 980_000, 4, "escuela", False),
    ("NB-14-R5", 'Notebook 14" Ryzen 5 16 GB', "Notebooks", "HP", 1_150_000, 3, "escuela", False),
    ("TAB-10-64", 'Tablet 10" 64 GB', "Notebooks", "Samsung", 390_000, 3, "escuela", False),
    ("HEL-300", "Heladera con freezer 300 L", "Heladeras", "Whirlpool", 1_150_000, 4, "", True),
    ("HEL-NF-400", "Heladera No Frost 400 L", "Heladeras", "Samsung", 1_750_000, 2, "", True),
    ("LAV-8KG", "Lavarropas automático 8 kg", "Lavarropas", "Drean", 850_000, 4, "", True),
    ("LAVSEC-9KG", "Lavasecarropas 9 kg", "Lavarropas", "LG", 1_550_000, 1.5, "", True),
    ("SEC-CENTR", "Secarropas centrífugo", "Lavarropas", "Drean", 230_000, 1.5, "", False),
    ("AA-3000", "Aire acondicionado split 3000 frigorías", "Climatización", "BGH", 1_100_000, 2.5, "calor", True),
    ("AA-4500", "Aire acondicionado split 4500 frigorías", "Climatización", "Surrey", 1_500_000, 1.2, "calor", True),
    ("VENT-20", 'Ventilador de pie 20"', "Climatización", "Liliana", 85_000, 2.5, "calor", False),
    ("CALOV-2000", "Caloventor 2000 W", "Climatización", "Liliana", 60_000, 2, "frio", False),
    ("EST-GAS-5000", "Estufa a gas 5000 kcal", "Climatización", "Eskabe", 320_000, 1.5, "frio", True),
    ("CALEF-PANEL", "Calefactor eléctrico de panel", "Climatización", "Peabody", 120_000, 1.5, "frio", False),
    ("MICRO-20L", "Microondas 20 L", "Pequeños electrodomésticos", "BGH", 230_000, 4, "", False),
    ("PAVA-17", "Pava eléctrica 1,7 L", "Pequeños electrodomésticos", "Philips", 48_000, 5, "", False),
    ("FREID-4L", "Freidora de aire 4 L", "Pequeños electrodomésticos", "Philco", 160_000, 5, "", False),
    ("CAFE-CAPS", "Cafetera de cápsulas", "Pequeños electrodomésticos", "Moulinex", 140_000, 2, "", False),
    ("ASP-ROBOT", "Aspiradora robot", "Pequeños electrodomésticos", "Xiaomi", 380_000, 1.5, "", False),
    ("AUR-BT", "Auriculares inalámbricos", "Audio", "JBL", 75_000, 5, "", False),
    ("PARL-BT", "Parlante portátil Bluetooth", "Audio", "JBL", 120_000, 3, "", False),
    ("CONSOLA-5", "Consola de videojuegos", "Gaming", "Sony", 1_100_000, 1.5, "fiestas", False),
    ("COC-4H", "Cocina a gas 4 hornallas", "Cocinas", "Longvie", 620_000, 2, "", True),
    ("COLCH-2P", "Colchón de resortes 2 plazas", "Hogar", "Piero", 520_000, 2, "", True),
]

# ── Dónde ───────────────────────────────────────────────────────────────────
# Una sucursal por localidad con peso como destino (dos en las grandes).
SUCURSALES: list[tuple] = []
for _l in LOCALIDADES:
    if _l[7] >= 1:
        SUCURSALES.append((f"Sucursal {_l[0]}" + (" Centro" if _l[7] >= 4 else ""), _l))
        if _l[7] >= 4:
            SUCURSALES.append((f"Sucursal {_l[0]} Shopping", _l))
SUCURSAL_DE = {}
for _nombre, _l in SUCURSALES:
    SUCURSAL_DE.setdefault(_l[0], []).append(_nombre)

CANALES = {"Sucursal": 40, "Web": 33, "App": 15, "Marketplace": 7, "Televenta": 5}
VENDEDORES_EXTERNOS = [f"Marketplace · Vendedor {n:04d}" for n in (17, 42, 108, 233, 391, 512, 777, 905)]
TARJETAS = {"Visa": 42, "Mastercard": 30, "Naranja": 14, "American Express": 8, "Cabal": 6}

NOMBRES = ["juan", "maria", "lucas", "sofia", "martin", "valentina", "diego", "camila", "pablo", "lucia", "nicolas",
           "florencia", "matias", "julieta", "gonzalo", "agustina", "federico", "micaela", "santiago", "carla"]
APELLIDOS = ["gonzalez", "rodriguez", "gomez", "fernandez", "lopez", "diaz", "martinez", "perez", "romero", "sosa",
             "alvarez", "torres", "ruiz", "ramirez", "flores", "benitez", "acosta", "medina", "herrera", "suarez"]
DOMINIOS = ["gmail.com", "hotmail.com", "yahoo.com.ar", "outlook.com"]

# ── Lo que dice el cliente en la posventa ───────────────────────────────────
# motivo → frases. Muchas formas de decir lo mismo, a propósito.
COMENTARIOS = {
    "Producto dañado": [
        "La heladera llegó con la puerta abollada", "El televisor vino con la pantalla rajada",
        "La caja estaba rota y el producto rayado", "El lavarropas llegó con un golpe en el costado",
        "Vino con la carcasa partida", "El microondas tiene la puerta torcida", "Llegó roto, ni lo pude usar",
    ],
    "Producto con falla": [
        "El lavarropas no centrifuga", "El celular no enciende", "La notebook se apaga sola",
        "El aire hace un ruido muy fuerte", "La pantalla tiene una línea vertical", "La heladera no enfría bien",
        "Los auriculares solo suenan de un lado", "La freidora no calienta",
    ],
    "Demora en la entrega": [
        "Me dijeron 48 horas y pasó una semana", "Todavía no llegó y nadie me avisa nada",
        "El envío está demorado hace días", "Pasó la fecha prometida y sigue sin llegar",
        "Me cambiaron la fecha de entrega tres veces", "Hace diez días que espero el pedido",
    ],
    "No coincide con lo publicado": [
        "El color no es el de la foto", "No es el modelo que compré", "Vino otro producto",
        "La publicación decía 128 GB y vino de 64", "Le faltan los accesorios que mostraban",
    ],
    "Arrepentimiento": [
        "Me arrepentí de la compra", "Encontré más barato en otro lado", "Ya no lo necesito",
        "Lo compré por error", "Prefiero esperar a otra promoción",
    ],
    "Problema con el cobro": [
        "Me cobraron dos veces", "Era en cuotas sin interés y me cobraron interés",
        "No se acreditó el reintegro de la promo", "Me debitaron el total en un solo pago",
        "Cancelé la compra y todavía no me devolvieron la plata",
    ],
    "No entra o no se puede instalar": [
        "No entra en el hueco de la cocina", "No pasa por la puerta del departamento",
        "Necesitaba instalación y nunca vino el técnico", "El aire necesita una instalación que no ofrecían",
        "Es más grande de lo que decía la ficha",
    ],
    "Problema en el retiro": [
        "En la sucursal no tenían el producto para retirar", "Me hicieron esperar dos horas para retirar",
        "Me pidieron que vaya el titular de la tarjeta", "Me mandaron a retirar a otra sucursal",
        "El aviso de retiro llegó pero el pedido no estaba",
    ],
    "Garantía": [
        "Se rompió a los dos meses y no me responden", "El service oficial no tiene turnos",
        "Llevé el producto a garantía hace un mes y no hay novedades", "Me rechazaron la garantía sin explicación",
    ],
}
# Cuándo y con qué peso aparece cada motivo
POSVENTA_DE_ENTREGADO = {"Producto dañado": 22, "Producto con falla": 22, "No coincide con lo publicado": 10,
                         "Arrepentimiento": 16, "Problema con el cobro": 10, "No entra o no se puede instalar": 10,
                         "Garantía": 10}
SIN_STOCK = ["Nos quedamos sin stock del modelo, se reintegra el pago", "Se cancela por falta de stock",
             "El producto no está disponible en el depósito, se devuelve el dinero"]
RECHAZOS = {"Fondos insuficientes": 34, "Supera el límite de la tarjeta": 22, "Datos de la tarjeta inválidos": 16,
            "Tarjeta vencida": 8, "Rechazado por antifraude": 12, "Error de comunicación con el procesador": 8}

# ── Lo que se nota (picos y anomalías) ──────────────────────────────────────
CYBERMONDAY = (date(2025, 11, 3), date(2025, 11, 5))
BLACK_FRIDAY = (date(2025, 11, 28), date(2025, 12, 1))
NAVIDAD = (date(2025, 12, 9), date(2025, 12, 24))
AGUINALDO = [(date(2025, 12, 18), date(2025, 12, 23)), (date(2026, 6, 26), date(2026, 7, 3))]
VERANO = (date(2025, 11, 20), date(2026, 2, 20))
OLA_DE_CALOR = (date(2026, 1, 12), date(2026, 1, 19))
VUELTA_A_CLASES = (date(2026, 2, 9), date(2026, 3, 6))
INVIERNO = (date(2026, 5, 10), date(2026, 8, 20))
OLA_POLAR = (date(2026, 7, 6), date(2026, 7, 12))
PREVIA_DEL_MUNDIAL = (date(2026, 5, 1), date(2026, 6, 14))
HOT_SALE = (date(2026, 5, 11), date(2026, 5, 13))
DIA_DEL_PADRE = (date(2026, 6, 8), date(2026, 6, 20))
DIA_DEL_NINO = (date(2026, 8, 3), date(2026, 8, 16))
# La noche del CyberMonday, cuando más se compra: el procesador de pagos se cae.
CAIDA_DEL_PROCESADOR = (datetime(2025, 11, 4, 19, 30, tzinfo=ART), datetime(2025, 11, 4, 23, 45, tzinfo=ART))
ATAQUE_DE_FRAUDE = (datetime(2026, 8, 7, 23, 10, tzinfo=ART), datetime(2026, 8, 8, 4, 40, tzinfo=ART))
SOBREVENTA = ("TV-55-4K", date(2026, 5, 11), date(2026, 5, 14))
DEMORAS_NEA = (date(2026, 9, 1), date(2026, 9, 20))
FERIADOS = {date(2025, 12, 25), date(2026, 1, 1)}


def _en(x, rango) -> bool:
    return rango[0] <= x <= rango[1]


def _h(horas: float) -> timedelta:
    return timedelta(hours=horas)


def _campania(d: date) -> str:
    for nombre, rango in (("CyberMonday", CYBERMONDAY), ("Black Friday", BLACK_FRIDAY), ("Hot Sale", HOT_SALE),
                          ("Navidad", NAVIDAD), ("Día del Padre", DIA_DEL_PADRE), ("Día del Niño", DIA_DEL_NINO)):
        if _en(d, rango):
            return nombre
    return ""


def _volumen(d: date, desde: date, hasta: date, por_dia: int, r: random.Random) -> int:
    v = por_dia * (0.92 + 0.16 * (d - desde).days / max(1, (hasta - desde).days))     # el negocio crece
    v *= {0: 1.05, 1: 1.0, 2: 0.98, 3: 0.98, 4: 1.05, 5: 1.25, 6: 0.7}[d.weekday()]
    if d.month == 1:
        v *= 0.9
    if _en(d, CYBERMONDAY) or _en(d, HOT_SALE):
        v *= 2.8
    elif _en(d, BLACK_FRIDAY):
        v *= 1.9
    elif _en(d, NAVIDAD):
        v *= 1.3 + 0.5 * (d - NAVIDAD[0]).days / (NAVIDAD[1] - NAVIDAD[0]).days
    elif _en(d, DIA_DEL_PADRE):
        v *= 1.3
    if any(_en(d, a) for a in AGUINALDO):
        v *= 1.2
    if _en(d, OLA_DE_CALOR):
        v *= 1.25                                    # los aires se agotan
    if d in FERIADOS:
        v *= 0.15
    return max(0, int(r.gauss(v, v * 0.08)))


def _peso_del_producto(p: tuple, d: date) -> float:
    w = p[5]
    temporada = p[6]
    if temporada == "tv":
        w *= 2.2 if _en(d, PREVIA_DEL_MUNDIAL) else 1.6 if _en(d, DIA_DEL_PADRE) else 1.0
        if p[0] == SOBREVENTA[0] and _en(d, HOT_SALE):
            w *= 4.0                                 # la oferta estrella del Hot Sale
    elif temporada == "calor":
        w *= (5.0 if _en(d, OLA_DE_CALOR) else 2.6) if _en(d, VERANO) else 0.35
    elif temporada == "frio":
        w *= (2.2 if _en(d, OLA_POLAR) else 2.8) if _en(d, INVIERNO) else 0.25
    elif temporada == "escuela":
        w *= 1.8 if _en(d, VUELTA_A_CLASES) else 1.0
    elif temporada == "fiestas":
        w *= 2.5 if _en(d, NAVIDAD) or _en(d, DIA_DEL_NINO) else 1.0
    if p[2] in ("Celulares", "Notebooks", "Gaming", "Audio") and _en(d, NAVIDAD):
        w *= 1.5
    return w


# ── Los clientes ────────────────────────────────────────────────────────────
def _clientes(r: random.Random, n: int = 26_000) -> list[dict]:
    destinos = [l for l in LOCALIDADES]
    pesos = [l[7] for l in LOCALIDADES]
    out = []
    for i in range(n):
        loc = r.choices(destinos, pesos)[0]
        nombre, apellido = r.choice(NOMBRES), r.choice(APELLIDOS)
        area = "11" if loc[2] == "AMBA" else str(r.choice([223, 341, 351, 261, 381, 362, 299, 291]))
        out.append({
            "id": f"CLI-{100_000 + i * 7 + r.randint(0, 6):07d}", "loc": loc,
            "dni": str(r.randint(14_000_000, 46_999_999)),
            "email": f"{nombre}.{apellido}{r.randint(1, 999)}@{r.choice(DOMINIOS)}",
            "telefono": (f"11-{r.randint(4000, 6999)}-{r.randint(1000, 9999)}" if area == "11"
                         else f"{area}-{r.randint(400, 699)}-{r.randint(1000, 9999)}"),
            "compras": 0,
        })
    return out


# ── Un pedido y sus eventos ─────────────────────────────────────────────────
def _hora(canal: str, d: date, r: random.Random) -> float:
    if canal == "Sucursal":
        return r.uniform(10, 13.5) if d.weekday() == 6 else r.uniform(9.5, 20.5)
    if canal == "Televenta":
        return r.uniform(9, 19)
    # online: de mañana y, sobre todo, de noche
    return r.choices([r.uniform(0, 7), r.uniform(7, 18), r.uniform(18, 24)], [8, 42, 50])[0]


def _pago(r: random.Random, canal: str, total: int, campania: str) -> dict:
    if canal == "Sucursal":
        medio = r.choices(["Tarjeta de crédito", "Tarjeta de débito", "Crédito de la casa", "Efectivo", "QR"],
                          [52, 14, 18, 10, 6])[0]
    else:
        medio = r.choices(["Tarjeta de crédito", "Tarjeta de débito", "Crédito de la casa", "Transferencia"],
                          [70, 12, 10, 8])[0]
    pago = {"medio": medio, "tarjeta": "", "cuotas": 1, "interes": ""}
    if medio == "Tarjeta de crédito":
        pago["tarjeta"] = r.choices(list(TARJETAS), list(TARJETAS.values()))[0]
        maximo = 24 if campania else 12
        pago["cuotas"] = r.choices([1, 3, 6, 12, 18, 24], [18, 14, 18, 30, 10, 10 if maximo == 24 else 0])[0]
        pago["interes"] = "Sin interés" if pago["cuotas"] <= (12 if campania else 6) else "Con interés"
    elif medio == "Crédito de la casa":
        pago["cuotas"] = r.choice([6, 9, 12, 18])
        pago["interes"] = "Con interés"
    elif medio == "Tarjeta de débito":
        pago["tarjeta"] = r.choices(["Visa", "Mastercard", "Cabal"], [55, 35, 10])[0]
    return pago


def _promesa(t0: datetime, loc: tuple, voluminoso: bool, tipo: str, r: random.Random) -> date:
    if tipo == "Retiro en sucursal":
        dias = r.choice([0, 1, 2])
    else:
        dias = {"AMBA": 2, "Costa Atlántica": 4, "Centro": 4, "Cuyo": 5, "NOA": 6, "NEA": 6, "Patagonia": 7}[loc[2]]
        dias += 2 if voluminoso else 0
    d = t0.date()
    while dias > 0:
        d += timedelta(days=1)
        if d.weekday() != 6 and d not in FERIADOS:
            dias -= 1
    return d


def _pedido(r: random.Random, d: date, clientes: list[dict], fraude: bool = False) -> list[dict]:
    """Los eventos de un pedido nuevo, desde que se crea hasta la posventa."""
    campania = _campania(d)
    online_extra = 2.2 if campania in ("CyberMonday", "Hot Sale", "Black Friday") else 1.0
    canal = r.choices(list(CANALES), [w * (online_extra if c in ("Web", "App", "Marketplace") else 1)
                                      for c, w in CANALES.items()])[0]
    if fraude:
        canal = r.choice(["Web", "App"])
        elegido = r.choice(["CEL-GAL-S256", "NB-14-R5", "CONSOLA-5", "CEL-GAL-A128"])
        p = next(x for x in PRODUCTOS if x[0] == elegido)
        cliente = {"id": f"CLI-{9_000_000 + r.randint(0, 999_999):07d}", "loc": r.choice(LOCALIDADES[:20]),
                   "dni": str(r.randint(40_000_000, 46_999_999)),
                   "email": f"{r.choice(NOMBRES)}{r.randint(1000, 9999)}@{r.choice(DOMINIOS)}",
                   "telefono": f"11-{r.randint(4000, 6999)}-{r.randint(1000, 9999)}", "compras": 0}
        t0 = r.uniform(ATAQUE_DE_FRAUDE[0].timestamp(), ATAQUE_DE_FRAUDE[1].timestamp())
        t0 = datetime.fromtimestamp(t0, ART)
    else:
        candidatos = [x for x in PRODUCTOS if canal != "Marketplace" or x[2] in ("Pequeños electrodomésticos", "Audio", "Hogar")]
        p = r.choices(candidatos, [_peso_del_producto(x, d) for x in candidatos])[0]
        cliente = r.choice(clientes) if r.random() < 0.55 else clientes[r.randint(0, 3_000)]   # hay recurrentes
        t0 = datetime(d.year, d.month, d.day, tzinfo=ART) + _h(_hora(canal, d, r))
    sku, nombre, categoria, marca, precio, _, _, voluminoso = p
    loc = cliente["loc"]
    tipo_cliente = "Recurrente" if cliente["compras"] else "Nuevo"
    cliente["compras"] += 1
    cantidad = 1 if precio > 300_000 or r.random() < 0.85 else 2
    precio_real = int(round(r.gauss(precio, precio * 0.04) * (0.85 if campania else 1.0) / 100) * 100)
    if canal == "Sucursal":
        tipo = "Envío desde sucursal" if voluminoso else "Se lo lleva en el momento"
        sucursal = r.choice(SUCURSAL_DE.get(loc[0]) or [s for s, _ in SUCURSALES])
    else:
        tipo = (r.choices(["Envío a domicilio", "Retiro en sucursal"], [80, 20])[0] if voluminoso
                else r.choices(["Envío a domicilio", "Retiro en sucursal"], [55, 45])[0])
        if canal == "Marketplace":
            tipo = "Envío a domicilio"
        sucursal = r.choice(SUCURSAL_DE.get(loc[0]) or [""]) if tipo == "Retiro en sucursal" else ""
        if tipo == "Retiro en sucursal" and not sucursal:
            tipo = "Envío a domicilio"
    envio = 0 if tipo != "Envío a domicilio" or precio_real * cantidad > 150_000 or campania else r.choice([6_900, 9_900, 12_900])
    total = precio_real * cantidad + envio
    pago = _pago(r, canal, total, campania)
    transportista = ("Flota propia" if voluminoso else r.choices(["Operador logístico", "Moto mensajería"], [80, 20])[0]
                     if loc[2] == "AMBA" else "Operador logístico") if tipo in ("Envío a domicilio", "Envío desde sucursal") else ""
    if canal == "Marketplace":
        transportista = "Operador logístico"
    promesa = _promesa(t0, loc, voluminoso, tipo, r) if tipo != "Se lo lleva en el momento" else ""
    nro = f"{1_520_000_000_000 + int(t0.timestamp() * 10) % 99_999_999_999 + r.randint(0, 999):013d}-01"
    base = {
        "nro_pedido": nro, "canal": canal, "vendedor": r.choice(VENDEDORES_EXTERNOS) if canal == "Marketplace" else "Tienda propia",
        "campania": campania, "cliente_id": cliente["id"], "tipo_cliente": tipo_cliente, "dni": cliente["dni"],
        "email": cliente["email"], "telefono": cliente["telefono"], "localidad": loc[0], "provincia": loc[1],
        "region": loc[2], "lat": round(loc[3] + r.uniform(-0.02, 0.02), 5), "lon": round(loc[4] + r.uniform(-0.02, 0.02), 5),
        "sku": sku, "producto": nombre, "categoria": categoria, "marca": marca, "cantidad": cantidad, "precio": precio_real,
        "total": total, "envio": envio, "medio": pago["medio"], "tarjeta": pago["tarjeta"], "cuotas": pago["cuotas"],
        "interes": pago["interes"], "tipo_entrega": tipo, "sucursal": sucursal, "transportista": transportista,
        "fecha_prometida": promesa.isoformat() if promesa else "",
    }
    ev: list[dict] = []

    contacto = ("tipo_cliente", "dni", "email", "telefono")

    def e(ts: datetime, evento: str, estado_oms: str = "", **kw):
        # Los datos de contacto viajan al crearse el pedido; después, el cliente por su id.
        datos = base if evento == "Pedido creado" else {k: v for k, v in base.items() if k not in contacto}
        ev.append({**datos, "ts": ts, "evento": evento, "estado_oms": estado_oms, **kw})

    seguro = "Sí" if precio_real > 400_000 and r.random() < 0.18 else "No"
    e(t0, "Pedido creado", "payment-pending", seguro=seguro)

    # El pago
    t = t0 + timedelta(minutes=r.uniform(0.2, 2) if canal == "Sucursal" else r.uniform(1, 12))
    rechazo = ""
    if pago["medio"] in ("Tarjeta de crédito", "Tarjeta de débito"):
        p_rechazo = 0.07
        if CAIDA_DEL_PROCESADOR[0] <= t <= CAIDA_DEL_PROCESADOR[1]:
            p_rechazo = 0.65
            rechazo = "Error de comunicación con el procesador"
        if fraude:
            p_rechazo, rechazo = 0.8, "Rechazado por antifraude"
        if r.random() < p_rechazo:
            rechazo = rechazo or r.choices(list(RECHAZOS), list(RECHAZOS.values()))[0]
        else:
            rechazo = ""
    elif pago["medio"] == "Crédito de la casa" and r.random() < 0.15:
        rechazo = "Rechazado por la evaluación crediticia"
    if rechazo:
        e(t, "Pago rechazado", "payment-denied", estado_pago="Rechazado", motivo_rechazo=rechazo)
        e(t + timedelta(minutes=r.uniform(15, 60)), "Cancelado", "canceled", estado_pago="Rechazado",
          motivo="Pago rechazado")
        return ev
    e(t, "Pago aprobado", "payment-approved", estado_pago="Aprobado")

    if fraude and r.random() < 0.7:                              # se detecta al día siguiente
        e(t + _h(r.uniform(6, 14)), "Cancelado", "canceled", estado_pago="Devuelto", motivo="Fraude confirmado",
          comentario="El titular desconoce la compra, se anula el pedido")
        return ev

    if tipo == "Se lo lleva en el momento":
        e(t + timedelta(minutes=r.uniform(3, 15)), "Facturado", "invoiced", estado_pago="Aprobado")
        if r.random() < 0.035:
            m = r.choices(["Producto con falla", "Garantía", "Problema con el cobro"], [60, 25, 15])[0]
            e(t + _h(r.uniform(24, 24 * 40)), "Reclamo", "", motivo=m, comentario=r.choice(COMENTARIOS[m]))
        return ev

    # Preparación (y la sobreventa del Hot Sale)
    t = t + _h(r.uniform(0.5, 3) if canal == "Sucursal" else r.uniform(1, 10))
    e(t, "En preparación", "handling", estado_pago="Aprobado")
    if sku == SOBREVENTA[0] and _en(d, (SOBREVENTA[1], SOBREVENTA[2])) and r.random() < 0.55:
        e(t + _h(r.uniform(12, 48)), "Cancelado", "canceled", estado_pago="Devuelto", motivo="Sin stock",
          comentario=r.choice(SIN_STOCK))
        return ev
    if r.random() < 0.02:
        e(t + _h(r.uniform(1, 30)), "Cancelado", "canceled", estado_pago="Devuelto", motivo="Arrepentimiento",
          comentario=r.choice(COMENTARIOS["Arrepentimiento"]))
        return ev
    t = t + _h(r.uniform(2, 20))
    e(t, "Facturado", "invoiced", estado_pago="Aprobado")

    prometida = datetime(promesa.year, promesa.month, promesa.day, tzinfo=ART) + _h(r.uniform(9, 19))
    if tipo == "Retiro en sucursal":
        listo = max(t + _h(r.uniform(1, 6)), prometida - _h(r.uniform(0, 8)))
        e(listo, "Listo para retirar", "", estado_pago="Aprobado")
        if r.random() < 0.06:
            m = "Problema en el retiro"
            e(listo + _h(r.uniform(2, 48)), "Reclamo", "", motivo=m, comentario=r.choice(COMENTARIOS[m]))
        if r.random() < 0.94:
            e(listo + _h(r.uniform(2, 120)), "Retirado en sucursal", "", estado_pago="Aprobado",
              calificacion=r.choices([5, 4, 3, 2, 1], [55, 25, 10, 5, 5])[0])
        else:
            e(listo + _h(r.uniform(240, 300)), "Cancelado", "canceled", estado_pago="Devuelto",
              motivo="Vencido el plazo de retiro")
        return ev

    # A domicilio: despacho, visitas y entrega
    salida = t + _h(r.uniform(6, 30))
    e(salida, "Despachado", "", estado_pago="Aprobado")
    entrega = prometida + _h(r.gauss(-6, 20))
    demora_extra = 0.0
    if loc[2] == "NEA" and _en(salida.date(), DEMORAS_NEA):
        demora_extra = r.uniform(3, 8) * 24
    elif r.random() < 0.08:
        demora_extra = r.uniform(1, 5) * 24
    entrega += _h(demora_extra)
    entrega = max(entrega, salida + _h(r.uniform(3, 10)))
    if demora_extra > 48 and r.random() < 0.75:
        m = "Demora en la entrega"
        e(prometida + _h(r.uniform(4, 30)), "Reclamo", "", motivo=m, comentario=r.choice(COMENTARIOS[m]))
    if r.random() < 0.07:
        e(entrega - _h(r.uniform(20, 30)), "Visita fallida", "", motivo="Cliente ausente",
          comentario=r.choice(["No había nadie en el domicilio", "El timbre no funciona y nadie atendió",
                               "Nadie respondió al llegar el repartidor"]))
    demora = max(0, (entrega.date() - promesa).days)
    e(entrega, "Entregado", "", estado_pago="Aprobado", dias_demora=demora,
      calificacion=max(1, min(5, round(r.gauss(4.6 - demora * 0.5, 0.7)))))

    # Posventa
    p_posventa = 0.06 + (0.08 if voluminoso else 0)
    if r.random() < p_posventa:
        m = r.choices(list(POSVENTA_DE_ENTREGADO), list(POSVENTA_DE_ENTREGADO.values()))[0]
        cuando = entrega + _h(r.uniform(2, 24 * (10 if m in ("Arrepentimiento", "No coincide con lo publicado") else 45)))
        devuelve = m in ("Arrepentimiento", "No coincide con lo publicado", "No entra o no se puede instalar") or (
            m == "Producto dañado" and r.random() < 0.7)
        e(cuando, "Devolución solicitada" if devuelve else "Reclamo", "", motivo=m, comentario=r.choice(COMENTARIOS[m]))
    return ev


def generar(desde: date, hasta: date, por_dia: int, semilla: int = 20261015) -> tuple[list[dict], Counter]:
    r = random.Random(semilla)
    clientes = _clientes(r)
    eventos: list[dict] = []
    d = desde - timedelta(days=10)                    # para que el primer día ya haya pedidos en curso
    while d <= hasta:
        for _ in range(_volumen(d, desde, hasta, por_dia, r)):
            eventos.extend(_pedido(r, d, clientes))
        if d == ATAQUE_DE_FRAUDE[0].date():
            for _ in range(140):
                eventos.extend(_pedido(r, d, clientes, fraude=True))
        d += timedelta(days=1)
    inicio = datetime(desde.year, desde.month, desde.day, tzinfo=ART)
    fin = datetime(hasta.year, hasta.month, hasta.day, tzinfo=ART) + timedelta(days=1)
    filas, cuenta = [], Counter()
    for x in eventos:
        if inicio <= x["ts"] < fin:
            cuenta[x["evento"]] += 1
            filas.append({**{k: v for k, v in x.items() if k != "ts"}, "fecha_hora": x["ts"].isoformat(timespec="seconds")})
    filas.sort(key=lambda f: f["fecha_hora"])
    return filas, cuenta


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--desde", default="2025-10-15")
    p.add_argument("--hasta", default="2026-10-14")
    # El Builder acepta hasta 50 MB.
    p.add_argument("--por-dia", type=int, default=30, help="pedidos por día (sin los picos)")
    a = p.parse_args()
    filas, eventos = generar(date.fromisoformat(a.desde), date.fromisoformat(a.hasta), a.por_dia)
    SALIDA.parent.mkdir(exist_ok=True)
    with SALIDA.open("w", encoding="utf-8", newline="") as f:
        f.writelines(a_json(fila) + "\n" for fila in filas)
    pedidos = len({f["nro_pedido"] for f in filas})
    print(f"{len(filas):,} eventos de {pedidos:,} pedidos -> {SALIDA} ({SALIDA.stat().st_size / 1e6:.1f} MB)")
    print("  " + " · ".join(f"{k}: {v:,}" for k, v in eventos.most_common()))


if __name__ == "__main__":
    main()
