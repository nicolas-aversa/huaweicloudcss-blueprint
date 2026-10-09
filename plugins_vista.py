"""La vista "Plugins": por caso, cada plugin que la plataforma dejó en el
cluster, qué se configuró con sus datos, si quedó bien y dónde verlo en
OpenSearch Dashboards.

La plataforma no reemplaza a Dashboards: los resultados se miran allá. Esta
vista guía la demo (qué hay, por qué, y el link directo a cada objeto) y suma
un número por plugin para que el SA sepa a dónde ir primero.

Todo sale de los registros locales, sin tocar el cluster; los números van
aparte (`/api/v1/plugins/numeros`), a demanda.
"""
from __future__ import annotations

import accesos
import ciclo_de_vida as cdv
import perfiles
from plan_de_cluster import _forecast_en_palabras, _nombre_de

# Apps de OpenSearch Dashboards 3.4 (los ids que registra cada plugin).
APP_FORECASTING = "forecasting"
APP_AD = "anomaly-detection-dashboards"
APP_ALERTING = "alerting"
APP_SA = "opensearch_security_analytics_dashboards"
APP_IM = "opensearch_index_management_dashboards"
APP_INSIGHTS = "query-insights-dashboards"
APP_DASHBOARDS = "dashboards"
APP_DISCOVER = "discover"
APP_REPORTING = "reports-dashboards"

# Estado de una tarjeta: cómo quedó lo que se provisionó.
OK, PARCIAL, FALLA, EN_CURSO, EXCLUIDO = "ok", "parcial", "falla", "en_curso", "excluido"
SIN_PROBAR = "sin_probar"


def base_de_dashboards(url: str) -> str:
    """La raíz de Dashboards a partir de `dashboards_url` (la consola de Huawei
    termina en `/app/login`; el acceso directo, en `/_dashboards`)."""
    url = (url or "").strip().rstrip("/")
    for cola in ("/app/login", "/app/home"):
        if url.endswith(cola):
            return url[: -len(cola)]
    return url


def link(base: str, app: str, ruta: str = "") -> str:
    """`<base>/app/<app>#<ruta>`, o "" sin base (no hay a dónde mandar)."""
    if not base:
        return ""
    return f"{base}/app/{app}" + (f"#{ruta}" if ruta else "")


def _tarjeta(plugin: str, titulo: str, que: str, estado: str, motivo: str = "",
             filas: "list[dict] | None" = None, links: "list[dict] | None" = None,
             numero: str = "", accion: "dict | None" = None) -> dict:
    t = {"plugin": plugin, "titulo": titulo, "que": que, "estado": estado, "motivo": motivo,
         "filas": filas or [], "links": [x for x in links or [] if x.get("url")], "numero": numero}
    if accion:
        t["accion"] = accion
    return t


def _estado_de_backtest(texto: str) -> tuple[str, str]:
    """(estado, en palabras) del backtest de un forecaster, como lo dejó el
    provisioning (`TEST_COMPLETE`, `parcial: X de N pasos`, `EN_CURSO (…)`…)."""
    texto = (texto or "").strip()
    if not texto:
        return OK, "creado"
    if texto == "TEST_COMPLETE":
        return OK, "backtest completo"
    if texto.startswith("EN_CURSO"):
        return EN_CURSO, "backtest en curso"
    if texto.startswith("parcial"):
        return PARCIAL, f"backtest {texto}"
    return FALLA, f"backtest: {texto}"


def _peor(estados: list[str]) -> str:
    """El estado de una tarjeta con varias piezas: todas bien es ok, ninguna
    bien es falla, y en el medio parcial."""
    if not estados:
        return OK
    buenos = sum(1 for e in estados if e == OK)
    if buenos == len(estados):
        return OK
    if all(e == EN_CURSO for e in estados if e != OK):
        return EN_CURSO
    return FALLA if buenos == 0 and EN_CURSO not in estados else PARCIAL


def _medida(fc: dict, fields: list[dict]) -> str:
    """Qué mide un forecast (o una feature del detector), en palabras: la
    descripción del spec ("Forecast de volumen de eventos por intervalo") o, si
    no la tiene, la agregación con la etiqueta del campo."""
    d = str(fc.get("description") or "").strip()
    if d.lower().startswith("forecast de "):
        return d[len("forecast de "):]
    return _forecast_en_palabras(fc, fields)


def _forecasting(spec: dict, ids: dict, estado: dict, fields: list[dict], base: str) -> dict:
    fc_ids = ids.get("forecaster_ids") or ([ids["forecaster_id"]] if ids.get("forecaster_id") else [])
    specs = spec.get("forecasts") or []
    if not fc_ids:
        return _tarjeta("forecasting", "Forecasting", "Pronostica las medidas del caso.", FALLA,
                        estado.get("motivo") or "no se creó ningún forecaster")
    guardados = {f.get("id"): f for f in estado.get("forecasters") or []}
    por_nombre = {s.get("name"): s for s in specs}
    filas, estados = [], []
    for i, fc_id in enumerate(fc_ids):
        # Por nombre: si uno no se creó, los demás se corren de lugar en la
        # lista. Por posición solo sin nombre guardado (IDs de antes).
        nombre = (guardados.get(fc_id) or {}).get("nombre")
        fc = por_nombre.get(nombre) if nombre else (specs[i] if i < len(specs) else None)
        fc = fc or {}
        st, texto = _estado_de_backtest((guardados.get(fc_id) or {}).get("estado", ""))
        estados.append(st)
        filas.append({"texto": (_medida(fc, fields) if fc else (guardados.get(fc_id) or {}).get("nombre", fc_id)),
                      "estado": st, "detalle": texto, "numero": f"forecast:{fc_id}",
                      "url": link(base, APP_FORECASTING, f"/forecasters/{fc_id}")})
    horizonte = int(spec.get("forecast_horizon") or 8)
    intervalo = (estado.get("ventana") or {}).get("interval_min")
    cada = f", cada {intervalo} min" if intervalo else ""
    n = len(fc_ids)
    return _tarjeta("forecasting", "Forecasting",
                    f"Pronostica {n} medida{'s' if n != 1 else ''}, {horizonte} pasos hacia adelante{cada}, "
                    "y lo compara con lo que pasó de verdad (backtest).",
                    _peor(estados), "" if _peor(estados) == OK else estado.get("motivo", ""), filas,
                    [{"texto": "Ver los forecasters", "url": link(base, APP_FORECASTING, "/forecasters")}])


def _anomalias(spec: dict, ids: dict, estado: dict, fields: list[dict], base: str) -> dict:
    import capabilities as caps

    por_feature = {fc.get("feature_name"): fc for fc in spec.get("forecasts") or []}
    feats = caps.features_de_anomalias(spec)
    vigila = "; ".join(_medida(por_feature.get(f["feature_name"]) or f, fields) for f in feats) or "el volumen"
    did = ids.get("detector_id")
    if not did:
        return _tarjeta("anomalias", "Anomaly Detection", f"Vigila {vigila}.", FALLA,
                        estado.get("motivo") or "no se creó el detector")
    ok = estado.get("ok", True)
    intervalo = estado.get("intervalo_min")
    return _tarjeta("anomalias", "Anomaly Detection",
                    f"Vigila {vigila}" + (f", cada {intervalo} min" if intervalo else "")
                    + ", sobre todo el rango de los datos (análisis histórico).",
                    OK if ok else FALLA, "" if ok else estado.get("motivo", ""),
                    links=[{"texto": "Ver las anomalías", "url": link(base, APP_AD, f"/detectors/{did}/results")}],
                    numero="anomalias")


def _alertas(ids: dict, estado: dict, base: str, canal: "dict | None" = None) -> dict:
    import capabilities as caps

    mid = ids.get("monitor_id")
    if not mid:
        return _tarjeta("alertas", "Alerting", "Avisa cuando aparece una anomalía de grado alto.", FALLA,
                        estado.get("motivo") or "no se creó el monitor")
    por = f" por {caps.nombre_del_tipo_de_canal(canal['tipo'])}" if (canal or {}).get("ok") and canal.get("tipo") else ""
    sin = "" if por else " Sin un canal configurado (⚙ Configuración), la alerta queda en Alerting y no le llega a nadie."
    return _tarjeta("alertas", "Alerting",
                    f"Avisa{por} cuando el detector encuentra una anomalía de grado alto (≥ 0,7).{sin}", OK,
                    links=[{"texto": "Ver el monitor", "url": link(base, APP_ALERTING, f"/monitors/{mid}?type=monitor")}])


def _seguridad(reg: dict, spec: dict, base: str) -> dict:
    tipos = {lt["nombre"]: lt for lt in (spec or {}).get("log_types") or []}
    detectores = reg.get("detectores") or {}
    filas, estados = [], []
    for nombre, d in detectores.items():
        lt = tipos.get(d.get("log_type", ""), {})
        estados.append(OK)
        filas.append({"texto": lt.get("descripcion") or d.get("log_type") or nombre, "estado": OK,
                      "detalle": f"{len(lt.get('reglas') or [])} regla{'s' if len(lt.get('reglas') or []) != 1 else ''} Sigma",
                      "numero": f"seguridad:{nombre}",
                      "url": link(base, APP_SA, f"/detector-details/{d.get('id', '')}") if d.get("id") else ""})
    faltan = [lt for n, lt in tipos.items() if n not in {d.get("log_type") for d in detectores.values()}]
    for lt in faltan:
        estados.append(FALLA)
        filas.append({"texto": lt.get("descripcion") or lt["nombre"], "estado": FALLA,
                      "detalle": "el detector no se creó (ver Actividad)", "numero": "", "url": ""})
    n_reglas, n_corr = len(reg.get("reglas") or {}), len(reg.get("correlaciones") or {})
    esperadas = len((spec or {}).get("correlaciones") or [])
    que = (f"{len(detectores)} detector{'es' if len(detectores) != 1 else ''} con {n_reglas} regla"
           f"{'s' if n_reglas != 1 else ''} Sigma sobre los campos del caso")
    if esperadas or n_corr:
        que += f", y {n_corr} correlaci{'ones' if n_corr != 1 else 'ón'} entre fuentes"
        if n_corr < esperadas:
            estados.append(FALLA)
    estado = _peor(estados)
    return _tarjeta("security_analytics", "Security Analytics", que + ".", estado,
                    "" if estado == OK else "faltan piezas: el detalle está en Actividad", filas,
                    [{"texto": "Ver los hallazgos", "url": link(base, APP_SA, "/findings")},
                     {"texto": "Ver las correlaciones", "url": link(base, APP_SA, "/correlations")} if (n_corr or esperadas) else {}])


def _perfil(slug: str, perfil: dict, estado: dict, base: str) -> dict:
    nombres = perfil.get("nombres") or {}
    medidas = [nombres.get(m, m) for m in perfil.get("medidas") or {}]
    ok = estado.get("ok", True)
    return _tarjeta("perfil", "Perfil por entidad (Transform)",
                    f"Una fila por {perfil.get('etiqueta') or 'entidad'}, que se actualiza sola: "
                    + ", ".join(medidas) + ".",
                    OK if ok else FALLA, "" if ok else estado.get("motivo", ""),
                    links=[{"texto": "Ver el Transform",
                            "url": link(base, APP_IM, f"/transform-details?id={perfiles.nombre_del_transform(slug)}")}],
                    numero="perfil")


def _analista(slug: str, enmascarados: list[str], estado: dict, fields: list[dict], base: str) -> dict:
    ok = estado.get("ok", True)
    campos = ", ".join(_nombre_de(c, fields) for c in enmascarados)
    return _tarjeta("analista", "Analista con datos enmascarados",
                    f"El usuario {accesos.nombre_del_usuario(slug)} ve los datos con {campos} enmascarados: "
                    "entrá a Dashboards con él (la contraseña está en Resumen → Accesos de demo).",
                    OK if ok else FALLA, "" if ok else estado.get("motivo", ""),
                    links=[{"texto": "Abrir Dashboards", "url": link(base, "home")}])


def _ciclo_de_vida(slug: str, estado: dict, base: str) -> dict:
    ok = estado.get("ok", True)
    dias = estado.get("retencion_dias") or cdv.RETENCION_POR_DEFECTO
    return _tarjeta("ciclo_de_vida", "Ciclo de vida (ISM)",
                    f"Los índices pasan a solo lectura y se compactan a los {cdv.DIAS_HASTA_TIBIO} días, "
                    f"y se borran pasada la retención de {dias} días.",
                    OK if ok else FALLA, "" if ok else estado.get("motivo", ""),
                    links=[{"texto": "Ver la política",
                            "url": link(base, APP_IM, f"/policy-details?id={cdv.nombre_de_politica(slug)}")}],
                    numero="ciclo_de_vida")


def _rollup(slug: str, estado: dict, fields: list[dict], base: str) -> dict:
    ok = estado.get("ok", True)
    medidas = ", ".join(_nombre_de(m, fields) for m in estado.get("medidas") or []) or "las medidas"
    dims = ", ".join(_nombre_de(d, fields) for d in estado.get("dimensiones") or [])
    return _tarjeta("rollup", "Resumen por hora (Rollup)",
                    f"{medidas} por hora" + (f" y por {dims}" if dims else "")
                    + f", en {cdv.indice_del_rollup(slug)}: queda aunque se borren los datos crudos.",
                    OK if ok else FALLA, "" if ok else estado.get("motivo", ""),
                    links=[{"texto": "Ver el rollup",
                            "url": link(base, APP_IM, f"/rollup-details?id={cdv.nombre_del_rollup(slug)}")}],
                    numero="rollup")


def nombre_del_reporte(slug: str) -> str:
    return f"[{slug}] Dashboard en PDF (plataforma)"


def definicion_de_reporte(slug: str, dashboard_id: str, origen: str) -> dict[str, Any]:
    """`POST _plugins/_reports/definition`: el dashboard del caso en PDF, a
    demanda (en Dashboards → Reporting, "Generar"). El backend del plugin
    espera los nombres de sus enums (`Pdf`, `OnDemand`); "pdf" y "On demand"
    son los de la interfaz de Dashboards, y con ellos CSS 3.4 respondía 400
    ("No enum constant ...FileFormat.pdf")."""
    return {"reportDefinition": {
        "name": nombre_del_reporte(slug),
        "isEnabled": True,
        "source": {"description": f"El dashboard de {slug}, en PDF", "type": "Dashboard",
                   "origin": origen, "id": dashboard_id},
        "format": {"duration": "PT8760H", "fileFormat": "Pdf", "header": "", "footer": ""},
        "trigger": {"triggerType": "OnDemand"},
    }}


def _dashboard(entry: dict, base: str, reporte: "dict | None" = None) -> dict:
    did = entry.get("dashboard_id", "")
    importado = bool(entry.get("dashboards_imported"))
    filas = [{"texto": "Búsqueda en Discover: " + (b.get("titulo") or "").split("] ", 1)[-1], "estado": OK,
              "detalle": "guardada", "numero": "", "url": link(base, APP_DISCOVER, f"/view/{b['id']}")}
             for b in entry.get("busquedas") or [] if b.get("id")]
    if reporte:
        filas.append({"texto": "Reporte en PDF (Reporting)", "estado": OK if reporte.get("ok") else FALLA,
                      "detalle": "a demanda" if reporte.get("ok") else (reporte.get("motivo") or "no se creó"),
                      "numero": "", "url": link(base, APP_REPORTING, f"/report_definition_details/{reporte['id']}")
                      if reporte.get("ok") and reporte.get("id") else ""})
    return _tarjeta("dashboard", "Dashboard del caso",
                    "Gráficos armados con los campos del caso, con el rango de fechas de sus datos"
                    + (", sus búsquedas en Discover" if entry.get("busquedas") else "")
                    + (" y su reporte en PDF" if (reporte or {}).get("ok") else "") + ".",
                    OK if importado else FALLA, "" if importado else "no se importó (ver Actividad)", filas,
                    links=[{"texto": "Abrir el dashboard",
                            "url": link(base, APP_DASHBOARDS, f"/view/{did}" if did else "/list")}] if importado else [])


def _busqueda(estado: dict) -> dict:
    """La búsqueda híbrida del caso: sobre qué campo, y el botón para probarla."""
    campo = estado.get("campo") or ""
    if estado.get("no_aplica"):
        return _tarjeta("busqueda", "Búsqueda híbrida", "", EXCLUIDO, estado.get("motivo") or "no aplica")
    ok = bool(estado.get("ok"))
    return _tarjeta("busqueda", "Búsqueda híbrida",
                    f"Busca en «{campo}» por palabras y por significado a la vez: lo exacto sale arriba y "
                    "encuentra también lo que está dicho con otras palabras.",
                    OK if ok else FALLA, "" if ok else estado.get("motivo", ""), numero="busqueda" if ok else "",
                    accion={"id": "probar_busqueda", "texto": "Probar una búsqueda"} if ok else None)


def tarjetas_del_caso(slug: str, *, entry: dict, ids: dict, spec: dict, perfil: "dict | None",
                      enmascarados: list[str], seguridad_reg: dict, seguridad_spec: dict,
                      estados: dict, analista_creado: bool, base: str,
                      fields: "list[dict] | None" = None, canal: "dict | None" = None) -> list[dict]:
    """Las tarjetas de un caso, en el orden en que conviene mostrarlas en una
    demo. Lo que se apagó en el paso 2 aparece como excluido (no como falla).
    Antes de la primera provisión (sin IDs ni estados) no hay nada que mostrar
    de lo que crea "Provisionar plugins"."""
    entry = entry or {}
    fields = fields or entry.get("fields") or []
    excluidos = {str(x) for x in entry.get("excluir") or []}
    provisionado = bool(ids) or bool(estados)
    fuera: list[dict] = []
    if entry.get("dashboards_imported") or entry.get("dashboard_id"):
        fuera.append(_dashboard(entry, base, estados.get("reporte")))
    if "security_analytics" in excluidos:
        fuera.append(_tarjeta("security_analytics", "Security Analytics", "", EXCLUIDO, "excluido en el paso 2"))
    elif seguridad_reg.get("detectores") or (seguridad_spec and provisionado):
        # Security Analytics se arma en el deploy (antes de la ingesta): con el
        # caso ya provisionado, un detector que falta es una falla.
        fuera.append(_seguridad(seguridad_reg, seguridad_spec, base))
    for plugin, armar in (("forecasting", lambda: _forecasting(spec, ids, estados.get("forecasting") or {}, fields, base)),
                          ("anomalias", lambda: _anomalias(spec, ids, estados.get("anomalias") or {}, fields, base)),
                          ("alertas", lambda: _alertas(ids, estados.get("alertas") or {}, base, canal))):
        titulo = {"forecasting": "Forecasting", "anomalias": "Anomaly Detection", "alertas": "Alerting"}[plugin]
        if plugin in excluidos or (plugin == "alertas" and "anomalias" in excluidos):
            fuera.append(_tarjeta(plugin, titulo, "", EXCLUIDO, "excluido en el paso 2"))
        elif plugin in estados or ids.get({"forecasting": "forecaster_ids", "anomalias": "detector_id",
                                            "alertas": "monitor_id"}[plugin]) or (plugin == "forecasting" and ids.get("forecaster_id")):
            fuera.append(armar())
    if "perfil" in excluidos:
        fuera.append(_tarjeta("perfil", "Perfil por entidad (Transform)", "", EXCLUIDO, "excluido en el paso 2"))
    elif perfil and provisionado:
        fuera.append(_perfil(slug, perfil, estados.get("perfil") or {}, base))
    if "analista" in excluidos:
        fuera.append(_tarjeta("analista", "Analista con datos enmascarados", "", EXCLUIDO, "excluido en el paso 2"))
    elif enmascarados and ("analista" in estados or (analista_creado and provisionado)):
        fuera.append(_analista(slug, enmascarados, estados.get("analista") or {}, fields, base))
    for plugin, titulo, armar in (
            ("ciclo_de_vida", "Ciclo de vida (ISM)", lambda: _ciclo_de_vida(slug, estados["ciclo_de_vida"], base)),
            ("rollup", "Resumen por hora (Rollup)", lambda: _rollup(slug, estados["rollup"], fields, base))):
        if plugin in excluidos:
            fuera.append(_tarjeta(plugin, titulo, "", EXCLUIDO, "excluido en el paso 2"))
        elif plugin in estados:
            fuera.append(armar())
    if "busqueda" in excluidos:
        fuera.append(_tarjeta("busqueda", "Búsqueda híbrida", "", EXCLUIDO, "excluido en el paso 2"))
    elif estados.get("busqueda"):
        fuera.append(_busqueda(estados["busqueda"]))
    return fuera


def tarjetas_del_cluster(*, agente: bool, text2viz: dict, base: str, canal: "dict | None" = None,
                         embeddings: "dict | None" = None) -> list[dict]:
    """Lo que es de todo el cluster, no de un caso."""
    import capabilities as caps

    fuera = []
    if canal:
        ok = bool(canal.get("ok"))
        nombre = caps.nombre_del_tipo_de_canal(canal.get("tipo", ""))
        fuera.append(_tarjeta("canal", f"Avisos por {nombre} (Notifications)",
                              f"Las alertas de anomalías avisan por {nombre}"
                              + (f" ({canal['host']})" if canal.get("host") else "") + ".",
                              OK if ok else FALLA, canal.get("motivo", "") if not ok else "",
                              filas=[{"texto": "Mensaje de prueba", "estado": OK if ok else FALLA,
                                      "detalle": "llegó" if ok else "no llegó", "numero": "", "url": ""}],
                              links=[{"texto": "Ver los canales", "url": link(base, "notifications-dashboards", "/channels")}]))
    if agente:
        fuera.append(_tarjeta("agente", "Asistente (ml-commons)",
                              "Un agente conversacional con una herramienta de consulta (PPL) por caso. En el CSS "
                              "se usa desde la plataforma: el Assistant de Dashboards se habilita por ticket.", OK))
    if text2viz:
        ok = bool(text2viz.get("ok"))
        fuera.append(_tarjeta("text2viz", "Text to visualization",
                              "Arma el gráfico de cada respuesta del asistente a partir de su consulta.",
                              OK if ok else FALLA, "" if ok else text2viz.get("motivo", "")))
    # El modelo de la búsqueda híbrida. El CSS no sale a internet: lo sube la
    # plataforma al bucket de demos y el cluster lo carga desde ahí.
    preparar = {"id": "probar_embeddings", "texto": "Volver a prepararlo" if embeddings else "Prepararlo ahora"}
    que = ("El modelo multilingüe que entiende el significado de los textos, para la búsqueda híbrida. "
           "El CSS no sale a internet: se carga desde el bucket de demos.")
    if embeddings is None:
        fuera.append(_tarjeta("embeddings", "Modelo de embeddings", que + " Se prepara al provisionar los plugins "
                              "si algún caso tiene texto libre (la primera vez sube ~490 MB al bucket).",
                              SIN_PROBAR, accion=preparar))
    else:
        ok = bool(embeddings.get("ok"))
        fuera.append(_tarjeta("embeddings", "Modelo de embeddings",
                              que + (f" Desplegado ({embeddings.get('dimensiones')} dimensiones)." if ok else ""),
                              OK if ok else FALLA, "" if ok else embeddings.get("motivo", ""), accion=preparar))
    fuera.append(_tarjeta("query_insights", "Query Insights",
                          "Las consultas más pesadas del cluster (latencia, CPU, memoria), sin configurar nada.", OK,
                          links=[{"texto": "Ver las consultas", "url": link(base, APP_INSIGHTS, "/queryInsights")}],
                          numero="insights"))
    return fuera


# ── Lo que queda guardado de cada provisión ─────────────────────────────────
# Sin esto el estado de un plugin solo vivía en la respuesta de "Provisionar
# plugins" y en Actividad (y ahí, sin el detalle de los backtests).
_YA = ("ya provisionado", "ya estaba")


def estados_desde_resultado(resultado: dict) -> dict:
    """De lo que devuelve `_provision_capabilities` para un caso, lo que se
    guarda por plugin. Un "ya provisionado" no trae nada nuevo: no se guarda
    (así no pisa el motivo de la vez que se creó)."""
    fuera: dict[str, dict] = {}
    for clave, plugin in (("forecast", "forecasting"), ("anomalias", "anomalias"), ("alertas", "alertas")):
        r = resultado.get(clave)
        if not isinstance(r, dict) or "ok" not in r or str(r.get("reason", "")).startswith(_YA):
            continue
        e = {"ok": bool(r["ok"]), "motivo": str(r.get("note") or r.get("reason") or "")[:500]}
        if plugin == "forecasting":
            ids = r.get("forecaster_ids") or []
            estados = [s.split("=", 1) for s in r.get("states") or []]
            e["forecasters"] = [{"id": fid, "nombre": (estados[i][0] if i < len(estados) else ""),
                                 "estado": (estados[i][1] if i < len(estados) and len(estados[i]) > 1 else "")}
                                for i, fid in enumerate(ids)]
            if r.get("window"):
                e["ventana"] = r["window"]
        if plugin == "anomalias" and r.get("intervalo_min"):
            e["intervalo_min"] = r["intervalo_min"]
        fuera[plugin] = e
    return fuera


def estado_simple(resultado: dict) -> "dict | None":
    """`{ok, reason}` de una pieza (perfil, analista, text2viz) → lo que se
    guarda; None si fue un "ya estaba" y ya hay algo guardado que vale más."""
    if not isinstance(resultado, dict) or "ok" not in resultado:
        return None
    return {"ok": bool(resultado["ok"]), "motivo": str(resultado.get("reason") or resultado.get("note") or "")[:500],
            "ya": str(resultado.get("reason", "")).startswith(_YA)}


def mezclar(guardado: dict, nuevo: dict) -> dict:
    """Lo nuevo pisa a lo guardado, salvo un "ya estaba" sobre algo que ya
    estaba bien y tenía su motivo. Sobre una falla, sí: lo que había fallado
    (p. ej. un timeout con el cluster saturado) igual quedó creado."""
    fuera = dict(guardado or {})
    for plugin, e in (nuevo or {}).items():
        if e.get("ya") and (fuera.get(plugin) or {}).get("ok"):
            continue
        fuera[plugin] = {k: v for k, v in e.items() if k != "ya"}
    return fuera
