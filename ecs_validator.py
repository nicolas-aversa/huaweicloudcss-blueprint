"""
ecs_validator.py
================

Valida los ``ecs_path`` que devuelve el LLM contra la spec real de
Elastic Common Schema (descargada a ``docs/fields.csv`` desde
`github.com/elastic/ecs/tree/v8.11.0/generated/csv`).

Por qué importa:
  El LLM genera paths como ``[transaction][amount]`` o ``[user][name]``
  basado en lo que recuerda de ECS de su training. La mayoría son
  correctos para namespaces comunes (source, destination, user, host),
  pero el modelo puede inventar paths en namespaces de dominio
  (financial, payment, etc.) que no existen en la spec.

  Este módulo cruza cada path contra la spec y permite que el frontend
  muestre un badge "ECS oficial" o "Custom" por cada field — el cliente
  ve transparente qué es estándar y qué es extensión nuestra.

Uso típico (desde ``/generate-filter`` endpoint):

    from ecs_validator import classify_field
    info = classify_field("[transaction][id]")
    # → {"is_ecs": True, "ecs_type": "keyword", "normalized": "transaction.id"}

Diseño:
  - **Carga única**: el CSV de 1747 filas se lee una vez al primer uso y
    se cachea en memoria. ~50 KB, no vale la pena hacerlo lazy por field.
  - **Path normalization**: el LLM puede devolver ``[a][b][c]`` (Logstash
    bracketed) o ``a.b.c`` (dot). ECS CSV usa dot. Convertimos siempre a
    dot antes de buscar.
  - **Match sobre ``Field``**: el CSV trae el nombre completo del field
    (ej. ``http.response.status_code``). Lookup es O(1) por hash set.
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path


_CSV_PATH = Path(__file__).parent / "docs" / "fields.csv"

# Patrón de Logstash bracketed: [name][sub][...]. Extraemos los segmentos
# del path y los unimos con punto. Aceptamos también un mix con dots:
# "[user][name]" → "user.name"; "user.name" → "user.name";
# "[event][duration]" → "event.duration".
_BRACKET_PATTERN = re.compile(r"\[([^\]]+)\]")


def normalize_path(raw: str) -> str:
    """Convierte un ``ecs_path`` en cualquier formato a forma dot.

    Acepta:
      - ``[a][b][c]``   → ``a.b.c``  (Logstash bracketed, lo más común del LLM)
      - ``a.b.c``       → ``a.b.c``  (forma ECS canónica, no cambia)
      - ``[a].b``       → ``a.b``    (mixto, lo manejamos)
      - ``""`` / None   → ``""``     (input vacío)

    Salida en lowercase y sin espacios para que el lookup sea robusto.
    """
    if not raw:
        return ""
    s = raw.strip()
    brackets = _BRACKET_PATTERN.findall(s)
    if brackets:
        # Si había brackets, los usamos como segmentos. Cualquier "." que
        # haya quedado fuera de brackets también lo incluimos (forma mixta).
        outside = _BRACKET_PATTERN.sub("", s).strip(".")
        if outside:
            parts = brackets + outside.split(".")
        else:
            parts = brackets
    else:
        parts = s.split(".")
    return ".".join(p.strip().lower() for p in parts if p.strip())


@lru_cache(maxsize=1)
def _load_spec() -> dict[str, str]:
    """Devuelve dict ``{field_path: ecs_type}`` desde el CSV.

    Cacheado en memoria. Si el archivo no existe (deploy mal armado),
    devuelve dict vacío — el validador degrada a "todo es custom" en vez
    de explotar.
    """
    if not _CSV_PATH.exists():
        return {}

    spec: dict[str, str] = {}
    with _CSV_PATH.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            field = (row.get("Field") or "").strip()
            ftype = (row.get("Type") or "").strip()
            if field:
                spec[field.lower()] = ftype
    return spec


def is_ecs_field(path: str) -> bool:
    """``True`` si ``path`` está en la spec ECS."""
    return normalize_path(path) in _load_spec()


def get_ecs_type(path: str) -> str | None:
    """Tipo declarado por ECS para ese field, o ``None`` si no existe."""
    return _load_spec().get(normalize_path(path))


def classify_field(path: str) -> dict:
    """Resumen completo para que el endpoint enriquezca cada FieldMapping.

    Returns
    -------
    dict con keys:
      - ``normalized``  (str): el path en forma dot, listo para indexar.
      - ``is_ecs``      (bool): si está en la spec.
      - ``ecs_type``    (str | None): tipo declarado por ECS, si aplica.
    """
    normalized = normalize_path(path)
    spec = _load_spec()
    ecs_type = spec.get(normalized)
    return {
        "normalized": normalized,
        "is_ecs": ecs_type is not None,
        "ecs_type": ecs_type,
    }


def spec_loaded() -> bool:
    """Para health-check del backend: ``True`` si el CSV está accesible
    y tiene filas. Útil para warnear al arranque si falta el archivo."""
    return len(_load_spec()) > 0


def spec_size() -> int:
    """Cantidad de fields ECS cargados — diagnóstico."""
    return len(_load_spec())
