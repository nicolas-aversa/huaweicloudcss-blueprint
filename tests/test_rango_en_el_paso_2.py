"""El rango de los dashboards se ajusta en el paso 2, con los primeros
documentos, y se sigue mientras entra la ingesta. Antes quedaba hasta
"Activar los plugins" la ventana fija de la puesta en marcha (2025-07…2026-07)
y un caso con datos de octubre abría vacío (visto en un deploy de 11 casos)."""
import inspect
import itertools
import json
import threading

import pytest

import main

_CLUSTER = {"public_endpoint": "1.2.3.4:9200"}
_RANGO = "2026-10-13 a 2026-10-20 (1 dashboard)"


def _pedido(**k):
    return main.TerraformDeployRequest(**{"pipeline_conf": "x", "pipeline_slug": "fix-oms",
                                          "opensearch_index": "fix-oms-%{+YYYY.MM}", "opensearch_password": "pw",
                                          "start_ingestion": True, **k})


def _pasos(gen):
    return [json.loads(e[6:]) for e in gen]


@pytest.fixture
def ajustes(monkeypatch):
    """Los ajustes pedidos y, aparte, el seguimiento que quedó andando."""
    hechos, seguidos, listo = [], [], threading.Event()
    monkeypatch.setattr(main, "_ajustar_tiempo_de_dashboards", lambda *a: hechos.append(a) or _RANGO)
    monkeypatch.setattr(main, "_seguir_el_rango_de_la_ingesta", lambda *a: seguidos.append(a) or listo.set())
    return hechos, seguidos, listo


def test_con_los_primeros_documentos_los_dashboards_abren_sobre_los_datos(ajustes, tmp_path):
    hechos, seguidos, listo = ajustes
    req = _pedido()
    pasos = _pasos(main._rango_de_los_dashboards(req, _CLUSTER, tmp_path))

    assert pasos == [{"type": "step", "name": "Rango de los dashboards", "ok": True,
                      "reason": f"del {_RANGO}; se reajusta solo mientras termina de entrar la ingesta"}]
    assert hechos == [(main._os_base(_CLUSTER, req.https_enabled), "admin", "pw", tmp_path, ["fix-oms"])]
    assert listo.wait(5), "la ingesta sigue entrando: alguien tiene que seguir el rango"
    assert seguidos == hechos


def test_con_varios_casos_ajusta_los_del_deploy(ajustes, tmp_path):
    hechos, _seguidos, listo = ajustes
    req = _pedido(cases=[main.PipelineCase(slug="fix-oms"), main.PipelineCase(slug="cts")])
    list(main._rango_de_los_dashboards(req, _CLUSTER, tmp_path))
    assert hechos[0][-1] == ["fix-oms", "cts"]
    assert listo.wait(5)


def test_sin_contrasena_no_toca_nada(ajustes, tmp_path):
    hechos, seguidos, _listo = ajustes
    assert _pasos(main._rango_de_los_dashboards(_pedido(opensearch_password=""), _CLUSTER, tmp_path)) == []
    assert hechos == [] and seguidos == []


def test_si_el_ajuste_falla_lo_dice_y_lo_sigue_intentando(ajustes, monkeypatch, tmp_path):
    _hechos, _seguidos, listo = ajustes

    def falla(*_a):
        raise RuntimeError("cluster ocupado")

    monkeypatch.setattr(main, "_ajustar_tiempo_de_dashboards", falla)
    pasos = _pasos(main._rango_de_los_dashboards(_pedido(), _CLUSTER, tmp_path))
    assert [(p["name"], p["ok"]) for p in pasos] == [("Rango de los dashboards", False)]
    assert "cluster ocupado" in pasos[0]["reason"]
    assert listo.wait(5)


def test_el_paso_2_ajusta_el_rango_despues_de_ver_los_primeros_documentos():
    src = inspect.getsource(main._deploy_stream_gen_raw)
    assert ("if request.start_ingestion:\n        yield from _verificar_ingesta(request, cluster)\n"
            "        yield from _rango_de_los_dashboards(request, cluster, terraform_dir)") in src
    assert src.index("_rango_de_los_dashboards(") < src.index('"type": "complete"')


# ── El seguimiento, mientras entra la ingesta ───────────────────────────────

def _registro(tmp_path, una_vez=True):
    conf = "input { s3 { watch_for_new_files => false } }" if una_vez else "input { s3 { } }"
    main._write_pipelines_registry(tmp_path, {"fix-oms": {
        "index": "fix-oms-%{+YYYY.MM}", "dashboard_id": "D1", "pipeline_conf": conf, "start_ingestion": True}})


def _seguir(monkeypatch, tmp_path, cuentas):
    """Corre el seguimiento original con `cuentas` como documentos del índice
    (y después, otra cuenta que no se tendría que llegar a ver), con un reloj
    que avanza una vuelta por consulta; devuelve los ajustes del rango."""
    hechos = []
    cuentas = itertools.chain(cuentas, itertools.repeat(999_999))
    vueltas = itertools.count()
    monkeypatch.setattr(main, "_index_doc_count", lambda *a, **k: next(cuentas))
    monkeypatch.setattr(main, "_ajustar_tiempo_de_dashboards", lambda *a: hechos.append(a) or _RANGO)
    main._seguimiento_original("http://x:9200", "admin", "pw", tmp_path, ["fix-oms"], dormir=lambda _s: None,
                               reloj=lambda: next(vueltas) * main._INGESTA_SONDEO_S)
    return hechos


def test_reajusta_mientras_crece_y_termina_cuando_deja_de_crecer(monkeypatch, tmp_path):
    _registro(tmp_path)
    hechos = _seguir(monkeypatch, tmp_path, [100, 250, 250])
    assert len(hechos) == 2, "un ajuste por cada vuelta en que creció; con la cuenta igual, ya vio todo"
    assert hechos[0] == ("http://x:9200", "admin", "pw", tmp_path, ["fix-oms"])


def test_si_el_cluster_no_contesta_vuelve_a_mirar(monkeypatch, tmp_path):
    _registro(tmp_path)
    assert len(_seguir(monkeypatch, tmp_path, [None, 100, 100])) == 1


def test_una_fuente_en_vivo_no_se_sigue(monkeypatch, tmp_path):
    _registro(tmp_path, una_vez=False)
    assert _seguir(monkeypatch, tmp_path, []) == [], "crece siempre: no hay un final que esperar"


def test_si_la_ingesta_no_termina_se_rinde_al_tope(monkeypatch, tmp_path):
    _registro(tmp_path)
    monkeypatch.setattr(main, "_INGESTA_ESPERA_MAX_S", 3 * main._INGESTA_SONDEO_S)
    assert 0 < len(_seguir(monkeypatch, tmp_path, itertools.count(1))) <= 3


def test_un_error_no_rompe_el_hilo(monkeypatch, tmp_path):
    _registro(tmp_path)

    def falla(*_a, **_k):
        raise RuntimeError("se cayó la conexión")

    monkeypatch.setattr(main, "_index_doc_count", falla)
    main._seguimiento_original("http://x:9200", "admin", "pw", tmp_path, ["fix-oms"], dormir=lambda _s: None)
