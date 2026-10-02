"""
recuperacion.py
===============

Lógica PURA (sin I/O) de "recuperar un deploy cortado". Si el proceso que
corre `terraform apply` muere a la mitad (se cayó la plataforma, se cortó la
red), Huawei termina de crear recursos que Terraform no llegó a guardar en el
state. El apply siguiente quiere crearlos otra vez y Huawei los rechaza:
`VPC.2024` (la regla DNAT ya existe) o "conflict in the request" (el cluster ya
existe). Y un cluster cuya creación Terraform no vio terminar queda marcado
`tainted`: el apply siguiente lo destruiría aunque esté sano.

Antes de cada apply, la plataforma compara el state con lo que hay en Huawei:
- lo que existe en Huawei y el state no tiene, se IMPORTA (se adopta tal cual);
- lo marcado como mal creado que en Huawei está sano, se DESMARCA. También la
  activación de las pipelines: si Terraform se cansó de esperar (timeout) pero
  después arrancaron (un reintento, o la consola), está bien aunque quedó
  marcada; reemplazarla las pararía y las volvería a arrancar.

Para no adoptar recursos de OTRO entorno de la misma cuenta, solo se miran:
- reglas DNAT del NAT gateway de este mismo state;
- clusters con el nombre de este entorno, en su misma subnet, y solo si el
  state ya tiene recursos de este entorno (un deploy en curso);
- configuraciones de Logstash del cluster Logstash de este entorno.
"""

from __future__ import annotations

from typing import Any

_ESTADO_SANO = "200"
_PIPELINE_CORRIENDO = "working"


def instancias(state: dict) -> list[tuple[str, Any, dict, str]]:
    """(dirección, índice, atributos, status) de cada instancia del state."""
    fuera = []
    for res in (state or {}).get("resources") or []:
        if res.get("mode") == "data":
            continue
        for i in res.get("instances") or []:
            k = i.get("index_key")
            base = f"{res['type']}.{res['name']}"
            if isinstance(k, int):
                direccion = f"{base}[{k}]"
            elif isinstance(k, str):
                direccion = f'{base}["{k}"]'
            else:
                direccion = base
            fuera.append((direccion, k, i.get("attributes") or {}, i.get("status") or ""))
    return fuera


def _por_direccion(state: dict) -> dict[str, dict]:
    return {d: a for d, _k, a, _s in instancias(state)}


def a_importar(state: dict, *, proyecto: str, subnet_id: str, reglas_dnat: list[dict],
               clusters: list[dict], confs_logstash: list[str], pipelines: list[str],
               puerto_dashboards: int = 5601, reusa_opensearch: bool = False) -> list[dict]:
    """Lo que existe en Huawei y el state no tiene: `[{direccion, id, que}]`.
    `clusters`: `[{id, name, subnet_id, status}]` de CSS. `reglas_dnat`:
    `[{id, external_service_port}]` del NAT gateway del state. `confs_logstash`:
    nombres de las configuraciones del cluster Logstash del entorno."""
    actual = _por_direccion(state)
    if not actual:
        return []   # sin nada de este entorno en el state: no se adopta nada
    fuera: list[dict] = []
    if not reusa_opensearch:
        for puerto, direccion, que in ((9200, "huaweicloud_nat_dnat_rule.opensearch[0]", "regla DNAT 9200 (OpenSearch)"),
                                       (puerto_dashboards, "huaweicloud_nat_dnat_rule.kibana[0]",
                                        f"regla DNAT {puerto_dashboards} (Dashboards)")):
            if direccion in actual:
                continue
            r = next((r for r in reglas_dnat if int(r.get("external_service_port") or 0) == puerto), None)
            if r:
                fuera.append({"direccion": direccion, "id": r["id"], "que": que})
    for nombre, direccion, saltar in ((f"{proyecto}-opensearch", "huaweicloud_css_cluster.opensearch_cluster[0]", reusa_opensearch),
                                      (f"{proyecto}-logstash", "huaweicloud_css_logstash_cluster.logstash_cluster", False)):
        if saltar or direccion in actual:
            continue
        c = next((c for c in clusters if c.get("name") == nombre and (not subnet_id or c.get("subnet_id") == subnet_id)), None)
        if c:
            fuera.append({"direccion": direccion, "id": c["id"], "que": f"cluster {nombre}"})
    ls = actual.get("huaweicloud_css_logstash_cluster.logstash_cluster") or \
        next(({"id": i["id"]} for i in fuera if i["direccion"] == "huaweicloud_css_logstash_cluster.logstash_cluster"), None)
    if ls and ls.get("id"):
        for slug in pipelines:
            nombre = f"pipeline-{slug}"[:32]
            direccion = f'huaweicloud_css_logstash_configuration.pipeline["{slug}"]'
            if direccion not in actual and nombre in confs_logstash:
                fuera.append({"direccion": direccion, "id": f"{ls['id']}/{nombre}", "que": f"configuración {nombre}"})
    return fuera


def a_desmarcar(state: dict, estados: dict[str, str],
                pipelines: dict[str, str] | None = None) -> list[dict]:
    """Los clusters marcados como mal creados (`tainted`) que en Huawei están
    sanos (status 200), y la activación marcada cuyas pipelines ya corren:
    `[{direccion, que}]`. `estados`: id → status en Huawei; `pipelines`:
    nombre → status de las pipelines del Logstash del entorno."""
    fuera = []
    for direccion, _k, attrs, status in instancias(state):
        if status != "tainted":
            continue
        if direccion.startswith("huaweicloud_css_logstash_pipeline."):
            nombres = list(attrs.get("names") or [])
            if nombres and all((pipelines or {}).get(n) == _PIPELINE_CORRIENDO for n in nombres):
                fuera.append({"direccion": direccion, "que": f"activación de {len(nombres)} pipelines (ya corren)"})
            continue
        if not direccion.startswith(("huaweicloud_css_cluster.", "huaweicloud_css_logstash_cluster.")):
            continue
        if estados.get(attrs.get("id", "")) == _ESTADO_SANO:
            fuera.append({"direccion": direccion, "que": f"cluster {attrs.get('name') or attrs.get('id')}"})
    return fuera
