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
import verticals

SPECS = verticals.security_specs()


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
    queries = " ".join(p["query"] for c in SPECS["siem"]["correlaciones"] for p in c["correlate"])
    for cmp_ in ("CMP-001", "CMP-002", "CMP-003"):
        assert f"event.campaign:{cmp_}" in queries


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
    d = seguridad.build_detector("siem", "siem_auth", "siem-*", [("R1", "high"), ("R2", "critical"), ("R3", "high")])
    assert d["name"] == "siem-siem-auth" and d["detector_type"] == "siem_auth" and d["enabled"] is True
    entrada = d["inputs"][0]["detector_input"]
    assert entrada["indices"] == ["siem-*"] and entrada["custom_rules"] == [{"id": "R1"}, {"id": "R2"}, {"id": "R3"}]
    assert [(t["sev_levels"], t["severity"]) for t in d["triggers"]] == [(["critical"], "1"), (["high"], "2")]
    assert all(t["ids"] == [] for t in d["triggers"]), "por nivel, no por id"
    assert "siem-all" not in json.dumps(d)


def test_la_correlacion():
    c = SPECS["siem"]["correlaciones"][0]
    b = seguridad.build_correlacion(c, "siem-*")
    assert b["name"] == c["nombre"] and b["time_window"] == c["ventana_min"] * 60_000
    assert b["correlate"] == [{"index": "siem-*", "query": p["query"], "category": p["log_type"]}
                              for p in c["correlate"]]


def test_el_indice_para_el_detector():
    assert seguridad.indice_para_detector("siem-*") == "siem-sa-bootstrap"
    assert seguridad.indice_para_detector("fortianalyzer*") == "fortianalyzer-sa-bootstrap"
    assert seguridad.indice_para_detector("transacciones") == "transacciones"


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
        if method == "PUT":
            self.indices.append(ruta.strip("/"))
            self.creados.append(("indice", ruta.strip("/")))
            return _Resp(200, {"acknowledged": True})
        if ruta == "/_plugins/_security_analytics/logtype/_search":
            return _Resp(200, {"hits": {"hits": [{"_id": i, "_source": {"name": n}} for n, i in self.log_types.items()]}})
        if ruta == "/_plugins/_security_analytics/logtype":
            i = self._id(); self.log_types[json_body["name"]] = i; self.creados.append(("log_type", json_body))
            return _Resp(201, {"_id": i})
        if ruta == "/_plugins/_security_analytics/detectors/_search":
            return _Resp(200, {"hits": {"hits": [{"_id": i, "_source": {"detector": {"name": n}}} for n, i in self.detectores.items()]}})
        if ruta == "/_plugins/_security_analytics/detectors":
            i = self._id(); self.detectores[json_body["name"]] = i; self.creados.append(("detector", json_body))
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
    # Primero el índice (el pattern no matcheaba ninguno), después tipos, reglas, detectores y correlaciones.
    tipos = [x[0] for x in c.creados]
    assert tipos[0] == "indice" and c.creados[0][1] == "siem-sa-bootstrap"
    assert tipos.index("log_type") < tipos.index("regla") < tipos.index("detector") < tipos.index("correlacion")
    assert [x[1]["name"] for x in c.creados if x[0] == "log_type"] == [
        "siem_fortigate", "siem_auth", "siem_cloudaudit", "siem_waf"]
    assert all(x[1]["source"] == "Custom" for x in c.creados if x[0] == "log_type")
    # Cada regla, en la categoría de su tipo de log.
    reglas = [x for x in c.creados if x[0] == "regla"]
    assert len(reglas) == 8 and ("regla", "siem_auth", "SSH: login fallido") in reglas
    # Un detector por tipo, sobre el pattern real, con las reglas de ese tipo.
    dets = {x[1]["name"]: x[1] for x in c.creados if x[0] == "detector"}
    assert set(dets) == {"siem-siem-fortigate", "siem-siem-auth", "siem-siem-cloudaudit", "siem-siem-waf"}
    auth = dets["siem-siem-auth"]["inputs"][0]["detector_input"]
    assert auth["indices"] == ["siem-*"]
    assert {r["id"] for r in auth["custom_rules"]} == {c.reglas["SSH: login fallido"], c.reglas["Acceso a /etc/shadow con sudo"]}
    corr = [x[1] for x in c.creados if x[0] == "correlacion"]
    assert len(corr) == 3 and all(p["index"] == "siem-*" for x in corr for p in x["correlate"])
    # En Actividad, cada pieza con su resultado.
    nombres = [p[0] for p in pasos]
    assert "Security Analytics · siem · tipos de log" in nombres
    assert ("Security Analytics · siem · 8 de 8 reglas", True, "") in pasos
    assert ("Security Analytics · siem · detector siem_waf", True, "") in pasos
    assert ("Security Analytics · siem · 3 de 3 correlaciones", True, "") in pasos
    assert all(ok for _, ok, _ in pasos)
    # Y queda registrado (lo lee la vista).
    reg = json.loads((tmp_path / main._SECURITY_REGISTRY_NAME).read_text(encoding="utf-8"))["siem"]
    assert set(reg["detectores"]) == set(dets) and reg["detectores"]["siem-siem-auth"]["log_type"] == "siem_auth"
    assert len(reg["reglas"]) == 8 and len(reg["correlaciones"]) == 3
    assert res["available"] is True


def test_la_segunda_vez_no_duplica_nada(monkeypatch, tmp_path):
    c = _Cluster()
    _provisionar(monkeypatch, tmp_path, c)
    antes = len(c.creados)
    (tmp_path / main._SECURITY_REGISTRY_NAME).unlink()   # aunque se pierda el registro
    _, pasos = _provisionar(monkeypatch, tmp_path, c)
    assert len(c.creados) == antes, "todo se encuentra por nombre o título"
    assert ("Security Analytics · siem · detector siem_auth", True, "ya estaba") in pasos
    assert len(json.loads((tmp_path / main._SECURITY_REGISTRY_NAME).read_text(encoding="utf-8"))["siem"]["correlaciones"]) == 3


def test_con_indice_no_se_crea_el_de_arranque(monkeypatch, tmp_path):
    c = _Cluster(indices=["siem-2025.10"])
    _provisionar(monkeypatch, tmp_path, c)
    assert not [x for x in c.creados if x[0] == "indice"]


def test_una_regla_que_falla_se_dice_con_el_motivo(monkeypatch, tmp_path):
    c = _Cluster(fallar_regla="WAF: webshell")
    _, pasos = _provisionar(monkeypatch, tmp_path, c)
    paso = next(p for p in pasos if "reglas" in p[0])
    assert paso[0] == "Security Analytics · siem · 7 de 8 reglas" and paso[1] is False
    assert "WAF: webshell: status 400: field [x] is not mapped" in paso[2]
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
    assert c.creados[0] == ("indice", "fortianalyzer-sa-bootstrap")


def test_se_provisiona_al_aplicar_para_cada_caso_de_seguridad():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def apply_schema(")
    cuerpo = src[i:src.index("\n    msg = ", i)]
    assert "specs_seguridad = verticals.security_specs()" in cuerpo
    assert "index_pattern_from_name(index_name_t), spec_sa," in cuerpo
    # Una sola llamada: el deploy ya no lo corre (lo hacía antes de que existiera el template).
    assert src.count("_provision_security_analytics(") == 2   # la definición y la de apply-schema
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
    regla_por_id = {"R1": {"titulo": "SSH: login fallido", "nivel": "high"}}
    f = {"index": "siem-2025.10", "timestamp": 111, "queries": [{"id": "R1", "name": "x"}],
         "document_list": [{"document": json.dumps({"@timestamp": "2025-10-21T03:14:07Z",
                                                    "source": {"ip": "1.2.3.4"}})}]}
    assert main._hallazgo(f, regla_por_id) == {"regla": "SSH: login fallido", "nivel": "high",
                                               "hora": "2025-10-21T03:14:07Z", "ip": "1.2.3.4",
                                               "indice": "siem-2025.10"}
    # FortiAnalyzer: la IP es `srcip`; sin documento, la hora del hallazgo.
    forti = {"queries": [{"id": "?", "name": "regla-x"}], "timestamp": 222,
             "document_list": [{"document": {"srcip": "5.6.7.8"}}]}
    h = main._hallazgo(forti, {})
    assert h["ip"] == "5.6.7.8" and h["regla"] == "regla-x" and h["hora"] == 222 and h["nivel"] == ""


def test_el_resumen_en_vivo(monkeypatch, tmp_path):
    main._write_security(tmp_path, {"siem": {"detectores": {"siem-siem-auth": {"id": "D1", "log_type": "siem_auth"}},
                                             "reglas": {"SSH: login fallido": "R1"}, "correlaciones": {"c": "C"}}})
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
        return _Resp(200, {"alerts": [{"severity": "1"}, {"severity": "2"}, {"severity": "2"}]})

    monkeypatch.setattr(main, "_os_req", fake)
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/api/v1/security/resumen")
    assert r.status_code == 200
    det = r.json()["casos"][0]["detectores"][0]
    assert det["total"] == 1234 and len(det["recientes"]) == 5 and det["recientes"][0]["ip"] == "9.9.9.9"
    assert det["recientes"][0]["regla"] == "SSH: login fallido" and det["recientes"][0]["nivel"] == "high"
    assert det["alertas"] == {"critical": 1, "high": 2} and det["error"] == ""
    assert det["descripcion"] == "Hosts Linux: SSH y sudo (SIEM)"
    assert r.json()["casos"][0]["correlaciones"] == 1
    assert all(m == "GET" for m, _ in pedidos) and "detector_id=D1" in pedidos[0][1]


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
    { regla: 'IPS "raro"', nivel: 'critical', hora: '2025-10-21T03:14:07.000Z', ip: '1.2.3.4' }] },
  { descripcion: 'Auth', total: 0, alertas: {}, error: '', recientes: [] },
  { descripcion: 'WAF', total: 0, alertas: {}, error: 'status 500: boom', recientes: [] },
] }]);
check('total con miles', h.includes('1.842 hallazgos'), h);
check('alertas por severidad', h.includes('sev--critical">1 alerta crítica<') && h.includes('sev--high">30 alertas altas<'), h);
check('sin alertas en cero', !h.includes('sev--low'));
check('el hallazgo', h.includes('IPS &quot;raro&quot;') && h.includes('1.2.3.4') && h.includes('>Crítica<'), h);
check('explicar con sus datos', h.includes('class="btn btn-secondary btn-sm hallazgo__explicar" data-slug="siem" data-regla="IPS &quot;raro&quot;"'), h);
check('sin hallazgos todavía', h.includes('Sin hallazgos todavía') && h.includes('Reiniciar ingesta'));
check('error del detector', h.includes('No se pudieron leer: status 500: boom'));
check('correlaciones', h.includes('SIEM · 3 correlaciones entre fuentes'));
check('nada', hallazgosHTML([]).includes('No hay detectores'));

// Explicar: le pregunta al asistente con la regla, la hora y la IP.
const b = { dataset: { slug: 'siem', regla: 'SSH', hora: '21/10 03:14', ip: '1.2.3.4' } };
check('sin asistente, avisa', explicarHallazgo(b) === false && toasts.length === 1 && toasts[0].includes('Provisionar plugins'));
let pedido = null;
capChatPreguntar = (slug, pregunta, contexto) => { pedido = { slug, pregunta, contexto }; return true; };
check('con asistente', explicarHallazgo(b) === true && toasts.length === 1);
check('la pregunta', pedido.slug === 'siem' && pedido.pregunta === '¿Por qué se disparó "SSH"? ¿Qué más pasó alrededor?', pedido.pregunta);
check('el contexto', pedido.contexto === 'Hallazgo de Security Analytics: regla "SSH", 21/10 03:14, IP de origen 1.2.3.4.', pedido.contexto);
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
    assert "${seguridadHTML(data.security_analytics)}" in html
    assert "body.querySelector('#infra-seguridad-ver')?.addEventListener('click', (e) => verHallazgos(e.currentTarget));" in html
    assert "const b = e.target.closest('.hallazgo__explicar');\n        if (b) explicarHallazgo(b);" in html
    i = html.index("      capChatPreguntar = (slug, pregunta, contexto) => {")
    fn = html[i:html.index("\n      };\n", i)]
    assert "if (!activeSlugs.includes(slug) || capChatBusy) return false;" in fn
    assert fn.index("_mostrarCaso(slug);") < fn.index("_abrir(true);") < fn.index("_modoInvestigar(true);") \
        < fn.index("sendCapChat(pregunta, { contexto });")
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
    payload = {v["slug"]: v for v in verticals.front_payload()["verticals"]}
    assert payload["siem"]["hasSecurity"] and payload["fortianalyzer"]["hasSecurity"]
    assert not payload["transacciones-billetera"]["hasSecurity"]
