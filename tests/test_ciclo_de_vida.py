"""El paso del tiempo: la política de ciclo de vida (ISM) de cada dataset y su
rollup por hora. Sin esto el cluster que se lleva el cliente crece hasta
llenar el disco: es lo primero que le falta a uno "pelado"."""
import json
import pathlib
import shutil
import subprocess

import pytest

import ciclo_de_vida as cdv
import exportar
import main
import plan_de_cluster
import plugins_vista as pv
import verticals

_INDEX = pathlib.Path(main.__file__).parent / "static" / "index.html"


# ── La política ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pedido, dias", [(0, 90), (None, 90), ("x", 90), (30, 30), (3, 7), (99999, 3650)])
def test_la_retencion(pedido, dias):
    assert cdv.retencion(pedido) == dias


def test_solo_lectura_recien_pasado_el_mes_y_borrado_pasada_la_retencion():
    """Los índices son mensuales: el del mes en curso se escribe hasta 31 días
    después de creado. Cada dato se conserva, al menos, la retención."""
    p = cdv.politica("pozos", "pozos-*", 30)["policy"]
    estados = {e["name"]: e for e in p["states"]}
    assert p["default_state"] == "caliente"
    assert estados["caliente"]["transitions"] == [{"state_name": "tibio", "conditions": {"min_index_age": "35d"}}]
    assert estados["tibio"]["actions"] == [{"read_only": {}}, {"force_merge": {"max_num_segments": 1}}]
    assert estados["tibio"]["transitions"] == [{"state_name": "borrar", "conditions": {"min_index_age": "61d"}}]
    assert estados["borrar"]["actions"] == [{"delete": {}}]
    assert p["ism_template"] == [{"index_patterns": ["pozos-*"], "priority": 100}]
    # Con la retención mínima, borrar nunca va antes de pasar a solo lectura.
    assert cdv.dias_hasta_borrar(7) > cdv.DIAS_HASTA_TIBIO


# ── El rollup ───────────────────────────────────────────────────────────────
def test_dimensiones_y_medidas_de_un_dataset_nuevo():
    campos = [{"field_path": "status", "type": "keyword", "role": "primary_dimension"},
              {"field_path": "customer_id", "type": "keyword", "role": "dimension"},
              {"field_path": "monto", "type": "double", "role": "measure"},
              {"field_path": "order_id", "type": "long"},
              {"field_path": "cantidad", "type": "integer"}]
    dims, medidas = cdv.dimensiones_y_medidas(campos)
    assert dims == ["status"], "un id no es una dimensión"
    assert medidas == ["monto"], "la marcada como medida; los números sueltos solo si no hay"
    assert cdv.dimensiones_y_medidas([{"field_path": "n", "type": "integer"}, {"field_path": "x_id", "type": "long"}]) == ([], ["n"])


def test_con_el_cluster_las_dimensiones_son_las_de_pocos_valores():
    """En los curados no hay roles: las dimensiones salen de `_discover_enums`
    (campos de texto con pocos valores) y las medidas, de los pronósticos."""
    v = verticals.get_vertical("produccion-pozos")
    dims, medidas = cdv.dimensiones_y_medidas(v["fields"], v["capability"], {"well_type": ["OP", "WI"], "status": ["A"]})
    assert dims == ["well_type", "status"]
    assert medidas == ["oil_vol", "gas_vol"], "los que suman los pronósticos"


def test_el_rollup_por_hora():
    r = cdv.rollup("pozos", "pozos-*", ["status"], ["oil_vol"], 1_790_000_000_000)["rollup"]
    assert (r["source_index"], r["target_index"], r["continuous"], r["enabled"]) == ("pozos-*", "rollup-pozos", True, True)
    assert not r["target_index"].startswith("pozos-"), "fuera del pattern del caso: si no, se mezcla con los datos"
    assert r["schedule"]["interval"] == {"period": 1, "unit": "Hours", "start_time": 1_790_000_000_000}
    assert r["dimensions"][0] == {"date_histogram": {"source_field": "@timestamp", "fixed_interval": "1h", "timezone": "UTC"}}
    assert r["dimensions"][1:] == [{"terms": {"source_field": "status"}}]
    assert r["metrics"] == [{"source_field": "oil_vol", "metrics": [{"sum": {}}, {"avg": {}}, {"min": {}}, {"max": {}},
                                                                    {"value_count": {}}]}]


# ── El plan del paso 2 ──────────────────────────────────────────────────────
_CAMPOS = [{"field_path": "@timestamp", "type": "date", "role": "timestamp"},
           {"field_path": "status", "type": "keyword", "role": "primary_dimension", "business_label": "Estado"},
           {"field_path": "monto", "type": "double", "role": "measure", "business_label": "Monto"}]


def test_el_plan_lo_dice_con_la_retencion_pedida():
    items = {i["plugin"]: i for i in plan_de_cluster.plan("ventas", _CAMPOS, retencion_dias=30)}
    assert items["ciclo_de_vida"]["aplica"] and items["ciclo_de_vida"]["opcional"]
    assert items["ciclo_de_vida"]["motivo"] == "solo lectura a los 35 días y borrado pasada la retención de 30 días"
    assert items["ciclo_de_vida"]["config"] == {"retencion_dias": 30}
    assert items["rollup"]["aplica"] and items["rollup"]["motivo"] == \
        "Monto por hora y por Estado: queda aunque se borren los datos crudos"
    sin_medida = {i["plugin"]: i for i in plan_de_cluster.plan("x", _CAMPOS[:2])}
    assert not sin_medida["rollup"]["aplica"] and sin_medida["rollup"]["motivo"] == "no hay medidas numéricas para resumir"


def test_la_retencion_viaja_del_paso_2_al_entorno():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert '"retencion_dias": request.get("retencion_dias") or 0,' in src, "al caso guardado"
    assert '"retencion_dias": case.retencion_dias or (custom_cases.get_case(case.slug) or {}).get("retencion_dias") or 0,' in src
    assert '"retencion_dias": request.retencion_dias or 0,' in src
    html = _INDEX.read_text(encoding="utf-8")
    assert html.count("retencion_dias: state.planRetencion || 0") == 5,         "el plan, el caso (una o varias fuentes), el deploy y el dimensionamiento"


def test_el_caso_guardado_conserva_la_retencion():
    import custom_cases
    src = pathlib.Path(custom_cases.__file__).read_text(encoding="utf-8")
    assert '"retencion_dias": _retencion(meta.get("retencion_dias")),' in src
    assert custom_cases._retencion("45") == 45 and custom_cases._retencion(-3) == 0 and custom_cases._retencion(99999) == 3650


# ── Provisionar ─────────────────────────────────────────────────────────────
class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


def _cluster(monkeypatch, existe=False, explain=None, crear=200):
    pedidos = []

    def req(m, url, user, password, json_body=None, timeout=30):
        pedidos.append((m, url, json_body))
        if m == "GET" and url.endswith("/_explain"):
            return _R(200, explain or {})
        if m == "GET":
            return _R(200, {"_seq_no": 7, "_primary_term": 2}) if existe else _R(404)
        if m == "POST" and "/_plugins/_ism/add/" in url:
            return _R(200, {"updated_indices": 12})
        if m == "PUT":
            return _R(crear)
        return _R(200)

    monkeypatch.setattr(main, "_os_req", req)
    return pedidos


def test_la_politica_se_crea_y_toma_los_indices_que_ya_estan(monkeypatch):
    pedidos = _cluster(monkeypatch)
    r = main._provisionar_ciclo_de_vida("http://x", "a", "p", "pozos", "pozos-*", 30, force=False)
    assert r == {"ok": True, "reason": "pozos-ciclo-de-vida: retención de 30 días, aplicada a 12 índices"}
    put = next(b for m, u, b in pedidos if m == "PUT")
    assert put == cdv.politica("pozos", "pozos-*", 30)
    assert ("POST", "http://x/_plugins/_ism/add/pozos-*", {"policy_id": "pozos-ciclo-de-vida"}) in pedidos


def test_si_ya_estaba_se_deja_y_con_force_se_actualiza(monkeypatch):
    _cluster(monkeypatch, existe=True)
    assert main._provisionar_ciclo_de_vida("http://x", "a", "p", "pozos", "pozos-*", 30, force=False) == \
        {"ok": True, "reason": "ya estaba"}
    pedidos = _cluster(monkeypatch, existe=True)
    r = main._provisionar_ciclo_de_vida("http://x", "a", "p", "pozos", "pozos-*", 60, force=True)
    assert r["ok"] and "retención de 60 días" in r["reason"]
    assert any(m == "PUT" and u.endswith("?if_seq_no=7&if_primary_term=2") for m, u, b in pedidos), "concurrencia optimista de ISM"
    assert any(u.endswith("/_plugins/_ism/change_policy/pozos-*") for m, u, b in pedidos)


def test_si_la_politica_no_se_crea_se_dice(monkeypatch):
    _cluster(monkeypatch, crear=400)
    r = main._provisionar_ciclo_de_vida("http://x", "a", "p", "pozos", "pozos-*", 30, force=False)
    assert not r["ok"] and r["reason"].startswith("no se pudo crear la política")


def test_el_rollup_se_crea_y_uno_que_fallo_se_rehace(monkeypatch):
    pedidos = _cluster(monkeypatch)
    r = main._provisionar_rollup("http://x", "a", "p", "pozos", "pozos-*", ["status"], ["oil_vol"], force=False)
    assert r == {"ok": True, "reason": "pozos-rollup → rollup-pozos: oil_vol por hora y por status"}
    assert any(m == "PUT" and u.endswith("/_plugins/_rollup/jobs/pozos-rollup") for m, u, b in pedidos)
    _cluster(monkeypatch, existe=True)
    assert main._provisionar_rollup("http://x", "a", "p", "pozos", "pozos-*", [], ["oil_vol"], force=False)["reason"] == "ya estaba"
    pedidos = _cluster(monkeypatch, existe=True, explain={"pozos-rollup": {"metadata": {"status": "failed", "failure_reason": "boom"}}})
    r = main._provisionar_rollup("http://x", "a", "p", "pozos", "pozos-*", [], ["oil_vol"], force=False)
    assert r["ok"] and "se rehízo: había fallado, boom" in r["reason"]
    assert ("DELETE", "http://x/rollup-pozos", None) in pedidos, "con su índice"


def test_el_paso_del_tiempo_al_provisionar(monkeypatch, tmp_path):
    hechos = []
    monkeypatch.setattr(main, "_provisionar_ciclo_de_vida", lambda b, u, p, slug, ip, dias, force:
                        hechos.append(("ism", slug, ip, dias)) or {"ok": True, "reason": "ok"})
    monkeypatch.setattr(main, "_provisionar_rollup", lambda b, u, p, slug, ip, dims, medidas, force:
                        hechos.append(("rollup", dims, medidas)) or {"ok": True, "reason": "ok"})
    monkeypatch.setattr(main, "_discover_enums", lambda *a, **k: {"status": ["A", "B"]})
    pasos = []
    monkeypatch.setattr(main.runs, "step", lambda run, n, ok, r="": pasos.append((n, ok)))
    entry = {"fields": _CAMPOS, "retencion_dias": 45}
    monkeypatch.setattr(main, "_resolve_capability_spec", lambda *a, **k: {})
    main._paso_del_tiempo_original("http://x", "a", "p", "ventas", "ventas-*", entry, False, tmp_path, {})
    assert hechos == [("ism", "ventas", "ventas-*", 45), ("rollup", ["status"], ["monto"])]
    assert pasos == [("Ciclo de vida · ventas", True), ("Rollup · ventas", True)]
    e = main._read_estados(tmp_path)["ventas"]
    assert e["ciclo_de_vida"] == {"ok": True, "motivo": "ok", "retencion_dias": 45}
    assert e["rollup"] == {"ok": True, "motivo": "ok", "dimensiones": ["status"], "medidas": ["monto"]}
    # Apagados en el paso 2: nada.
    hechos.clear()
    main._paso_del_tiempo_original("http://x", "a", "p", "ventas", "ventas-*",
                                   {**entry, "excluir": ["ciclo_de_vida", "rollup"]}, False, tmp_path, {})
    assert hechos == []


def test_la_provision_lo_hace_para_cada_caso():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def provision_capabilities(")
    cuerpo = src[i:src.index("\n    msg = ", i)]
    assert "_provisionar_el_paso_del_tiempo(_base_analistas, user, password, slug, index_pattern_from_name(indice)," in cuerpo


# ── La vista, el export y la verificación ───────────────────────────────────
def test_las_tarjetas():
    estados = {"ciclo_de_vida": {"ok": True, "retencion_dias": 30},
               "rollup": {"ok": False, "motivo": "boom", "dimensiones": ["status"], "medidas": ["monto"]}}
    t = {x["plugin"]: x for x in pv.tarjetas_del_caso(
        "ventas", entry={"fields": _CAMPOS}, ids={"agent_id": "A"}, spec={}, perfil=None, enmascarados=[],
        seguridad_reg={}, seguridad_spec={}, estados=estados, analista_creado=False, base="https://d")}
    assert t["ciclo_de_vida"]["que"].endswith("se borran pasada la retención de 30 días.")
    assert t["ciclo_de_vida"]["links"][0]["url"] == \
        "https://d/app/opensearch_index_management_dashboards#/policy-details?id=ventas-ciclo-de-vida"
    assert (t["rollup"]["estado"], t["rollup"]["motivo"]) == (pv.FALLA, "boom")
    assert t["rollup"]["que"].startswith("Monto por hora y por Estado, en rollup-ventas")
    assert t["rollup"]["links"][0]["url"].endswith("#/rollup-details?id=ventas-rollup")
    excl = {x["plugin"]: x for x in pv.tarjetas_del_caso(
        "ventas", entry={"excluir": ["rollup"]}, ids={"agent_id": "A"}, spec={}, perfil=None, enmascarados=[],
        seguridad_reg={}, seguridad_spec={}, estados={}, analista_creado=False, base="")}
    assert excl["rollup"]["estado"] == pv.EXCLUIDO and "ciclo_de_vida" not in excl


def test_el_export_lo_lleva():
    texto = exportar.devtools("ventas", _CAMPOS, label="Ventas", retencion_dias=30)
    assert "PUT _plugins/_ism/policies/ventas-ciclo-de-vida" in texto and '"min_index_age": "61d"' in texto
    assert "POST _plugins/_ism/add/ventas-*" in texto
    assert "PUT _plugins/_rollup/jobs/ventas-rollup" in texto and '"start_time": "<AHORA_EN_EPOCH_MS>"' in texto
    sin = exportar.devtools("ventas", _CAMPOS, excluir=["ciclo_de_vida", "rollup"])
    assert "_plugins/_ism" not in sin and "_plugins/_rollup" not in sin


def test_se_comprueba_en_el_cluster(monkeypatch):
    def req(m, url, *a, **k):
        if url.endswith("/_plugins/_ism/policies/ventas-ciclo-de-vida"):
            return _R(200, {"policy": {}})
        if url.endswith("/_plugins/_rollup/jobs/ventas-rollup/_explain"):
            return _R(200, {"ventas-rollup": {"metadata": {"status": "failed", "failure_reason": "sin mapping"}}})
        return _R(404)

    monkeypatch.setattr(main, "_os_req", req)
    v = main._verificar_en_el_cluster("http://x", "a", "p", "ventas", {}, {}, {},
                                      {"ciclo_de_vida": {"ok": True}, "rollup": {"ok": True}})
    assert v["ciclo_de_vida"] == {"ok": True, "detalle": "política en el cluster"}
    assert v["rollup"] == {"ok": False, "detalle": "el rollup falló: sin mapping"}


_ARNES = r"""
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const icon = (n) => `<svg data-i="${n}"></svg>`;
const SLUG_LABELS = {};
const state = {};
const _fmtPron = (v) => Number(v).toLocaleString('es-AR', { maximumFractionDigits: 1 });
""" + "{FUNCIONES}" + r"""
const f = [];
const check = (n, c, x) => { if (!c) f.push(n + ' -> ' + x); };
check('índices bajo la política', numeroDePlugin('ciclo_de_vida', { indices: 12 }, 's') === '<span><strong>12</strong> índices bajo la política</span>');
check('filas del rollup', numeroDePlugin('rollup', { docs: 8760 }, 's').includes('<strong>8.760</strong> filas en el resumen'));
check('rollup sin escribir', numeroDePlugin('rollup', { docs: null }, 's').includes('corre cada hora'));
console.log(f.join('\n')); process.exit(f.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_los_numeros_en_node(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    const _ESTADO_PLUGIN = {")
    js = tmp_path / "ciclo.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", html[i:html.index("    async function verPlugins(btn) {", i)]), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_el_paso_2_deja_cambiar_la_retencion():
    html = _INDEX.read_text(encoding="utf-8")
    assert "${i.plugin === 'ciclo_de_vida' && i.aplica && !apagado" in html
    assert 'class="plan-cluster__dias" min="7" max="3650"' in html
    assert "state.planRetencion = dias >= 7 && dias <= 3650 ? dias : 0;" in html


@pytest.mark.parametrize("slug, medidas", [
    ("transacciones-billetera", ["transaction.funnel.steps_total", "transaction.funnel.steps_completed"]),
    ("cts", []),                                   # su único número es el `code` HTTP: sin rollup
    ("siem", ["source.bytes", "destination.bytes", "event.risk_score"]),
])
def test_un_codigo_no_es_una_medida(slug, medidas):
    """Visto en CSS 3.4: el rollup de billetera sumaba `message_type` y
    `sequence_number`, y el de CTS el `code` HTTP."""
    v = verticals.get_vertical(slug)
    assert cdv.dimensiones_y_medidas(v["fields"], v["capability"])[1] == medidas


def test_las_dimensiones_las_dice_el_cluster_tambien_en_los_curados():
    """`_discover_enums` solo mira los campos marcados como dimensión, y en un
    caso curado no hay marcas: el rollup quedaba sin dimensiones."""
    campos = [{"field_path": "status", "type": "keyword"}, {"field_path": "order_id", "type": "keyword"},
              {"field_path": "monto", "type": "double"}]
    assert cdv.candidatos_a_dimension(campos) == [{"field_path": "status", "type": "keyword", "dimension": True}]
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert "_discover_enums(base, user, password, index_pattern, cdv.candidatos_a_dimension(fields))" in src
