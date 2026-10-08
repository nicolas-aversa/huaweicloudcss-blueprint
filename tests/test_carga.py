"""Un deploy de 10 casos saturó el nodo de OpenSearch (4 vCPU): Security
Analytics procesando toda la ingesta llenó la cola de búsquedas (1.000 en cola,
260.000 rechazadas) y fallaron forecasts y el alta de un detector de anomalías.
Lo que se cambió: nodo más grande en entornos pesados (solo al crearlo),
espera y reintento en el paso 3, reglas del SIEM afinadas y Transforms cada
minuto."""
import json

import pytest

import main
import perfiles
import verticals


# ── Tamaño del nodo ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("slugs, pesado", [
    (["siem"], True), (["a", "b", "c", "d", "e"], True), (["a", "b", "c", "d"], False), ([], False),
])
def test_cuando_un_entorno_es_pesado(slugs, pesado):
    assert main._entorno_pesado(slugs) is pesado


def test_el_tamano_segun_el_entorno():
    liviano, pesado = main._capacity_for(3), main._capacity_for(3, pesado=True)
    assert (liviano["opensearch_flavor"], liviano["opensearch_volume_size"]) == ("ess.spec-4u8g", 40)
    assert (pesado["opensearch_flavor"], pesado["opensearch_volume_size"]) == ("ess.spec-8u16g", 80)
    assert pesado["logstash_flavor"] == "ess.spec-4u8g", "Logstash no cambia"


def test_un_cluster_existente_conserva_su_tamano(monkeypatch, tmp_path):
    estado = {"resources": [{"type": "huaweicloud_css_cluster", "name": "opensearch_cluster", "instances": [
        {"attributes": {"node_config": [{"flavor": "ess.spec-4u8g", "volume": [{"size": 40}]}]}}]}]}
    monkeypatch.setattr(main.tfstate, "read_state", lambda td: estado)
    assert main._opensearch_del_state(tmp_path) == ("ess.spec-4u8g", 40)
    monkeypatch.setattr(main.tfstate, "read_state", lambda td: {"resources": []})
    assert main._opensearch_del_state(tmp_path) is None


def test_los_tfvars_respetan_el_cluster_existente():
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("    _cap = _capacity_for(len(pipelines_var), pesado=_entorno_pesado(")
    bloque = src[i:i + 600]
    assert 'if _existente:\n        _cap["opensearch_flavor"], _cap["opensearch_volume_size"] = _existente' in bloque


# ── Transforms ──────────────────────────────────────────────────────────────
def test_el_transform_corre_enseguida():
    t = perfiles.build_transform("siem", "siem*", verticals.get_vertical("siem")["perfil"])["transform"]
    assert t["schedule"]["interval"]["unit"] == "Minutes" and t["schedule"]["interval"]["period"] == 1


# ── Esperar a que el cluster tenga lugar y reintentar ───────────────────────
@pytest.mark.parametrize("texto, es", [
    ("INIT_TEST_FAILED: OpenSearchRejectedExecutionException: rejected execution of TimedRunnable", True),
    ("Failed to execute phase [query], all shards failed", True),
    ('status 500: {"reason":"Fail to create detector"}', True),
    ("status 429: too many requests", True),
    ("INIT_TEST_FAILED: el índice no tiene datos", False),
    ("", False),
])
def test_que_es_un_rechazo_por_saturacion(texto, es):
    assert main._es_saturacion(texto) is es


def test_espera_a_que_baje_la_cola(monkeypatch):
    colas = iter([1002, 800, 50])
    monkeypatch.setattr(main, "_cola_de_busquedas", lambda *a: next(colas))
    monkeypatch.setattr(main, "_ESPERA_CLUSTER_MAX_S", 100.0)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    assert main._esperar_cluster_libre("http://x:9200", "a", "p") is True


def test_si_no_baja_sigue_igual(monkeypatch):
    monkeypatch.setattr(main, "_cola_de_busquedas", lambda *a: 1000)
    monkeypatch.setattr(main, "_ESPERA_CLUSTER_MAX_S", 30.0)
    monkeypatch.setattr(main, "_ESPERA_CLUSTER_S", 15.0)
    esperas = []
    monkeypatch.setattr(main.time, "sleep", lambda s: esperas.append(s))
    assert main._esperar_cluster_libre("http://x:9200", "a", "p") is False
    assert esperas == [15.0, 15.0]


def test_sin_poder_medir_no_se_frena(monkeypatch):
    monkeypatch.setattr(main, "_cola_de_busquedas", lambda *a: None)
    assert main._esperar_cluster_libre("http://x:9200", "a", "p") is True


def test_la_cola_se_lee_de_thread_pool(monkeypatch):
    class _R:
        status_code = 200

        def json(self):
            return [{"queue": "1002"}]

    monkeypatch.undo()   # el conftest la neutraliza; acá se prueba la de verdad
    monkeypatch.setattr(main, "_os_req", lambda m, url, *a, **k: _R() if url.endswith("search?format=json&h=queue") else None)
    assert main._cola_de_busquedas("http://x:9200", "a", "p") == 1002


def test_el_paso_3_espera_y_reintenta():
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    assert "_esperar_cluster_libre(_os_base(cluster, request.https_enabled), user, password)" in src[i:src.index("\n    msg = ", i)]
    j = src.index("def _provisionar_anomalias(")
    ad = src[j:src.index("\ndef ", j + 10)]
    assert ad.index("if not _resp_ok(rd) and _es_saturacion(_resp_motivo(rd)):") < ad.index("detector_id = _resp_id(rd)")
    # Los backtests: de a uno con la cola casi vacía, reintento si no arranca,
    # y se juzgan por los pasos escritos (los truncados se relanzan una vez).
    k = src.index("def _lanzar_backtest_de_a_uno(")
    uno = src[k:src.index("\n\n\n", k)]
    assert uno.index("umbral=_COLA_BACKTEST") < uno.index("_lanzar_backtest(base, user, password, fc_id)")
    assert "for intento in range(2):" in uno
    k = src.index("estados = _juzgar_backtests(base, user, password, lanzados,")
    bloque = src[k:k + 1200]
    assert "x[\"task_id\"] = _lanzar_backtest_de_a_uno(base, user, password, x[\"fc_id\"])" in bloque


# ── Reglas del SIEM afinadas ────────────────────────────────────────────────
def test_las_reglas_del_siem_apuntan_a_lo_sospechoso():
    reglas = [r for lt in verticals.security_specs()["siem"]["log_types"] for r in lt["reglas"]]
    assert len(reglas) == 7
    # Ninguna dispara sobre todo un tipo de evento: cada una combina al menos dos
    # condiciones además de la fuente (lo que antes generaba decenas de miles).
    for r in reglas:
        condiciones = {k for k in r["seleccion"] if k != "event.dataset"}
        assert len(condiciones) >= 2, r["titulo"]
    assert not [r for r in reglas if r["seleccion"] == {"threat.matched": "true"}], "threat intel sola: ~8.600 eventos"
    campos = set(verticals.get_vertical("siem")["capability"]["fields"])
    assert {c for r in reglas for c in r["seleccion"]} <= campos


def test_un_detector_se_recrea_si_cambian_sus_reglas(monkeypatch, tmp_path):
    from test_seguridad import _Cluster, _provisionar, MESES_SIEM
    c = _Cluster(indices=MESES_SIEM)
    _provisionar(monkeypatch, tmp_path, c)
    auth_id = c.detectores["siem-siem-auth"]
    # El detector existente tenía otra regla (las de antes de afinar).
    c.entradas[auth_id] = [{"detector_input": {"indices": MESES_SIEM, "custom_rules": [{"id": "REGLA-VIEJA"}]}}]
    c.creados.clear()
    _, pasos = _provisionar(monkeypatch, tmp_path, c)
    assert ("borrado_detector", auth_id) in c.creados
    assert ("Security Analytics · siem · detector siem_auth", True, "recreado con sus índices y reglas actuales") in pasos
    # Los otros, iguales: no se tocan.
    assert ("Security Analytics · siem · detector siem_waf", True, "ya estaba") in pasos


def test_con_security_analytics_el_entorno_es_pesado():
    """Visto en CSS 3.4: FortiAnalyzer con 3 casos más llenó la cola de
    búsquedas del nodo de 4 vCPU y el chat contestaba "all shards failed"."""
    assert main._entorno_pesado(["fortianalyzer", "cts"]) is True
    assert main._entorno_pesado(["mis-firewall", "cts"], {"mis-firewall"}) is True, "un dataset nuevo con reglas"
    assert main._entorno_pesado(["ventas-ecommerce", "cts"]) is False
