"""Security Analytics para SIEM y FortiAnalyzer.

Había 8 reglas Sigma fijas (`category: network`) sobre un índice `siem-all` que
no existía, "correlaciones" de dos consultas idénticas, el paso de Actividad en
ok con solo tener el plugin, y nada para FortiAnalyzer ni en la vista. Ahora:
un tipo de log propio por fuente, reglas sobre los campos reales del caso, un
detector por tipo de log sobre el index pattern real, correlaciones entre
fuentes, y una tarjeta con los hallazgos.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

import main
import seguridad
import verticales_de_prueba as verticals

SPECS = verticals.security_specs()


@pytest.fixture(autouse=True)
def _specs_en_el_registro(monkeypatch, tmp_path):
    """Como las deja el deploy de un dataset con reglas: la spec de cada caso en
    el registro de pipelines del entorno (de ahí la leen todos los pasos)."""
    main._write_pipelines_registry(tmp_path, {slug: {"index": f"{slug}-%{{+YYYY_MM}}", "seguridad": spec}
                                              for slug, spec in SPECS.items()})
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)


# ── Los specs ───────────────────────────────────────────────────────────────
def test_los_casos_de_seguridad_tienen_spec():
    assert set(SPECS) == {"siem", "fortianalyzer"}
    assert [lt["nombre"] for lt in SPECS["siem"]["log_types"]] == [
        "siem_fortigate", "siem_auth", "siem_cloudaudit", "siem_waf"]
    assert [lt["nombre"] for lt in SPECS["fortianalyzer"]["log_types"]] == ["fortianalyzer"]


@pytest.mark.parametrize("slug", ["siem", "fortianalyzer"])
def test_cada_campo_de_una_regla_existe_en_el_caso(slug):
    """Lo de `siem-all` no vuelve: las reglas miran campos que el filter deja."""
    campos = {f["field_path"] for f in verticals.get_vertical(slug)["fields"]}
    assert seguridad.campos_de_reglas(SPECS[slug]) <= campos


def test_nombres_titulos_y_niveles_validos():
    titulos = [r["titulo"] for s in SPECS.values() for lt in s["log_types"] for r in lt["reglas"]]
    assert len(titulos) == len(set(titulos)), "el id Sigma sale del título: no se pueden repetir"
    for s in SPECS.values():
        for lt in s["log_types"]:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", lt["nombre"]), lt["nombre"]
            for r in lt["reglas"]:
                assert r["nivel"] in seguridad.NIVELES and r["seleccion"], r["titulo"]


@pytest.mark.parametrize("slug", ["siem", "fortianalyzer"])
def test_las_correlaciones_cruzan_fuentes_de_verdad(slug):
    tipos = {lt["nombre"] for lt in SPECS[slug]["log_types"]}
    for c in SPECS[slug].get("correlaciones", []):
        pares = c["correlate"]
        assert len(pares) >= 2
        assert {p["log_type"] for p in pares} <= tipos, c["nombre"]
        assert len({p["log_type"] for p in pares}) == len(pares), "fuentes distintas"
        assert len({p["query"] for p in pares}) == len(pares), "no dos consultas iguales"


def test_las_campanas_del_siem_son_las_del_dataset():
    """Como están en los datos: los eventos de cada campaña traen sus propias
    acciones (create_access_key, no createAccessKey), así que se correlaciona por
    campaña y fuente. CMP-002 solo tiene eventos de CloudAudit: no hay dos fuentes."""
    queries = " ".join(p["query"] for c in SPECS["siem"]["correlaciones"] for p in c["correlate"])
    for cmp_ in ("CMP-001", "CMP-003"):
        assert f"event.campaign:{cmp_}" in queries
    assert "CMP-002" not in queries
    assert "event.action" not in queries and "rule.name" not in queries
    for c in SPECS["siem"]["correlaciones"]:
        for par in c["correlate"]:
            fuente = par["log_type"].removeprefix("siem_")
            assert f"event.dataset:{fuente}" in par["query"], par


# ── Los builders ────────────────────────────────────────────────────────────
def test_la_regla_en_yaml_sigma():
    regla = {"titulo": 'Acceso "raro": SSH', "descripcion": "Línea: con dos puntos", "nivel": "high",
             "tags": ["attack.t1021"], "seleccion": {"action": ["blocked", "dropped"], "dstport": [22, 3389],
                                                     "subtype": "ips"}}
    y = seguridad.sigma_yaml(regla, "fortianalyzer")
    assert y.startswith('title: "Acceso \\"raro\\": SSH"\n')
    assert f"id: {seguridad.id_de_regla(regla['titulo'])}\n" in y
    assert 'description: "Línea: con dos puntos"' in y
    # Sin fecha, Security Analytics rechaza la regla con un 500 (visto en CSS 3.4).
    assert "\ndate: 2026/09/28\n" in y
    assert "tags:\n  - attack.t1021\n" in y
    assert "logsource:\n  product: fortianalyzer\n" in y
    assert '    action:\n      - "blocked"\n      - "dropped"\n' in y
    assert "    dstport:\n      - 22\n      - 3389\n" in y, "los números van sin comillas"
    assert '    subtype: "ips"\n' in y
    assert y.endswith("  condition: selection\nlevel: high\n")
    assert seguridad.id_de_regla("x") == seguridad.id_de_regla("x") != seguridad.id_de_regla("y")


def test_el_detector_va_al_index_pattern_real_con_un_trigger_por_severidad():
    d = seguridad.build_detector("siem", "siem_auth", ["siem-2025.07", "siem-2025.08"],
                                 [("R1", "high"), ("R2", "critical"), ("R3", "high")])
    assert d["name"] == "siem-siem-auth" and d["detector_type"] == "siem_auth" and d["enabled"] is True
    entrada = d["inputs"][0]["detector_input"]
    assert entrada["indices"] == ["siem-2025.07", "siem-2025.08"] and entrada["custom_rules"] == [{"id": "R1"}, {"id": "R2"}, {"id": "R3"}]
    assert [(t["sev_levels"], t["severity"]) for t in d["triggers"]] == [(["critical"], "1"), (["high"], "2")]
    assert all(t["ids"] == [] for t in d["triggers"]), "por nivel, no por id"
    assert "siem-all" not in json.dumps(d)


def test_la_correlacion():
    c = SPECS["siem"]["correlaciones"][0]
    b = seguridad.build_correlacion(c, "siem-*")
    assert b["name"] == c["nombre"] and b["time_window"] == c["ventana_min"] * 60_000
    assert b["correlate"] == [{"index": "siem-*", "query": p["query"], "category": p["log_type"]}
                              for p in c["correlate"]]


def test_el_alias_del_caso():
    assert seguridad.alias_del_caso("siem-*") == "siem-seguridad"
    assert seguridad.alias_del_caso("fortianalyzer-*") == "fortianalyzer-seguridad"


def test_el_template_de_un_caso_de_seguridad_lleva_el_alias(monkeypatch):
    """Así cada índice nuevo del caso (siem-2025.08, …) entra solo al alias."""
    enviados = {}

    class _R:
        status_code = 200
        text = "{}"

    def fake_put(url, json=None, **k):
        enviados[url.rsplit("/", 1)[1]] = json
        return _R()

    monkeypatch.setattr("requests.put", fake_put)
    caso = lambda slug, idx: main.PipelineCase(slug=slug, index_name=idx, fields=[{"field_path": "a", "type": "keyword"}])
    req = main.TerraformDeployRequest(project_name="p", opensearch_password="pw", pipeline_conf="x",
                                      cases=[caso("siem", "siem-%{+YYYY.MM}"), caso("transacciones-billetera", "tb-%{+YYYY.MM}")])
    assert main._apply_index_templates(req, {"public_endpoint": "x:9200"})
    assert enviados["p-siem"]["template"]["aliases"] == {"siem-seguridad": {}}
    assert "aliases" not in enviados["p-transacciones-billetera"]["template"]


MESES_SIEM = [f"siem-{a}_{m:02d}" for a, m in [(2025, x) for x in range(7, 13)] + [(2026, x) for x in range(1, 8)]]


def test_los_indices_mensuales_del_caso():
    assert seguridad.indices_mensuales("siem-*", ("2025-11", "2026-02")) == [
        "siem-2025_11", "siem-2025_12", "siem-2026_01", "siem-2026_02"]
    assert seguridad.indices_mensuales("fortianalyzer*", ("2026-07", "2026-07")) == ["fortianalyzer-2026_07"]
    # Sin punto: con punto Security Analytics rechaza el detector ("Index patterns
    # are not supported for doc level monitors"), aunque sea un solo índice.
    assert not [i for i in MESES_SIEM if "." in i]
    # Con el mismo nombre que les pone Logstash (`<caso>-%{+YYYY.MM}`).
    salida = seguridad.indice_de_salida("siem")
    assert salida == "siem-%{+YYYY_MM}"
    assert main._slug_from_index(salida) == "siem" and main.index_pattern_from_name(salida) == "siem-*"
    assert seguridad.indices_mensuales(main.index_pattern_from_name(salida), ("2025-07", "2026-07")) == MESES_SIEM


_MES_DEL_EVENTO = (
    (re.compile(r"date=(\d{4})-(\d{2})-"), None),           # FortiGate / FortiAnalyzer
    (re.compile(r"^<\d+>(\d{4})-(\d{2})-"), None),         # syslog (auth)
    (re.compile(r'"time":\s*(\d{13})'), "ms"),               # cloudaudit / waf
)


def _meses_del_archivo(path: pathlib.Path) -> set[str]:
    import datetime as dt
    meses = set()
    for linea in path.read_text(encoding="utf-8").splitlines():
        for rx, tipo in _MES_DEL_EVENTO:
            m = rx.search(linea)
            if m:
                if tipo == "ms":
                    t = dt.datetime.fromtimestamp(int(m.group(1)) / 1000, dt.timezone.utc)
                    meses.add(f"{t.year:04d}-{t.month:02d}")
                else:
                    meses.add(f"{m.group(1)}-{m.group(2)}")
                break
    return meses


# ── Provisionar ─────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status, data=None, text=None):
        self.status_code, self._data = status, data if data is not None else {}
        self.text = text if text is not None else json.dumps(self._data)

    def json(self):
        return self._data


class _Cluster:
    """Security Analytics de mentira: guarda lo que se crea y lo devuelve en
    los `_search`, como el de verdad."""

    def __init__(self, plugin=True, indices=(), fallar_regla=None, fallar_categoria=None):
        self.plugin, self.indices, self.fallar_regla = plugin, list(indices), fallar_regla
        self.fallar_categoria = fallar_categoria
        self.log_types, self.reglas, self.detectores, self.correlaciones = {}, {}, {}, {}
        self.entradas = {}
        self.creados, self.pedidos, self.n = [], [], 0

    def _id(self):
        self.n += 1
        return f"ID{self.n}"

    def req(self, method, url, user, password, json_body=None, timeout=30):
        self.pedidos.append((method, url))
        ruta = url.split(":9200", 1)[1]
        if ruta.startswith("/_plugins/_security_analytics/rules/_search"):
            if not self.plugin:
                return _Resp(400, text="no handler found")
            return _Resp(200, {"hits": {"hits": [{"_id": i, "_source": {"title": t}} for t, i in self.reglas.items()]}})
        if ruta.startswith("/_cat/indices/"):
            return _Resp(200, [{"index": i} for i in self.indices])
        if method == "HEAD":
            return _Resp(200 if ruta.strip("/") in self.indices else 404)
        if method == "DELETE" and ruta.startswith("/_plugins/_security_analytics/detectors/"):
            did = ruta.rsplit("/", 1)[1]
            self.detectores = {n: i for n, i in self.detectores.items() if i != did}
            self.creados.append(("borrado_detector", did))
            return _Resp(200)
        if method == "DELETE":
            self.indices.remove(ruta.strip("/"))
            self.creados.append(("borrado_indice", ruta.strip("/")))
            return _Resp(200)
        if ruta == "/_aliases":
            self.creados.append(("alias", json_body))
            return _Resp(200, {"acknowledged": True})
        if ruta == "/_cluster/settings":
            self.creados.append(("settings", json_body))
            return _Resp(200, {"acknowledged": True})
        if method == "PUT":
            if ruta.strip("/") in self.indices:
                return _Resp(400, text="resource_already_exists_exception")
            self.indices.append(ruta.strip("/"))
            self.creados.append(("indice", ruta.strip("/")))
            return _Resp(200, {"acknowledged": True})
        if ruta == "/_plugins/_security_analytics/logtype/_search":
            return _Resp(200, {"hits": {"hits": [{"_id": i, "_source": {"name": n}} for n, i in self.log_types.items()]}})
        if ruta == "/_plugins/_security_analytics/logtype":
            i = self._id(); self.log_types[json_body["name"]] = i; self.creados.append(("log_type", json_body))
            return _Resp(201, {"_id": i})
        if ruta == "/_plugins/_security_analytics/detectors/_search":
            return _Resp(200, {"hits": {"hits": [{"_id": i, "_source": {"detector": {"name": n, "inputs": self.entradas.get(i, [])}}}
                                                 for n, i in self.detectores.items()]}})
        if ruta == "/_plugins/_security_analytics/detectors":
            i = self._id(); self.detectores[json_body["name"]] = i; self.creados.append(("detector", json_body))
            self.entradas[i] = json_body["inputs"]
            return _Resp(201, {"_id": i})
        if ruta == "/_plugins/_security_analytics/correlation/rules/_search":
            return _Resp(200, {"hits": {"hits": [{"_id": i, "_source": {"name": n}} for n, i in self.correlaciones.items()]}})
        if ruta == "/_plugins/_security_analytics/correlation/rules":
            i = self._id(); self.correlaciones[json_body["name"]] = i; self.creados.append(("correlacion", json_body))
            return _Resp(201, {"_id": i})
        return _Resp(404, text="?")

    def post_texto(self, url, user, password, texto):
        categoria = url.split("category=", 1)[1]
        titulo = json.loads(texto.split("\n", 1)[0].split("title: ", 1)[1])
        self.creados.append(("regla", categoria, titulo))
        if (self.fallar_regla and self.fallar_regla == titulo) or categoria == self.fallar_categoria:
            return _Resp(400, text="field [x] is not mapped")
        i = self._id()
        self.reglas[titulo] = i
        return _Resp(201, {"_id": i})


def _provisionar(monkeypatch, tmp_path, cluster, slug="siem", pattern="siem-*"):
    monkeypatch.setattr(main, "_os_req", cluster.req)
    monkeypatch.setattr(main, "_sa_post_texto", cluster.post_texto)
    pasos = []
    monkeypatch.setattr(main.runs, "step", lambda run, name, ok, reason="": pasos.append((name, ok, reason)))
    res = main._provision_security_analytics({"public_endpoint": "x:9200"}, "admin", "pw", False,
                                             slug, pattern, SPECS[slug], tmp_path, run={"id": "r"})
    return res, pasos


def test_sin_el_plugin_se_dice(monkeypatch, tmp_path):
    res, pasos = _provisionar(monkeypatch, tmp_path, _Cluster(plugin=False))
    assert res == {"available": False}
    assert pasos == [("Security Analytics · siem · plugin", False, "Security Analytics no está en este cluster")]
    assert not (tmp_path / main._SECURITY_REGISTRY_NAME).exists()


def test_crea_todo_para_el_siem(monkeypatch, tmp_path):
    c = _Cluster()
    res, pasos = _provisionar(monkeypatch, tmp_path, c)
    # Primero los índices de cada mes, después tipos, reglas, detectores y correlaciones.
    tipos = [x[0] for x in c.creados]
    assert [x[1] for x in c.creados[:13]] == MESES_SIEM and set(tipos[:13]) == {"indice"}
    assert tipos.index("log_type") < tipos.index("regla") < tipos.index("detector") < tipos.index("correlacion")
    assert [x[1]["name"] for x in c.creados if x[0] == "log_type"] == [
        "siem_fortigate", "siem_auth", "siem_cloudaudit", "siem_waf"]
    assert all(x[1]["source"] == "Custom" for x in c.creados if x[0] == "log_type")
    # Cada regla, en la categoría de su tipo de log.
    reglas = [x for x in c.creados if x[0] == "regla"]
    assert len(reglas) == 7 and ("regla", "siem_auth", "SSH: fuerza bruta sobre root") in reglas
    # Un detector por tipo, sobre el pattern real, con las reglas de ese tipo.
    dets = {x[1]["name"]: x[1] for x in c.creados if x[0] == "detector"}
    assert set(dets) == {"siem-siem-fortigate", "siem-siem-auth", "siem-siem-cloudaudit", "siem-siem-waf"}
    auth = dets["siem-siem-auth"]["inputs"][0]["detector_input"]
    # A los índices mensuales POR NOMBRE: no acepta un pattern ("Index patterns
    # are not supported for doc level monitors", CSS 3.4) y con el alias el
    # monitor no guarda hasta dónde leyó.
    assert auth["indices"] == MESES_SIEM
    alias = next(x[1] for x in c.creados if x[0] == "alias")
    assert alias == {"actions": [{"add": {"index": "siem-*", "alias": "siem-seguridad"}}]}
    assert tipos.index("alias") < tipos.index("detector")
    assert {r["id"] for r in auth["custom_rules"]} == {c.reglas["SSH: fuerza bruta sobre root"], c.reglas["Acceso a /etc/shadow con sudo"]}
    corr = [x[1] for x in c.creados if x[0] == "correlacion"]
    assert len(corr) == 2 and all(p["index"] == "siem-seguridad" for x in corr for p in x["correlate"])
    # En Actividad, cada pieza con su resultado.
    nombres = [p[0] for p in pasos]
    assert "Security Analytics · siem · tipos de log" in nombres
    assert ("Security Analytics · siem · 7 de 7 reglas", True, "") in pasos
    assert ("Security Analytics · siem · detector siem_waf", True, "") in pasos
    assert ("Security Analytics · siem · alias siem-seguridad", True, "") in pasos
    assert ("Security Analytics · siem · 2 de 2 correlaciones", True, "") in pasos
    assert ("Security Analytics · siem · 13 índices mensuales", True, "creados antes que los detectores") in pasos
    assert all(ok for _, ok, _ in pasos)
    # Y queda registrado (lo lee la vista).
    reg = json.loads((tmp_path / main._SECURITY_REGISTRY_NAME).read_text(encoding="utf-8"))["siem"]
    assert set(reg["detectores"]) == set(dets) and reg["detectores"]["siem-siem-auth"]["log_type"] == "siem_auth"
    assert len(reg["reglas"]) == 7 and len(reg["correlaciones"]) == 2
    assert res["available"] is True


def test_la_segunda_vez_no_duplica_nada(monkeypatch, tmp_path):
    c = _Cluster()
    _provisionar(monkeypatch, tmp_path, c)
    antes = len([x for x in c.creados if x[0] not in ("alias", "settings")])
    (tmp_path / main._SECURITY_REGISTRY_NAME).unlink()   # aunque se pierda el registro
    _, pasos = _provisionar(monkeypatch, tmp_path, c)
    # El alias se vuelve a sumar (en OpenSearch agregarlo de nuevo no cambia nada);
    # lo demás no se duplica.
    assert len([x for x in c.creados if x[0] not in ("alias", "settings")]) == antes, "todo se encuentra por nombre o título"
    assert ("Security Analytics · siem · detector siem_auth", True, "ya estaba") in pasos
    assert len(json.loads((tmp_path / main._SECURITY_REGISTRY_NAME).read_text(encoding="utf-8"))["siem"]["correlaciones"]) == 2


def test_si_el_indice_es_nuevo_los_detectores_se_recrean(monkeypatch, tmp_path):
    """La ingesta borra los índices para arrancar limpia: un detector que ya
    estaba no sigue a los que se crean después (solo vería sus primeros minutos)."""
    c = _Cluster()
    _provisionar(monkeypatch, tmp_path, c)
    viejos = dict(c.detectores)
    c.indices.remove("siem-2026_01")
    c.creados.clear()
    _, pasos = _provisionar(monkeypatch, tmp_path, c)
    tipos = [x[0] for x in c.creados]
    assert c.creados[0] == ("indice", "siem-2026_01")
    assert sorted(x[1] for x in c.creados if x[0] == "borrado_detector") == sorted(viejos.values())
    assert tipos.index("indice") < tipos.index("borrado_detector") < tipos.index("detector")
    assert set(c.detectores) == set(viejos) and not set(c.detectores.values()) & set(viejos.values())
    assert ("Security Analytics · siem · detector siem_auth", True, "recreado con sus índices y reglas actuales") in pasos
    assert ("Security Analytics · siem · 1 índices mensuales", True, "creados antes que los detectores") in pasos
    reg = json.loads((tmp_path / main._SECURITY_REGISTRY_NAME).read_text(encoding="utf-8"))["siem"]
    assert reg["detectores"]["siem-siem-auth"]["id"] == c.detectores["siem-siem-auth"]
    assert not [x for x in c.creados if x[0] == "log_type"], "tipos y reglas no se tocan"


def test_sin_meses_declarados_se_dice(monkeypatch, tmp_path):
    spec = {k: v for k, v in SPECS["siem"].items() if k != "meses"}
    monkeypatch.setitem(SPECS, "siem", spec)
    c = _Cluster()
    _, pasos = _provisionar(monkeypatch, tmp_path, c)
    assert ("Security Analytics · siem · índices", False,
            "el caso no declara los meses de su dataset (security.meses)") in pasos
    assert not [x for x in c.creados if x[0] == "indice"]


def test_una_regla_que_falla_se_dice_con_el_motivo(monkeypatch, tmp_path):
    c = _Cluster(fallar_regla="WAF: webshell desde una IP maliciosa")
    _, pasos = _provisionar(monkeypatch, tmp_path, c)
    paso = next(p for p in pasos if "reglas" in p[0])
    assert paso[0] == "Security Analytics · siem · 6 de 7 reglas" and paso[1] is False
    assert "WAF: webshell desde una IP maliciosa: status 400: field [x] is not mapped" in paso[2]
    # El detector del WAF sale igual, con la regla que sí se creó.
    waf = next(x[1] for x in c.creados if x[0] == "detector" and x[1]["name"] == "siem-siem-waf")
    assert len(waf["inputs"][0]["detector_input"]["custom_rules"]) == 1


def test_sin_ninguna_regla_no_hay_detector_vacio(monkeypatch, tmp_path):
    c = _Cluster(fallar_categoria="siem_waf")
    _, pasos = _provisionar(monkeypatch, tmp_path, c)
    assert "siem-siem-waf" not in [x[1]["name"] for x in c.creados if x[0] == "detector"]
    assert ("Security Analytics · siem · detector siem_waf", False, "sin reglas creadas") in pasos
    assert "siem-siem-auth" in [x[1]["name"] for x in c.creados if x[0] == "detector"], "los demás salen"


def test_fortianalyzer_sin_correlaciones(monkeypatch, tmp_path):
    c = _Cluster()
    _, pasos = _provisionar(monkeypatch, tmp_path, c, slug="fortianalyzer", pattern="fortianalyzer-*")
    assert [x[1]["name"] for x in c.creados if x[0] == "detector"] == ["fortianalyzer-fortianalyzer"]
    assert not [x for x in c.creados if x[0] == "correlacion"]
    assert not [p for p in pasos if "correlaciones" in p[0]]
    assert c.creados[0] == ("indice", "fortianalyzer-2025_07")


def test_se_provisiona_al_aplicar_para_cada_caso_de_seguridad():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def apply_schema(")
    cuerpo = src[i:src.index("\n    msg = ", i)]
    assert "specs_seguridad = _specs_de_seguridad(terraform_dir)" in cuerpo
    assert "index_pattern_from_name(index_name_t), spec_sa," in cuerpo
    # Una sola llamada: el deploy ya no lo corre (lo hacía antes de que existiera el template).
    # La definición, la de apply-schema y la de la ingesta (índice + detectores antes de Logstash).
    assert src.count("_provision_security_analytics(") == 3
    for rastro in ("_SA_SIGMA_RULES", "_SA_CORRELATIONS", "siem-unified-detector", "_PCT_SECURITY"):
        assert rastro not in src, rastro


def test_el_registro_se_expone_y_se_va_con_el_entorno(tmp_path):
    main._write_security(tmp_path, {"siem": {"detectores": {"siem-siem-auth": {"id": "D", "log_type": "siem_auth"}},
                                             "reglas": {"a": "1", "b": "2"}, "correlaciones": {"c": "3"}},
                                    "vacio": {"detectores": {}}})
    assert main._resumen_de_seguridad(tmp_path) == {"siem": {
        "detectores": [{"nombre": "siem-siem-auth", "log_type": "siem_auth",
                        "descripcion": "Hosts Linux: SSH y sudo (SIEM)"}],
        "reglas": 2, "correlaciones": 1}}
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert "security_analytics=_resumen_de_seguridad(terraform_dir)," in src
    k = src.index("_remove_capabilities(terraform_dir)\n    for tmp in")
    assert "_SECURITY_REGISTRY_NAME" in src[k:k + 300]
    gi = (pathlib.Path(main.__file__).parent / ".gitignore").read_text(encoding="utf-8")
    assert "terraform/.security_analytics.json" in gi


# ── Los hallazgos ───────────────────────────────────────────────────────────
def test_un_hallazgo_para_la_vista():
    regla_por_id = {"R1": {"titulo": "SSH: fuerza bruta sobre root", "nivel": "high"}}
    f = {"index": "siem-2025.10", "timestamp": 111, "queries": [{"id": "R1", "name": "x"}],
         "document_list": [{"document": json.dumps({"@timestamp": "2025-10-21T03:14:07Z",
                                                    "source": {"ip": "1.2.3.4"}})}]}
    assert main._hallazgo(f, regla_por_id) == {"regla": "SSH: fuerza bruta sobre root", "nivel": "high",
                                               "hora": "2025-10-21T03:14:07Z",
                                               "hora_ppl": "2025-10-21 03:14:07", "ip": "1.2.3.4",
                                               "indice": "siem-2025.10"}
    # FortiAnalyzer: la IP es `srcip`; sin documento, la hora del hallazgo.
    forti = {"queries": [{"id": "?", "name": "regla-x"}], "timestamp": 222,
             "document_list": [{"document": {"srcip": "5.6.7.8"}}]}
    h = main._hallazgo(forti, {})
    assert h["ip"] == "5.6.7.8" and h["regla"] == "regla-x" and h["hora"] == 222 and h["nivel"] == ""
    assert h["hora_ppl"] == "1970-01-01 00:00:00"


@pytest.mark.parametrize("valor, esperado", [
    ("2026-02-28T12:26:30.000Z", "2026-02-28 12:26:30"),       # así llega @timestamp: UTC
    ("2026-02-28T09:26:30-03:00", "2026-02-28 12:26:30"),      # con zona: se pasa a UTC
    ("2026-02-28T12:26:30", "2026-02-28 12:26:30"),            # sin zona: se toma UTC
    (1741918200000, "2025-03-14 02:10:00"),
    ("1741918200000", "2025-03-14 02:10:00"),
    ("basura", ""),
])
def test_la_hora_para_el_asistente_es_utc_y_con_anio(valor, esperado):
    """La vista mostraba "28/2, 09:26" (hora local, sin año) y eso iba al
    asistente: filtró 2025-02-28 09:26 cuando el evento era 2026-02-28 12:26 UTC."""
    assert main._hora_utc_ppl(valor) == esperado


def test_el_resumen_en_vivo(monkeypatch, tmp_path):
    main._write_security(tmp_path, {"siem": {"detectores": {"siem-siem-auth": {"id": "D1", "log_type": "siem_auth"}},
                                             "reglas": {"SSH: fuerza bruta sobre root": "R1"}, "correlaciones": {"c": "C"}}})
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "x:9200"})
    monkeypatch.setattr(main, "_cluster_admin_password", lambda td: "pw")
    monkeypatch.setattr(main, "_read_https_enabled_from_state", lambda td: False)
    pedidos = []

    def fake(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((method, url))
        if "/findings/_search" in url:
            return _Resp(200, {"total_findings": 1234, "findings": [
                {"queries": [{"id": "R1"}], "document_list": [{"document": json.dumps({"source": {"ip": "9.9.9.9"}})}]}] * 7})
        return _Resp(200, {"alerts": [{"severity": "1"}, {"severity": "2"}, {"severity": "2"},
                                      {"severity": "", "state": "ERROR",
                                       "error_message": "IndexNotFoundException[no such index [siem-seguridad]]"}]})

    monkeypatch.setattr(main, "_os_req", fake)
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/api/v1/security/resumen")
    assert r.status_code == 200
    det = r.json()["casos"][0]["detectores"][0]
    assert det["total"] == 1234 and len(det["recientes"]) == 5 and det["recientes"][0]["ip"] == "9.9.9.9"
    assert det["recientes"][0]["regla"] == "SSH: fuerza bruta sobre root" and det["recientes"][0]["nivel"] == "high"
    assert det["alertas"] == {"critical": 1, "high": 2} and det["error"] == ""
    # Una alerta en ERROR es el detector que no pudo correr: va aparte.
    assert det["fallas"] == ["IndexNotFoundException[no such index [siem-seguridad]]"]
    assert det["descripcion"] == "Hosts Linux: SSH y sudo (SIEM)"
    assert r.json()["casos"][0]["correlaciones"] == 1
    # Solo lectura: GETs y el conteo de eventos (POST a _count).
    assert all(m == "GET" or u.endswith("/_count") for m, u in pedidos) and "detector_id=D1" in pedidos[0][1]


def test_sin_registro_no_se_toca_el_cluster(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: pytest.fail("no debería llamar al cluster"))
    from fastapi.testclient import TestClient
    assert TestClient(main.app).get("/api/v1/security/resumen").json() == {"casos": []}


# ── La vista ────────────────────────────────────────────────────────────────
_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"


def _funciones_de_seguridad(html: str) -> str:
    i = html.index("    function seguridadHTML(sa) {")
    return html[i:html.index("    async function verHallazgos(btn) {", i)]


_ARNES = r"""
const icon = (n) => `<i:${n}>`;
const escapeHtml = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = { siem: 'SIEM' };
let capChatPreguntar = null;
const toasts = [];
const toast = (m) => toasts.push(m);
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
check('sin registro, nada', seguridadHTML({}) === '' && seguridadHTML(null) === '');
const card = seguridadHTML({ siem: { detectores: [{ log_type: 'siem_auth', descripcion: 'Hosts <Linux>' }, { log_type: 'siem_waf', descripcion: '' }], reglas: 8, correlaciones: 3 } });
check('resumen', card.includes('2 detectores · 8 reglas Sigma · 3 correlaciones'), card);
check('caso y fuentes', card.includes('<strong>SIEM</strong>') && card.includes('Hosts &lt;Linux&gt; · siem_waf'), card);
check('botón', card.includes('id="infra-seguridad-ver"') && card.includes('id="infra-seguridad-detalle"'));
const uno = seguridadHTML({ x: { detectores: [{ log_type: 'a' }], reglas: 1, correlaciones: 1 } });
check('singular', uno.includes('1 detector · 1 reglas Sigma · 1 correlación'), uno);

const h = hallazgosHTML([{ slug: 'siem', correlaciones: 3, detectores: [
  { descripcion: 'FortiGate', total: 1842, alertas: { critical: 1, high: 30, low: 0 }, error: '', recientes: [
    { regla: 'IPS "raro"', nivel: 'critical', hora: '2025-10-21T03:14:07.000Z', hora_ppl: '2026-02-28 12:26:30', ip: '1.2.3.4' }] },
  { descripcion: 'Auth', total: 0, alertas: {}, error: '', recientes: [] },
  { descripcion: 'WAF', total: 0, alertas: {}, error: 'status 500: boom', recientes: [] },
] }]);
check('total con miles', h.includes('>1.842 eventos detectados<'), h);
const inflado = hallazgosHTML([{ slug: 'siem', detectores: [{ descripcion: 'F', total: 26707, hallazgos_sa: 251463, alertas: {}, error: '', recientes: [] }] }]);
check('lo que registró SA, aparte', inflado.includes('title="Security Analytics registró 251.463: con el cluster saturado cuenta el mismo evento varias veces">26.707 eventos detectados<'), inflado);
check('si coincide, sin aclaración', !h.includes('Security Analytics registró'), h);
check('alertas por severidad', h.includes('sev--critical">1 alerta crítica<') && h.includes('sev--high">30 alertas altas<'), h);
check('sin alertas en cero', !h.includes('sev--low'));
check('el hallazgo', h.includes('IPS &quot;raro&quot;') && h.includes('1.2.3.4') && h.includes('>Crítica<'), h);
check('explicar con sus datos', h.includes('<button type="button" class="hallazgo hallazgo__explicar" data-slug="siem" data-regla="IPS &quot;raro&quot;"'), h);
check('la fila entera explica, sin un botón por renglón', !h.includes('btn btn-secondary btn-sm hallazgo__explicar') && h.includes('<span class="hallazgo__accion"><i:spark> Explicar</span></button>'), h);
check('sin hallazgos todavía', h.includes('Sin hallazgos todavía') && h.includes('Reiniciar ingesta'));
check('error del detector', h.includes('No se pudieron leer: status 500: boom'));
check('correlaciones', h.includes('SIEM · 3 correlaciones entre fuentes'));
check('nada', hallazgosHTML([]).includes('No hay detectores'));

// Explicar: le pregunta al asistente con la regla, la hora y la IP.
const b = { dataset: { slug: 'siem', regla: 'SSH', hora: '2026-02-28 12:26:30', ip: '1.2.3.4' } };
check('sin asistente, avisa', explicarHallazgo(b) === false && toasts.length === 1 && toasts[0].includes('Provisionar plugins'));
let pedido = null;
capChatPreguntar = (slug, pregunta, contexto, explicar) => { pedido = { slug, pregunta, contexto, explicar }; return true; };
check('con asistente', explicarHallazgo(b) === true && toasts.length === 1);
check('la pregunta', pedido.slug === 'siem' && pedido.pregunta === '¿Por qué se disparó "SSH"? ¿Qué más pasó alrededor?', pedido.pregunta);
check('el contexto', pedido.contexto === 'Hallazgo de Security Analytics: regla "SSH", evento del 2026-02-28 12:26:30 (UTC, como @timestamp), IP de origen 1.2.3.4. Mirá qué más hizo esa IP en todo el período y en las horas cercanas (no solo ese minuto).', pedido.contexto);
check('la ventana: la hora del evento', JSON.stringify(pedido.explicar) === JSON.stringify({ desde: '2026-02-28 12:26:30' }), JSON.stringify(pedido.explicar));
check('explicar lleva la hora UTC', h.includes('data-hora="2026-02-28 12:26:30"'), h);
const conFalla = hallazgosHTML([{ slug: 'siem', detectores: [{ descripcion: 'Auth', total: 0, alertas: {}, fallas: ['no such index <x>'], error: '', recientes: [] }] }]);
check('la falla del detector, aparte', conFalla.includes('el detector falló 1 vez') && conFalla.includes('title="no such index &lt;x&gt;"'), conFalla);
capChatPreguntar = () => false;
check('asistente ocupado, avisa', explicarHallazgo(b) === false && toasts.length === 2);
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_tarjeta_y_los_hallazgos_en_node(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    js = tmp_path / "seg.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", _funciones_de_seguridad(html)), encoding="utf-8")
    res = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, "checks fallidos:\n" + (res.stdout or res.stderr)


def test_la_vista_la_pinta_y_el_asistente_se_deja_preguntar():
    html = _INDEX.read_text(encoding="utf-8")
    assert "{ id: 'seguridad', label: 'Security Analytics', icon: 'shield', cuenta: nDetectores, html: seguridadHTML(data.security_analytics) }," in html
    assert "body.querySelector('#infra-seguridad-ver')?.addEventListener('click', (e) => verHallazgos(e.currentTarget));" in html
    assert "const b = e.target.closest('.hallazgo__explicar');\n        if (b) explicarHallazgo(b);" in html
    i = html.index("      capChatPreguntar = (slug, pregunta, contexto, explicar = null) => {")
    fn = html[i:html.index("\n      };\n", i)]
    assert "if (!activeSlugs.includes(slug) || capChatBusy) return false;" in fn
    assert fn.index("_mostrarCaso(slug);") < fn.index("_abrir(true);") \
        < fn.index("sendCapChat(pregunta, { contexto, investigar: true, explicar });")
    j = html.index("function _quitarAsistente() {")
    assert "capChatPreguntar = null;" in html[j:html.index("\n    }\n", j)]
    k = html.index("async function verHallazgos(btn) {")
    ver = html[k:html.index("\n    }\n", k)]
    assert "fetch('/api/v1/security/resumen')" in ver and "destino.innerHTML = hallazgosHTML(data.casos || []);" in ver


def test_que_se_va_a_crear_lo_dice():
    """El resumen del paso 4 seguía diciendo "agente + forecasts"."""
    html = _INDEX.read_text(encoding="utf-8")
    assert "SECURITY_SLUGS = new Set(_VVIS.filter(v => v.hasSecurity).map(v => v.slug));" in html
    assert "Plugins de OpenSearch: agente conversacional (NL→PPL), forecasts, detección de anomalías y alertas" in html
    assert "const conSeguridad = types.filter(t => SECURITY_SLUGS.has(t))" in html
    assert "li('shield', `Security Analytics para ${escapeHtml(conSeguridad.join(' y '))}" in html


# ── Iniciar ingesta: índices y detectores antes de Logstash ─────────────────
def _preparar(monkeypatch, tmp_path, cluster, casos):
    monkeypatch.setattr(main, "_os_req", cluster.req)
    monkeypatch.setattr(main, "_sa_post_texto", cluster.post_texto)
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "x:9200"})
    req = main.TerraformDeployRequest(project_name="p", opensearch_password="pw", pipeline_conf="x",
                                      cases=[main.PipelineCase(slug=s, index_name=i) for s, i in casos])
    return [json.loads(e[len("data: "):]) for e in main._preparar_seguridad_para_ingesta(req, tmp_path)]


def test_la_ingesta_crea_los_indices_y_recrea_los_detectores(monkeypatch, tmp_path):
    """"Iniciar ingesta" borra los índices del caso para arrancar limpia: antes
    de que Logstash escriba, vuelven a existir y los detectores son posteriores."""
    c = _Cluster()
    c.detectores = {"siem-siem-auth": "VIEJO"}
    eventos = _preparar(monkeypatch, tmp_path, c, [("siem", "siem-%{+YYYY.MM}"), ("transacciones-billetera", "tb-%{+YYYY.MM}")])
    assert sorted(c.indices) == MESES_SIEM
    assert ("borrado_detector", "VIEJO") in c.creados and "VIEJO" not in c.detectores.values()
    tipos = [x[0] for x in c.creados]
    auth = next(n for n, x in enumerate(c.creados) if x[0] == "detector" and x[1]["name"] == "siem-siem-auth")
    assert tipos.index("indice") < tipos.index("borrado_detector") < auth
    assert eventos == [{"type": "step", "name": "Security Analytics · siem", "ok": True,
                        "reason": "13 índices mensuales y 4 detectores listos antes de la ingesta"}]
    assert not [u for _, u in c.pedidos if "/tb" in u], "un caso sin seguridad no se toca"


def test_con_los_indices_la_ingesta_no_toca_nada(monkeypatch, tmp_path):
    c = _Cluster(indices=MESES_SIEM)
    assert _preparar(monkeypatch, tmp_path, c, [("siem", "siem-%{+YYYY.MM}")]) == []
    assert [(m, u.split(":9200")[1]) for m, u in c.pedidos] == [("GET", "/_cat/indices/siem-*?format=json&h=index")]
    assert not c.creados


def test_si_falta_un_mes_la_ingesta_lo_crea(monkeypatch, tmp_path):
    c = _Cluster(indices=[i for i in MESES_SIEM if i != "siem-2025_09"])
    eventos = _preparar(monkeypatch, tmp_path, c, [("siem", "siem-%{+YYYY.MM}")])
    assert [x[1] for x in c.creados if x[0] == "indice"] == ["siem-2025_09"]
    assert eventos[0]["ok"] is True


def test_sin_casos_de_seguridad_la_ingesta_no_consulta(monkeypatch, tmp_path):
    c = _Cluster()
    assert _preparar(monkeypatch, tmp_path, c, [("transacciones-billetera", "tb-%{+YYYY.MM}")]) == []
    assert c.pedidos == []


def test_en_la_ingesta_va_despues_de_limpiar_y_antes_de_terraform():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def _deploy_stream_gen_raw(")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    limpiar = cuerpo.index("_clear_case_indices(request")
    preparar = cuerpo.index("yield from _preparar_seguridad_para_ingesta(request, terraform_dir)")
    assert limpiar < preparar < cuerpo.index('["terraform", "init"')
    # Dentro de la fase 2: con el deploy (fase 1) no hay Logstash que arrancar.
    fase2 = cuerpo.index("if request.start_ingestion:\n        if request.clear_indices:")
    assert fase2 < preparar < cuerpo.index("    else:\n        yield _sse({\"type\": \"progress\", \"percent\": 2")


def test_los_casos_de_seguridad_nombran_el_mes_con_guion_bajo(monkeypatch):
    import custom_cases
    casos = [{"slug": "fw", "label": "FW", "fields": [{"field_path": "a"}], "seguridad": {"reglas": [1]}},
             {"slug": "ventas", "label": "V", "fields": [{"field_path": "a"}]}]
    monkeypatch.setattr(custom_cases, "list_cases", lambda: casos)
    payload = {v["slug"]: v for v in custom_cases.front_entries()}
    assert payload["fw"]["outputIndex"] == "fw-%{+YYYY_MM}" and payload["ventas"]["outputIndex"] == ""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("function caseMeta(id)")
    assert "(meta.outputIndex || `${meta.indexBase}-%{+YYYY.MM}`)" in html[i:html.index("\n    }\n", i)]


def test_el_backend_fuerza_el_mes_con_guion_bajo():
    """El body de "Reiniciar ingesta" se rearma del registro del deploy, que
    puede traer `siem-%{+YYYY.MM}` de antes."""
    req = main.TerraformDeployRequest(project_name="p", opensearch_password="pw", pipeline_conf="x", cases=[
        main.PipelineCase(slug="siem", index_name="siem-%{+YYYY.MM}", seguridad={"reglas": [{"titulo": "x"}]}),
        main.PipelineCase(slug="transacciones-billetera", index_name="tb-%{+YYYY.MM}")])
    main._casos_de_seguridad_al_indice_mensual(req)
    assert [c.index_name for c in req.cases] == ["siem-%{+YYYY_MM}", "tb-%{+YYYY.MM}"]


def test_el_deploy_y_el_schema_normalizan_al_entrar():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    for funcion, siguiente in (("def _deploy_stream_gen(", "for raw in _deploy_stream_gen_raw("),
                               ("def apply_schema(", "terraform_dir = _active_terraform_dir()")):
        i = src.index(funcion)
        assert "_casos_de_seguridad_al_indice_mensual(request)" in src[i:src.index(siguiente, i)], funcion


# ── Después de la ingesta: ¿algún mes quedó sin detección? ──────────────────
def _revisar(monkeypatch, tmp_path, cluster, slugs=("siem", "transacciones-billetera"), registrar=True):
    monkeypatch.setattr(main, "_os_req", cluster.req)
    pasos = []
    monkeypatch.setattr(main.runs, "step", lambda run, name, ok, reason="": pasos.append((name, ok, reason)))
    if registrar:
        main._write_security(tmp_path, {"siem": {"detectores": {"siem-siem-auth": {"id": "D"}}}})
    main._revisar_meses_de_seguridad({"public_endpoint": "x:9200"}, "admin", "pw", False,
                                     list(slugs), tmp_path, {"id": "r"})
    return pasos


def test_si_todos_los_meses_estaban_se_dice_en_verde(monkeypatch, tmp_path):
    pasos = _revisar(monkeypatch, tmp_path, _Cluster(indices=MESES_SIEM))
    assert pasos == [("Security Analytics · siem · meses", True,
                      "todos los meses del dataset tenían su índice antes que los detectores")]


def test_un_mes_que_creo_logstash_se_avisa(monkeypatch, tmp_path):
    """Un evento fuera de `security.meses`: Logstash creó el índice durante la
    ingesta, después de los detectores, y sus eventos no se evaluaron."""
    c = _Cluster(indices=MESES_SIEM + ["siem-2026_08", ".kibana_1"])
    pasos = _revisar(monkeypatch, tmp_path, c)
    assert len(pasos) == 1 and pasos[0][:2] == ("Security Analytics · siem · meses", False)
    assert pasos[0][2].startswith("siem-2026_08: lo creó Logstash durante la ingesta")
    assert "security.meses" in pasos[0][2] and ".kibana" not in pasos[0][2]


def test_si_no_se_puede_listar_se_dice(monkeypatch, tmp_path):
    class _SinCat(_Cluster):
        def req(self, method, url, *a, **k):
            return _Resp(500, text="boom") if "/_cat/indices/" in url else super().req(method, url, *a, **k)
    pasos = _revisar(monkeypatch, tmp_path, _SinCat())
    assert pasos == [("Security Analytics · siem · meses", False,
                      "no se pudieron listar los índices del caso para revisar la cobertura")]


def test_sin_security_analytics_provisionado_no_se_revisa(monkeypatch, tmp_path):
    c = _Cluster(indices=MESES_SIEM + ["siem-2026_08"])
    assert _revisar(monkeypatch, tmp_path, c, registrar=False) == []
    assert c.pedidos == []


def test_la_revision_usa_el_indice_del_deploy(monkeypatch, tmp_path):
    main._write_pipelines_registry(tmp_path, {"siem": {"index": "siem-%{+YYYY.MM}", "seguridad": SPECS["siem"]}})
    c = _Cluster(indices=MESES_SIEM)
    _revisar(monkeypatch, tmp_path, c)
    assert c.pedidos == [("GET", "http://x:9200/_cat/indices/siem-*?format=json&h=index")]


def test_la_revision_corre_al_provisionar_plugins():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    cuerpo = src[i:src.index("\n    msg = ", i)]
    assert "_revisar_meses_de_seguridad(cluster, user, password, request.https_enabled,\n" in cuerpo
    assert "list(slugs), terraform_dir, run)" in cuerpo


def test_un_detector_sobre_el_alias_se_recrea_sobre_los_indices(monkeypatch, tmp_path):
    """Un entorno de antes: el detector apuntaba al alias. Medido en CSS 3.4:
    sobre el alias el monitor no guarda hasta dónde leyó y no ve lo que entra."""
    c = _Cluster(indices=MESES_SIEM)
    c.detectores = {"siem-siem-auth": "VIEJO"}
    c.entradas = {"VIEJO": [{"detector_input": {"indices": ["siem-seguridad"]}}]}
    _, pasos = _provisionar(monkeypatch, tmp_path, c)
    assert ("borrado_detector", "VIEJO") in c.creados
    auth = next(x[1] for x in c.creados if x[0] == "detector" and x[1]["name"] == "siem-siem-auth")
    assert auth["inputs"][0]["detector_input"]["indices"] == MESES_SIEM
    assert ("Security Analytics · siem · detector siem_auth", True, "recreado con sus índices y reglas actuales") in pasos


def test_sin_meses_el_detector_queda_sobre_el_alias(monkeypatch, tmp_path):
    spec = {k: v for k, v in SPECS["siem"].items() if k != "meses"}
    monkeypatch.setitem(SPECS, "siem", spec)
    c = _Cluster()
    _provisionar(monkeypatch, tmp_path, c)
    auth = next(x[1] for x in c.creados if x[0] == "detector" and x[1]["name"] == "siem-siem-auth")
    assert auth["inputs"][0]["detector_input"]["indices"] == ["siem-seguridad"]


# ── Hallazgos repetidos con el cluster saturado ─────────────────────────────
def test_los_eventos_detectados_se_cuentan_con_las_reglas():
    """Security Analytics registró 251.463 hallazgos para 26.707 eventos en
    FortiAnalyzer (cluster saturado). El total que muestra la plataforma son los
    eventos que matchean las reglas, cada uno una vez."""
    import seguridad
    q = seguridad.consulta_de_reglas([
        {"seleccion": {"subtype": "ips", "severity": "critical"}},
        {"seleccion": {"action": ["blocked", "dropped"], "dstport": [22, 3389]}}])
    assert q == {"bool": {"minimum_should_match": 1, "should": [
        {"bool": {"filter": [{"terms": {"subtype": ["ips"]}}, {"terms": {"severity": ["critical"]}}]}},
        {"bool": {"filter": [{"terms": {"action": ["blocked", "dropped"]}}, {"terms": {"dstport": [22, 3389]}}]}}]}}


def test_el_total_son_los_eventos_y_lo_de_sa_va_aparte(monkeypatch, tmp_path):
    main._write_security(tmp_path, {"siem": {"detectores": {"siem-siem-auth": {"id": "D1", "log_type": "siem_auth"}},
                                             "reglas": {}, "correlaciones": {}}})
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "x:9200"})
    monkeypatch.setattr(main, "_cluster_admin_password", lambda td: "pw")
    monkeypatch.setattr(main, "_read_https_enabled_from_state", lambda td: False)
    contados = []

    def fake(method, url, user, password, json_body=None, timeout=30):
        if url.endswith("/_count"):
            contados.append((url, json_body))
            return _Resp(200, {"count": 1279})
        if "/findings/_search" in url:
            f = lambda d: {"related_doc_ids": [d], "queries": [], "document_list": []}
            return _Resp(200, {"total_findings": 4616, "findings": [f("a"), f("a"), f("b"), f("a"), f("c")]})
        return _Resp(200, {"alerts": []})

    monkeypatch.setattr(main, "_os_req", fake)
    from fastapi.testclient import TestClient
    det = TestClient(main.app).get("/api/v1/security/resumen").json()["casos"][0]["detectores"][0]
    assert det["total"] == 1279 and det["hallazgos_sa"] == 4616
    assert len(det["recientes"]) == 3, "el mismo evento repetido se muestra una vez"
    (url, cuerpo), = contados
    assert url == "http://x:9200/siem-*/_count"
    reglas_auth = next(lt["reglas"] for lt in verticals.security_specs()["siem"]["log_types"] if lt["nombre"] == "siem_auth")
    import seguridad
    assert cuerpo == {"query": seguridad.consulta_de_reglas(reglas_auth)}


def test_si_no_se_puede_contar_queda_lo_de_sa(monkeypatch):
    monkeypatch.setattr(main, "_os_req", lambda *a, **k: _Resp(500, text="boom"))
    assert main._eventos_detectados("http://x:9200", "a", "p", "siem*", verticals.security_specs()["siem"], "siem_auth") is None
    assert main._eventos_detectados("http://x:9200", "a", "p", "siem*", {}, "siem_auth") is None


def test_los_detectores_tienen_mas_tiempo_por_corrida(monkeypatch, tmp_path):
    c = _Cluster()
    _, pasos = _provisionar(monkeypatch, tmp_path, c)
    (i_set,) = [i for i, x in enumerate(c.creados) if x[0] == "settings"]
    assert c.creados[i_set][1] == {"persistent": {
        "plugins.alerting.monitor.doc_level_monitor_execution_max_duration": "15m",
        "plugins.alerting.monitor.doc_level_monitor_fanout_max_duration": "12m"}}
    assert i_set < min(i for i, x in enumerate(c.creados) if x[0] == "detector"), "antes de crear los detectores"
    assert ("Security Analytics · siem · tiempo por corrida de los detectores", True, "") in pasos
