"""El Logstash que CSS rechaza mientras crea el OpenSearch se reintenta solo.

En la cuenta de un SA nuevo, CSS retuvo el alta del Logstash los 6 minutos que
tardó el OpenSearch y recién ahí la rechazó con CSS.0001 ("Transaction rolled
back…"). Los parámetros eran los de siempre: el deploy quedaba sin Logstash y
había que volver a desplegar a mano.
"""
import inspect
import json

import pytest

import main

LS = "huaweicloud_css_logstash_cluster.logstash_cluster"

_RECHAZO = (
    'Error: error creating CSS logstash cluster: Bad request with: [POST '
    'https://css.la-south-2.myhuaweicloud.com/v1.0/0123456789abcdef0123456789abcdef/clusters], '
    'request_id: 00000000000000000000000000000000, error message: {"errCode":"CSS.0001",'
    '"externalMessage":"CSS.0001 : Incorrect parameters. (Transaction rolled back because it has '
    'been marked as rollback-only)"}')


def _salida(error: str = _RECHAZO, recurso: str = LS) -> list[str]:
    tipo, nombre = recurso.split("[")[0].split(".")[:2]
    return [l + "\n" for l in (
        "huaweicloud_css_cluster.opensearch_cluster[0]: Creation complete after 6m5s [id=abc]",
        "",
        error,
        "",
        f"  with {recurso},",
        f'  on main.tf line 334, in resource "{tipo}" "{nombre}":',
    )]


_COMPLETO = [{"args": [], "desde": 5, "hasta": 90, "sin_activar": []}]


def _correr(gen):
    eventos = []
    try:
        while True:
            eventos.append(json.loads(next(gen)[len("data: "):]))
    except StopIteration as fin:
        return fin.value, eventos


def _fake_apply(vistos, rc=0, salida=()):
    def _fake(terraform_dir, args, desde, hasta, tf_lines, plan=True):
        vistos.append((args, plan))
        tf_lines.extend(salida)
        return rc
        yield  # noqa: unreachable — lo vuelve generador
    return _fake


# ── Cuándo es este rechazo ──────────────────────────────────────────────────
def test_el_rechazo_mientras_se_crea_el_opensearch_se_reconoce():
    assert main._logstash_rechazado_al_crear(main._errores_de_terraform(_salida()))


@pytest.mark.parametrize("salida", [
    # Otro motivo de CSS: repetir no lo arregla.
    _salida(_RECHAZO.replace('"errCode":"CSS.0001"', '"errCode":"CSS.5021"')),
    # El alta pasó y falló después: reintentar no es repetir un pedido rechazado.
    _salida('Error: error waiting for CSS logstash cluster (abc) to complete: "errCode":"CSS.0001"'),
    # Lo que falló es otra cosa.
    _salida('Error: error creating CSS cluster: "errCode":"CSS.0001"',
            "huaweicloud_css_cluster.opensearch_cluster[0]"),
    # El Logstash y además otra cosa: no es "lo único que falló".
    _salida() + _salida('Error: Error creating security group rule: already exists',
                        "huaweicloud_networking_secgroup_rule.kibana_public[0]"),
])
def test_otros_errores_no_son_este_rechazo(salida):
    assert not main._logstash_rechazado_al_crear(main._errores_de_terraform(salida))


# ── El reintento ────────────────────────────────────────────────────────────
def test_se_reintenta_una_vez_y_el_deploy_sigue_con_el_logstash_creado(monkeypatch, tmp_path):
    vistos = []
    exito = ["huaweicloud_css_logstash_cluster.logstash_cluster: Creation complete after 7m [id=x]\n",
             "Apply complete! Resources: 2 added, 0 changed, 0 destroyed.\n"]
    monkeypatch.setattr(main, "_correr_apply", _fake_apply(vistos, rc=0, salida=exito))
    tf_lines = _salida()

    rc, eventos = _correr(main._reintentar_el_logstash(tmp_path, _COMPLETO, 1, tf_lines))

    assert rc == 0
    # El apply completo de nuevo, sin reemplazar la lista de la pantalla.
    assert vistos == [([], False)]
    assert tf_lines == exito, "los errores que cuentan después son los del reintento"
    pasos = [e for e in eventos if e["type"] == "step"]
    assert [(p["name"], p["ok"]) for p in pasos] == [("Reintento del Logstash", True)]
    assert any(e["type"] == "progress" and "se reintenta" in e["message"] for e in eventos)


def test_si_el_reintento_falla_lo_dice_con_su_error(monkeypatch, tmp_path):
    otra = _salida(_RECHAZO.replace("rollback-only", "otra vez"))
    monkeypatch.setattr(main, "_correr_apply", _fake_apply([], rc=1, salida=otra))
    tf_lines = _salida()

    rc, eventos = _correr(main._reintentar_el_logstash(tmp_path, _COMPLETO, 1, tf_lines))

    assert rc == 1 and tf_lines == otra
    paso = [e for e in eventos if e["type"] == "step"][-1]
    assert not paso["ok"] and "otra vez" in paso["reason"]


def test_una_sola_vez_aunque_vuelva_a_rechazarlo(monkeypatch, tmp_path):
    vistos = []
    monkeypatch.setattr(main, "_correr_apply", _fake_apply(vistos, rc=1, salida=_salida()))
    _correr(main._reintentar_el_logstash(tmp_path, _COMPLETO, 1, _salida()))
    assert len(vistos) == 1


def test_si_se_corta_la_lectura_no_hay_paso(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_correr_apply", _fake_apply([], rc=None))
    rc, eventos = _correr(main._reintentar_el_logstash(tmp_path, _COMPLETO, 1, _salida()))
    assert rc is None and not [e for e in eventos if e["type"] == "step"]


_DOS_PASOS = [{"args": [], "desde": 5, "hasta": 47, "sin_activar": ["a"]},
              {"args": [main._TARGET_ACTIVACION], "desde": 47, "hasta": 90, "sin_activar": []}]


@pytest.mark.parametrize("pasos, rc, salida", [
    (_COMPLETO, 0, []),                  # salió bien
    (_COMPLETO, None, _salida()),        # se cortó la lectura: el error ya salió
    (_COMPLETO, 1, _salida('Error: error creating CSS logstash cluster: "errCode":"CSS.5021"')),
    # "Iniciar ingesta" en dos pasos, o solo la activación: repetirlo no es lo mismo.
    (_DOS_PASOS, 1, _salida()),
    (_DOS_PASOS[1:], 1, _salida()),
])
def test_cuando_no_corresponde_no_se_corre_nada(monkeypatch, tmp_path, pasos, rc, salida):
    vistos = []
    monkeypatch.setattr(main, "_correr_apply", _fake_apply(vistos))
    tf_lines = list(salida)
    assert _correr(main._reintentar_el_logstash(tmp_path, pasos, rc, tf_lines)) == (rc, [])
    assert vistos == [] and tf_lines == salida


def test_el_deploy_reintenta_antes_de_juzgar_el_apply():
    src = inspect.getsource(main._deploy_stream_gen_raw)
    aplica = src.index("yield from _aplicar_pasos(")
    reintenta = src.index("returncode = yield from _reintentar_el_logstash(terraform_dir, pasos, returncode, tf_lines)")
    assert aplica < reintenta < src.index("if returncode is None:") < src.index("apply_failed = returncode != 0")
