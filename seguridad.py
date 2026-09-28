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


def build_detector(slug: str, log_type: str, index_pattern: str,
                   reglas: list[tuple[str, str]]) -> dict[str, Any]:
    """`POST _plugins/_security_analytics/detectors`, sobre el index pattern REAL
    del caso. `reglas`: [(id de la regla creada, nivel)]. Un trigger por cada
    severidad que haya, por nivel y no por id: así cada alerta sale con su
    severidad y una regla nueva del mismo nivel entra sola."""
    niveles = [n for n in NIVELES if any(nv == n for _, nv in reglas)]
    return {
        "type": "detector",
        "name": nombre_de_detector(slug, log_type),
        "detector_type": log_type,
        "enabled": True,
        "schedule": {"period": {"interval": 1, "unit": "MINUTES"}},
        "inputs": [{"detector_input": {
            "description": f"{slug}: {log_type}",
            "indices": [index_pattern],
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


def indice_para_detector(index_pattern: str) -> str:
    """El índice que se crea si el pattern todavía no matchea ninguno.

    Security Analytics valida los campos de las reglas contra el mapping del
    índice, y el detector tiene que existir ANTES de la ingesta: su monitor
    procesa entero un índice que nace después de él, pero de uno que ya existía
    solo ve lo nuevo. Un índice vacío que matchea el pattern (y toma el index
    template) resuelve las dos cosas. Si el pattern es un nombre fijo, es ese.
    """
    if not index_pattern.endswith("*"):
        return index_pattern
    return index_pattern.rstrip("*").rstrip("-._") + "-sa-bootstrap"
