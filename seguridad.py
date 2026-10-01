"""
seguridad.py
============

Builders **puros** (sin I/O) de lo que la plataforma provisiona en el plugin
Security Analytics de OpenSearch para los casos de seguridad (SIEM,
FortiAnalyzer): tipos de log propios, reglas Sigma, detectores y correlaciones.

Lo que se crea sale de la clave `security` de cada vertical (`verticals/`): un
tipo de log por fuente, con sus reglas escritas sobre los campos que de verdad
deja el filter del caso. La orquestación REST vive en `main.py`.

Por qué tipos de log propios y no `network`: las reglas usan los campos del
caso (`event.action`, `subtype`, …) y un tipo estándar espera los suyos; con
un tipo propio las reglas se validan contra lo que llega.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# Severidad Sigma → la del trigger del detector (1 es la más alta).
_SEVERIDAD_TRIGGER = {"critical": "1", "high": "2", "medium": "3", "low": "4"}
NIVELES = tuple(_SEVERIDAD_TRIGGER)
# Security Analytics exige `date` en la regla (sin él, el alta falla con un 500
# "Cannot invoke java.util.Date.getTime()"). Fija, para que la regla no cambie.
FECHA_DE_LAS_REGLAS = "2026/09/28"


def _yaml_escalar(v: Any) -> str:
    """Un valor escalar en YAML: números tal cual, texto entre comillas dobles
    (JSON es YAML válido y escapa todo lo que haga falta)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def id_de_regla(titulo: str) -> str:
    """Un id Sigma estable por título: la misma regla, el mismo id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"css-blueprint/sigma/{titulo}"))


def sigma_yaml(regla: dict, log_type: str) -> str:
    """La regla en YAML Sigma, como la recibe `POST _plugins/_security_analytics/rules`.

    `regla`: {titulo, descripcion, nivel, seleccion: {campo: valor | [valores]}, tags}.
    Todos los campos de la selección tienen que cumplirse (AND); una lista es
    cualquiera de sus valores (OR).
    """
    lineas = [
        f"title: {_yaml_escalar(regla['titulo'])}",
        f"id: {id_de_regla(regla['titulo'])}",
        "status: experimental",
        f"description: {_yaml_escalar(regla.get('descripcion') or regla['titulo'])}",
        "author: CSS Blueprint",
        f"date: {FECHA_DE_LAS_REGLAS}",
    ]
    if regla.get("tags"):
        lineas.append("tags:")
        lineas += [f"  - {t}" for t in regla["tags"]]
    lineas += [
        "logsource:",
        f"  product: {log_type}",
        "detection:",
        "  selection:",
    ]
    for campo, valor in regla["seleccion"].items():
        if isinstance(valor, (list, tuple)):
            lineas.append(f"    {campo}:")
            lineas += [f"      - {_yaml_escalar(v)}" for v in valor]
        else:
            lineas.append(f"    {campo}: {_yaml_escalar(valor)}")
    lineas += ["  condition: selection", f"level: {regla['nivel']}"]
    return "\n".join(lineas) + "\n"


def build_log_type(log_type: dict) -> dict[str, Any]:
    """`POST _plugins/_security_analytics/logtype`: un tipo de log propio."""
    return {"name": log_type["nombre"], "description": log_type.get("descripcion") or log_type["nombre"],
            "source": "Custom"}


def nombre_de_detector(slug: str, log_type: str) -> str:
    return f"{slug}-{log_type}".replace("_", "-")


def build_detector(slug: str, log_type: str, indices: list[str],
                   reglas: list[tuple[str, str]]) -> dict[str, Any]:
    """`POST _plugins/_security_analytics/detectors`, sobre los índices del caso
    POR NOMBRE (ver `indices_mensuales`). `reglas`: [(id de la regla creada,
    nivel)]. Un trigger por cada severidad que haya, por nivel y no por id: así
    cada alerta sale con su severidad y una regla nueva del mismo nivel entra sola."""
    niveles = [n for n in NIVELES if any(nv == n for _, nv in reglas)]
    return {
        "type": "detector",
        "name": nombre_de_detector(slug, log_type),
        "detector_type": log_type,
        "enabled": True,
        "schedule": {"period": {"interval": 1, "unit": "MINUTES"}},
        "inputs": [{"detector_input": {
            "description": f"{slug}: {log_type}",
            "indices": list(indices),
            "custom_rules": [{"id": rid} for rid, _ in reglas],
            "pre_packaged_rules": [],
        }}],
        "triggers": [{
            "id": f"{nombre_de_detector(slug, log_type)}-{n}",
            "name": f"Severidad {n}",
            "severity": _SEVERIDAD_TRIGGER[n],
            "ids": [], "types": [], "tags": [], "sev_levels": [n], "actions": [],
        } for n in niveles],
    }


def build_correlacion(correlacion: dict, index_pattern: str) -> dict[str, Any]:
    """`POST _plugins/_security_analytics/correlation/rules`: hallazgos de dos
    (o más) tipos de log que ocurren dentro de la ventana."""
    return {
        "name": correlacion["nombre"],
        "time_window": int(correlacion.get("ventana_min", 60)) * 60_000,
        "correlate": [{"index": index_pattern, "query": c["query"], "category": c["log_type"]}
                      for c in correlacion["correlate"]],
    }


def campos_de_reglas(spec: dict) -> set[str]:
    """Todos los campos que usan las reglas del spec (para validarlos contra
    los del caso)."""
    return {campo for lt in spec.get("log_types", []) for r in lt.get("reglas", [])
            for campo in r["seleccion"]}


def alias_del_caso(index_pattern: str) -> str:
    """El alias al que apuntan el detector y las correlaciones del caso.

    Security Analytics (CSS 3.4) rechaza un index pattern en el detector: "Index
    patterns are not supported for doc level monitors". Un alias sí: lo lleva
    cada índice del caso desde el index template (los nuevos) y un `_aliases`
    (los que ya estaban). `siem-*` → `siem-seguridad`.
    """
    return index_pattern.rstrip("*").rstrip("-._") + "-seguridad"


# Los casos con Security Analytics nombran el mes con guion bajo: `siem-2025_07`.
# Con punto (`siem-2025.07`, el formato del resto de los casos) Security Analytics
# rechaza el detector aunque sea UN índice concreto: "Index patterns are not
# supported for doc level monitors" (medido en CSS 3.4: con punto 500 con 1, 2 y
# 13 índices; con guion bajo o guion, 201). El pattern sigue siendo `siem-*`.
FORMATO_DEL_MES = "YYYY_MM"


def indice_de_salida(index_base: str) -> str:
    """El `index` del output de Logstash de un caso con Security Analytics."""
    return f"{index_base}-%{{+{FORMATO_DEL_MES}}}"


def indices_mensuales(index_pattern: str, meses: "tuple[str, str] | list[str]") -> list[str]:
    """Los índices por mes del dataset del caso, como los nombra Logstash
    (`indice_de_salida`): `siem-*`, ("2025-07", "2025-09") →
    [siem-2025_07, siem-2025_08, siem-2025_09].

    Se crean vacíos ANTES que los detectores. El monitor de un detector
    (doc-level, CSS 3.4) procesa todo lo que entra a un índice que ya existía
    cuando se creó; de uno que se suma al alias después solo lee sus primeros
    minutos y nunca guarda hasta dónde llegó (medido en un cluster real: la
    tanda que llegó 3 min después no se evaluó). Si Logstash creara cada mes
    durante la ingesta, de cada uno se procesaría solo el comienzo.

    Y el detector va a estos índices POR NOMBRE, no al alias del caso: con un
    alias el monitor tampoco guarda hasta dónde leyó (medido: mismos índices y
    mismos datos, el detector por nombre vio 7 de 7 en dos tandas y guardó su
    avance; el del alias, 0 y nunca lo guardó).
    """
    base = index_pattern.rstrip("*").rstrip("-._")
    desde, hasta = meses
    anio, mes = (int(x) for x in desde.split("-"))
    fin = tuple(int(x) for x in hasta.split("-"))
    salida = []
    while (anio, mes) <= fin:
        salida.append(f"{base}-{anio:04d}_{mes:02d}")
        anio, mes = (anio + 1, 1) if mes == 12 else (anio, mes + 1)
    return salida
