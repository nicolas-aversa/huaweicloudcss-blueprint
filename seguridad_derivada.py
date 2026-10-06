"""Security Analytics para un dataset nuevo: reglas Sigma sobre SUS campos.

Las reglas prepackaged de Security Analytics esperan los campos de su tipo de
log (`src_ip`, `event_uid`…); un log crudo del cliente casi nunca los trae y
las reglas no matchean nada. Lo que sí funciona —y es lo que hacían a mano las
verticales de seguridad— es un tipo de log propio con reglas escritas sobre los
campos y valores que de verdad llegan.

Acá el LLM las propone mirando los campos descubiertos (con sus valores
frecuentes) y unas líneas, y TODO se valida antes de usarlo:

* cada campo de una regla existe en el dataset;
* cada valor se vio en la muestra (en los valores frecuentes del campo o
  literal en una línea): nada inventado;
* una regla no puede matchear más de una de cada cinco filas de la muestra:
  una regla amplia ("todo login") genera decenas de miles de hallazgos en la
  ingesta y satura el cluster (medido con el SIEM curado).

El resultado tiene la misma forma que la clave `security` de una vertical
(`log_types` con sus `reglas`), así lo provisiona el mismo camino
(`main._provision_security_analytics`).
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

import seguridad

_MODELO = "deepseek-v4.1-flash"
_TIMEOUT_S = 60
_MAX_REGLAS = 8
# Más que esto de la muestra es una regla de "todo un tipo de evento".
_MAX_COBERTURA = 0.2
_TAG = re.compile(r"^attack\.[a-z0-9_.]+$")

# Señales de que un dataset es de seguridad: sin ninguna, no se llama al LLM.
_PISTA = re.compile(
    r"(^|[._])(src|dst|source|destination|client|remote|attack|threat|severity|action|"
    r"virus|malware|ips|firewall|waf|login|logon|auth|sudo|ssh|blocked|denied|policy|rule|"
    r"signature|alert|user|usuario|event|evento)([._]|$)", re.I)

_PROMPT = """Sos analista de seguridad. Te paso los campos de un log (con sus valores \
frecuentes) y unas líneas. Si el log tiene eventos de seguridad (tráfico de red, \
autenticación, ataques, malware, accesos, cambios de configuración), escribí reglas \
de detección para el Security Analytics de OpenSearch. Devolvé SOLO un JSON así:

{{"es_seguridad": true, "descripcion": "qué fuente es, corto",
  "reglas": [{{"titulo": "...", "descripcion": "...", "nivel": "critical|high|medium|low",
              "tags": ["attack.t1110"], "seleccion": {{"<campo>": "<valor>" o ["<v1>", "<v2>"]}}}}]}}

- Hasta {max_reglas} reglas, en castellano, sobre lo SOSPECHOSO de verdad (un ataque \
bloqueado de severidad alta, un login fallido de admin, un virus): nunca "todo el \
tráfico" ni "todos los logins". Cada regla tiene que ser rara en los datos.
- "seleccion": todos los campos se cumplen a la vez; una lista es cualquiera de sus \
valores. Usá SOLO campos de la lista, por su nombre exacto, y SOLO valores que \
aparecen en sus valores frecuentes o en las líneas.
- "tags": técnicas de MITRE ATT&CK como attack.tNNNN (o attack.<táctica>).
- Si no es un log de seguridad: {{"es_seguridad": false, "reglas": []}}.

Campos:
{campos}

Líneas:
{lineas}
"""


def candidato(fields: list[dict]) -> bool:
    """¿Vale la pena preguntarle al LLM? Una IP o un campo con nombre de
    seguridad. Un dataset de ventas no paga el llamado."""
    return any((f.get("type") == "ip") or _PISTA.search(f.get("field_path") or f.get("raw_name") or "")
               for f in fields or [])


def _payload(fields: list[dict]) -> str:
    return json.dumps([{"campo": f.get("field_path"), "tipo": f.get("type"),
                        "etiqueta": f.get("business_label") or "",
                        "valores_frecuentes": (f.get("frecuentes") or [])[:8]}
                       for f in fields if f.get("field_path")], ensure_ascii=False)


def _llamar_al_llm(prompt: str) -> str:
    from maas_integrator import _build_client, _chat

    client = _build_client().with_options(timeout=_TIMEOUT_S, max_retries=0)
    r = _chat(client, model=_MODELO, messages=[{"role": "user", "content": prompt}],
              response_format={"type": "json_object"}, temperature=0,
              extra_body={"chat_template_kwargs": {"thinking": False}})
    return r.choices[0].message.content or ""


def _visto(campo: str, valor: Any, fields_por_ruta: dict, texto: str) -> bool:
    v = str(valor)
    if v in [str(x) for x in (fields_por_ruta.get(campo) or {}).get("frecuentes") or []]:
        return True
    return bool(v) and v in texto


def _cobertura(seleccion: dict, lineas: list[str]) -> float:
    """Qué parte de la muestra matchea la regla, aproximado por texto: una línea
    matchea si trae algún valor de cada campo."""
    if not lineas:
        return 0.0
    def matchea(l: str) -> bool:
        return all(any(str(v) in l for v in (vals if isinstance(vals, list) else [vals]))
                   for vals in seleccion.values())
    return sum(1 for l in lineas if matchea(l)) / len(lineas)


def validar(datos: Any, fields: list[dict], lineas: list[str]) -> "dict | None":
    """El spec de seguridad (`{descripcion, reglas}`) con lo válido, o None."""
    if not isinstance(datos, dict) or datos.get("es_seguridad") is not True:
        return None
    por_ruta = {f.get("field_path"): f for f in fields or [] if f.get("field_path")}
    texto = "\n".join(lineas)
    reglas, titulos = [], set()
    for r in datos.get("reglas") or []:
        if not isinstance(r, dict) or len(reglas) >= _MAX_REGLAS:
            continue
        titulo = str(r.get("titulo") or "").strip()[:120]
        nivel = str(r.get("nivel") or "").strip().lower()
        sel = r.get("seleccion")
        if not titulo or titulo in titulos or nivel not in seguridad.NIVELES or not isinstance(sel, dict) or not sel:
            continue
        limpia = {}
        for campo, valor in sel.items():
            valores = valor if isinstance(valor, list) else [valor]
            buenos = [v for v in valores if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip()]
            if (campo not in por_ruta or not buenos or len(buenos) != len(valores)
                    or not all(_visto(campo, v, por_ruta, texto) for v in buenos)):
                limpia = {}
                break
            limpia[campo] = buenos if isinstance(valor, list) else buenos[0]
        if not limpia or _cobertura(limpia, lineas) > _MAX_COBERTURA:
            continue
        tags = [t for t in (r.get("tags") or []) if isinstance(t, str) and _TAG.match(t.strip().lower())]
        reglas.append({"titulo": titulo, "descripcion": str(r.get("descripcion") or titulo).strip()[:300],
                       "nivel": nivel, "tags": [t.strip().lower() for t in tags][:5], "seleccion": limpia})
        titulos.add(titulo)
    if not reglas:
        return None
    return {"descripcion": str(datos.get("descripcion") or "").strip()[:120], "reglas": reglas}


def proponer(fields: list[dict], lineas: list[str],
             llamar: "Callable[[str], str] | None" = None) -> "dict | None":
    """Las reglas de un dataset de seguridad, validadas; None si no es de
    seguridad, si el LLM falla o si ninguna regla pasa la validación."""
    if not candidato(fields):
        return None
    muestra = [l for l in lineas or [] if l.strip()]
    prompt = _PROMPT.format(max_reglas=_MAX_REGLAS, campos=_payload(fields),
                            lineas="\n".join(l[:600] for l in muestra[:8]))
    try:
        crudo = (llamar or _llamar_al_llm)(prompt)
        inicio, fin = crudo.index("{"), crudo.rindex("}")
        datos = json.loads(crudo[inicio:fin + 1])
    except Exception as exc:  # noqa: BLE001 — sin LLM no hay reglas, nada más
        print(f"[seguridad] sin reglas: {type(exc).__name__}: {str(exc)[:160]}")
        return None
    return validar(datos, fields, muestra)


def spec_del_caso(slug: str, propuesta: dict, meses: "tuple[str, str] | None" = None) -> dict:
    """La propuesta como la clave `security` de una vertical: un tipo de log
    propio con el nombre del caso."""
    nombre = re.sub(r"[^a-z0-9_]", "_", slug.lower())
    spec = {"log_types": [{"nombre": nombre, "descripcion": propuesta.get("descripcion") or slug,
                           "reglas": list(propuesta.get("reglas") or [])}],
            "correlaciones": list(propuesta.get("correlaciones") or [])}
    if meses:
        spec["meses"] = tuple(meses)
    return spec


# ── Los meses del dataset ───────────────────────────────────────────────────
def _mes(valor: str, formato: str) -> "str | None":
    """'YYYY-MM' de una fecha con su formato de Logstash."""
    from datetime import datetime, timezone

    import perfilador

    v = str(valor).strip()
    if formato in ("UNIX", "UNIX_MS"):
        try:
            seg = int(v) / (1000 if formato == "UNIX_MS" else 1)
            return datetime.fromtimestamp(seg, tz=timezone.utc).strftime("%Y-%m")
        except (ValueError, OverflowError, OSError):
            return None
    f = next((x for x in perfilador._FECHAS if x.joda == formato), None)
    d = perfilador.leer_fecha(v, f) if f is not None else None
    return d.strftime("%Y-%m") if d else None


def _siguiente(mes: str) -> str:
    anio, m = (int(x) for x in mes.split("-"))
    return f"{anio + (m == 12):04d}-{1 if m == 12 else m + 1:02d}"


def meses_del_dataset(texto: str, fields: list[dict]) -> "tuple[str, str] | None":
    """(primer mes, último mes) de los eventos del archivo, para crear sus
    índices mensuales antes que los detectores. Se lee cada línea con el mismo
    perfil que arma el .conf; si no es una tabla (syslog, CEF), la primera
    fecha `YYYY-MM-DD` de cada línea. Un mes de margen al final: una hora local
    de fin de mes cae en el mes siguiente en UTC, que es como nombra Logstash."""
    import perfilador

    lineas = [l for l in (texto or "").splitlines() if l.strip()]
    if not lineas:
        return None
    meses: set[str] = set()
    perfil = perfilador.perfilar(lineas[:201])
    col = perfil.columna(perfil.fecha_evento) if perfil.estructurado and perfil.fecha_evento else None
    if col is not None:
        for l in lineas:
            valores = perfilador._valores_de_linea(perfil, l.strip()) or {}
            v = valores.get(col.path)
            m = _mes(v, col.formato_fecha) if v not in (None, "") else None
            if m:
                meses.add(m)
    if not meses:
        for l in lineas:
            m = re.search(r"\b((?:19|20)\d{2})-(\d{2})-\d{2}", l)
            if m and 1 <= int(m.group(2)) <= 12:
                meses.add(f"{m.group(1)}-{m.group(2)}")
    if not meses:
        return None
    return min(meses), _siguiente(max(meses))


# ── Correlaciones entre las fuentes de un mismo dataset ─────────────────────
_MAX_CORRELACIONES = 3


def _valor_lucene(v: Any) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def consulta_de_reglas(reglas: list[dict]) -> str:
    """Los eventos que matchean alguna regla, en query string: lo que la
    correlación busca de cada lado."""
    partes = []
    for r in reglas:
        condiciones = []
        for campo, valor in r["seleccion"].items():
            valores = valor if isinstance(valor, list) else [valor]
            condiciones.append(f"{campo}:{_valor_lucene(valores[0])}" if len(valores) == 1
                               else f"{campo}:(" + " OR ".join(_valor_lucene(v) for v in valores) + ")")
        partes.append("(" + " AND ".join(condiciones) + ")")
    return " OR ".join(partes)


def correlaciones(familia: str, fuentes: list[tuple[str, dict, str]]) -> list[dict]:
    """De a pares de fuentes (slug, spec, index pattern): un hallazgo en una y
    otro en la otra dentro de una hora. Hasta tres."""
    fuera = []
    for i in range(len(fuentes)):
        for j in range(i + 1, len(fuentes)):
            if len(fuera) >= _MAX_CORRELACIONES:
                return fuera
            lados = []
            for slug, spec, patron in (fuentes[i], fuentes[j]):
                lt = (spec.get("log_types") or [{}])[0]
                if not lt.get("reglas"):
                    break
                lados.append({"log_type": lt["nombre"], "index": patron, "query": consulta_de_reglas(lt["reglas"])})
            if len(lados) == 2:
                a, b = fuentes[i][0], fuentes[j][0]
                fuera.append({"nombre": f"{familia}-{a}-{b}"[:120],
                              "descripcion": f"Hallazgos en {a} y en {b} dentro de una hora",
                              "ventana_min": 60, "correlate": lados})
    return fuera
