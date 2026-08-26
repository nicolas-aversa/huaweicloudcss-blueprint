"""Registro declarativo de verticales de demo.

Cada vertical se define en UN módulo (`verticals/<slug>.py`, `VERTICAL: dict`).
Este paquete los agrega y expone las vistas que consumen el backend
(capabilities.py, dashboards.py, main.py) y el frontend (payload inyectado en
`GET /`). Agregar un vertical = crear un módulo nuevo acá (+ su dataset y
`py -m dashboards`); no hay que tocar 7 archivos como antes.

Convenciones del shape de `VERTICAL`:
  slug, label, full_label, group, icon, index_base, description   -> card + labels
  dedup_id                                                        -> document_id de dedup (opcional)
  sample, filter_code, fields                                     -> EXAMPLE_DATA del wizard
  suggested_questions                                             -> preguntas del chatbot
  industry_fields (set)                                           -> matching de industria (productivo)
  dataset_files (list)                                            -> archivos a pre-cargar en OBS
  capability, dashboard                                           -> specs de OpenSearch
  extra_capabilities/extra_dashboards/extra_industry_fields       -> sub-specs backend-only
  hidden (bool)                                                   -> sin card ni grupo (ej. cts legacy)
"""
from __future__ import annotations

from . import (
    transacciones_alyc,
    cts,
    fraud_detection,
    transacciones_billetera,
    fortianalyzer,
    encuentros_clinicos,
    ventas_ecommerce,
    streaming_ott,
    produccion_pozos,
    siem,
)

# Grupos del grid del paso 1 (id, label, icon). El orden define el orden en la
# UI; los members se derivan del campo `group` de cada vertical, en el orden en
# que aparecen los verticales en _MODULES.
GROUPS: list[dict] = [
    {"id": "seguridad", "label": "Seguridad", "icon": "shield"},
    {"id": "fintech", "label": "Fintech", "icon": "activity"},
    {"id": "retail", "label": "Retail", "icon": "shopping-cart"},
    {"id": "media", "label": "Media", "icon": "play"},
    {"id": "energia", "label": "Oil & Gas", "icon": "droplet"},
    {"id": "salud", "label": "Salud", "icon": "heart"},
]

# Orden canónico (= orden del grid y de LOG_EXAMPLES). Los hidden van al final y
# solo aportan datos (EXAMPLE_DATA, dashboards legacy), no card.
_MODULES = [
    siem, fortianalyzer, transacciones_billetera, fraud_detection, transacciones_alyc,
    streaming_ott, produccion_pozos, ventas_ecommerce, encuentros_clinicos, cts,
]
_VERTICALS: list[dict] = [m.VERTICAL for m in _MODULES]
_BY_SLUG: dict[str, dict] = {v["slug"]: v for v in _VERTICALS}


def all_verticals() -> list[dict]:
    """Todos los verticales en orden canónico (incluye hidden)."""
    return list(_VERTICALS)


def visible_verticals() -> list[dict]:
    """Verticales con card (no hidden), en orden canónico."""
    return [v for v in _VERTICALS if not v.get("hidden")]


def get_vertical(slug: str) -> dict | None:
    return _BY_SLUG.get(slug)


def capability_specs() -> dict:
    """`slug -> spec`, incluyendo los sub-specs backend-only (extra_capabilities)."""
    out: dict = {}
    for v in _VERTICALS:
        if "capability" in v:
            out[v["slug"]] = v["capability"]
        out.update(v.get("extra_capabilities", {}))
    return out


def dashboard_specs() -> dict:
    """`slug -> spec` de dashboards de los verticales (+ extra_dashboards)."""
    out: dict = {}
    for v in _VERTICALS:
        if "dashboard" in v:
            out[v["slug"]] = v["dashboard"]
        out.update(v.get("extra_dashboards", {}))
    return out


def industry_fields() -> dict:
    """`slug -> set(campos)` para el matching de industria (+ extra_industry_fields)."""
    out: dict = {}
    for v in _VERTICALS:
        if "industry_fields" in v:
            out[v["slug"]] = v["industry_fields"]
        out.update(v.get("extra_industry_fields", {}))
    return out


def demo_dataset_files() -> dict:
    """`slug -> [archivos]` a pre-cargar en OBS (solo verticales visibles con dataset;
    los hidden no se pre-cargan)."""
    return {v["slug"]: v["dataset_files"] for v in _VERTICALS
            if v.get("dataset_files") and not v.get("hidden")}


def _members(group_id: str) -> list[str]:
    return [v["slug"] for v in _VERTICALS if v.get("group") == group_id]


def front_payload() -> dict:
    """Payload JSON-serializable inyectado en el front (`window.__VERTICALS__`).

    El front deriva de acá LOG_EXAMPLES, DEMO_VERTICALS, EXAMPLE_DATA,
    SLUG_LABELS, CAPABILITY_SLUGS y SUGGESTED_BY_SLUG (los entries `custom`
    UI-only se quedan hardcodeados en el front)."""
    groups = [
        {"id": g["id"], "label": g["label"], "icon": g["icon"],
         "members": _members(g["id"])}
        for g in GROUPS
    ]
    verticals = []
    for v in _VERTICALS:
        verticals.append({
            "slug": v["slug"],
            "label": v.get("label", ""),
            "fullLabel": v.get("full_label", ""),
            "group": v.get("group", ""),
            "icon": v.get("icon", ""),
            "indexBase": v.get("index_base", ""),
            "description": v.get("description", ""),
            "dedupId": v.get("dedup_id", ""),
            "hidden": bool(v.get("hidden")),
            "hasCapability": "capability" in v,
            "sample": v.get("sample", ""),
            "filterCode": v.get("filter_code", ""),
            "fields": v.get("fields", []),
            "questions": v.get("suggested_questions", []),
        })
    return {"groups": groups, "verticals": verticals}
