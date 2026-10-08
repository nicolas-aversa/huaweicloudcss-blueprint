"""
dimensionamiento.py
===================

Cuánto cluster necesita un dataset en producción: nodos, flavor, disco por
nodo y shards por índice, a partir de lo que ya sabemos de la muestra (bytes
por evento) y de lo que dice el cliente (eventos por día, retención, alta
disponibilidad). Es el puente entre la PoC y la compra.

Es una estimación con supuestos explícitos (van en la respuesta, para que el
SA los pueda discutir), no una medición: el número fino sale de una prueba de
carga en el cluster de la PoC.
"""

from __future__ import annotations

import math
from typing import Any

# Flavors de nodo de datos de CSS (vCPU, GB de RAM), de menor a mayor.
FLAVORS = (("ess.spec-4u8g", 4, 8), ("ess.spec-8u16g", 8, 16),
           ("ess.spec-16u32g", 16, 32), ("ess.spec-32u64g", 32, 64))
# Supuestos (los de logs "calientes", buscados seguido).
FACTOR_INDEXADO = 1.3          # el índice ocupa ~1,3 veces el texto crudo (keyword + doc_values)
MARGEN_DE_DISCO = 0.75         # usar hasta el 75 %: watermarks y merges
DISCO_POR_GB_DE_RAM = 50       # GB de disco por GB de RAM en un nodo caliente
EVENTOS_POR_SEG_POR_VCPU = 1500
PICO_SOBRE_PROMEDIO = 3        # el pico de ingesta, sobre el promedio del día
SHARD_GB_MAX = 30              # tamaño máximo recomendado de un shard primario
EVENTOS_POR_SEG_POR_LOGSTASH = 5000  # un nodo Logstash de 4 vCPU
DISCO_MINIMO_GB = 40           # el volumen mínimo de un nodo CSS
_MAX_NODOS = 32


def bytes_por_evento(muestra: str) -> int:
    """El tamaño promedio de un evento de la muestra (una línea, en UTF-8)."""
    lineas = [l for l in (muestra or "").splitlines() if l.strip()]
    if not lineas:
        return 0
    return max(1, round(sum(len(l.encode("utf-8")) for l in lineas) / len(lineas)))


def dimensionar(bytes_evento: int, eventos_por_dia: int, retencion_dias: int,
                alta_disponibilidad: bool = True) -> dict[str, Any]:
    """La recomendación. Con alta disponibilidad: una réplica y al menos tres
    nodos (CSS reparte los shards y sobrevive a la caída de uno)."""
    bytes_evento = max(1, int(bytes_evento or 0))
    eventos_por_dia = max(0, int(eventos_por_dia or 0))
    retencion_dias = max(1, int(retencion_dias or 1))
    replicas = 1 if alta_disponibilidad else 0
    gb_dia = eventos_por_dia * bytes_evento * FACTOR_INDEXADO / 1e9
    gb_datos = gb_dia * retencion_dias
    gb_disco = gb_datos * (1 + replicas) / MARGEN_DE_DISCO
    pico = eventos_por_dia / 86400 * PICO_SOBRE_PROMEDIO
    vcpus = max(1, math.ceil(pico / EVENTOS_POR_SEG_POR_VCPU))
    minimo = 3 if alta_disponibilidad else 1
    eleccion = None
    for nombre, vcpu, ram in FLAVORS:
        for nodos in range(minimo, _MAX_NODOS + 1):
            if nodos * vcpu >= vcpus and nodos * ram * DISCO_POR_GB_DE_RAM >= gb_disco:
                # Lo más barato (vCPU en total); a igual costo, menos nodos.
                costo = (nodos * vcpu, nodos)
                if eleccion is None or costo < eleccion[0]:
                    eleccion = (costo, nombre, vcpu, ram, nodos)
                break
    excede = eleccion is None
    if excede:
        nombre, vcpu, ram = FLAVORS[-1]
        nodos = _MAX_NODOS
    else:
        _, nombre, vcpu, ram, nodos = eleccion
    disco_por_nodo = max(DISCO_MINIMO_GB, math.ceil(gb_disco / nodos / 10) * 10)
    gb_indice_mensual = gb_dia * 31
    shards = max(1, math.ceil(gb_indice_mensual / SHARD_GB_MAX))
    return {
        "bytes_por_evento": bytes_evento,
        "eventos_por_dia": eventos_por_dia,
        "retencion_dias": retencion_dias,
        "alta_disponibilidad": alta_disponibilidad,
        "gb_por_dia": round(gb_dia, 2),
        "gb_de_datos": round(gb_datos, 1),
        "gb_de_disco": round(gb_disco, 1),
        "eventos_por_seg_pico": round(pico),
        "nodos": nodos,
        "flavor": nombre,
        "vcpu_por_nodo": vcpu,
        "ram_por_nodo_gb": ram,
        "disco_por_nodo_gb": disco_por_nodo,
        "shards_primarios": shards,
        "replicas": replicas,
        "nodos_logstash": max(1, math.ceil(pico / EVENTOS_POR_SEG_POR_LOGSTASH)),
        "excede": excede,
        "supuestos": [
            f"el índice ocupa ~{FACTOR_INDEXADO:g} veces el texto crudo",
            f"el disco se usa hasta el {round(MARGEN_DE_DISCO * 100)} %",
            f"un nodo caliente lleva hasta {DISCO_POR_GB_DE_RAM} GB de disco por GB de RAM",
            f"el pico de ingesta es {PICO_SOBRE_PROMEDIO} veces el promedio del día",
            f"~{EVENTOS_POR_SEG_POR_VCPU:,} eventos por segundo por vCPU".replace(",", "."),
            f"shards primarios de hasta {SHARD_GB_MAX} GB, en índices mensuales",
        ],
    }


def en_palabras(d: dict) -> str:
    """La recomendación en una línea, para el paso 2 y el traspaso."""
    if not d.get("eventos_por_dia"):
        return "sin volumen diario no se puede dimensionar"
    prefijo = "más de " if d.get("excede") else ""
    return (f"{prefijo}{d['nodos']} nodo{'s' if d['nodos'] != 1 else ''} {d['flavor']} con "
            f"{d['disco_por_nodo_gb']} GB de disco cada uno · {d['shards_primarios']} shard"
            f"{'s' if d['shards_primarios'] != 1 else ''} primario{'s' if d['shards_primarios'] != 1 else ''} por índice mensual, "
            f"{d['replicas']} réplica{'s' if d['replicas'] != 1 else ''} · {d['nodos_logstash']} nodo"
            f"{'s' if d['nodos_logstash'] != 1 else ''} de Logstash")
