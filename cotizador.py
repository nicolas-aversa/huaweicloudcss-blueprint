"""
cotizador.py
============

La cotización de un cluster CSS OpenSearch a partir de un formulario, para
cualquier escenario de logs:

- una o varias fuentes, cada una con lo que llega por día, cuándo llega, su
  pico y cuántos días se guarda (y cuántos de esos en nodos hot);
- dónde va lo viejo (índices congelados o nodos cold), el respaldo, las
  réplicas, las AZ (1, 2 o 3) y cómo llegan los logs (misma VPC, acceso
  público o VPN);
- el hardware: automático (el cluster más barato que cumple) o elegido a mano
  (flavor, cantidad de nodos y disco), con avisos si no alcanza;
- los precios: los de lista de LA-Santiago o cargados a mano para otra región.

Devuelve el cluster, el costo por mes desglosado, el primer año mes a mes, los
avisos y los supuestos con su fuente.

El modelo es el que se validó con un cluster real de un OMS FIX de 30 sesiones
en LA-Santiago (pico sostenido 20 minutos, máximo medido, caída de nodos,
índices congelados, respaldo y restauración en OBS, VPN entre regiones):

- Disco: índice × copias × (días con log + 1) ÷ 0,74. El 0,74 deja el disco al
  80 % visto por OpenSearch, que cuenta como usado el 5 % que reserva ext4 (de
  1.200 GB contratados el nodo ve 1.180).
- Nodos de escritura: la fórmula de Huawei, pico (MB/s) × copias ÷ (vCPU por
  nodo × 1 MB/s), sumando el pico de todas las fuentes.
- AZ (guía de Huawei): con 2 AZ, al menos 4 nodos de cada tipo de datos; con 3,
  al menos 3; siempre en múltiplos de las AZ.
- Nodos cold: hasta 32. Índices congelados: se pagan por GB y por hora y
  liberan solos el disco local.

Es una estimación: el número fino sale de una prueba con los datos del cliente.
"""

from __future__ import annotations

import math
from typing import Any

# ── Precios ─────────────────────────────────────────────────────────────────
# Lista de Huawei Cloud para LA-Santiago, sin impuestos ni descuentos. Por mes
# salvo que diga otra cosa. El pago anual cuesta 10 meses (nodos y disco). Se
# pueden reemplazar todos desde el formulario, para cotizar en otra región.
PRECIOS: dict[str, Any] = {
    "region": "LA-Santiago",
    "consultado": "noviembre de 2026",
    "disco_ultra_gb": 0.171,        # Ultra-high I/O
    "disco_high_gb": 0.051,         # High I/O
    "obs_gb": 0.021,                # OBS Standard, una AZ, pago por uso
    "obs_tb_anio": 174.18,          # paquete anual de OBS, por TB (1.024 GB)
    "congelado_gb_hora": 0.00003209,
    "ancho_banda_mbit": 8.016,      # acceso público del cluster
    "vpn_gateway_mes": 209.0,       # VPN Enterprise Professional 1, pago mensual (calculadora)
    "vpn_gateway_hora": 0.33,       # la misma, pago por uso
    "eip_hora": 0.005,              # reserva de una EIP
    "trafico_salida_gb": 0.081,     # EIP por tráfico, hasta 10 TB por mes
    "meses_pago_anual": 10,
    "recargo_por_hora": 0.39,       # nodos con pago por uso, sobre el precio mensual
}
# (clave, etiqueta, unidad, mínimo, máximo): lo que el formulario deja cambiar.
PRECIOS_CAMPOS: list[tuple[str, str, str, float, float]] = [
    ("disco_ultra_gb", "Disco Ultra-high I/O", "US$ por GB y mes", 0, 100),
    ("disco_high_gb", "Disco High I/O", "US$ por GB y mes", 0, 100),
    ("obs_gb", "OBS Standard", "US$ por GB y mes", 0, 100),
    ("obs_tb_anio", "Paquete anual de OBS", "US$ por TB y año", 0, 100000),
    ("congelado_gb_hora", "Índices congelados", "US$ por GB y hora", 0, 1),
    ("ancho_banda_mbit", "Acceso público", "US$ por Mbit/s y mes", 0, 10000),
    ("vpn_gateway_mes", "Gateway VPN, pago mensual", "US$ por mes", 0, 100000),
    ("vpn_gateway_hora", "Gateway VPN, pago por hora", "US$ por hora", 0, 1000),
    ("eip_hora", "Reserva de una EIP", "US$ por hora", 0, 1000),
    ("trafico_salida_gb", "Tráfico que sale", "US$ por GB", 0, 100),
    ("meses_pago_anual", "Pago anual", "meses que cuesta un año", 1, 12),
    ("recargo_por_hora", "Recargo del pago por hora", "sobre el mensual (0,39 = 39 %)", 0, 5),
]
HORAS_MES = 730
# (vCPU, RAM GB, US$ por mes, disco máximo por nodo en GB; 0 = no es de datos)
FLAVORS: dict[str, tuple[int, int, float, int]] = {
    "4u16g": (4, 16, 127.05, 0),
    "4u32g": (4, 32, 165.90, 2560),
    "8u32g": (8, 32, 254.63, 3200),
    "8u64g": (8, 64, 332.33, 5120),
    "16u64g": (16, 64, 507.68, 6400),
    "16u128g": (16, 128, 664.65, 10240),
}
DE_DATOS = tuple(k for k, v in FLAVORS.items() if v[3])
HOT = DE_DATOS                                   # el automático prueba todos los de datos
COLD = ("4u32g", "8u64g", "16u128g", "16u64g")
FLAVOR_CLIENTE, FLAVOR_MASTER = "8u32g", "4u16g"
DISCO_CLIENTE_MASTER_GB = 40

# ── Supuestos ───────────────────────────────────────────────────────────────
USO_DISCO = 0.74
MB_S_POR_VCPU_MEDIDO = 0.6     # log por vCPU en el máximo, contando las dos copias (medido)
SHARD_MAX_GB = 50
DISCO_MIN_GB = 40
MAX_COLD = 32
MAX_NODOS = 200
MAX_FUENTES = 20
MINIMO_POR_AZ = {1: 1, 2: 4, 3: 3}   # nodos de cada tipo de datos (guía de Huawei)
GB_POR_NODO_GUIA = 1000        # la guía de Huawei: 100 a 1.000 GB de datos por nodo hot
RED_SOBRE_LOG = 0.45           # bytes en la red por byte de log, con gzip y HTTPS (150 de 333, medido)
IPSEC = 1.05
RESPUESTA_CORTA_B, RESPUESTA_COMPLETA_B = 29, 243   # vuelta por mensaje (medido)
MAX_PUBLICO_MBIT = 200
MAX_VPN_MBIT = 300

# Una fuente de logs y el resto del formulario. Lo que falta toma este valor.
FUENTE: dict[str, Any] = {
    "nombre": "", "gb_por_dia": 50.0, "bytes_por_evento": 500, "dias_por_semana": 7, "horas_activas": 24,
    "pico": 3.0, "retencion_dias": 30, "dias_hot": 7,
}
DEFAULTS: dict[str, Any] = {
    "resto": "congelados", "respaldo": True, "replicas": 1, "azs": 2, "enlace": "interno", "respuesta_corta": True,
    "crecimiento": 1.33, "mb_s_por_vcpu": 1.0,
    "flavor_hot": "auto", "nodos_hot": 0, "disco_hot_gb": 0, "disco_hot": "ultra",
    "flavor_cold": "auto", "nodos_cold": 0, "masters": "regla", "pago": "mensual",
}

# (etiqueta, mínimo, máximo, entero): la etiqueta es la del formulario, para
# que el error diga qué campo corregir.
_CAMPOS_FUENTE: dict[str, tuple[str, float, float, bool]] = {
    "gb_por_dia": ("Log por día (GB)", 0.01, 200000, False),
    "bytes_por_evento": ("Bytes por evento", 20, 1000000, True),
    "dias_por_semana": ("Días con log por semana", 1, 7, True),
    "horas_activas": ("Horas en que llega", 1, 24, False),
    "pico": ("Pico sobre el promedio", 1, 20, False),
    "retencion_dias": ("Se guarda (días)", 1, 3650, True),
    "dias_hot": ("En hot (días)", 1, 3650, True),
}
_CAMPOS: dict[str, tuple[str, float, float, bool]] = {
    "replicas": ("Réplicas", 0, 2, True),
    "azs": ("AZ", 1, 3, True),
    "crecimiento": ("Crecimiento del índice sobre el log", 0.3, 5, False),
    "mb_s_por_vcpu": ("MB/s de escritura por vCPU", 0.1, 10, False),
    "nodos_hot": ("Nodos hot", 0, MAX_NODOS, True),
    "disco_hot_gb": ("Disco por nodo hot (GB)", 0, max(v[3] for v in FLAVORS.values()), True),
    "nodos_cold": ("Nodos cold", 0, MAX_COLD, True),
}
_OPCIONES: dict[str, tuple[str, tuple[str, ...]]] = {
    "resto": ("Pasados los días en hot, va a", ("congelados", "cold")),
    "enlace": ("Cómo llegan los logs", ("interno", "publico", "vpn")),
    "disco_hot": ("Tipo de disco hot", ("ultra", "high")),
    "masters": ("Masters dedicados", ("regla", "siempre")),
    "flavor_hot": ("Flavor hot", ("auto",) + DE_DATOS),
    "flavor_cold": ("Flavor cold", ("auto",) + DE_DATOS),
    "pago": ("Cómo se va a pagar", ("mensual", "anual", "por_hora")),
}

# ── El formulario simple ────────────────────────────────────────────────────
# Preguntas de negocio, sin términos de CSS, que se traducen a los datos del
# modelo. Si junto con ellas llega un dato técnico (desde «Ajustes técnicos»),
# gana el técnico.
GB_POR_SESION_FIX = 8      # medido en un OMS FIX: unos 8 GB de log por sesión por día
BYTES_FIX = 333            # tamaño medio de un mensaje FIX (medido)
BYTES_EVENTO = 500         # una línea de log típica (supuesto)
UNIDADES: dict[str, str] = {
    "gb_dia": "GB por día",
    "eventos_s": "eventos por segundo",
    "millones_dia": "millones de eventos por día",
    "sesiones_fix": "sesiones FIX",
}
# (texto, días con log por semana, horas en que llega casi todo, pico sobre el promedio)
HORARIOS: dict[str, tuple[str, int, int, float]] = {
    "parejo": ("Parejo, las 24 horas, todos los días", 7, 24, 2.0),
    "dia": ("Sobre todo de día (8 a 20 h), todos los días", 7, 12, 2.0),
    "habil": ("En horario hábil (9 a 18 h), de lunes a viernes", 5, 9, 2.0),
    "mercado": ("En horario de mercado (10 a 17 h), de lunes a viernes", 5, 7, 3.0),
}
# (texto, AZ, réplicas, respaldo)
CRITICIDAD: dict[str, tuple[str, int, int, bool]] = {
    "alta": ("Producción crítica: sigue andando aunque se caiga una zona", 2, 1, True),
    "media": ("Producción: sigue andando aunque se caiga un servidor", 1, 1, True),
    "prueba": ("Prueba de concepto o desarrollo", 1, 0, False),
}
LO_VIEJO: dict[str, tuple[str, str]] = {
    "lento": ("Rara vez: puede tardar unos minutos", "congelados"),
    "rapido": ("Seguido: tiene que responder rápido", "cold"),
}
ORIGEN: dict[str, tuple[str, str]] = {
    "misma_region": ("En Huawei Cloud, en la misma región que el cluster", "interno"),
    "otro_lugar": ("En otra región de Huawei Cloud, en otra nube o en su datacenter", "vpn"),
    "internet": ("Por internet, sin VPN", "publico"),
}
PAGO: dict[str, str] = {
    "mensual": "Por mes",
    "anual": "Por año (cuesta 10 meses)",
    "por_hora": "Por hora, sin compromiso",
}
SIMPLE: dict[str, Any] = {"criticidad": "alta", "viejo": "lento", "origen": "misma_region", "pago": "mensual"}
FUENTE_SIMPLE: dict[str, Any] = {
    "nombre": "", "cantidad": 50, "unidad": "gb_dia", "horario": "parejo", "retencion_dias": 30, "consulta_dias": 7,
    "bytes_por_evento": "",
}


class DatoInvalido(ValueError):
    pass


def _miles(x: float) -> str:
    """Un entero con punto de miles: 76.289."""
    return f"{x:,.0f}".replace(",", ".")


def _dec(x: float, decimales: int = 1) -> str:
    """Coma decimal y punto de miles, sin ceros de más: 0,6 · 1,33 · 200.000."""
    s = f"{x:,.{decimales}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s.rstrip("0").rstrip(",") if "," in s else s


def _pct(x: float) -> str:
    return f"{round(x * 100)} %"


def _flavor(clave: str) -> str:
    vcpu, ram, _, _ = FLAVORS[clave]
    return f"{vcpu} vCPU y {ram} GB"


def _valor(v: Any, etiqueta: str, minimo: float, maximo: float, entero: bool = False) -> float:
    if isinstance(v, bool):
        v = int(v)
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise DatoInvalido(f"{etiqueta}: no es un número") from None
    if math.isnan(x) or not minimo <= x <= maximo:
        raise DatoInvalido(f"{etiqueta}: tiene que estar entre {_dec(minimo, 2)} y {_dec(maximo, 2)}")
    return int(round(x)) if entero else x


def _si(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "si", "sí", "yes")
    return bool(v)


def _opcion(datos: dict, clave: str, opciones: dict, etiqueta: str, defecto: str) -> str:
    v = str(datos.get(clave) or defecto)
    if v not in opciones:
        raise DatoInvalido(f"{etiqueta}: «{v}» no es una de las opciones ({', '.join(opciones)})")
    return v


def _plazo(dias: int) -> str:
    """7 → «7 días», 182 → «6 meses», 365 → «1 año», 1825 → «5 años»."""
    if dias >= 365 and dias % 365 == 0:
        return f"{dias // 365} año{'s' if dias > 365 else ''}"
    if dias in (180, 182, 183):
        return "6 meses"
    return f"{dias} día{'s' if dias != 1 else ''}"


def _fuente_simple(d: dict, prefijo: str) -> tuple[dict, "dict | None"]:
    """Una fuente del formulario simple en los datos del modelo, y cómo se contó en
    palabras. Una fuente técnica (con `gb_por_dia`, o sin `cantidad`) pasa tal cual."""
    if d.get("cantidad") in (None, "") or d.get("gb_por_dia") not in (None, ""):
        return dict(d), None
    unidad = _opcion(d, "unidad", UNIDADES, prefijo + "Unidad", "gb_dia")
    horario = _opcion(d, "horario", HORARIOS, prefijo + "Cuándo llega", "parejo")
    texto_horario, dias, horas, pico = HORARIOS[horario]
    cantidad = _valor(d["cantidad"], prefijo + "Cuánto llega", 0.0001, 1e9)
    b = d.get("bytes_por_evento")
    if b in (None, ""):
        b = BYTES_FIX if unidad == "sesiones_fix" else BYTES_EVENTO
    bytes_ev = _valor(b, prefijo + "Bytes por evento", 20, 1000000, True)
    gb = {"gb_dia": cantidad, "eventos_s": cantidad * bytes_ev * horas * 3600 / 1e9,
          "millones_dia": cantidad * 1e6 * bytes_ev / 1e9, "sesiones_fix": cantidad * GB_POR_SESION_FIX}[unidad]
    minimo, maximo = _CAMPOS_FUENTE["gb_por_dia"][1:3]
    if not minimo <= gb <= maximo:
        raise DatoInvalido(f"{prefijo}Cuánto llega: son unos {_dec(gb, 2)} GB de log por día y el cotizador va de "
                           f"{_dec(minimo, 2)} a {_miles(maximo)}")
    t = {k: v for k, v in d.items() if k not in ("cantidad", "unidad", "horario", "consulta_dias")}
    t.update(gb_por_dia=gb, bytes_por_evento=bytes_ev)
    for k, v in (("dias_por_semana", dias), ("horas_activas", horas), ("pico", pico)):
        if t.get(k) in (None, ""):
            t[k] = v
    consulta = d.get("consulta_dias")
    if consulta not in (None, "") and t.get("dias_hot") in (None, ""):
        retencion = t.get("retencion_dias")
        t["dias_hot"] = (FUENTE["retencion_dias"] if retencion in (None, "") else retencion) if str(consulta) == "todo" else consulta
    cuanto = f"{_dec(cantidad, 2)} {UNIDADES[unidad]}" + ("" if unidad == "gb_dia" else f" (unos {_dec(gb)} GB de log por día)")
    return t, {"cuanto": cuanto, "cuando": texto_horario}


def _simple(datos: dict) -> tuple[dict, dict]:
    """Traduce el formulario simple a los datos del modelo. Devuelve esos datos y
    las respuestas en palabras. Lo técnico que llegue explícito gana."""
    t = dict(datos)
    criticidad = _opcion(datos, "criticidad", CRITICIDAD, "Qué tan crítico es", SIMPLE["criticidad"])
    viejo = _opcion(datos, "viejo", LO_VIEJO, "Cómo se busca lo que ya no se consulta seguido", SIMPLE["viejo"])
    origen = _opcion(datos, "origen", ORIGEN, "Dónde están los sistemas que generan los logs", SIMPLE["origen"])
    _, azs, replicas, respaldo = CRITICIDAD[criticidad]
    derivados = {"azs": azs, "replicas": replicas, "respaldo": respaldo, "resto": LO_VIEJO[viejo][1],
                 "enlace": ORIGEN[origen][1]}
    tecnicos = [k for k in derivados if datos.get(k) not in (None, "")]
    for k, v in derivados.items():
        if k not in tecnicos:
            t[k] = v
    comos: list = []
    lista = datos.get("fuentes")
    if isinstance(lista, list):
        nuevas = []
        for i, d in enumerate(lista):
            if not isinstance(d, dict):
                nuevas.append(d)
                comos.append(None)
                continue
            nombre = str(d.get("nombre") or "").strip()[:60] or (f"Fuente {i + 1}" if len(lista) > 1 else "Logs")
            f, como = _fuente_simple(d, f"{nombre} · " if len(lista) > 1 else "")
            nuevas.append(f)
            comos.append(como)
        t["fuentes"] = nuevas
    return t, {"criticidad": criticidad, "viejo": viejo, "origen": origen, "comos": comos, "tecnicos": tecnicos}


def _fuentes(datos: dict) -> list[dict[str, Any]]:
    """Las fuentes del formulario; sin `fuentes`, una sola con los campos sueltos."""
    lista = datos.get("fuentes")
    if lista is None:
        lista = [{k: datos[k] for k in FUENTE if k in datos}]
    if not isinstance(lista, list) or not 1 <= len(lista) <= MAX_FUENTES:
        raise DatoInvalido(f"Fuentes: tiene que haber entre 1 y {MAX_FUENTES}")
    fuentes = []
    for i, d in enumerate(lista):
        if not isinstance(d, dict):
            raise DatoInvalido(f"Fuente {i + 1}: no tiene el formato esperado")
        nombre = str(d.get("nombre") or "").strip()[:60] or (f"Fuente {i + 1}" if len(lista) > 1 else "Logs")
        prefijo = f"{nombre} · " if len(lista) > 1 else ""
        f: dict[str, Any] = {"nombre": nombre}
        for k, (etiqueta, minimo, maximo, entero) in _CAMPOS_FUENTE.items():
            v = d.get(k)
            f[k] = _valor(FUENTE[k] if v in (None, "") else v, prefijo + etiqueta, minimo, maximo, entero)
        f["dias_hot"] = min(f["dias_hot"], f["retencion_dias"])
        fuentes.append(f)
    return fuentes


def _precios(otros: Any) -> dict[str, Any]:
    """Los de lista, con lo que se haya cargado a mano encima."""
    p = dict(PRECIOS, flavors={k: v[2] for k, v in FLAVORS.items()}, editados=False)
    if otros in (None, ""):
        return p
    if not isinstance(otros, dict):
        raise DatoInvalido("Precios: no tienen el formato esperado")
    for clave, etiqueta, _, minimo, maximo in PRECIOS_CAMPOS:
        v = otros.get(clave)
        if v in (None, ""):
            continue
        v = _valor(v, f"Precio: {etiqueta}", minimo, maximo)
        if abs(v - p[clave]) > 1e-12:
            p[clave], p["editados"] = v, True
    flavors = otros.get("flavors") or {}
    if not isinstance(flavors, dict):
        raise DatoInvalido("Precios: los de los nodos no tienen el formato esperado")
    for clave, v in flavors.items():
        if clave not in FLAVORS:
            raise DatoInvalido(f"Precios: no hay un flavor «{clave}»")
        if v in (None, ""):
            continue
        v = _valor(v, f"Precio: nodo de {_flavor(clave)}", 0, 100000)
        if abs(v - p["flavors"][clave]) > 1e-9:
            p["flavors"][clave], p["editados"] = v, True
    region = str(otros.get("region") or "").strip()[:60]
    if region and region != PRECIOS["region"]:
        p["region"], p["editados"] = region, True
    return p


def normalizar(datos: dict) -> dict[str, Any]:
    datos = datos if isinstance(datos, dict) else {}
    e: dict[str, Any] = {"fuentes": _fuentes(datos)}
    for k, (etiqueta, minimo, maximo, entero) in _CAMPOS.items():
        v = datos.get(k)
        e[k] = _valor(DEFAULTS[k] if v in (None, "") else v, etiqueta, minimo, maximo, entero)
    for k, (etiqueta, validas) in _OPCIONES.items():
        v = str(datos.get(k) or DEFAULTS[k])
        if v not in validas:
            raise DatoInvalido(f"{etiqueta}: «{v}» no es una de las opciones ({', '.join(validas)})")
        e[k] = v
    for k in ("respaldo", "respuesta_corta"):
        e[k] = _si(datos.get(k, DEFAULTS[k]))
    e["precios"] = _precios(datos.get("precios"))
    # Lo que la consola de Huawei no deja crear.
    minimo = MINIMO_POR_AZ[e["azs"]]
    for k, tipo in (("nodos_hot", "hot"), ("nodos_cold", "cold")):
        if 0 < e[k] < minimo:
            raise DatoInvalido(f"Nodos {tipo}: con {e['azs']} AZ, Huawei pide al menos {minimo}")
    if 0 < e["disco_hot_gb"] < DISCO_MIN_GB:
        raise DatoInvalido(f"Disco por nodo hot (GB): al menos {DISCO_MIN_GB}")
    if e["flavor_hot"] != "auto" and e["disco_hot_gb"] > FLAVORS[e["flavor_hot"]][3]:
        raise DatoInvalido(f"Disco por nodo hot (GB): el flavor de {_flavor(e['flavor_hot'])} admite hasta "
                           f"{_miles(FLAVORS[e['flavor_hot']][3])}")
    return e


def dias_con_log(dias: int, por_semana: int) -> int:
    """Días con log dentro de una retención en días calendario (en 30, 22 hábiles)."""
    return math.ceil(dias * por_semana / 7 - 1e-9)


def clientes(nodos_de_datos: int, azs: int = 1) -> int:
    """Nodos client según Huawei: ninguno con menos de 5 de datos, 2 entre 5 y 9, uno cada 5 desde 10;
    si hay, al menos uno por AZ (no guardan datos: que una AZ tenga uno más no desbalancea nada)."""
    n = 0 if nodos_de_datos < 5 else 2 if nodos_de_datos < 10 else math.ceil(nodos_de_datos / 5)
    return max(n, azs) if n else 0


def masters(nodos: int, modo: str = "regla") -> int:
    """Masters dedicados según Huawei: 3 con más de 10 nodos, 5 desde 50; «siempre», al menos 3."""
    n = 0 if nodos <= 10 else 3 if nodos < 50 else 5
    return max(n, 3) if modo == "siempre" else n


def _por_az(n: int, azs: int) -> int:
    return math.ceil(n / azs) * azs


def minimo_hot(e: dict) -> int:
    """Los nodos hot que se recomiendan: lo que piden las AZ, una copia por nodo y al menos 3 con réplica."""
    return _por_az(max(MINIMO_POR_AZ[e["azs"]], 1 + e["replicas"], 3 if e["replicas"] else 1), e["azs"])


def _falta(opcion: dict) -> float:
    """Cuánto le falta a una opción para cumplir: 1 o menos, nada."""
    return max(opcion.get("escritura", 0) / opcion["nodos"], opcion["disco_necesario_gb"] / opcion["disco_gb"])


def _orden(falta: float, usd: float) -> tuple:
    """Para elegir: la más barata entre las que cumplen; si ninguna cumple, la que menos falla."""
    no_cumple = falta > 1 + 1e-9
    return (no_cumple, falta if no_cumple else 0, usd)


def _hot(e: dict, disco: float, mb_s_pico: float) -> list[dict]:
    """Una opción por flavor hot: los nodos que pide la escritura, el disco y el mínimo (o los elegidos)."""
    p = e["precios"]
    copias = 1 + e["replicas"]
    claves = HOT if e["flavor_hot"] == "auto" else (e["flavor_hot"],)
    precio_disco = p["disco_ultra_gb"] if e["disco_hot"] == "ultra" else p["disco_high_gb"]
    opciones = []
    for clave in claves:
        vcpu, ram, _, disco_max = FLAVORS[clave]
        if e["disco_hot_gb"] > disco_max:
            continue
        escritura = math.ceil(mb_s_pico * copias / (vcpu * e["mb_s_por_vcpu"]) - 1e-9)
        n = e["nodos_hot"] or _por_az(max(minimo_hot(e), escritura,
                                          math.ceil(disco / (e["disco_hot_gb"] or disco_max) - 1e-9)), e["azs"])
        por_nodo = e["disco_hot_gb"] or min(disco_max, max(DISCO_MIN_GB, math.ceil(disco / n / 20 - 1e-9) * 20))
        usd_nodos, usd_disco = n * p["flavors"][clave], n * por_nodo * precio_disco
        opciones.append({"flavor": clave, "vcpu": vcpu, "ram": ram, "nodos": n, "disco_gb": por_nodo,
                         "disco_max_gb": disco_max, "disco_total_gb": disco, "disco_necesario_gb": disco / n,
                         "escritura": escritura, "usd_nodos": usd_nodos, "usd_disco": usd_disco,
                         "usd": usd_nodos + usd_disco})
    return opciones


def _cold(e: dict, gb_viejo: float) -> list[dict]:
    """Una opción por flavor cold para guardar `gb_viejo` (sin réplica, disco High I/O)."""
    p = e["precios"]
    disco = gb_viejo / USO_DISCO
    claves = COLD if e["flavor_cold"] == "auto" else (e["flavor_cold"],)
    opciones = []
    for clave in claves:
        vcpu, ram, _, disco_max = FLAVORS[clave]
        n = e["nodos_cold"] or _por_az(max(MINIMO_POR_AZ[e["azs"]], math.ceil(disco / disco_max - 1e-9)), e["azs"])
        if n > MAX_COLD:
            continue
        por_nodo = min(disco_max, max(DISCO_MIN_GB, math.ceil(disco / n / 20 - 1e-9) * 20))
        opciones.append({"flavor": clave, "vcpu": vcpu, "ram": ram, "nodos": n, "disco_gb": por_nodo,
                         "disco_necesario_gb": disco / n,
                         "usd": n * p["flavors"][clave] + n * por_nodo * p["disco_high_gb"]})
    return opciones


def costo_cliente(p: dict) -> float:
    return p["flavors"][FLAVOR_CLIENTE] + DISCO_CLIENTE_MASTER_GB * p["disco_ultra_gb"]


def costo_master(p: dict) -> float:
    return p["flavors"][FLAVOR_MASTER] + DISCO_CLIENTE_MASTER_GB * p["disco_high_gb"]


def _cluster(e: dict, hot: dict, gb_viejo: "float | None") -> dict:
    """El cluster con este hot: el cold que da el más barato que cumple (si va), client y masters."""
    p = e["precios"]
    mejor: dict = {}
    for cold in (_cold(e, gb_viejo) if gb_viejo is not None else [None]):
        datos = hot["nodos"] + (cold["nodos"] if cold else 0)
        cl = clientes(datos, e["azs"])
        ms = masters(datos + cl, e["masters"])
        c = {"hot": hot, "cold": cold, "clientes": cl, "masters": ms,
             "usd": hot["usd"] + (cold["usd"] if cold else 0) + cl * costo_cliente(p) + ms * costo_master(p)}
        c["orden"] = _orden(_falta(cold) if cold else 0, c["usd"])
        if not mejor or c["orden"] < mejor["orden"]:
            mejor = c
    return mejor


def _elegir(e: dict, disco_hot: float, mb_s_pico: float, gb_viejo: "float | None") -> dict:
    """El flavor hot que da el cluster más barato que cumple (con su cold, client y masters)."""
    clusters = [c for c in (_cluster(e, h, gb_viejo) for h in _hot(e, disco_hot, mb_s_pico)) if c]
    return min(clusters, key=lambda c: _orden(_falta(c["hot"]), c["usd"])) if clusters else {}


def _volumen(f: dict, crecimiento: float) -> dict[str, Any]:
    """Lo que trae una fuente: por día, en el pico y los días con log que se guardan."""
    segundos = f["horas_activas"] * 3600
    eventos_dia = f["gb_por_dia"] * 1e9 / f["bytes_por_evento"]
    total = dias_con_log(f["retencion_dias"], f["dias_por_semana"])
    hot = dias_con_log(f["dias_hot"], f["dias_por_semana"])
    return {"nombre": f["nombre"], "gb_dia": f["gb_por_dia"], "eventos_dia": eventos_dia,
            "eventos_s_promedio": eventos_dia / segundos, "eventos_s_pico": eventos_dia / segundos * f["pico"],
            "mb_s_pico": f["gb_por_dia"] * 1000 / segundos * f["pico"], "indice_gb_dia": f["gb_por_dia"] * crecimiento,
            "dias_mes": dias_con_log(365, f["dias_por_semana"]) / 12, "retencion_dias": f["retencion_dias"],
            "dias_hot": f["dias_hot"], "dias_con_log": total, "dias_con_log_hot": hot,
            "dias_con_log_viejo": max(0, total - hot)}


def _guardado(vols: list[dict], mes: "int | None" = None) -> tuple[float, float]:
    """GB de índice (una copia) en hot y en lo viejo al final de `mes`, o con todo lleno."""
    hot = viejo = 0.0
    for v in vols:
        acumulado = v["dias_con_log"] if mes is None else min(v["dias_con_log"], mes * v["dias_mes"])
        hot += v["indice_gb_dia"] * min(v["dias_con_log_hot"], acumulado)
        viejo += v["indice_gb_dia"] * max(0.0, acumulado - v["dias_con_log_hot"])
    return hot, viejo


def _mes(e: dict, cluster: dict, gb_hot: float, gb_viejo: float) -> dict:
    """Lo que se paga en un mes con estos GB guardados en hot y en lo viejo."""
    p = e["precios"]
    congelado = gb_viejo if e["resto"] == "congelados" else 0.0
    obs_gb = gb_hot + gb_viejo if e["respaldo"] else 0.0
    return {"cluster": cluster["usd"], "congelados": congelado * p["congelado_gb_hora"] * HORAS_MES,
            "congelado_gb": congelado, "obs": obs_gb * p["obs_gb"], "obs_gb": obs_gb}


def _enlace(e: dict, mb_s_pico: float, eventos_mes: float, log_gb_mes: float) -> dict:
    p = e["precios"]
    mbit = mb_s_pico * RED_SOBRE_LOG * 8 * (IPSEC if e["enlace"] == "vpn" else 1)
    r: dict[str, Any] = {"tipo": e["enlace"], "mbit_pico": mbit, "usd": 0.0, "detalle": ""}
    if e["enlace"] == "publico":
        # Con margen para ponerse al día después de un corte, sin pasar el máximo.
        contratado = min(MAX_PUBLICO_MBIT, max(10, math.ceil(mbit * 1.5 / 10) * 10))
        r.update(contratado_mbit=contratado, usd=contratado * p["ancho_banda_mbit"],
                 detalle=f"{contratado} Mbit/s de acceso público")
    elif e["enlace"] == "vpn":
        # El gateway con pago mensual (el de la calculadora); por hora sale más caro.
        vuelta_gb = eventos_mes * (RESPUESTA_CORTA_B if e["respuesta_corta"] else RESPUESTA_COMPLETA_B) * IPSEC / 1024 ** 3
        eips = 2 * p["eip_hora"] * HORAS_MES
        trafico = vuelta_gb * p["trafico_salida_gb"]
        r.update(ida_gb_mes=log_gb_mes * RED_SOBRE_LOG * IPSEC, vuelta_gb_mes=vuelta_gb,
                 gateway_mes=p["vpn_gateway_mes"], gateway_por_hora=p["vpn_gateway_hora"] * HORAS_MES,
                 eips_usd=eips, trafico_usd=trafico, usd=p["vpn_gateway_mes"] + eips + trafico,
                 detalle=f"gateway VPN Professional 1 (US$ {_miles(p['vpn_gateway_mes'])} por mes), sus 2 EIP y "
                         f"{_miles(vuelta_gb)} GB de respuestas que salen del cluster")
    return r


# Cómo nombra la calculadora de Huawei Cloud cada cosa (export de una cotización
# real): el modo de pago, la familia de cada flavor y los discos.
MODOS_CALCULADORA = {"mensual": ("Monthly", "1 month"), "anual": ("Yearly", "1 year"), "por_hora": ("Pay-per-use", "730 hours")}
FAMILIAS = {"4u8g": "Compute-intensive", "8u16g": "Compute-intensive", "4u16g": "General computing",
            "8u32g": "General computing", "16u64g": "General computing", "4u32g": "Memory-optimized",
            "8u64g": "Memory-optimized", "16u128g": "Memory-optimized"}
CALCULADORA_URL = "https://www.huaweicloud.com/intl/en-us/pricing/calculator.html"


def _tipo_en_calculadora(clave: str, n: int) -> str:
    vcpu, ram, _, _ = FLAVORS[clave]
    return f"x86 | {FAMILIAS.get(clave, '')} | ess.spec-{clave} | {vcpu}vCPUs | {ram}GB × {n}"


def _calculadora(e: dict, cluster: dict, lleno: dict, enlace: dict, tipo_disco: str) -> dict[str, Any]:
    """Qué cargar en la calculadora de Huawei Cloud, ítem por ítem, con los campos
    que ella muestra y el precio que tendría que dar cada uno en el modo de pago
    elegido. Suma lo mismo que la cotización."""
    p, pago = e["precios"], e["pago"]
    modo, cantidad = MODOS_CALCULADORA[pago]
    anual, por_hora = pago == "anual", pago == "por_hora"
    por_uso = "8760 hours" if anual else "730 hours"     # lo que se paga por uso, en el período
    meses = 12 if anual else 1
    items: list[dict] = []

    def nodos(descripcion: str, clave: str, n: int, disco: str, disco_gb: int, precio_gb: float) -> None:
        usd_nodos, usd_disco = n * p["flavors"][clave], n * disco_gb * precio_gb
        if anual:
            usd_nodos, usd_disco = usd_nodos * p["meses_pago_anual"], usd_disco * p["meses_pago_anual"]
        elif por_hora:
            usd_nodos *= 1 + p["recargo_por_hora"]
        items.append({"servicio": "Cloud Search Service", "descripcion": descripcion, "modo": modo, "cantidad": cantidad,
                      "campos": [["Type", _tipo_en_calculadora(clave, n), usd_nodos],
                                 ["Node Storage Type", f"{disco} | {disco_gb}GB × {n}", usd_disco]],
                      "usd": usd_nodos + usd_disco})

    hot, cold = cluster["hot"], cluster["cold"]
    if cluster["masters"]:
        nodos("Nodos Master", FLAVOR_MASTER, cluster["masters"], "High I/O", DISCO_CLIENTE_MASTER_GB, p["disco_high_gb"])
    nodos("Nodos Hot", hot["flavor"], hot["nodos"], tipo_disco, hot["disco_gb"],
          p["disco_ultra_gb"] if tipo_disco == "Ultra-high I/O" else p["disco_high_gb"])
    if cluster["clientes"]:
        nodos("Nodos Client", FLAVOR_CLIENTE, cluster["clientes"], "Ultra-high I/O", DISCO_CLIENTE_MASTER_GB, p["disco_ultra_gb"])
    if cold:
        nodos("Nodos Cold", cold["flavor"], cold["nodos"], "High I/O", cold["disco_gb"], p["disco_high_gb"])
    if lleno["congelado_gb"]:
        usd = lleno["congelados"] * meses
        items.append({"servicio": "Cloud Search Service", "descripcion": "Índices congelados", "modo": "Pay-per-use",
                      "cantidad": por_uso, "campos": [["Frozen index storage", f"{round(lleno['congelado_gb'])}GB", usd]],
                      "usd": usd, "nota": f"Si la calculadora no los tiene, sumalos aparte: se pagan US$ "
                                          f"{_dec(p['congelado_gb_hora'], 8)} por GB y por hora."})
    if lleno["obs_gb"]:
        if anual:
            tb = math.ceil(lleno["obs_gb"] / 1024 - 1e-9)
            items.append({"servicio": "Object Storage Service", "descripcion": "Respaldo", "modo": "Yearly", "cantidad": "1 year",
                          "campos": [["Storage Space", f"{tb}TB", tb * p["obs_tb_anio"]]], "usd": tb * p["obs_tb_anio"]})
        else:
            items.append({"servicio": "Object Storage Service", "descripcion": "Respaldo", "modo": "Pay-per-use",
                          "cantidad": por_uso, "campos": [["Storage Space", f"Standard | {round(lleno['obs_gb'])}GB", lleno["obs"]]],
                          "usd": lleno["obs"]})
    if enlace["tipo"] == "vpn":
        gateway = enlace["gateway_por_hora"] if por_hora else enlace["gateway_mes"] * meses
        items.append({"servicio": "Virtual Private Network", "descripcion": "VPN", "modo": "Pay-per-use" if por_hora else "Monthly",
                      "cantidad": "730 hours" if por_hora else "12 months" if anual else "1 month",
                      "campos": [["Specification", "Professional 1", gateway]], "usd": gateway})
        trafico, eips = enlace["trafico_usd"] * meses, enlace["eips_usd"] * meses
        items.append({"servicio": "Elastic IP", "descripcion": "Elastic IP VPN", "modo": "Pay-per-use", "cantidad": por_uso,
                      "campos": [["Bandwidth Price", f"Dedicated | Dynamic BGP | Traffic | {round(enlace['vuelta_gb_mes'] * meses)}GB", trafico],
                                 ["EIP Price", "2", eips]],
                      "usd": trafico + eips})
    elif enlace["tipo"] == "publico":
        usd = enlace["usd"] * meses
        items.append({"servicio": "Elastic IP", "descripcion": "Elastic IP acceso público", "modo": "Monthly",
                      "cantidad": "12 months" if anual else "1 month",
                      "campos": [["Bandwidth Price", f"Dedicated | Dynamic BGP | Bandwidth | {enlace['contratado_mbit']}Mbit/s", usd],
                                 ["EIP Price", "1", 0.0]],
                      "usd": usd})
    # Numerados como en el export: «Cloud Search Service 1», «Elastic IP 2»...
    vistos: dict[str, int] = {}
    for it in items:
        vistos[it["servicio"]] = vistos.get(it["servicio"], 0) + 1
        it["servicio_n"] = f"{it['servicio']} {vistos[it['servicio']]}"
        it["region"], it["az"] = p["region"], "General AZ"
    return {"url": CALCULADORA_URL, "region": p["region"], "periodo": "por año" if anual else "por mes",
            "items": items, "total": sum(it["usd"] for it in items)}


def _gb(x: float) -> str:
    return f"{_dec(x / 1000)} TB" if x >= 1000 else f"{_miles(x)} GB"


def _respuestas(e: dict, vols: list[dict], simple: dict, hay_resto: bool, a_mano: list[str]) -> list[dict[str, str]]:
    """Lo que se pidió, en palabras: una línea por fuente y una por cada pregunta general."""
    comos = simple["comos"] + [None] * (len(vols) - len(simple["comos"]))
    filas = []
    for f, v, como in zip(e["fuentes"], vols, comos):
        cuanto = como["cuanto"] if como else f"{_dec(f['gb_por_dia'], 2)} GB de log por día"
        cuando = (como["cuando"] if como else
                  f"{f['dias_por_semana']} días por semana, casi todo en {_dec(f['horas_activas'])} h").lower()
        consulta = ("todo lo guardado" if f["dias_hot"] >= f["retencion_dias"] else
                    f"lo de los últimos {_plazo(f['dias_hot'])}")
        filas.append({"que": f["nombre"], "respuesta": f"{cuanto}, {cuando}. Se guarda {_plazo(f['retencion_dias'])} y se "
                                                        f"consulta seguido {consulta}."})
    filas.append({"que": "Qué tan crítico es", "respuesta": CRITICIDAD[simple["criticidad"]][0]})
    if hay_resto:
        filas.append({"que": "Lo que ya no se consulta seguido", "respuesta": LO_VIEJO[simple["viejo"]][0]})
    filas.append({"que": "Dónde están los sistemas que generan los logs", "respuesta": ORIGEN[simple["origen"]][0]})
    filas.append({"que": "Cómo se va a pagar", "respuesta": PAGO[e["pago"]]})
    if simple["tecnicos"] or a_mano:
        filas.append({"que": "Ajustes técnicos", "respuesta": "Se cambiaron a mano: "
                      + ", ".join(sorted(set(simple["tecnicos"]) | set(a_mano))) + "."})
    return filas


def _explicacion(e: dict, vols: list[dict], cluster: dict, enlace: dict, hay_resto: bool, a_pagar: tuple) -> list[str]:
    """El dimensionamiento en palabras simples, para quien no conoce CSS."""
    hot, cold = cluster["hot"], cluster["cold"]
    copias = 1 + e["replicas"]
    gb_dia = sum(v["gb_dia"] for v in vols)
    lineas = [f"Llegan unos {_gb(gb_dia)} de logs por día; en el momento de más carga, unos "
              f"{_miles(sum(v['eventos_s_pico'] for v in vols))} eventos por segundo."]
    hot_dias = sorted({min(v["dias_hot"], v["retencion_dias"]) for v in vols})
    if not hay_resto:
        plazo = "Todo lo que se guarda"
    elif len(hot_dias) == 1:
        plazo = f"Lo de los últimos {_plazo(hot_dias[0])}"
    else:
        plazo = f"Lo más reciente (de {_plazo(hot_dias[0])} a {_plazo(hot_dias[-1])}, según la fuente)"
    lineas.append(f"{plazo} queda listo para consultar al instante, en {hot['nodos']} servidores de {hot['vcpu']} vCPU y "
                  f"{hot['ram']} GB de memoria con discos rápidos.")
    if hay_resto:
        retencion = _plazo(max(v["retencion_dias"] for v in vols))
        if e["resto"] == "congelados":
            lineas.append(f"Lo más viejo, hasta {retencion}, pasa a un almacenamiento de bajo costo (índices congelados): se "
                          "sigue consultando, pero cada búsqueda tarda de segundos a minutos.")
        else:
            lineas.append(f"Lo más viejo, hasta {retencion}, pasa a {cold['nodos']} servidores más baratos (nodos cold), "
                          "con discos de menor costo, y se sigue consultando rápido.")
    if e["replicas"] == 0:
        lineas.append("Cada dato se guarda una sola vez: si se cae un servidor, sus datos no se consultan hasta que vuelve. "
                      "Sirve para una prueba, no para producción.")
    elif e["azs"] > 1:
        lineas.append(f"Los servidores se reparten en {e['azs']} zonas del datacenter de Huawei y cada dato tiene {copias} "
                      "copias en zonas distintas: si se cae una zona, sigue andando.")
    else:
        lineas.append(f"Cada dato tiene {copias} copias en servidores distintos: si se cae un servidor, sigue andando.")
    extra = [f"{cluster['clientes']} que reparten las consultas" if cluster["clientes"] else "",
             f"{cluster['masters']} que coordinan el cluster" if cluster["masters"] else ""]
    if any(extra):
        lineas.append(f"Por el tamaño, Huawei pide sumar servidores de apoyo: {' y '.join(x for x in extra if x)}.")
    if e["respaldo"]:
        lineas.append("Además, todo lo guardado se respalda en OBS, para poder recuperarlo.")
    if enlace["tipo"] == "interno":
        lineas.append("Los logs llegan por la red interna de Huawei Cloud, sin costo de tráfico.")
    elif enlace["tipo"] == "vpn":
        lineas.append(f"Los logs llegan por una VPN; en el pico, unos {_miles(enlace['mbit_pico'])} Mbit/s.")
    else:
        lineas.append(f"Los logs llegan por internet al acceso público del cluster, con {enlace['contratado_mbit']} Mbit/s "
                      "contratados.")
    usd, periodo = a_pagar
    lineas.append({"mensual": f"Pagando por mes: US$ {_miles(usd)} por mes, cuando ya está todo guardado.",
                   "anual": f"Pagando por año: US$ {_miles(usd)} por año, cuando ya está todo guardado (el año cuesta "
                            f"{_dec(e['precios']['meses_pago_anual'])} meses de servidores y disco).",
                   "por_hora": f"Pagando por hora, sin compromiso: unos US$ {_miles(usd)} por mes, cuando ya está todo "
                               "guardado."}[e["pago"]])
    return lineas


def cotizar(datos: dict) -> dict[str, Any]:
    datos = datos if isinstance(datos, dict) else {}
    tecnico, simple = _simple(datos)
    e = normalizar(tecnico)
    p = e["precios"]
    azs, copias = e["azs"], 1 + e["replicas"]
    vols = [_volumen(f, e["crecimiento"]) for f in e["fuentes"]]
    varias = len(vols) > 1

    # Lo que pide el conjunto: los picos se suman (como si coincidieran) y cada
    # fuente guarda en hot sus días más uno.
    mb_s_pico = sum(v["mb_s_pico"] for v in vols)
    disco_hot = sum(v["indice_gb_dia"] * copias * (v["dias_con_log_hot"] + 1) for v in vols) / USO_DISCO
    gb_hot, gb_viejo = _guardado(vols)
    hay_resto = gb_viejo > 0
    usa_cold = hay_resto and e["resto"] == "cold"

    cluster = _elegir(e, disco_hot, mb_s_pico, gb_viejo if usa_cold else None)
    if not cluster:
        if usa_cold:
            raise DatoInvalido("Lo viejo no entra en 32 nodos de datos cold: guardá menos días en cold o usá índices congelados")
        raise DatoInvalido("Disco por nodo hot (GB): ningún flavor hot admite tanto disco por nodo")
    hot, cold = cluster["hot"], cluster["cold"]
    datos_nodos = hot["nodos"] + (cold["nodos"] if cold else 0)

    # Primer año, mes a mes: el hot se contrata entero desde el primer mes; lo
    # viejo (congelados, cold) y el respaldo crecen hasta llenarse.
    progresion = []
    primer_anio = primer_anio_anual = 0.0
    for m in range(1, 13):
        en_hot, viejo = _guardado(vols, m)
        c_mes = (_cluster(e, hot, viejo) or cluster) if usa_cold else cluster
        mes = _mes(e, c_mes, en_hot, viejo)
        total = mes["cluster"] + mes["congelados"] + mes["obs"]
        primer_anio += total
        primer_anio_anual += mes["cluster"] * p["meses_pago_anual"] / 12 + mes["congelados"] + mes["obs"]
        progresion.append({"mes": m, "gb_hot": en_hot, "gb_viejo": viejo, "gb_respaldo": mes["obs_gb"],
                           "nodos_cold": (c_mes["cold"] or {}).get("nodos", 0) if usa_cold else 0, "usd": total})

    # Con todo lo guardado (el mes en que se llena la retención y los siguientes).
    lleno = _mes(e, cluster, gb_hot, gb_viejo)
    enlace = _enlace(e, mb_s_pico, sum(v["eventos_dia"] * v["dias_mes"] for v in vols),
                     sum(v["gb_dia"] * v["dias_mes"] for v in vols))
    tipo_disco = "Ultra-high I/O" if e["disco_hot"] == "ultra" else "High I/O"
    costos = [
        {"concepto": "Nodos de datos hot", "detalle": f"{hot['nodos']} de {hot['vcpu']} vCPU y {hot['ram']} GB",
         "usd": hot["usd_nodos"]},
        {"concepto": "Disco de los nodos hot", "detalle": f"{hot['nodos']} × {_miles(hot['disco_gb'])} GB {tipo_disco}",
         "usd": hot["usd_disco"]},
    ]
    if cold:
        costos.append({"concepto": "Nodos de datos cold",
                       "detalle": f"{cold['nodos']} de {cold['vcpu']} vCPU y {cold['ram']} GB, con {_miles(cold['disco_gb'])} GB High I/O cada uno",
                       "usd": cold["usd"]})
    if cluster["clientes"]:
        costos.append({"concepto": "Nodos client", "detalle": f"{cluster['clientes']} de {_flavor(FLAVOR_CLIENTE)}",
                       "usd": cluster["clientes"] * costo_cliente(p)})
    if cluster["masters"]:
        costos.append({"concepto": "Masters dedicados", "detalle": f"{cluster['masters']} de {_flavor(FLAVOR_MASTER)}",
                       "usd": cluster["masters"] * costo_master(p)})
    if lleno["congelado_gb"]:
        costos.append({"concepto": "Índices congelados", "detalle": f"{_miles(lleno['congelado_gb'])} GB en OBS, una copia",
                       "usd": lleno["congelados"]})
    if e["respaldo"]:
        costos.append({"concepto": "Respaldo en OBS", "detalle": f"{_miles(lleno['obs_gb'])} GB, un snapshot de todo lo guardado",
                       "usd": lleno["obs"]})
    if enlace["usd"]:
        costos.append({"concepto": "Enlace", "detalle": enlace["detalle"], "usd": enlace["usd"]})
    total_mes = sum(c["usd"] for c in costos)
    fl = p["flavors"]
    nodos_mes = (hot["usd_nodos"] + (cold["nodos"] * fl[cold["flavor"]] if cold else 0)
                 + cluster["clientes"] * fl[FLAVOR_CLIENTE] + cluster["masters"] * fl[FLAVOR_MASTER])
    obs_paquete = math.ceil(lleno["obs_gb"] / 1024 - 1e-9) * p["obs_tb_anio"] if lleno["obs_gb"] else 0.0
    anio_siguiente = total_mes * 12
    anio_siguiente_anual = (cluster["usd"] * p["meses_pago_anual"] + obs_paquete + lleno["congelados"] * 12
                            + enlace["usd"] * 12)
    primer_anio += enlace["usd"] * 12
    primer_anio_anual += enlace["usd"] * 12

    # Shards: un índice por día y por fuente, de hasta 50 GB, en múltiplos de los nodos hot.
    fuentes = []
    for v in vols:
        s = max(1, math.ceil(v["indice_gb_dia"] / SHARD_MAX_GB - 1e-9))
        s = math.ceil(s / hot["nodos"]) * hot["nodos"]
        fuentes.append({**{k: v[k] for k in ("nombre", "gb_dia", "eventos_s_pico", "mb_s_pico", "indice_gb_dia",
                                             "retencion_dias", "dias_hot", "dias_con_log", "dias_con_log_hot")},
                        "shards": s, "gb_por_shard": v["indice_gb_dia"] / s,
                        "gb_hot": v["indice_gb_dia"] * v["dias_con_log_hot"],
                        "gb_viejo": v["indice_gb_dia"] * v["dias_con_log_viejo"]})
    shards = sum(f["shards"] for f in fuentes)
    capacidad = hot["nodos"] * hot["vcpu"] * MB_S_POR_VCPU_MEDIDO
    uso_pico = mb_s_pico / capacidad if capacidad else 0
    datos_por_nodo = hot["disco_total_gb"] * USO_DISCO / hot["nodos"]
    a_mano = [k for k in ("flavor_hot", "nodos_hot", "disco_hot_gb", "flavor_cold", "nodos_cold")
              if e[k] not in ("auto", 0)] + (["disco_hot"] if e["disco_hot"] != "ultra" else []) \
        + (["masters"] if e["masters"] != "regla" else [])

    avisos = []
    if uso_pico > 0.8:
        avisos.append(f"En el pico el cluster usaría cerca del {_pct(uso_pico)} de lo que medimos que escribe: sumá nodos hot "
                      "o validalo con una prueba de carga.")
    elif hot["nodos"] < hot["escritura"]:
        avisos.append(f"Para escribir el pico, la fórmula de Huawei pide {hot['escritura']} nodos hot de {hot['vcpu']} vCPU "
                      f"y hay {hot['nodos']}.")
    if hot["disco_necesario_gb"] > hot["disco_gb"] + 0.5:
        avisos.append(f"Lo que se guarda en hot pide unos {_miles(hot['disco_necesario_gb'])} GB de disco por nodo y hay "
                      f"{_miles(hot['disco_gb'])}: no entra. Sumá nodos o disco, o guardá menos días en hot.")
    if datos_por_nodo > GB_POR_NODO_GUIA:
        avisos.append(f"Cada nodo hot guardaría unos {_miles(datos_por_nodo)} GB de datos: la guía de Huawei habla de 100 a 1.000 GB "
                      "por nodo. Entra en lo que admite la consola, pero conviene confirmarlo con Huawei.")
    if e["nodos_hot"] and e["nodos_hot"] < minimo_hot(e):
        avisos.append(f"Con {e['replicas']} réplica{'s' if e['replicas'] != 1 else ''} y {azs} AZ se recomiendan al menos "
                      f"{minimo_hot(e)} nodos hot: Huawei aconseja 3 nodos de datos para la disponibilidad.")
    for k, tipo in (("nodos_hot", "hot"), ("nodos_cold", "cold")):
        if e[k] and e[k] % azs:
            avisos.append(f"Con {azs} AZ, Huawei aconseja que los nodos {tipo} sean múltiplo de {azs}: con {e[k]}, una AZ "
                          "queda con más nodos que otra.")
    if cold and cold["disco_necesario_gb"] > cold["disco_gb"] + 0.5:
        avisos.append(f"Lo viejo pide unos {_miles(cold['disco_necesario_gb'])} GB por nodo cold y el flavor admite "
                      f"{_miles(cold['disco_gb'])}: no entra. Sumá nodos cold o usá índices congelados.")
    if e["disco_hot"] == "high":
        avisos.append("Lo medido fue con disco Ultra-high I/O: con High I/O la escritura puede ser menor; validalo con una "
                      "prueba de carga.")
    if e["replicas"] == 0:
        avisos.append("Sin réplica, si cae un nodo sus datos no se consultan hasta que vuelve, y Huawei pide una réplica para "
                      "que varias AZ protejan los datos.")
    if azs == 3:
        avisos.append("Con 3 AZ, cada tipo de nodo va de a 3 (uno por AZ). No todas las regiones tienen 3 AZ: confirmalo en "
                      "la consola.")
    if hay_resto and not e["respaldo"]:
        avisos.append("Los índices congelados y los nodos cold guardan una sola copia: sin respaldo en OBS no hay de dónde "
                      "recuperarlos.")
    if hay_resto and e["resto"] == "congelados" and e["respaldo"]:
        avisos.append("Respaldá cada índice antes de congelarlo: respaldar uno ya congelado fue 10 veces más lento.")
    if e["enlace"] == "publico" and enlace["mbit_pico"] > MAX_PUBLICO_MBIT:
        avisos.append(f"El pico pide unos {_miles(enlace['mbit_pico'])} Mbit/s y el acceso público llega a {MAX_PUBLICO_MBIT}: "
                      "usá una VPN.")
    if e["enlace"] == "vpn" and enlace["mbit_pico"] > MAX_VPN_MBIT * 0.85:
        avisos.append(f"El pico pide unos {_miles(enlace['mbit_pico'])} Mbit/s: una VPN Professional 1 dio unos {MAX_VPN_MBIT} "
                      "por sentido, con poco margen para ponerse al día después de un corte.")
    if e["enlace"] == "vpn":
        avisos.append(f"El costo de la VPN es el lado de {p['region']}. El del origen (su gateway y el tráfico que sale de su "
                      "región) depende de dónde están sus servidores.")
    if datos_nodos > 32 and not cluster["masters"]:
        avisos.append("Más de 32 nodos de datos exige masters dedicados.")

    resumen = (f"{hot['nodos']} nodos hot de {hot['vcpu']} vCPU y {hot['ram']} GB con {_gb(hot['disco_gb'])} cada uno"
               + (f", {cold['nodos']} cold de {cold['vcpu']} vCPU con {_gb(cold['disco_gb'])}" if cold else "")
               + (f", {cluster['clientes']} client" if cluster["clientes"] else "")
               + (f" y {cluster['masters']} masters" if cluster["masters"] else "")
               + (f", repartidos en {azs} AZ" if azs > 1 else ""))

    supuestos = [
        ("Disco al 80 % visto por OpenSearch", "Los datos ocupan como máximo el 74 % del disco contratado: OpenSearch cuenta como "
         "usado el 5 % que reserva el sistema de archivos (medido: 1.180 GB de 1.200, con 60 reservados). A partir del 85 % deja "
         "de asignar réplicas."),
        ("Un día más de disco", "Mientras se llena el índice de hoy, el más viejo todavía no se borró."),
        (f"El índice crece un {_pct(e['crecimiento'] - 1)} sobre el log", "Con el códec zstd_no_dict; medido en un OMS FIX "
         "(31 % con 103 millones de mensajes). Huawei supone un 25 %. Depende de cuántos campos se parsean."),
        (f"{_dec(e['mb_s_por_vcpu'], 2)} MB/s de escritura por vCPU", "Fórmula de Huawei, contando la primaria y la réplica. En el "
         f"máximo medimos unos {_dec(MB_S_POR_VCPU_MEDIDO)} MB/s de log por vCPU con las dos copias: con eso sale el uso en el pico."),
        ("Nodos según las AZ", "Con 2 AZ, al menos 4 nodos de cada tipo de datos (2 por AZ); con 3, al menos 3; siempre en "
         "múltiplos de las AZ, para que cada AZ tenga los mismos. Huawei recomienda al menos 3 nodos de datos para la "
         "disponibilidad."),
        ("Nodos client y masters", "Client: ninguno con menos de 5 nodos de datos, 2 entre 5 y 9 y uno cada 5 desde 10, con al "
         "menos uno por AZ. Masters dedicados: 3 con más de 10 nodos y 5 desde 50 (reglas de Huawei)."),
        ("Nodos cold", "Al menos los que piden las AZ, hasta 32, sin réplica y con disco High I/O; se pueden sumar después de "
         "crear el cluster. En automático se elige el tamaño más barato."),
        ("Índices congelados", "Los datos pasan a OBS y se consultan sin restaurar, de segundos a minutos por consulta; el disco "
         "local se libera solo (medido: en menos de 40 minutos)."),
        ("Respaldo en OBS", "Un snapshot de todo lo guardado, en un bucket Standard (la consola de CSS no acepta otra clase). "
         "Restaurar anduvo a 41 MB/s por nodo."),
        ("Shards de hasta 50 GB", "Un índice por día y por fuente, con los shards en múltiplos de los nodos hot (recomendación "
         "de Huawei)."),
        ("Días con log", "Los días por semana de cada fuente: con 5, en 30 días calendario hay 22 con log."),
        ("Pago anual y por hora", f"El año cuesta {_dec(p['meses_pago_anual'])} meses, en nodos y en disco. Por hora, los nodos "
         f"cuestan cerca de un {_pct(p['recargo_por_hora'])} más. El respaldo y los congelados se pagan por uso; el respaldo, "
         "con paquete anual cuando deja de crecer."),
        ("Precios", f"Cargados a mano para {p['region']}, sin impuestos ni descuentos." if p["editados"] else
         f"Lista de Huawei Cloud para {p['region']}, consultada en {p['consultado']}, sin impuestos ni descuentos."),
    ]
    if varias:
        supuestos.insert(0, ("Varias fuentes en un cluster", "Los picos se suman, como si coincidieran; cada fuente tiene su "
                                                              "índice por día, su retención y sus días en hot."))
    if a_mano:
        supuestos.insert(0, ("Hardware elegido a mano", "Se respeta lo que se cargó (flavor, nodos, disco o masters); los "
                                                         "avisos dicen si no alcanza."))
    if e["enlace"] != "interno":
        supuestos.append(("Red", "Unos 0,45 bytes por byte de log con gzip y HTTPS (150 de 333, medido), más un 5 % de IPsec "
                                 f"en una VPN; de vuelta, {RESPUESTA_CORTA_B} bytes por mensaje con la respuesta corta de "
                                 f"Fluent Bit y {RESPUESTA_COMPLETA_B} sin ella."))

    gb_dia = sum(v["gb_dia"] for v in vols)
    eventos_dia = sum(v["eventos_dia"] for v in vols)
    # Por hora, los nodos llevan el recargo y el gateway VPN se paga por hora (sale más que el mensual).
    por_hora_mes = total_mes + nodos_mes * p["recargo_por_hora"] + (
        enlace["gateway_por_hora"] - enlace["gateway_mes"] if enlace["tipo"] == "vpn" else 0)
    a_pagar = {"mensual": (total_mes, "por mes"), "anual": (anio_siguiente_anual, "por año"),
               "por_hora": (por_hora_mes, "por mes")}[e["pago"]]
    calculadora = _calculadora(e, cluster, lleno, enlace, tipo_disco)
    respuestas = _respuestas(e, vols, simple, hay_resto, a_mano)
    explicacion = _explicacion(e, vols, cluster, enlace, hay_resto, a_pagar)
    return {
        "entrada": {k: v for k, v in e.items() if k != "precios"},
        "respuestas": respuestas,
        "explicacion": explicacion,
        "a_pagar": {"modo": e["pago"], "texto": PAGO[e["pago"]], "usd": a_pagar[0], "periodo": a_pagar[1]},
        "calculadora": calculadora,
        "resumen": resumen,
        "fuentes": fuentes,
        "volumen": {"gb_dia": gb_dia, "eventos_dia": eventos_dia,
                    "eventos_s_promedio": sum(v["eventos_s_promedio"] for v in vols),
                    "eventos_s_pico": sum(v["eventos_s_pico"] for v in vols), "mb_s_pico": mb_s_pico,
                    "indice_gb_dia": sum(v["indice_gb_dia"] for v in vols), "gb_hot": gb_hot, "gb_viejo": gb_viejo},
        "cluster": {"hot": {k: hot[k] for k in ("flavor", "vcpu", "ram", "nodos", "disco_gb", "escritura")} | {"disco": tipo_disco},
                    "cold": {k: cold[k] for k in ("flavor", "vcpu", "ram", "nodos", "disco_gb")} if cold else None,
                    "clientes": cluster["clientes"], "masters": cluster["masters"], "shards": shards,
                    "gb_por_shard": max(f["gb_por_shard"] for f in fuentes), "uso_pico": uso_pico,
                    "datos_por_nodo_gb": datos_por_nodo, "a_mano": a_mano},
        "costos": costos,
        "total_mes": total_mes,
        "anual": {"por_hora_mes": por_hora_mes,
                  "primer_anio": primer_anio, "primer_anio_pago_anual": primer_anio_anual,
                  "anio_siguiente": anio_siguiente, "anio_siguiente_pago_anual": anio_siguiente_anual},
        "progresion": progresion,
        "enlace": enlace,
        "avisos": avisos,
        "supuestos": [{"que": a, "fuente": b} for a, b in supuestos],
        "precios": {**{k: v for k, v in p.items() if k != "flavors"},
                    "flavors": {k: {"vcpu": v[0], "ram": v[1], "usd_mes": p["flavors"][k]} for k, v in FLAVORS.items()}},
    }


def valores() -> dict[str, Any]:
    """Lo que el formulario necesita para armarse: valores por defecto, flavors y precios de lista."""
    return {
        "simple": SIMPLE,
        "fuente_simple": FUENTE_SIMPLE,
        "opciones": {
            "unidad": UNIDADES,
            "horario": {k: v[0] for k, v in HORARIOS.items()},
            "criticidad": {k: v[0] for k, v in CRITICIDAD.items()},
            "viejo": {k: v[0] for k, v in LO_VIEJO.items()},
            "origen": {k: v[0] for k, v in ORIGEN.items()},
            "pago": PAGO,
        },
        "calculadora_url": CALCULADORA_URL,
        "defaults": DEFAULTS,
        "fuente": FUENTE,
        "max_fuentes": MAX_FUENTES,
        "region": PRECIOS["region"],
        "consultado": PRECIOS["consultado"],
        "precios": {clave: PRECIOS[clave] for clave, *_ in PRECIOS_CAMPOS},
        "precios_campos": [{"clave": c, "etiqueta": et, "unidad": u, "minimo": lo, "maximo": hi}
                           for c, et, u, lo, hi in PRECIOS_CAMPOS],
        "flavors": [{"clave": k, "vcpu": v[0], "ram": v[1], "usd_mes": v[2], "disco_max_gb": v[3],
                     "datos": bool(v[3]), "uso": "masters" if k == FLAVOR_MASTER else "datos y client" if k == FLAVOR_CLIENTE
                     else "datos"} for k, v in FLAVORS.items()],
    }
