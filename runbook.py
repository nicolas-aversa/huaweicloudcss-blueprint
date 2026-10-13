"""
runbook.py
==========

El runbook para el cliente: cómo dejar un dataset andando en su propio
cluster CSS, sin la plataforma y sin desplegar nada antes. El despliegue
productivo no lo hace el cliente con esta herramienta: se lleva los pasos y
los archivos, y los aplica él.

Un ZIP con `LEEME.md` (los pasos, en orden) y lo que nombran: el index
template y los plugins como requests de Dev Tools (`exportar`), el pipeline
de Logstash y los dashboards. Puro (sin I/O): `main` arma cada pieza con los
mismos builders que usa el provisioning, así no se desincroniza.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone

import traspaso

# Lo que va entre <> en los archivos, en palabras. Los IDs (`<…_ID>`) los
# devuelve la respuesta de un request anterior: se explican aparte.
_MARCADORES = {
    "MAAS_API_KEY": "la API key de MaaS (ModelArts Studio): el asistente y lo que usa un LLM",
    "NVIDIA_API_KEY": "la API key de NVIDIA NIM: los vectores de la búsqueda por significado",
    "ENDPOINT_DEL_CLUSTER": "el endpoint de tu cluster CSS (IP o dominio, sin el puerto)",
    "USUARIO": "un usuario del cluster que pueda escribir en los índices del dataset",
    "CONTRASEÑA": "su contraseña",
    "BUCKET": "el bucket de OBS de donde lee Logstash",
    "PREFIJO": "la carpeta del bucket con los archivos",
    "REGION": "la región de OBS (p. ej. la-south-2)",
    "ACCESS_KEY_ID": "una AK con lectura del bucket",
    "SECRET_ACCESS_KEY": "su SK",
    "BOOTSTRAP_SERVERS": "los brokers de Kafka (host:puerto, separados por coma)",
    "SASL_PASSWORD": "la contraseña SASL de Kafka",
    "SSL_TRUSTSTORE_PASSWORD": "la contraseña del truststore de Kafka",
    "JDBC_CONNECTION_STRING": "la conexión JDBC a la base",
    "JDBC_USER": "el usuario de la base",
    "JDBC_PASSWORD": "su contraseña",
    "JDBC_DRIVER_LIBRARY": "la ruta del .jar del driver",
    "JDBC_DRIVER_CLASS": "la clase del driver",
    "STATEMENT": "la consulta SQL que trae los registros",
    "URL_DEL_WEBHOOK": "la URL del canal de avisos (Slack, Teams o un webhook)",
    "URL_DE_DASHBOARDS": "la URL de tu OpenSearch Dashboards",
    "AHORA_EN_EPOCH_MS": "la hora actual, en milisegundos desde 1970",
    "CONTRASEÑA_ADMIN": "la contraseña del usuario admin del cluster",
    "ENDPOINT": "el endpoint de tu cluster",
}
_MARCA = re.compile(r"<([A-ZÑ][A-ZÑ0-9_]{2,})>")


def marcadores(*textos: str) -> list[str]:
    """Los `<MARCADORES>` de los textos, sin repetir y en orden de aparición."""
    vistos: dict[str, None] = {}
    for texto in textos:
        for m in _MARCA.findall(texto or ""):
            vistos.setdefault(m, None)
    return list(vistos)


def archivos(slug: str, varios: bool) -> dict[str, str]:
    """Dónde va cada archivo del dataset en el ZIP (con varios, una carpeta por dataset)."""
    base = f"{slug}/" if varios else ""
    return {"template": f"{base}1-index-template.txt", "logstash": f"{base}2-logstash.conf",
            "dashboards": f"{base}3-dashboards.ndjson", "plugins": f"{base}4-plugins.txt"}


def _cada(datasets: list[dict], clave: str, varios: bool) -> str:
    """El archivo, nombrado como lo encuentra el cliente: `x`, `carpeta/x` o
    «el `x` de cada carpeta» (`varios`: el ZIP tiene una carpeta por dataset)."""
    if len(datasets) == 1:
        return f"`{archivos(datasets[0]['slug'], varios)[clave]}`"
    carpetas = ", ".join(f"`{d['slug']}/`" for d in datasets)
    return f"el `{archivos('', False)[clave]}` de cada carpeta ({carpetas})"


def documento(nombre: str, datasets: list[dict], *, dimensionamiento: "dict | None" = None,
              ahora: "datetime | None" = None) -> str:
    """El `LEEME.md`: qué vas a tener, qué necesitás, los pasos y lo que completás.

    Cada dataset: `{slug, label, plan, excluir, index_pattern, template,
    plugins, logstash, dashboards}` (los cuatro últimos, el texto de cada archivo)."""
    ahora = ahora or datetime.now(timezone.utc)
    varios = len(datasets) > 1
    textos = [d[k] for d in datasets for k in ("template", "plugins", "logstash")]
    marcas = marcadores(*textos)
    con_plugins = [d for d in datasets if d.get("plugins")]
    l: list[str] = [
        f"# Runbook: {nombre}",
        "",
        f"Generado por la plataforma el {ahora.strftime('%Y-%m-%d %H:%M')} UTC. Cómo dejar "
        + ("estos datasets" if varios else "este dataset") + " andando en tu cluster CSS (OpenSearch 3.x), sin la "
        "plataforma: los pasos van en orden y cada uno nombra el archivo que usa. Lo que va entre `<>` lo completás vos.",
        "",
    ]
    for d in datasets:
        l += [f"## Qué vas a tener" + (f": {d['label']}" if varios else ""), ""]
        l += traspaso.tabla_del_plan(d["plan"], d.get("excluir")) + [""]

    l += ["## Antes de empezar", "",
          "- Un cluster CSS con OpenSearch 3.x y un Logstash de CSS"
          + (" (el tamaño, en «Para producción»)." if dimensionamiento and dimensionamiento.get("eventos_por_dia") else ".")]
    if "MAAS_API_KEY" in marcas:
        l.append("- Una API key de MaaS, y que el cluster pueda salir a MaaS: en CSS, una Cluster Route a su IP.")
    if "NVIDIA_API_KEY" in marcas:
        l.append("- Una API key de NVIDIA NIM (se crea en build.nvidia.com), y que el cluster pueda salir a "
                 "`integrate.api.nvidia.com`: en CSS, una Cluster Route a cada una de sus IPs. Los textos del campo "
                 "de búsqueda viajan a NVIDIA para vectorizarse.")
    if "URL_DEL_WEBHOOK" in marcas:
        l.append("- El canal de avisos de las alertas (Slack, Teams o un webhook), y salida del cluster a ese host.")
    l.append("")

    l += ["## Pasos", "",
          "### 1. Index template (Dev Tools)", "",
          f"En Dashboards → Dev Tools, corré {_cada(datasets, 'template', varios)}. Fija el tipo de cada campo: "
          "tiene que estar antes de que entre el primer dato.", "",
          "### 2. Logstash", "",
          f"En la consola de CSS → Logstash → Configuration file, creá un pipeline con {_cada(datasets, 'logstash', varios)} "
          "y arrancalo. Completá de dónde lee (el `input`) y tu cluster (`<ENDPOINT_DEL_CLUSTER>`, `<USUARIO>`, "
          "`<CONTRASEÑA>`). El `filter` salió del análisis de la muestra: no hace falta tocarlo.", "",
          "Para verificar, en Dev Tools: " + ", ".join(f"`GET {d['index_pattern']}/_count`" for d in datasets)
          + " tiene que crecer.", "",
          "### 3. Dashboards", "",
          f"En Dashboards → Dashboards Management → Saved objects → Import, subí {_cada(datasets, 'dashboards', varios)}.", "",
          "### 4. Plugins (Dev Tools)", ""]
    if con_plugins:
        l += [f"Cuando ya estén entrando datos, corré {_cada(con_plugins, 'plugins', varios)} request por request, en orden: "
              "los detectores, los pronósticos y los transforms arrancan sobre los datos. Cada respuesta devuelve "
              "IDs (`connector_id`, `model_id`, `_id`…) que usan los requests siguientes: van donde dice `<…_ID>`.", ""]
    else:
        l += ["No hay plugins para configurar: con los pasos 1 a 3 alcanza.", ""]

    propios = [m for m in marcas if m in _MARCADORES]
    if propios:
        l += ["## Lo que completás", "", "| En el archivo | Qué va |", "|---|---|"]
        l += [f"| `<{m}>` | {_MARCADORES[m]} |" for m in propios]
        if any(m.endswith("_ID") for m in marcas):
            l.append("| `<…_ID>` | lo devuelve la respuesta de un request anterior (está dicho en cada uno) |")
        l.append("")

    if dimensionamiento and dimensionamiento.get("eventos_por_dia"):
        l += ["## Para producción", ""] + traspaso.para_produccion(dimensionamiento) + [""]
    l += ["## Cómo se opera", ""]
    for d in datasets:
        operar = traspaso.como_se_opera(d["plan"], d.get("excluir"))
        if not operar:
            continue
        if varios:
            l += [f"**{d['label']}**", ""]
        l += operar + [""]
    return "\n".join(l)


def paquete(nombre: str, datasets: list[dict], *, dimensionamiento: "dict | None" = None,
            ahora: "datetime | None" = None) -> bytes:
    """El ZIP: `LEEME.md` y los archivos de cada dataset."""
    varios = len(datasets) > 1
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("LEEME.md", documento(nombre, datasets, dimensionamiento=dimensionamiento, ahora=ahora))
        for d in datasets:
            n = archivos(d["slug"], varios)
            for clave in ("template", "logstash", "dashboards", "plugins"):
                if d.get(clave):
                    z.writestr(n[clave], d[clave])
    return buf.getvalue()
