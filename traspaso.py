"""
traspaso.py
===========

El documento de traspaso de un dataset: lo que el cliente se lleva además
del cluster. Qué se configuró y por qué (el plan, en palabras de negocio), qué
quedó en el cluster y cómo quedó, cuánto cluster hace falta en producción, y
cómo se opera y se replica sin la plataforma.

Markdown: se lee en cualquier lado y se pega en una wiki. Puro (sin I/O): lo
arma `main` con el plan, los estados guardados y el dimensionamiento.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Dónde se mira cada cosa en OpenSearch Dashboards.
_DONDE = {
    "agente": "El asistente se usa desde la plataforma (en el CSS, el Assistant de Dashboards se habilita por ticket). "
              "Por API: `POST _plugins/_ml/agents/<agent_id>/_execute`.",
    "forecasting": "Dashboards → Forecasting: cada forecaster con su pronóstico y su banda.",
    "anomalias": "Dashboards → Anomaly Detection → el detector → Historical analysis.",
    "alertas": "Dashboards → Alerting → Monitors. El canal de aviso, en Notifications → Channels.",
    "perfil": "Dashboards → Index Management → Transform jobs; la tabla está al final del dashboard del caso.",
    "analista": "Entrar a Dashboards con el usuario analista: ve los datos con los campos sensibles enmascarados.",
    "mapa": "En el dashboard del caso.",
    "explicar": "Desde la plataforma (\"Explicarla con IA\" en la tarjeta de anomalías).",
    "security_analytics": "Dashboards → Security Analytics → Findings, Detectors y Correlations.",
    "ciclo_de_vida": "Dashboards → Index Management → State management policies. La retención se cambia "
                     "editando la política (y `_plugins/_ism/change_policy` para los índices que ya existen).",
    "rollup": "Dashboards → Index Management → Rollup jobs; el índice `rollup-<caso>` se consulta como cualquier otro.",
}

_ESTADO = {"ok": "listo", "parcial": "parcial", "falla": "falló", "en_curso": "en curso", "excluido": "excluido"}


def _num(x) -> str:
    """Un número como se escribe acá: punto de miles y coma decimal."""
    texto = f"{x:,.1f}" if isinstance(x, float) and x != int(x) else f"{int(x):,}"
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def _del_cluster(item: dict) -> bool:
    cfg = item.get("config")
    return isinstance(cfg, dict) and cfg.get("alcance") == "cluster"


def _linea(texto: str) -> str:
    return " ".join(str(texto or "").split())


def documento(slug: str, label: str, plan: list[dict], *, excluir: "list[str] | None" = None,
              tarjetas: "list[dict] | None" = None, dimensionamiento: "dict | None" = None,
              dashboards_url: str = "", ahora: "datetime | None" = None) -> str:
    """El Markdown de traspaso del dataset `slug`."""
    excluir = set(excluir or [])
    ahora = ahora or datetime.now(timezone.utc)
    nombre = label or slug
    l: list[str] = [
        f"# Traspaso: {nombre}",
        "",
        f"Generado por la plataforma el {ahora.strftime('%Y-%m-%d %H:%M')} UTC. Explica qué se configuró en "
        "OpenSearch para este dataset, por qué, cómo quedó y cómo se opera sin la plataforma.",
        "",
        "## Qué se configuró y por qué",
        "",
        "Cada plugin se decidió a partir de los campos de los datos. Lo que no aplica dice por qué.",
        "",
        "| Plugin | ¿Se configura? | Por qué |",
        "|---|---|---|",
    ]
    for i in plan:
        if _del_cluster(i):
            continue
        si = "apagado en el paso 2" if i["plugin"] in excluir and i.get("aplica") else ("sí" if i.get("aplica") else "no")
        l.append(f"| {_linea(i.get('titulo'))} | {si} | {_linea(i.get('motivo'))} |")
    del_cluster = [i for i in plan if _del_cluster(i)]
    if del_cluster:
        l += ["", "Y para todo el cluster: " + "; ".join(f"{_linea(i['titulo'])} ({_linea(i['motivo'])})" for i in del_cluster) + "."]

    l += ["", "## Cómo quedó en el cluster", ""]
    if not tarjetas:
        l.append("Todavía no se desplegó (o no se provisionaron los plugins): esta sección se completa después de "
                 "\"Provisionar plugins\".")
    else:
        if dashboards_url:
            l += [f"Dashboards: {dashboards_url}", ""]
        for t in tarjetas:
            l.append(f"- **{_linea(t['titulo'])}**: {_ESTADO.get(t.get('estado'), t.get('estado'))}."
                     + (f" {_linea(t['que'])}" if t.get("que") else "")
                     + (f" Motivo: {_linea(t['motivo'])}." if t.get("motivo") else ""))
            for f in t.get("filas") or []:
                l.append(f"  - {_linea(f.get('texto'))}: {_linea(f.get('detalle'))}"
                         + (f" ({f['url']})" if f.get("url") else ""))
            for enlace in t.get("links") or []:
                l.append(f"  - {_linea(enlace['texto'])}: {enlace['url']}")

    l += ["", "## Para producción", ""]
    if dimensionamiento and dimensionamiento.get("eventos_por_dia"):
        d = dimensionamiento
        l += [
            f"Con **{_num(d['eventos_por_dia'])} eventos por día** ({_num(d['bytes_por_evento'])} bytes cada uno, de la muestra), "
            f"**{d['retencion_dias']} días** de retención y "
            f"{'alta disponibilidad' if d['alta_disponibilidad'] else 'un solo nodo, sin réplica'}:",
            "",
            f"- **{d['nodos']} nodo{'s' if d['nodos'] != 1 else ''} {d['flavor']}** "
            f"({d['vcpu_por_nodo']} vCPU, {d['ram_por_nodo_gb']} GB de RAM) con **{d['disco_por_nodo_gb']} GB** de disco cada uno"
            + (" — el volumen excede lo que dimensiona la plataforma: hace falta un diseño a medida." if d.get("excede") else "."),
            f"- {d['shards_primarios']} shard{'s' if d['shards_primarios'] != 1 else ''} primario"
            f"{'s' if d['shards_primarios'] != 1 else ''} por índice mensual y {d['replicas']} réplica{'s' if d['replicas'] != 1 else ''}.",
            f"- {d['nodos_logstash']} nodo{'s' if d['nodos_logstash'] != 1 else ''} de Logstash.",
            f"- ~{_num(d['gb_por_dia'])} GB por día; {_num(d['gb_de_disco'])} GB de disco en total.",
            "",
            "Es una estimación. Supuestos: " + "; ".join(d.get("supuestos") or []) + ". "
            "El número fino sale de una prueba de carga en el cluster de la PoC.",
        ]
    else:
        l.append("Sin el volumen diario del cliente no se dimensionó: se completa en el paso 2 (\"Para producción\").")

    l += ["", "## Cómo se opera", ""]
    for i in plan:
        if i.get("aplica") and i["plugin"] not in excluir and i["plugin"] in _DONDE:
            l.append(f"- **{_linea(i['titulo'])}**: {_DONDE[i['plugin']]}")

    l += [
        "",
        "## Cómo aplicarlo en otro cluster",
        "",
        f"1. **Dev Tools**: `{slug}-devtools.txt` (\"Llevarse → Dev Tools\" en la plataforma). Se pega en Dev Tools y se "
        "corre cada request en orden; lo que va entre `<>` se completa (la API key de MaaS, las contraseñas y los IDs "
        "que devuelve cada respuesta).",
        f"2. **Dashboards**: `{slug}-dashboards.ndjson` (\"Llevarse → Dashboards\"), en Dashboards → Dashboards "
        "Management → Saved objects → Import.",
        "3. **Logstash**: el filter del dataset está en el export de Dev Tools, como comentario; va en CSS → "
        "Logstash → Configuration file, con output al índice del caso.",
        "4. **Red**: el cluster tiene que poder salir a MaaS (asistente) y al canal de avisos. En CSS, una Cluster "
        "Route a cada IP.",
        "",
    ]
    return "\n".join(l)
