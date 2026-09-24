""""Iniciar ingesta" arranca sin re-verificar cada .conf cuando no hace falta.

Un apply completo "actualiza" cada configuración de Logstash (el provider
guarda el .conf con las credenciales como `***`: diff perpetuo) y CSS las vuelve
a verificar antes de arrancar. Si las configuraciones ya están y no cambiaron,
alcanza con aplicar la activación. Si algo es nuevo o distinto, dos applies:
primero las configuraciones SIN activarlas, después la activación —la
activación ya no depende de ellas en Terraform, así que en uno solo podría
arrancar una pipeline cuyo .conf todavía se está verificando.
"""
import json
import pathlib
import re

import pytest

import main

_TF = pathlib.Path(__file__).resolve().parent.parent / "terraform" / "main.tf"

PREVIO = {"fintech": {"pipeline_conf": "input {} filter {} output {}", "start_ingestion": False},
          "siem": {"pipeline_conf": "input {} filter { mutate {} } output {}", "start_ingestion": True}}


def _actual(**cambios):
    actual = {k: dict(v) for k, v in PREVIO.items()}
    for slug, conf in cambios.items():
        actual.setdefault(slug, {})["pipeline_conf"] = conf
    for v in actual.values():
        v["start_ingestion"] = True
    return actual


# ── Qué applies se corren ───────────────────────────────────────────────────
def test_el_deploy_sigue_siendo_un_apply_completo():
    pasos = main._pasos_del_apply(False, {}, _actual(), ["fintech"], None)
    assert [p["args"] for p in pasos] == [[]]


def test_con_todo_listo_se_activa_directo():
    pasos = main._pasos_del_apply(True, PREVIO, _actual(), ["fintech"], {"fintech", "siem"})
    assert [p["args"] for p in pasos] == [[main._TARGET_ACTIVACION]]
    assert pasos[0]["sin_activar"] == []
    assert (pasos[0]["desde"], pasos[0]["hasta"]) == (main._PCT_APPLY_DESDE, main._PCT_APPLY_HASTA)


@pytest.mark.parametrize("previo, actual, en_state, por_que", [
    (PREVIO, _actual(fintech="input {} filter { otro } output {}"), {"fintech", "siem"}, "el .conf cambió"),
    (PREVIO, _actual(nuevo="input {} filter {} output {}"), {"fintech", "siem"}, "es nueva"),
    # Registrada e igual, pero su configuración no está en el state (el apply
    # anterior falló antes de crearla): activarla directo arrancaría la nada.
    (PREVIO, _actual(), {"siem"}, "no está en el state"),
    ({}, _actual(), {"fintech", "siem"}, "no hay deploy anterior registrado"),
    (PREVIO, _actual(), None, "no se pudo leer el state"),
])
def test_con_algo_nuevo_o_distinto_van_dos_applies(previo, actual, en_state, por_que):
    slugs = ["fintech", "nuevo"] if "nuevo" in actual else ["fintech"]
    pasos = main._pasos_del_apply(True, previo, actual, slugs, en_state)

    assert [p["args"] for p in pasos] == [[], [main._TARGET_ACTIVACION]], por_que
    # El primero deja las configuraciones listas SIN activar esas pipelines.
    assert pasos[0]["sin_activar"] == slugs
    assert pasos[1]["sin_activar"] == []
    # Y los dos se reparten el tramo del apply, en orden.
    assert pasos[0]["desde"] == main._PCT_APPLY_DESDE
    assert pasos[0]["hasta"] == pasos[1]["desde"] < pasos[1]["hasta"] == main._PCT_APPLY_HASTA


# ── El state ────────────────────────────────────────────────────────────────
def test_se_leen_las_configuraciones_del_state(monkeypatch, tmp_path):
    from types import SimpleNamespace as NS
    salida = ("huaweicloud_css_cluster.opensearch_cluster[0]\n"
              'huaweicloud_css_logstash_configuration.pipeline["fintech"]\n'
              'huaweicloud_css_logstash_configuration.pipeline["siem"]\n'
              "huaweicloud_css_logstash_pipeline.pipeline[0]\n")
    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: NS(returncode=0, stdout=salida))
    assert main._configuraciones_en_state(tmp_path) == {"fintech", "siem"}

    monkeypatch.setattr(main.subprocess, "run", lambda *a, **k: NS(returncode=1, stdout=""))
    assert main._configuraciones_en_state(tmp_path) is None


# ── Los applies en orden, con el tfvars de cada uno ─────────────────────────
def _tfvars(tmp_path):
    ruta = tmp_path / "deploy.auto.tfvars.json"
    ruta.write_text(json.dumps({"pipelines": {
        "fintech": {"pipeline_conf": "x", "start_ingestion": True},
        "siem": {"pipeline_conf": "y", "start_ingestion": True}}}), encoding="utf-8")
    return ruta


def test_el_primer_apply_no_activa_y_el_segundo_si(monkeypatch, tmp_path):
    ruta = _tfvars(tmp_path)
    original = ruta.read_text(encoding="utf-8")
    vistos = []

    def _fake(terraform_dir, args, desde, hasta, tf_lines):
        vistos.append((args, json.loads(ruta.read_text(encoding="utf-8"))["pipelines"]))
        return 0
        yield  # noqa: unreachable — lo vuelve generador

    monkeypatch.setattr(main, "_correr_apply", _fake)
    pasos = main._pasos_del_apply(True, {}, {"fintech": {"pipeline_conf": "x"}}, ["fintech"], set())
    rc = _correr(main._aplicar_pasos(tmp_path, pasos, []))

    assert rc == 0
    (args1, tv1), (args2, tv2) = vistos
    assert args1 == [] and tv1["fintech"]["start_ingestion"] is False
    assert tv1["siem"]["start_ingestion"] is True, "las que ya corrían siguen activas"
    assert args2 == [main._TARGET_ACTIVACION] and tv2["fintech"]["start_ingestion"] is True
    assert ruta.read_text(encoding="utf-8") == original


def test_si_el_primer_apply_falla_no_se_activa_nada(monkeypatch, tmp_path):
    ruta = _tfvars(tmp_path)
    original = ruta.read_text(encoding="utf-8")
    llamados = []

    def _fake(terraform_dir, args, desde, hasta, tf_lines):
        llamados.append(args)
        return 1
        yield  # noqa

    monkeypatch.setattr(main, "_correr_apply", _fake)
    pasos = main._pasos_del_apply(True, {}, {"fintech": {}}, ["fintech"], set())
    rc = _correr(main._aplicar_pasos(tmp_path, pasos, []))

    assert rc == 1 and llamados == [[]]
    assert ruta.read_text(encoding="utf-8") == original, "el tfvars vuelve a lo pedido igual"


def _correr(gen):
    """Consume un generador y devuelve su `return`."""
    try:
        while True:
            next(gen)
    except StopIteration as fin:
        return fin.value


# ── Terraform ───────────────────────────────────────────────────────────────
def test_la_activacion_no_depende_de_las_configuraciones():
    """Leer `huaweicloud_css_logstash_configuration.pipeline[k].name` hacía que
    hasta un `-target` a la activación arrastrara a todas las configuraciones."""
    tf = _TF.read_text(encoding="utf-8")
    bloque = tf[tf.index("active_pipeline_names = ["):]
    bloque = bloque[:bloque.index("]") + 1]
    assert "huaweicloud_css_logstash_configuration" not in bloque
    assert "local.pipeline_conf_names[k]" in bloque
    # Y la configuración usa el mismo nombre: si divergieran, se activaría una
    # configuración que no existe.
    assert re.search(r"name\s*=\s*local\.pipeline_conf_names\[each\.key\]", tf)
    assert 'substr("pipeline-${k}", 0, 32)' in tf


def test_el_target_apunta_al_recurso_de_la_activacion():
    tf = _TF.read_text(encoding="utf-8")
    recurso = main._TARGET_ACTIVACION.split("=", 1)[1]
    tipo, nombre = recurso.split(".")
    assert f'resource "{tipo}" "{nombre}"' in tf
