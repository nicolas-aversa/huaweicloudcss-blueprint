"""Qué tiene el cluster: los plugins y si PPL corre sobre Calcite.

La plataforma lee `_cat/plugins` y un setting (solo metadatos) y con eso decide
qué ofrecer: PPL 3 en el chat, Anomaly Detection, Alerting, Security Analytics.
"""
import json
import pathlib

import capabilities as caps
import main

_CAT = [{"component": c} for c in (
    "opensearch-ml", "opensearch-skills", "opensearch-sql", "opensearch-anomaly-detection",
    "opensearch-alerting", "opensearch-notifications-core", "opensearch-notifications",
    "opensearch-security-analytics", "opensearch-knn",
)] * 2   # un renglón por nodo: se repiten


def test_resume_los_plugins_que_la_plataforma_usa():
    r = caps.resumir_capacidades(_CAT, {"defaults": {"plugins": {"calcite": {"enabled": "true"}}}})
    for clave in ("ml", "skills", "ppl", "ad", "alerting", "notifications", "security_analytics", "ppl_v3"):
        assert r[clave] is True, clave
    assert r["plugins"] == sorted({p["component"] for p in _CAT}), "sin repetidos, ordenados"
    assert [f["clave"] for f in r["funciones"]] == ["ml", "ppl_v3", "ad", "alerting", "security_analytics"]
    assert all(f["ok"] for f in r["funciones"])


def test_la_palabra_va_entera():
    # "ml" es opensearch-ml, no un componente que tenga esas letras adentro.
    r = caps.resumir_capacidades([{"component": "opensearch-html-tools"}, {"component": "sqlite"}], {})
    assert r["ml"] is False and r["ppl"] is False
    assert r["funciones"][0] == {"clave": "ml", "nombre": "ML Commons", "ok": False}


def test_calcite_efectivo_transient_persistent_default_plano_o_anidado():
    sql = [{"component": "opensearch-sql"}]
    assert caps.resumir_capacidades(sql, {})["ppl_v3"] is False, "si no se informa, no se asume"
    assert caps.resumir_capacidades(sql, {})["calcite"] is None
    anidado = {"defaults": {"plugins": {"calcite": {"enabled": "true"}}}}
    assert caps.resumir_capacidades(sql, anidado)["ppl_v3"] is True
    # Lo persistente pisa al default, y lo transitorio a los dos.
    apagado = {"persistent": {"plugins.calcite.enabled": "false"}, **anidado}
    assert caps.resumir_capacidades(sql, apagado)["ppl_v3"] is False
    prendido = {"transient": {"plugins": {"calcite": {"enabled": True}}}, **apagado}
    assert caps.resumir_capacidades(sql, prendido)["ppl_v3"] is True
    # Sin el plugin SQL no hay PPL, aunque el setting diga que sí.
    assert caps.resumir_capacidades([], anidado)["ppl_v3"] is False


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


def test_detectar_lee_solo_metadatos_y_guarda(monkeypatch, tmp_path):
    pedidos = []

    def fake(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((method, url))
        if "_cat/plugins" in url:
            return _Resp(200, _CAT)
        return _Resp(200, {"defaults": {"plugins": {"calcite": {"enabled": "true"}}}})

    monkeypatch.setattr(main, "_os_req", fake)
    pasos = []
    monkeypatch.setattr(main.runs, "step", lambda run, name, ok, reason="": pasos.append((name, ok, reason)))
    feats = main._registrar_capacidades({"public_endpoint": "1.2.3.4:9200"}, "admin", "pw", False,
                                        tmp_path, run={"id": "r"})
    assert feats["ppl_v3"] is True
    # Solo GETs de metadatos: nada de _search ni de índices del cliente.
    assert all(m == "GET" for m, _ in pedidos) and len(pedidos) == 2
    assert all("_search" not in u and "_cat/indices" not in u for _, u in pedidos)
    assert "include_defaults=true" in pedidos[1][1] and "plugins.calcite.enabled" in pedidos[1][1]
    assert main._read_cluster_features(tmp_path) == feats
    assert pasos == [("Plugins del cluster", True, "")]


def test_si_el_cluster_no_contesta_no_se_guarda_nada(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: None)
    pasos = []
    monkeypatch.setattr(main.runs, "step", lambda run, name, ok, reason="": pasos.append((name, ok, reason)))
    assert main._registrar_capacidades({"public_endpoint": "x:9200"}, "admin", "pw", False,
                                       tmp_path, run={"id": "r"}) is None
    assert not (tmp_path / main._CLUSTER_FEATURES_NAME).exists()
    assert pasos == [("Plugins del cluster", False, "el cluster no respondió a _cat/plugins")]


def test_el_paso_dice_que_falta(monkeypatch, tmp_path):
    solo_ml = [{"component": "opensearch-ml"}]
    monkeypatch.setattr(main, "_os_req",
                        lambda m, url, *a, **k: _Resp(200, solo_ml if "_cat" in url else {}))
    pasos = []
    monkeypatch.setattr(main.runs, "step", lambda run, name, ok, reason="": pasos.append((name, ok, reason)))
    main._registrar_capacidades({"public_endpoint": "x:9200"}, "admin", "pw", False, tmp_path, run={"id": "r"})
    assert pasos == [("Plugins del cluster", True,
                      "sin PPL 3 (Calcite), Anomaly Detection, Alerting, Security Analytics")]


def test_se_detecta_al_aplicar_y_al_provisionar_y_se_expone():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def apply_schema(")
    assert "_registrar_capacidades(cluster, os_user, request.opensearch_password," in src[i:i + 5000]
    j = src.index("def provision_capabilities(")
    assert "_registrar_capacidades(cluster, user, password, request.https_enabled, terraform_dir, run)" in src[j:j + 3500]
    assert "cluster_features=_read_cluster_features(terraform_dir) or None," in src
    # Al destruir el entorno se va con él, y nunca va al repo.
    k = src.index("_remove_capabilities(terraform_dir)\n    for tmp in")
    assert "_CLUSTER_FEATURES_NAME" in src[k:k + 300]
    gi = (pathlib.Path(main.__file__).parent / ".gitignore").read_text(encoding="utf-8")
    assert "terraform/.cluster_features.json" in gi


def test_el_front_muestra_las_funciones():
    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "${funcionesDelClusterHTML(data.cluster_features)}" in html
    i = html.index("function funcionesDelClusterHTML(feats) {")
    fn = html[i:html.index("\n    }\n", i)]
    assert "if (!funciones.length) return '';" in fn
    assert 'cap-funcion is-falta" title="No está en este cluster"' in fn
    assert ".cap-funcion.is-falta { background: var(--bg-tertiary); color: var(--text-muted); }" in html
