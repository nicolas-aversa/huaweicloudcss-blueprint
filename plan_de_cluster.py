"""Qué va a tener el cluster de un dataset, y por qué.

Una sola derivación desde los campos descubiertos (y lo que se marcó en el
paso 2): cada plugin de OpenSearch aplica o no según lo que hay en los datos,
con el motivo dicho en el idioma del negocio. La usan el paso 2 ("Lo que va a
tener tu cluster") y el provisioning, así lo que se muestra es lo que se crea.

No agrega lógica: junta las derivaciones que ya existían por separado
(`capabilities.build_spec_from_fields`, `perfiles.perfil_desde_campos`,
`accesos.enmascarados_desde_campos`, el mapa de `dashboards`).
"""
from __future__ import annotations

from typing import Any

import accesos
import capabilities as caps
import perfiles
from dashboards import _CODIGO_DE_PAIS, _PISTA_DE_PAIS

_TEXTO = ("string", "keyword", "text")


def _etiqueta(f: dict) -> str:
    return (f.get("business_label") or f.get("raw_name") or f.get("field_path") or "").strip()


def _por_ruta(fields: list[dict]) -> dict[str, dict]:
    return {(f.get("field_path") or "").strip(): f for f in fields or [] if (f.get("field_path") or "").strip()}


def _nombre_de(campo: str, fields: list[dict]) -> str:
    """La etiqueta de negocio de un campo (`taxful_total_price` → "Total")."""
    campo = campo.removesuffix(".keyword")
    return _etiqueta(_por_ruta(fields).get(campo, {})) or campo


def fecha_del_evento(fields: list[dict]) -> "dict | None":
    """El campo con la fecha real de cada registro. Sin él, `@timestamp` es la
    hora de ingesta y no hay serie temporal que pronosticar ni anomalías que
    buscar."""
    return next((f for f in fields or [] if f.get("role") == "timestamp" and f.get("type") == "date"), None)


def campo_de_mapa(fields: list[dict]) -> "tuple[str, str] | None":
    """("coordenadas" | "pais", campo) para el mapa del dashboard, con la misma
    regla que `dashboards._spec_from_fields`."""
    for f in fields or []:
        if f.get("type") == "geo_point":
            return "coordenadas", f["field_path"]
    for f in fields or []:
        texto = f"{f.get('field_path', '')} {_etiqueta(f)}"
        if (f.get("type") in _TEXTO and _PISTA_DE_PAIS.search(texto)
                and not _CODIGO_DE_PAIS.search(texto)):
            return "pais", f["field_path"]
    return None


# Los que se pueden apagar en el paso 2 (el provisioning los saltea, ver
# `main._excluidos`). El resto es la base: el asistente y lo del cluster.
OPCIONALES = ("forecasting", "anomalias", "alertas", "perfil", "analista", "security_analytics")


def _item(plugin: str, titulo: str, aplica: bool, motivo: str, config: Any = None) -> dict:
    return {"plugin": plugin, "titulo": titulo, "aplica": aplica, "motivo": motivo, "config": config,
            "opcional": plugin in OPCIONALES}


def _forecast_en_palabras(fc: dict, fields: list[dict]) -> str:
    agg = next(iter((fc.get("aggregation_query") or {}).values()), {})
    tipo, cuerpo = next(iter(agg.items()), ("", {}))
    campo = _nombre_de((cuerpo or {}).get("field", ""), fields)
    return {"value_count": "cantidad de registros" if fc.get("feature_name") == "event_volume" else f"cantidad con {campo}",
            "sum": f"total de {campo}", "cardinality": f"{campo} distintos"}.get(tipo, campo)


def plan(slug: str, fields: list[dict], label: str = "",
         enums: "dict[str, list[str]] | None" = None, seguridad: "dict | None" = None) -> list[dict]:
    """Los plugins de OpenSearch para el dataset `slug`, cada uno con
    `{plugin, titulo, aplica, motivo, config}`."""
    from index_template import index_pattern_from_name

    fields = [f for f in fields or [] if isinstance(f, dict)]
    ip = index_pattern_from_name(f"{slug}-logs") if slug else "*"
    spec = caps.build_spec_from_fields(slug, ip, fields, label, enums)
    fecha = fecha_del_evento(fields)
    items: list[dict] = []

    # El asistente: PPLTool sobre los campos que sirven para preguntar.
    n = len(spec["fields"])
    items.append(_item("agente", "Asistente en lenguaje natural", n > 0,
                       f"responde con consultas PPL sobre {n} campos" if n
                       else "no hay campos para consultar", {"fields": sorted(spec["fields"])}))

    sin_fecha = "no hay una fecha del evento: sin ella el tiempo es el de la ingesta"
    fcs = spec["forecasts"]
    items.append(_item(
        "forecasting", "Pronósticos (Forecasting)", bool(fecha and fcs),
        (f"sobre {_etiqueta(fecha)}: " + "; ".join(_forecast_en_palabras(fc, fields) for fc in fcs))
        if fecha and fcs else (sin_fecha if not fecha else "no hay qué pronosticar"),
        fcs))
    feats = caps.features_de_anomalias(spec)
    items.append(_item(
        "anomalias", "Detección de anomalías", bool(fecha and feats),
        ("vigila " + ", ".join(_forecast_en_palabras(f, fields) for f in feats)) if fecha and feats
        else (sin_fecha if not fecha else "no hay qué vigilar"), feats))
    items.append(_item("alertas", "Alerta de anomalías (Alerting)", bool(fecha and feats),
                       "avisa cuando el detector encuentra una anomalía de grado alto" if fecha and feats
                       else "necesita el detector de anomalías"))

    perfil = perfiles.perfil_desde_campos(fields)
    items.append(_item("perfil", "Perfil por entidad (Transform)", perfil is not None,
                       f"un resumen por {perfil['etiqueta']}, actualizado solo" if perfil
                       else "no hay una entidad que se repita (marcala como Entidad en el paso 2)", perfil))

    sensibles = accesos.enmascarados_desde_campos(fields)
    items.append(_item("analista", "Analista con datos enmascarados", bool(sensibles),
                       "enmascara " + ", ".join(_nombre_de(s, fields) for s in sensibles) if sensibles
                       else "ningún campo marcado como Sensible en el paso 2", sensibles))

    mapa = campo_de_mapa(fields)
    items.append(_item("mapa", "Mapa", mapa is not None,
                       (f"por coordenadas ({_nombre_de(mapa[1], fields)})" if mapa[0] == "coordenadas"
                        else f"por país ({_nombre_de(mapa[1], fields)})") if mapa
                       else "no hay coordenadas ni un campo de país",
                       {"tipo": mapa[0], "campo": mapa[1]} if mapa else None))

    patrones = spec.get("pattern_field", "")
    items.append(_item("explicar", "Explicar un cambio", bool(fecha),
                       ("compara la distribución de los campos" + (f" y los patrones de {_nombre_de(patrones, fields)}" if patrones else ""))
                       if fecha else sin_fecha, {"pattern_field": patrones}))

    reglas = (seguridad or {}).get("reglas") or []
    items.append(_item(
        "security_analytics", "Security Analytics (reglas Sigma)", bool(reglas),
        (f"{len(reglas)} regla{'s' if len(reglas) != 1 else ''} sobre tus campos: "
         + "; ".join(r["titulo"] for r in reglas[:4]) + ("…" if len(reglas) > 4 else ""))
        if reglas else "no es un log de seguridad (o no hay eventos sospechosos para detectar)",
        {"reglas": reglas} if reglas else None))

    # Para todo el cluster, no por dataset.
    for plugin, titulo, motivo in (
            ("text2viz", "Gráficos desde una pregunta (text to visualization)", "arma el gráfico de cada respuesta del asistente"),
            ("documentos", "Base de conocimiento (RAG)", "contesta con los documentos que se suban: manuales, procedimientos"),
            ("query_insights", "Query Insights", "muestra las consultas más pesadas del cluster")):
        items.append(_item(plugin, titulo, True, motivo, {"alcance": "cluster"}))
    return items
