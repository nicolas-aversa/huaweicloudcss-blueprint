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

# Lo que cada vertical curada provisiona hoy, en los nombres del Builder.
ESPERADO = {
    "ventas-ecommerce.log": {"fecha": True, "medida": "taxful_total_price", "entidad": "customer_id",
                             "perfil": "customer_id"},
    "produccion-pozos.log": {"fecha": True, "medida": "oil_vol", "perfil": "well",
                             # El curado cuenta las lecturas con el pozo parado (`downtime`,
                             # un campo que arma su filter).
                             "critico": "downtime"},
    "streaming-ott.log": {"fecha": True, "medida": "watch_seconds", "entidad": "user_id",
                          "critico": "error_code"},
    "encuentros-clinicos.log": {"fecha": True, "entidad": "id_paciente", "critico": "triaje",
                                "sensibles": {"id_paciente"}},
    "fortianalyzer.log": {"fecha": True, "medida": "sentbyte", "entidad": "srcip", "critico": "attack",
                          "seguridad": True},
    "fraud-detection.log": {"fecha": True, "medida": "amount", "critico": "is_fraud"},
    "transacciones-billetera.log": {"fecha": True, "entidad": "customer_id", "critico": "failed_at_code",
                                    "sensibles": {"customer_id", "account_ref"}},
    "transacciones-alyc.log": {"fecha": True, "medida": "notional", "entidad": "comitente",
                               "sensibles": {"comitente"}},
}

# Los huecos de hoy: (archivo, criterio) → por qué.
HUECOS = {
    ("produccion-pozos.log", "critico"): "el crítico es un valor de `status` (parado), no un campo propio",
    ("fortianalyzer.log", "seguridad"): "Security Analytics sale solo de las verticales curadas",
    ("transacciones-billetera.log", "critico"): "el fallo está dentro del JSON de `steps`",
    ("transacciones-billetera.log", "sensibles"): "un log clave=valor no pasa por la semántica",
    ("transacciones-alyc.log", "fecha"): "la fecha del principio de la línea no se reconoce",
    ("transacciones-alyc.log", "medida"): "un log clave=valor no pasa por la semántica",
    ("transacciones-alyc.log", "entidad"): "un log clave=valor no pasa por la semántica",
    ("transacciones-alyc.log", "sensibles"): "un log clave=valor no pasa por la semántica",
}


@functools.lru_cache(maxsize=None)
def _descubierto(archivo: str) -> tuple:
    """(campos, plan) del Builder para las primeras 200 filas, como las manda el front."""
    import maas_integrator
    import semantica

    ruta = _RAIZ / "datasets" / archivo
    lineas = [l for l in ruta.read_text(encoding="utf-8").splitlines() if l.strip()][:201]

    def _sin_llm(*_a, **_k):
        raise RuntimeError("sin LLM en la paridad")

    def _grabada(_prompt):
        if archivo not in _SEMANTICA:
            raise RuntimeError("sin respuesta grabada")
        return _SEMANTICA[archivo]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(semantica, "_llamar_al_llm", _grabada)
        mp.setattr(maas_integrator, "_build_client", _sin_llm)
        r = main.generate_filter_endpoint(main.GenerateFilterRequest(raw_log="\n".join(lineas)))
    campos = [f.model_dump() if hasattr(f, "model_dump") else dict(f) for f in r.fields]
    slug = archivo.removesuffix(".log")
    return campos, {i["plugin"]: i for i in plan_de_cluster.plan(slug, campos)}


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
    campos, plan = _descubierto(archivo)
    fcs = _forecasts(campos, plan)
    if criterio == "fecha":
        assert plan["forecasting"]["aplica"] and plan["anomalias"]["aplica"], plan["forecasting"]["motivo"]
    elif criterio == "medida":
        assert fcs.get("measure_sum") == ("sum", esperado), fcs
    elif criterio == "entidad":
        assert fcs.get("unique_entities") == ("cardinality", esperado), fcs
    elif criterio == "perfil":
        assert plan["perfil"]["aplica"] and _nombre(campos, plan["perfil"]["config"]["campo"]) == esperado
    elif criterio == "critico":
        assert (fcs.get("critical_events") or ("", ""))[1] == esperado, fcs
    elif criterio == "sensibles":
        assert esperado <= {_nombre(campos, s) for s in plan["analista"]["config"] or []}, plan["analista"]
    elif criterio == "seguridad":
        assert (plan.get("security_analytics") or {}).get("aplica"), "sin Security Analytics"
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
