"""Los juicios del LLM sobre la búsqueda de cada caso, guardados para toda la
instancia.

La evaluación de la búsqueda en Search Relevance le pide al LLM búsquedas
realistas y que juzgue lo que trae cada configuración. Con el mismo dataset
da lo mismo cada vez, así que el LLM juzga una sola vez: se guardan sus
búsquedas y la nota de cada texto, y los deploys siguientes, de cualquier SA,
las importan (`IMPORT_JUDGMENT`): la evaluación sale igual en cada deploy, sin
armar el juez ni llamar al LLM (medido en un entorno: 20 s importando contra
30 s juzgando, por caso; lo que más tardaba era otra cosa, ver
`_provisionar_la_busqueda`).

Por texto y no por id de documento: el índice de búsqueda se arma de nuevo en
cada entorno. Vale mientras el caso use el mismo campo de texto y el mismo
modelo de embeddings (con otro, la búsqueda híbrida trae otros documentos).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import auth

# Qué parte de los textos juzgados tiene que estar en el índice para importar
# en vez de volver a juzgar (con otro dataset, el caso cambió).
COBERTURA_MINIMA = 0.8


def _dir() -> Path:
    return auth.DATA_ROOT / "evaluaciones_de_busqueda"


def _ruta(slug: str) -> Path:
    return _dir() / f"{re.sub(r'[^a-z0-9_-]+', '-', slug.lower())}.json"


def leer(slug: str, campo: str, modelo: str) -> dict[str, Any] | None:
    """La evaluación guardada del caso, si es del mismo campo y modelo."""
    try:
        guardada = json.loads(_ruta(slug).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return None
    if guardada.get("campo") != campo or guardada.get("modelo") != modelo:
        return None
    if not guardada.get("consultas") or not guardada.get("juicios"):
        return None
    return guardada


def guardar(slug: str, campo: str, modelo: str, consultas: list[str],
            juicios: dict[str, list[list[str]]]) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    _ruta(slug).write_text(json.dumps({
        "slug": slug, "campo": campo, "modelo": modelo, "consultas": list(consultas), "juicios": juicios,
        "guardado_en": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        ensure_ascii=False, indent=1), encoding="utf-8")


def por_texto(judgment_ratings: list[dict], texto_de_id: dict[str, str]) -> dict[str, list[list[str]]]:
    """Los `judgmentRatings` de Search Relevance como `{búsqueda: [[texto, nota], …]}`."""
    fuera: dict[str, list[list[str]]] = {}
    for j in judgment_ratings or []:
        consulta = j.get("query")
        if not consulta:
            continue
        fuera[consulta] = [[texto_de_id[r["docId"]], str(r.get("rating"))]
                           for r in j.get("ratings") or [] if r.get("docId") in texto_de_id]
    return fuera


def textos(guardada: dict[str, Any]) -> list[str]:
    """Los textos juzgados, sin repetir."""
    return sorted({t for notas in guardada["juicios"].values() for t, _ in notas})


def para_importar(nombre: str, guardada: dict[str, Any], id_de_texto: dict[str, str]) -> tuple[dict, float]:
    """El cuerpo de `PUT judgments` con las notas guardadas (`IMPORT_JUDGMENT`)
    y qué parte de los textos juzgados está en el índice."""
    total = encontrados = 0
    juicios = []
    for consulta in guardada["consultas"]:
        notas = []
        for texto, nota in guardada["juicios"].get(consulta) or []:
            total += 1
            if texto in id_de_texto:
                encontrados += 1
                notas.append({"docId": id_de_texto[texto], "rating": nota})
        if notas:
            juicios.append({"query": consulta, "ratings": notas})
    return ({"name": nombre, "type": "IMPORT_JUDGMENT", "judgmentRatings": juicios},
            encontrados / total if total else 0.0)
