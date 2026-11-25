"""
cotizador.py
============

La cotización de un cluster CSS OpenSearch a partir de un formulario: cuántos
logs llegan, cuánto se guarda y dónde, y con qué disponibilidad. Devuelve el
cluster (nodos, flavor, disco, shards), el costo por mes desglosado, el primer
año mes a mes y los supuestos con su fuente.

El modelo es el que se validó con un cluster de 30 sesiones de un OMS FIX en
LA-Santiago (pico sostenido 20 minutos, máximo medido, caída de nodos, índices
congelados, respaldo y restauración en OBS, VPN entre regiones):

- Disco: log × crecimiento del índice × copias × (días con log + 1) ÷ 0,74. El
  0,74 deja el disco al 80 % visto por OpenSearch, que cuenta como usado el 5 %
  que reserva ext4 (de 1.200 GB contratados el nodo ve 1.180).
- Nodos de escritura: la fórmula de Huawei, pico (MB/s) × copias ÷ (vCPU por
  nodo × 1 MB/s). Con dos AZ, en cantidad par y al menos 4. Se elige el flavor
  más barato que la cumple, contando nodos client y masters.
- Nodos cold: con dos AZ, al menos 4 y en cantidad par, hasta 32.
- Índices congelados: se pagan por GB y por hora y liberan solos el disco local.

Es una estimación: el número fino sale de una prueba con los datos del cliente.
"""

from __future__ import annotations

import math
from typing import Any

# ── Precios ─────────────────────────────────────────────────────────────────
# Lista de Huawei Cloud para LA-Santiago, sin impuestos ni descuentos. Por mes
# salvo que diga otra cosa. El pago anual cuesta 10 meses (nodos y disco).
PRECIOS: dict[str, Any] = {
    "region": "LA-Santiago",
    "consultado": "noviembre de 2026",
    "disco_ultra_gb": 0.171,        # Ultra-high I/O, nodos hot y client
    "disco_high_gb": 0.051,         # High I/O, nodos cold y masters
    "obs_gb": 0.021,                # OBS Standard, una AZ, pago por uso
    "obs_tb_anio": 174.18,          # paquete anual de OBS, por TB (1.024 GB)
    "congelado_gb_hora": 0.00003209,
    "ancho_banda_mbit": 8.016,      # acceso público del cluster
    "vpn_gateway_hora": 0.33,       # VPN Enterprise Professional 1, pago por uso
    "eip_hora": 0.005,              # reserva de una EIP
    "trafico_salida_gb": 0.081,     # EIP por tráfico, hasta 10 TB por mes
    "meses_pago_anual": 10,
    "recargo_por_hora": 0.39,       # nodos con pago por uso, sobre el precio mensual
}
HORAS_MES = 730
# (vCPU, RAM GB, US$ por mes, disco máximo por nodo en GB)
FLAVORS: dict[str, tuple[int, int, float, int]] = {
    "8u32g": (8, 32, 254.63, 3200),
    "16u64g": (16, 64, 507.68, 6400),
    "4u16g": (4, 16, 127.05, 0),
    "4u32g": (4, 32, 165.90, 2560),
    "8u64g": (8, 64, 332.33, 5120),
    "16u128g": (16, 128, 664.65, 10240),
}
HOT = ("8u32g", "16u64g")
COLD = ("4u32g", "8u64g", "16u128g", "16u64g")
CLIENTE = FLAVORS["8u32g"][2] + 40 * PRECIOS["disco_ultra_gb"]
MASTER = FLAVORS["4u16g"][2] + 40 * PRECIOS["disco_high_gb"]

# ── Supuestos ───────────────────────────────────────────────────────────────
USO_DISCO = 0.74
MB_S_POR_VCPU_MEDIDO = 0.6     # log por vCPU en el máximo, contando las dos copias (medido)
SHARD_MAX_GB = 50
DISCO_MIN_GB = 40
MAX_COLD = 32
GB_POR_NODO_GUIA = 1000        # la guía de Huawei: 100 a 1.000 GB de datos por nodo hot
RED_SOBRE_LOG = 0.45           # bytes en la red por byte de log, con gzip y HTTPS (150 de 333, medido)
IPSEC = 1.05
RESPUESTA_CORTA_B, RESPUESTA_COMPLETA_B = 29, 243   # vuelta por mensaje (medido)
MAX_PUBLICO_MBIT = 200
MAX_VPN_MBIT = 300

DEFAULTS: dict[str, Any] = {
    "gb_por_dia": 50.0, "bytes_por_evento": 500, "dias_por_semana": 7, "horas_activas": 24, "pico": 3.0,
    "retencion_dias": 30, "dias_hot": 7, "resto": "congelados", "respaldo": True,
    "replicas": 1, "azs": 2, "enlace": "interno", "respuesta_corta": True,
    "crecimiento": 1.33, "mb_s_por_vcpu": 1.0,
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


def _num(datos: dict, clave: str, minimo: float, maximo: float, entero: bool = False) -> float:
    v = datos.get(clave, DEFAULTS[clave])
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise DatoInvalido(f"{clave}: no es un número") from None
    if not minimo <= v <= maximo or math.isnan(v):
        raise DatoInvalido(f"{clave}: tiene que estar entre {_dec(minimo, 2)} y {_dec(maximo, 2)}")
    return int(round(v)) if entero else v


def normalizar(datos: dict) -> dict[str, Any]:
    datos = datos or {}
    e = {
        "gb_por_dia": _num(datos, "gb_por_dia", 0.01, 200000),
        "bytes_por_evento": _num(datos, "bytes_por_evento", 20, 1000000, True),
        "dias_por_semana": _num(datos, "dias_por_semana", 1, 7, True),
        "horas_activas": _num(datos, "horas_activas", 1, 24),
        "pico": _num(datos, "pico", 1, 20),
        "retencion_dias": _num(datos, "retencion_dias", 1, 3650, True),
        "dias_hot": _num(datos, "dias_hot", 1, 3650, True),
        "replicas": _num(datos, "replicas", 0, 2, True),
        "azs": _num(datos, "azs", 1, 2, True),
        "crecimiento": _num(datos, "crecimiento", 0.3, 5),
        "mb_s_por_vcpu": _num(datos, "mb_s_por_vcpu", 0.1, 10),
        "resto": str(datos.get("resto", DEFAULTS["resto"])),
        "enlace": str(datos.get("enlace", DEFAULTS["enlace"])),
        "respaldo": bool(datos.get("respaldo", DEFAULTS["respaldo"])),
        "respuesta_corta": bool(datos.get("respuesta_corta", DEFAULTS["respuesta_corta"])),
    }
    if e["resto"] not in ("congelados", "cold"):
        raise DatoInvalido("resto: tiene que ser «congelados» o «cold»")
    if e["enlace"] not in ("interno", "publico", "vpn"):
        raise DatoInvalido("enlace: tiene que ser «interno», «publico» o «vpn»")
    e["dias_hot"] = min(e["dias_hot"], e["retencion_dias"])
    return e


def dias_con_log(dias: int, por_semana: int) -> int:
    """Días con log dentro de una retención en días calendario (en 30, 22 hábiles)."""
    return math.ceil(dias * por_semana / 7 - 1e-9)


def clientes(nodos_de_datos: int) -> int:
    """Nodos client según Huawei: ninguno con menos de 5 de datos, 2 entre 5 y 9, uno cada 5 desde 10."""
    return 0 if nodos_de_datos < 5 else 2 if nodos_de_datos < 10 else math.ceil(nodos_de_datos / 5)


def masters(nodos: int) -> int:
    """Masters dedicados según Huawei: 3 con más de 10 nodos, 5 desde 50."""
    return 0 if nodos <= 10 else 3 if nodos < 50 else 5


def _hot(e: dict, indice_gb_dia: float, mb_s_pico: float) -> list[dict]:
    """Una opción por flavor hot: los nodos que pide la escritura, el disco y el mínimo."""
    copias = 1 + e["replicas"]
    disco = indice_gb_dia * copias * (dias_con_log(e["dias_hot"], e["dias_por_semana"]) + 1) / USO_DISCO
    minimo = 1 if e["replicas"] == 0 else 4 if e["azs"] == 2 else 3
    opciones = []
    for clave in HOT:
        vcpu, ram, precio, disco_max = FLAVORS[clave]
        escritura = math.ceil(mb_s_pico * copias / (vcpu * e["mb_s_por_vcpu"]) - 1e-9)
        n = max(minimo, escritura, math.ceil(disco / disco_max - 1e-9))
        if e["azs"] == 2 and n % 2:
            n += 1
        por_nodo = max(DISCO_MIN_GB, math.ceil(disco / n / 20 - 1e-9) * 20)
        opciones.append({"flavor": clave, "vcpu": vcpu, "ram": ram, "nodos": n, "disco_gb": por_nodo,
                         "disco_total_gb": disco, "escritura": escritura,
                         "usd": n * precio + n * por_nodo * PRECIOS["disco_ultra_gb"]})
    return opciones


def _cold(e: dict, gb_viejo: float) -> list[dict]:
    """Una opción por flavor cold para guardar `gb_viejo` (sin réplica, disco High I/O)."""
    disco = gb_viejo / USO_DISCO
    minimo = 4 if e["azs"] == 2 else 1
    opciones = []
    for clave in COLD:
        vcpu, ram, precio, disco_max = FLAVORS[clave]
        n = max(minimo, math.ceil(disco / disco_max - 1e-9))
        if e["azs"] == 2 and n % 2:
            n += 1
        if n > MAX_COLD:
            continue
        por_nodo = max(DISCO_MIN_GB, math.ceil(disco / n / 20 - 1e-9) * 20)
        opciones.append({"flavor": clave, "vcpu": vcpu, "ram": ram, "nodos": n, "disco_gb": por_nodo,
                         "usd": n * precio + n * por_nodo * PRECIOS["disco_high_gb"]})
    return opciones


def _cluster(e: dict, hot: dict, gb_viejo: "float | None") -> dict:
    """El cluster más barato con este hot: cold (si va), client y masters."""
    opciones_cold = _cold(e, gb_viejo) if gb_viejo is not None else [None]
    if not opciones_cold:
        return {}
    mejor = None
    for cold in opciones_cold:
        datos = hot["nodos"] + (cold["nodos"] if cold else 0)
        cl = clientes(datos)
        ms = masters(datos + cl)
        usd = hot["usd"] + (cold["usd"] if cold else 0) + cl * CLIENTE + ms * MASTER
        if mejor is None or usd < mejor["usd"]:
            mejor = {"hot": hot, "cold": cold, "clientes": cl, "masters": ms, "usd": usd}
    return mejor


def _elegir(e: dict, indice_gb_dia: float, mb_s_pico: float, gb_viejo: "float | None") -> dict:
    """El flavor hot que da el cluster más barato (con su cold, client y masters)."""
    mejor = None
    for hot in _hot(e, indice_gb_dia, mb_s_pico):
        c = _cluster(e, hot, gb_viejo)
        if c and (mejor is None or c["usd"] < mejor["usd"]):
            mejor = c
    return mejor or {}


def _mes(e: dict, cluster: dict, indice_gb_dia: float, dias_hot: float, dias_viejo: float) -> dict:
    """Lo que se paga en un mes con estos días guardados en hot y en lo viejo."""
    congelado = indice_gb_dia * dias_viejo if e["resto"] == "congelados" else 0.0
    obs_gb = indice_gb_dia * (dias_hot + dias_viejo) if e["respaldo"] else 0.0
    return {"cluster": cluster["usd"], "congelados": congelado * PRECIOS["congelado_gb_hora"] * HORAS_MES,
            "congelado_gb": congelado, "obs": obs_gb * PRECIOS["obs_gb"], "obs_gb": obs_gb}


def _enlace(e: dict, mb_s_pico: float, eventos_mes: float, log_gb_mes: float) -> dict:
    mbit = mb_s_pico * RED_SOBRE_LOG * 8 * (IPSEC if e["enlace"] == "vpn" else 1)
    r: dict[str, Any] = {"tipo": e["enlace"], "mbit_pico": mbit, "usd": 0.0, "detalle": ""}
    if e["enlace"] == "publico":
        # Con margen para ponerse al día después de un corte, sin pasar el máximo.
        contratado = min(MAX_PUBLICO_MBIT, max(10, math.ceil(mbit * 1.5 / 10) * 10))
        r.update(contratado_mbit=contratado, usd=contratado * PRECIOS["ancho_banda_mbit"],
                 detalle=f"{contratado} Mbit/s de acceso público")
    elif e["enlace"] == "vpn":
        vuelta_gb = eventos_mes * (RESPUESTA_CORTA_B if e["respuesta_corta"] else RESPUESTA_COMPLETA_B) * IPSEC / 1024 ** 3
        fijo = (PRECIOS["vpn_gateway_hora"] + 2 * PRECIOS["eip_hora"]) * HORAS_MES
        r.update(ida_gb_mes=log_gb_mes * RED_SOBRE_LOG * IPSEC, vuelta_gb_mes=vuelta_gb,
                 usd=fijo + vuelta_gb * PRECIOS["trafico_salida_gb"],
                 detalle=f"gateway Professional 1 y sus 2 EIP (US$ {_miles(fijo)}) y {_miles(vuelta_gb)} GB de respuestas que salen del cluster")
    return r


def cotizar(datos: dict) -> dict[str, Any]:
    e = normalizar(datos)
    p = PRECIOS
    # Volumen.
    dias_anio = dias_con_log(365, e["dias_por_semana"])
    dias_mes = dias_anio / 12
    gb_dia = e["gb_por_dia"]
    eventos_dia = gb_dia * 1e9 / e["bytes_por_evento"]
    mb_s_promedio = gb_dia * 1000 / (e["horas_activas"] * 3600)
    mb_s_pico = mb_s_promedio * e["pico"]
    indice_gb_dia = gb_dia * e["crecimiento"]
    total_log = dias_con_log(e["retencion_dias"], e["dias_por_semana"])
    hot_log = dias_con_log(e["dias_hot"], e["dias_por_semana"])
    viejo_log = max(0, total_log - hot_log)
    hay_resto = viejo_log > 0
    usa_cold = hay_resto and e["resto"] == "cold"

    cluster = _elegir(e, indice_gb_dia, mb_s_pico, indice_gb_dia * viejo_log if usa_cold else None)
    if not cluster:
        raise DatoInvalido("lo viejo no entra en 32 nodos de datos cold: guardá menos en cold o usá índices congelados")
    hot, cold = cluster["hot"], cluster["cold"]
    datos_nodos = hot["nodos"] + (cold["nodos"] if cold else 0)

    # Primer año, mes a mes: el hot se contrata entero desde el primer mes; lo
    # viejo (congelados, cold) y el respaldo crecen hasta llenarse.
    progresion = []
    primer_anio = primer_anio_anual = 0.0
    for m in range(1, 13):
        acumulado = min(total_log, m * dias_mes)
        en_hot = min(hot_log, acumulado)
        viejo = max(0.0, acumulado - hot_log)
        c_mes = _elegir(e, indice_gb_dia, mb_s_pico, indice_gb_dia * viejo if usa_cold else None) if usa_cold else cluster
        mes = _mes(e, c_mes, indice_gb_dia, en_hot, viejo)
        total = mes["cluster"] + mes["congelados"] + mes["obs"]
        primer_anio += total
        primer_anio_anual += mes["cluster"] * p["meses_pago_anual"] / 12 + mes["congelados"] + mes["obs"]
        progresion.append({"mes": m, "dias_hot": round(en_hot), "dias_viejo": round(viejo),
                           "dias_respaldo": round(en_hot + viejo) if e["respaldo"] else 0,
                           "nodos_cold": (c_mes["cold"] or {}).get("nodos", 0) if usa_cold else 0, "usd": total})

    # Con todo lo guardado (el mes en que se llena la retención y los siguientes).
    lleno = _mes(e, cluster, indice_gb_dia, hot_log, viejo_log)
    enlace = _enlace(e, mb_s_pico, eventos_dia * dias_mes, gb_dia * dias_mes)
    costos = [
        {"concepto": "Nodos de datos hot", "detalle": f"{hot['nodos']} de {hot['vcpu']} vCPU y {hot['ram']} GB",
         "usd": hot["nodos"] * FLAVORS[hot["flavor"]][2]},
        {"concepto": "Disco de los nodos hot", "detalle": f"{hot['nodos']} × {_miles(hot['disco_gb'])} GB Ultra-high I/O",
         "usd": hot["nodos"] * hot["disco_gb"] * p["disco_ultra_gb"]},
    ]
    if cold:
        costos.append({"concepto": "Nodos de datos cold",
                       "detalle": f"{cold['nodos']} de {cold['vcpu']} vCPU y {cold['ram']} GB, con {_miles(cold['disco_gb'])} GB High I/O cada uno",
                       "usd": cold["usd"]})
    if cluster["clientes"]:
        costos.append({"concepto": "Nodos client", "detalle": f"{cluster['clientes']} de 8 vCPU y 32 GB",
                       "usd": cluster["clientes"] * CLIENTE})
    if cluster["masters"]:
        costos.append({"concepto": "Masters dedicados", "detalle": f"{cluster['masters']} de 4 vCPU y 16 GB",
                       "usd": cluster["masters"] * MASTER})
    if lleno["congelado_gb"]:
        costos.append({"concepto": "Índices congelados", "detalle": f"{_miles(lleno['congelado_gb'])} GB en OBS, una copia",
                       "usd": lleno["congelados"]})
    if e["respaldo"]:
        costos.append({"concepto": "Respaldo en OBS", "detalle": f"{_miles(lleno['obs_gb'])} GB, un snapshot de todo lo guardado",
                       "usd": lleno["obs"]})
    if enlace["usd"]:
        costos.append({"concepto": "Enlace", "detalle": enlace["detalle"], "usd": enlace["usd"]})
    total_mes = sum(c["usd"] for c in costos)
    cluster_mes = cluster["usd"]
    nodos_mes = (hot["nodos"] * FLAVORS[hot["flavor"]][2] + (cold["nodos"] * FLAVORS[cold["flavor"]][2] if cold else 0)
                 + cluster["clientes"] * FLAVORS["8u32g"][2] + cluster["masters"] * FLAVORS["4u16g"][2])
    obs_paquete = math.ceil(lleno["obs_gb"] / 1024 - 1e-9) * p["obs_tb_anio"] if lleno["obs_gb"] else 0.0
    anio_siguiente = total_mes * 12
    anio_siguiente_anual = (cluster_mes * p["meses_pago_anual"] + obs_paquete + lleno["congelados"] * 12
                            + enlace["usd"] * 12)
    primer_anio += enlace["usd"] * 12
    primer_anio_anual += enlace["usd"] * 12

    # Shards del índice diario: hasta 50 GB, en múltiplos de los nodos hot.
    shards = max(1, math.ceil(indice_gb_dia / SHARD_MAX_GB - 1e-9))
    shards = math.ceil(shards / hot["nodos"]) * hot["nodos"]
    capacidad = hot["nodos"] * hot["vcpu"] * MB_S_POR_VCPU_MEDIDO
    uso_pico = mb_s_pico / capacidad if capacidad else 0

    avisos = []
    if uso_pico > 0.8:
        avisos.append(f"En el pico el cluster usaría cerca del {_pct(uso_pico)} de lo que medimos que escribe: sumá nodos hot "
                      "o validalo con una prueba de carga.")
    datos_por_nodo = hot["disco_total_gb"] * USO_DISCO / hot["nodos"]
    if datos_por_nodo > GB_POR_NODO_GUIA:
        avisos.append(f"Cada nodo hot guardaría unos {_miles(datos_por_nodo)} GB de datos: la guía de Huawei habla de 100 a 1.000 GB "
                      "por nodo. Entra en lo que admite la consola, pero conviene confirmarlo con Huawei.")
    if e["replicas"] == 0:
        avisos.append("Sin réplica, si cae un nodo sus datos no se consultan hasta que vuelve, y Huawei pide una réplica para "
                      "que dos AZ protejan los datos.")
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
        avisos.append("El costo de la VPN es el lado de LA-Santiago. El del origen (su gateway y el tráfico que sale de su "
                      "región) depende de dónde están sus servidores.")
    if datos_nodos > 32 and not cluster["masters"]:
        avisos.append("Más de 32 nodos de datos exige masters dedicados.")

    def gb(x: float) -> str:
        return f"{_dec(x / 1000)} TB" if x >= 1000 else f"{_miles(x)} GB"

    resumen = (f"{hot['nodos']} nodos hot de {hot['vcpu']} vCPU y {hot['ram']} GB con {gb(hot['disco_gb'])} cada uno"
               + (f", {cold['nodos']} cold de {cold['vcpu']} vCPU con {gb(cold['disco_gb'])}" if cold else "")
               + (f", {cluster['clientes']} client" if cluster["clientes"] else "")
               + (f" y {cluster['masters']} masters" if cluster["masters"] else "")
               + (f", repartidos en {e['azs']} AZ" if e["azs"] == 2 else ""))

    supuestos = [
        ("Disco al 80 % visto por OpenSearch", "Los datos ocupan como máximo el 74 % del disco contratado: OpenSearch cuenta como "
         "usado el 5 % que reserva el sistema de archivos (medido: 1.180 GB de 1.200, con 60 reservados). A partir del 85 % deja "
         "de asignar réplicas."),
        ("Un día más de disco", "Mientras se llena el índice de hoy, el más viejo todavía no se borró."),
        (f"El índice crece un {_pct(e['crecimiento'] - 1)} sobre el log", "Con el códec zstd_no_dict; medido en un OMS FIX "
         "(31 % con 103 millones de mensajes). Huawei supone un 25 %. Depende de cuántos campos se parsean."),
        (f"{_dec(e['mb_s_por_vcpu'], 2)} MB/s de escritura por vCPU", "Fórmula de Huawei, contando la primaria y la réplica. En el "
         f"máximo medimos unos {_dec(MB_S_POR_VCPU_MEDIDO)} MB/s de log por vCPU con las dos copias: con eso sale el uso en el pico."),
        ("Nodos hot en cantidad par y al menos 4 con dos AZ", "Cada AZ guarda una copia de cada dato. Huawei recomienda al "
         "menos 3 nodos de datos para la disponibilidad."),
        ("Nodos client y masters", "Client: ninguno con menos de 5 nodos de datos, 2 entre 5 y 9 y uno cada 5 desde 10. "
         "Masters dedicados: 3 con más de 10 nodos y 5 desde 50 (reglas de Huawei)."),
        ("Nodos cold", "Con dos AZ, al menos 4 y en cantidad par, hasta 32, sin réplica y con disco High I/O; se pueden sumar "
         "después de crear el cluster. Se elige el tamaño más barato."),
        ("Índices congelados", "Los datos pasan a OBS y se consultan sin restaurar, de segundos a minutos por consulta; el disco "
         "local se libera solo (medido: en menos de 40 minutos)."),
        ("Respaldo en OBS", "Un snapshot de todo lo guardado, en un bucket Standard (la consola de CSS no acepta otra clase). "
         "Restaurar anduvo a 41 MB/s por nodo."),
        ("Shards de hasta 50 GB", "Un índice por día, con los shards en múltiplos de los nodos hot (recomendación de Huawei)."),
        ("Días con log", f"{e['dias_por_semana']} por semana: en 30 días calendario, {dias_con_log(30, e['dias_por_semana'])} con log."),
        ("Pago anual y por hora", "El año cuesta 10 meses, en nodos y en disco. Por hora, los nodos cuestan cerca de un 39 % "
         "más. El respaldo y los congelados se pagan por uso; el respaldo, con paquete anual cuando deja de crecer."),
        ("Precios", f"Lista de Huawei Cloud para {p['region']}, consultada en {p['consultado']}, sin impuestos ni descuentos."),
    ]
    if e["enlace"] != "interno":
        supuestos.append(("Red", "Unos 0,45 bytes por byte de log con gzip y HTTPS (150 de 333, medido), más un 5 % de IPsec "
                                 f"en una VPN; de vuelta, {RESPUESTA_CORTA_B} bytes por mensaje con la respuesta corta de "
                                 f"Fluent Bit y {RESPUESTA_COMPLETA_B} sin ella."))

    return {
        "entrada": e,
        "resumen": resumen,
        "volumen": {"gb_dia": gb_dia, "eventos_dia": eventos_dia, "eventos_s_promedio": eventos_dia / (e["horas_activas"] * 3600),
                    "eventos_s_pico": eventos_dia / (e["horas_activas"] * 3600) * e["pico"], "mb_s_pico": mb_s_pico,
                    "indice_gb_dia": indice_gb_dia, "dias_con_log": total_log, "dias_con_log_hot": hot_log},
        "cluster": {"hot": {k: hot[k] for k in ("flavor", "vcpu", "ram", "nodos", "disco_gb")},
                    "cold": {k: cold[k] for k in ("flavor", "vcpu", "ram", "nodos", "disco_gb")} if cold else None,
                    "clientes": cluster["clientes"], "masters": cluster["masters"], "shards": shards,
                    "gb_por_shard": indice_gb_dia / shards, "uso_pico": uso_pico,
                    "datos_por_nodo_gb": datos_por_nodo},
        "costos": costos,
        "total_mes": total_mes,
        "anual": {"por_hora_mes": total_mes + nodos_mes * p["recargo_por_hora"],
                  "primer_anio": primer_anio, "primer_anio_pago_anual": primer_anio_anual,
                  "anio_siguiente": anio_siguiente, "anio_siguiente_pago_anual": anio_siguiente_anual},
        "progresion": progresion,
        "enlace": enlace,
        "avisos": avisos,
        "supuestos": [{"que": a, "fuente": b} for a, b in supuestos],
        "precios": dict(p, flavors={k: {"vcpu": v[0], "ram": v[1], "usd_mes": v[2]} for k, v in FLAVORS.items()}),
    }
