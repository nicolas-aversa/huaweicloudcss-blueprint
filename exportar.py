"""Llevarse el cluster: la configuración de un dataset como requests de Dev Tools.

Lo que la plataforma provisiona para un dataset —index template, el asistente
de ml-commons, pronósticos, anomalías y su alerta, el perfil por entidad, el
analista enmascarado y Security Analytics— escrito como lo pegaría una persona
en Dev Tools de su propio cluster, sin la plataforma. Sale del mismo plan
(`plan_de_cluster`) y de los mismos builders que usa el provisioning, así que
no se desincroniza.

Nada de secretos: la API key, las contraseñas y los IDs que devuelve cada
respuesta van como `<PLACEHOLDER>`. Las reglas Sigma, que la API recibe en
YAML y no en JSON, van como `curl` en comentarios.
"""
from __future__ import annotations

import json
from typing import Any

import accesos
import capabilities as caps
import ciclo_de_vida as cdv
import perfiles
import plan_de_cluster
import seguridad
from index_template import build_index_template, index_pattern_from_name

MAAS_API_KEY = "<MAAS_API_KEY>"


def _req(metodo: str, ruta: str, cuerpo: Any = None) -> str:
    linea = f"{metodo} {ruta}"
    if cuerpo is None:
        return linea
    return linea + "\n" + json.dumps(cuerpo, ensure_ascii=False, indent=2)


def _comentario(texto: str) -> str:
    return "\n".join(f"# {l}" if l else "#" for l in texto.splitlines())


def devtools(slug: str, fields: list[dict], *, label: str = "", index_name: str = "",
             seguridad_propuesta: "dict | None" = None, excluir: "list[str] | None" = None,
             filter_code: str = "", retencion_dias: int = 0) -> str:
    """El script de Dev Tools del dataset `slug`."""
    excluir = set(excluir or [])
    index_name = index_name or f"{slug}-%{{+YYYY.MM}}"
    ip = index_pattern_from_name(index_name)
    plan = {i["plugin"]: i for i in plan_de_cluster.plan(slug, fields, label, seguridad=seguridad_propuesta,
                                                         retencion_dias=retencion_dias)}
    aplica = {k: v["aplica"] and k not in excluir for k, v in plan.items()}
    spec = caps.build_spec_from_fields(slug, ip, fields, label)
    bloques: list[str] = [_comentario(
        f"Configuración de OpenSearch para «{label or slug}» (índices {ip}).\n"
        "Pegalo en Dev Tools y corré cada request en orden. Lo que va entre <> lo\n"
        "completás vos: la API key de MaaS, las contraseñas y los IDs que devuelve\n"
        "cada respuesta (connector_id, model_id, _id…).\n"
        "Los dashboards van aparte: el .ndjson se importa en Dashboards → Management →\n"
        "Saved objects → Import.")]

    def seccion(titulo: str, motivo: str = "") -> None:
        bloques.append(_comentario(f"── {titulo} " + "─" * max(4, 60 - len(titulo))
                                   + (f"\n{motivo}" if motivo else "")))

    # El índice y su template.
    template = build_index_template(fields, "", index_name)
    con_sa = aplica.get("security_analytics")
    if con_sa:
        template.setdefault("template", {}).setdefault("aliases", {})[seguridad.alias_del_caso(ip)] = {}
    seccion("Index template", "Los tipos de cada campo, antes de la ingesta.")
    bloques.append(_req("PUT", f"_index_template/{slug}", template))
    if filter_code:
        seccion("Logstash", "El filter del dataset: en CSS → Logstash → Configuration file,\n"
                            f"con output al índice {index_name}.")
        bloques.append(_comentario(filter_code.strip()))

    # El asistente: PPLTool sobre este índice.
    if aplica.get("agente"):
        seccion("Asistente en lenguaje natural (ml-commons)", plan["agente"]["motivo"])
        ppl_prompt = caps.build_ppl_system_prompt(ip, spec["operations"], spec["fields"],
                                                  spec.get("success_code", ""), label or slug)
        bloques += [
            _req("PUT", "_cluster/settings", caps.build_cluster_settings()),
            _req("POST", "_plugins/_ml/model_groups/_register", caps.build_model_group()),
            _req("POST", "_plugins/_ml/connectors/_create", caps.build_ppl_connector(MAAS_API_KEY, ppl_prompt)),
            _req("POST", "_plugins/_ml/connectors/_create", caps.build_llm_connector(MAAS_API_KEY)),
            _req("POST", "_plugins/_ml/models/_register",
                 caps.build_remote_model("platform-ppl", "<CONNECTOR_ID_PPL>", "<MODEL_GROUP_ID>", "NL to PPL")),
            _req("POST", "_plugins/_ml/models/_register",
                 caps.build_remote_model("platform-llm", "<CONNECTOR_ID_LLM>", "<MODEL_GROUP_ID>", "LLM")),
            _req("POST", "_plugins/_ml/models/<MODEL_ID_PPL>/_deploy"),
            _req("POST", "_plugins/_ml/models/<MODEL_ID_LLM>/_deploy"),
        ]
        fuente = {"tool_name": f"PPLTool-{slug}", "label": label or slug, "index_pattern": ip,
                  "operations": spec["operations"], "fields": spec["fields"],
                  "success_code": spec.get("success_code", ""), "ppl_system_prompt": ppl_prompt}
        bloques.append(_req("POST", "_plugins/_ml/agents/_register", caps.build_conversational_agent(
            "<MODEL_ID_LLM>", "<MODEL_ID_PPL>", caps.build_agent_system_instruction([fuente]), [fuente])))
        bloques.append(_comentario("Preguntale: POST _plugins/_ml/agents/<AGENT_ID>/_execute\n"
                                   '{"parameters": {"question": "¿…?"}}'))

    if aplica.get("forecasting"):
        seccion("Pronósticos (Forecasting)", plan["forecasting"]["motivo"])
        for fc in spec["forecasts"]:
            bloques.append(_req("POST", "_plugins/_forecast/forecasters", caps.build_forecaster(
                ip, spec["volume_field"], interval_minutes=spec.get("forecast_interval_minutes", 240),
                horizon=spec.get("forecast_horizon", 8), name=fc["name"], feature_name=fc["feature_name"],
                aggregation_query=fc.get("aggregation_query"), description=fc.get("description", ""),
                history=fc.get("history", 4380), window_delay_minutes=1)))
            bloques.append(_req("POST", f"_plugins/_forecast/forecasters/<FORECASTER_ID_{fc['feature_name'].upper()}>/_start"))

    feats = caps.features_de_anomalias(spec)
    if aplica.get("anomalias"):
        seccion("Detección de anomalías", plan["anomalias"]["motivo"])
        bloques += [_req("POST", "_plugins/_anomaly_detection/detectors", caps.build_ad_detector(slug, ip, feats, 10)),
                    _req("POST", "_plugins/_anomaly_detection/detectors/<DETECTOR_ID>/_start")]
        if aplica.get("alertas"):
            seccion("Alerta de anomalías (Alerting)", plan["alertas"]["motivo"])
            bloques.append(_comentario("Para que la alerta le llegue a alguien, el canal (Slack; para Teams o un\n"
                                       "webhook cambiá `config_type` y su clave). El cluster tiene que poder salir\n"
                                       "a ese host: en CSS, una Cluster Route a su IP."))
            bloques.append(_req("POST", "_plugins/_notifications/configs",
                                caps.build_canal_de_alertas("slack", "<URL_DEL_WEBHOOK>")))
            bloques.append(_req("POST", f"_plugins/_notifications/feature/test/{caps.CANAL_DE_ALERTAS}"))
            bloques.append(_req("POST", "_plugins/_alerting/monitors",
                                caps.build_monitor_de_anomalias(slug, "<DETECTOR_ID>", canal_id=caps.CANAL_DE_ALERTAS)))

    if aplica.get("perfil"):
        seccion("Perfil por entidad (Transform)", plan["perfil"]["motivo"])
        perfil = plan["perfil"]["config"]
        nombre = perfiles.nombre_del_transform(slug)
        bloques += [_req("PUT", f"_plugins/_transform/{nombre}", perfiles.build_transform(slug, ip, perfil)),
                    _req("POST", f"_plugins/_transform/{nombre}/_start")]

    if aplica.get("ciclo_de_vida"):
        seccion("Ciclo de vida de los índices (ISM)", plan["ciclo_de_vida"]["motivo"])
        pid = cdv.nombre_de_politica(slug)
        bloques += [_req("PUT", f"_plugins/_ism/policies/{pid}",
                         cdv.politica(slug, ip, plan["ciclo_de_vida"]["config"]["retencion_dias"])),
                    _comentario("Los índices que ya existen la toman con:"),
                    _req("POST", f"_plugins/_ism/add/{ip}", {"policy_id": pid})]

    if aplica.get("rollup"):
        seccion("Resumen por hora (Rollup)", plan["rollup"]["motivo"])
        cfg = plan["rollup"]["config"]
        cuerpo = cdv.rollup(slug, ip, cfg["dimensiones"], cfg["medidas"], 0)
        cuerpo["rollup"]["schedule"]["interval"]["start_time"] = "<AHORA_EN_EPOCH_MS>"
        bloques.append(_req("PUT", f"_plugins/_rollup/jobs/{cdv.nombre_del_rollup(slug)}", cuerpo))

    if aplica.get("analista"):
        seccion("Analista con datos enmascarados", plan["analista"]["motivo"])
        bloques += [
            _req("PUT", f"_plugins/_security/api/roles/{accesos.nombre_del_rol(slug)}",
                 accesos.rol_analista(ip, plan["analista"]["config"])),
            _req("PUT", f"_plugins/_security/api/internalusers/{accesos.nombre_del_usuario(slug)}",
                 accesos.usuario_analista(slug, "<CONTRASEÑA_DEL_ANALISTA>")),
        ]

    if aplica.get("busqueda"):
        import busqueda
        seccion("Búsqueda híbrida (por palabras y por significado)", plan["busqueda"]["motivo"])
        cfg = plan["busqueda"]["config"]
        tid, idx = busqueda.nombre_del_transform(slug), busqueda.indice(slug)
        bloques += [
            _comentario("El modelo de embeddings: el cluster no lo baja de internet. Subí a un bucket de OBS\n"
                        + busqueda.MODELO["origen"] + busqueda.MODELO["archivo"]
                        + "\ny registralo desde un link firmado de ese objeto."),
            _req("PUT", "_cluster/settings", busqueda.ajustes_del_cluster()),
            _req("POST", "_plugins/_ml/models/_register", busqueda.registro_del_modelo("<LINK_FIRMADO_DE_OBS>")),
            _comentario("Con el model_id de la tarea (GET _plugins/_ml/tasks/<TASK_ID>):\n"
                        "POST _plugins/_ml/models/<MODEL_ID>/_deploy"),
            _req("PUT", f"_ingest/pipeline/{busqueda.PIPELINE_DE_INGESTA}", busqueda.pipeline_de_ingesta("<MODEL_ID>")),
            _req("PUT", f"_search/pipeline/{busqueda.PIPELINE_DE_BUSQUEDA}", busqueda.pipeline_de_busqueda()),
            _req("PUT", idx, busqueda.mapping_del_indice()),
            _req("PUT", f"_plugins/_transform/{tid}", busqueda.transform(slug, ip, cfg["campo"], cfg["tipo"])),
            _req("POST", f"_plugins/_transform/{tid}/_start"),
            _comentario("Probala:"),
            _req("POST", f"{idx}/_search?search_pipeline={busqueda.PIPELINE_DE_BUSQUEDA}",
                 busqueda.consulta_hibrida("<LO QUE BUSCÁS>", "<MODEL_ID>")),
        ]

    if con_sa:
        seccion("Security Analytics", plan["security_analytics"]["motivo"])
        lt = seguridad_derivada_spec(slug, seguridad_propuesta)
        bloques.append(_req("POST", "_plugins/_security_analytics/logtype", seguridad.build_log_type(lt)))
        curls = []
        for n, r in enumerate(lt["reglas"], start=1):
            curls.append(f"Regla {n}: {r['titulo']}  →  su id es <RULE_ID_{n}>\n"
                         f"curl -k -u admin:<CONTRASEÑA_ADMIN> -X POST \"https://<ENDPOINT>:9200/"
                         f"_plugins/_security_analytics/rules?category={lt['nombre']}\" \\\n"
                         "  -H 'Content-Type: application/json' --data-binary @- <<'YAML'\n"
                         + seguridad.sigma_yaml(r, lt["nombre"]) + "YAML")
        bloques.append(_comentario("Las reglas Sigma van en YAML, no en JSON: se crean con curl.\n\n"
                                   + "\n\n".join(curls)))
        reglas = [(f"<RULE_ID_{n}>", r["nivel"]) for n, r in enumerate(lt["reglas"], start=1)]
        bloques.append(_req("POST", "_plugins/_security_analytics/detectors",
                            seguridad.build_detector(slug, lt["nombre"], [seguridad.alias_del_caso(ip)], reglas)))
    return "\n\n".join(bloques) + "\n"


def seguridad_derivada_spec(slug: str, propuesta: dict) -> dict:
    """El tipo de log con sus reglas (sin los meses: en el cluster del cliente
    los índices ya existen o los crea Logstash)."""
    import seguridad_derivada

    return seguridad_derivada.spec_del_caso(slug, propuesta)["log_types"][0]
