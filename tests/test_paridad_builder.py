"""Paridad: el flujo de dataset nuevo, sobre los archivos de las demos, tiene
que dar lo mismo que hoy dan las verticales curadas. Es la vara para borrar
`verticals/`: cuando todo esto pasa, una demo es el Builder sobre su archivo.

Corre el descubrimiento real (perfilador, catálogo de formatos) con la
respuesta del LLM de semántica grabada en `fixtures/semantica_demos.json`
(con `deepseek-v4.1-flash`, una vez), así no depende de MaaS. Lo esperado son
las decisiones de cada vertical curada (su medida, su entidad, sus campos
enmascarados), leídas en los nombres que descubre el Builder. Los `xfail`
estrictos son los huecos que faltan cerrar: cuando uno se arregla, el test
avisa para sacarle la marca.

Los datasets no están en el repo (`datasets/*.log`): sin el archivo, se saltea.
"""
import functools
import json
import pathlib

import pytest

import main
import plan_de_cluster

_RAIZ = pathlib.Path(__file__).resolve().parent.parent
_SEMANTICA = json.loads((_RAIZ / "tests" / "fixtures" / "semantica_demos.json").read_text(encoding="utf-8"))
# Las reglas de Security Analytics que propuso el LLM (`seguridad_derivada`).
_SEGURIDAD = json.loads((_RAIZ / "tests" / "fixtures" / "seguridad_demos.json").read_text(encoding="utf-8"))

# Lo que cada vertical curada provisiona hoy, en los nombres del Builder.
ESPERADO = {
    "produccion-pozos.log": {"fecha": True, "medida": "oil_vol", "perfil": "well",
                             # El curado cuenta las lecturas con el pozo parado (`downtime`,
                             # un campo que arma su filter); el Builder, `status_falla`.
                             "critico": "status_falla"},
    # Tiene campos con nombre de seguridad (event, user_id) pero no lo es.
    "streaming-ott.log": {"fecha": True, "medida": "watch_seconds", "entidad": "user_id",
                          "critico": "error_code", "seguridad": False},
    "ventas-ecommerce.log": {"fecha": True, "medida": "taxful_total_price", "entidad": "customer_id",
                             "perfil": "customer_id", "seguridad": False},
    # Sin header: el nombre de la columna lo pone la semántica (triage o triaje).
    "encuentros-clinicos.log": {"fecha": True, "entidad": ("id_paciente", "paciente_id"),
                                "critico": ("triage", "triaje"),
                                "sensibles": [("id_paciente", "paciente_id")]},
    "fortianalyzer.log": {"fecha": True, "medida": "sentbyte", "entidad": "srcip", "critico": "attack",
                          "seguridad": True},
    "fraud-detection.log": {"fecha": True, "medida": "amount", "critico": "is_fraud"},
    # El curado cuenta el paso que falló dentro de `steps`; el Builder, las
    # respuestas distintas de 000 (`response_code_falla`): las mismas transacciones.
    # `account_ref` (el curado lo enmascaraba) es un índice de 1 a 4: no identifica a nadie.
    "transacciones-billetera.log": {"fecha": True, "entidad": "customer_id", "critico": "response_code_falla",
                                    "sensibles": ["customer_id"]},
    "transacciones-alyc.log": {"fecha": True, "medida": "notional", "entidad": "comitente",
                               "sensibles": ["comitente"]},
}

# Los huecos de hoy: (archivo, criterio) → por qué.
HUECOS = {
}


def muestra_repartida(lineas: list[str], n: int = 200) -> list[str]:
    """La muestra que manda el front (`muestraRepartida` en index.html)."""
    if len(lineas) <= n + 1:
        return list(lineas)
    if any(l.count('"') % 2 == 1 for l in lineas[:n + 1]):
        return lineas[:n + 1]
    mitad = n // 2
    salto = (len(lineas) - (mitad + 1)) / mitad
    return lineas[:mitad + 1] + [lineas[mitad + 1 + int(i * salto)] for i in range(mitad)]


@functools.lru_cache(maxsize=None)
def _descubierto(archivo: str) -> tuple:
    """(campos, plan) del Builder para las primeras 200 filas, como las manda el front."""
    import maas_integrator
    import semantica
    import seguridad_derivada

    ruta = _RAIZ / "datasets" / archivo
    lineas = muestra_repartida([l for l in ruta.read_text(encoding="utf-8").splitlines() if l.strip()])

    def _sin_llm(*_a, **_k):
        raise RuntimeError("sin LLM en la paridad")

    def _grabada(_prompt):
        if archivo not in _SEMANTICA:
            raise RuntimeError("sin respuesta grabada")
        return _SEMANTICA[archivo]

    def _reglas_grabadas(_prompt):
        if archivo not in _SEGURIDAD:
            raise RuntimeError("sin respuesta grabada")
        return _SEGURIDAD[archivo]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(semantica, "_llamar_al_llm", _grabada)
        mp.setattr(seguridad_derivada, "_llamar_al_llm", _reglas_grabadas)
        mp.setattr(maas_integrator, "_build_client", _sin_llm)
        r = main.generate_filter_endpoint(main.GenerateFilterRequest(raw_log="\n".join(lineas)))
    campos = [f.model_dump() if hasattr(f, "model_dump") else dict(f) for f in r.fields]
    slug = archivo.removesuffix(".log")
    return campos, {i["plugin"]: i for i in plan_de_cluster.plan(slug, campos, seguridad=r.seguridad)}


def _nombre(campos: list[dict], ruta: str) -> str:
    """El nombre de la columna (`card_number`) de una ruta (`card.number`)."""
    ruta = ruta.removesuffix(".keyword")
    return next((f.get("raw_name") or ruta for f in campos if f.get("field_path") == ruta), ruta)


def _forecasts(campos, plan) -> dict[str, tuple[str, str]]:
    """feature → (operación, columna) de los pronósticos del plan."""
    fuera = {}
    for fc in plan["forecasting"]["config"] or []:
        op, cuerpo = next(iter(next(iter(fc["aggregation_query"].values())).items()))
        fuera[fc["feature_name"]] = (op, _nombre(campos, cuerpo["field"]))
    return fuera


def _chequeo(archivo: str, criterio: str):
    esperado = ESPERADO[archivo][criterio]
    # Un nombre o varios aceptables (los que pone la semántica a un CSV sin header).
    nombres = esperado if isinstance(esperado, tuple) else (esperado,)
    campos, plan = _descubierto(archivo)
    fcs = _forecasts(campos, plan)
    if criterio == "fecha":
        assert plan["forecasting"]["aplica"] and plan["anomalias"]["aplica"], plan["forecasting"]["motivo"]
    elif criterio == "medida":
        assert fcs.get("measure_sum") == ("sum", esperado), fcs
    elif criterio == "entidad":
        assert fcs.get("unique_entities") in {("cardinality", n) for n in nombres}, fcs
    elif criterio == "perfil":
        assert plan["perfil"]["aplica"] and _nombre(campos, plan["perfil"]["config"]["campo"]) == esperado
    elif criterio == "critico":
        # `is_fraud` o el `is_fraud_falla` que arma el .conf: cuentan lo mismo.
        assert (fcs.get("critical_events") or ("", ""))[1] in {x for n in nombres for x in (n, f"{n}_falla")}, fcs
    elif criterio == "sensibles":
        # Cada uno, por cualquiera de sus nombres posibles.
        marcados = {_nombre(campos, s) for s in plan["analista"]["config"] or []}
        for alternativas in esperado:
            alternativas = alternativas if isinstance(alternativas, tuple) else (alternativas,)
            assert marcados & set(alternativas), (alternativas, plan["analista"])
    elif criterio == "seguridad":
        assert bool((plan.get("security_analytics") or {}).get("aplica")) == esperado, plan.get("security_analytics")
    else:
        raise AssertionError(criterio)


def _casos():
    for archivo, crit in ESPERADO.items():
        for criterio in crit:
            marcas = [pytest.mark.skipif(not (_RAIZ / "datasets" / archivo).exists(),
                                         reason=f"falta datasets/{archivo}")]
            if (archivo, criterio) in HUECOS:
                marcas.append(pytest.mark.xfail(strict=True, reason=HUECOS[(archivo, criterio)]))
            yield pytest.param(archivo, criterio, marks=marcas, id=f"{archivo.removesuffix('.log')}-{criterio}")


@pytest.mark.parametrize("archivo,criterio", list(_casos()))
def test_paridad(archivo, criterio):
    _chequeo(archivo, criterio)


def test_los_huecos_son_de_criterios_esperados():
    assert all(c in ESPERADO.get(a, {}) for a, c in HUECOS)
