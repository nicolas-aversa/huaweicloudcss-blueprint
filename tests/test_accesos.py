"""Analista de demo con datos enmascarados: un usuario de solo lectura por
caso de salud/fintech que ve los campos sensibles enmascarados."""
import json
import pathlib
import shutil
import subprocess

import pytest

import accesos
import main
import verticals

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


def test_el_rol_lee_el_caso_con_los_campos_enmascarados():
    r = accesos.rol_analista("encuentros-clinicos-*", ["patient"])
    ip = r["index_permissions"][0]
    assert ip["index_patterns"] == ["encuentros-clinicos-*"] and ip["masked_fields"] == ["patient"]
    assert "read" in ip["allowed_actions"] and not [a for a in ip["allowed_actions"] if "write" in a or a == "*"]
    assert r["cluster_permissions"] == ["cluster_composite_ops_ro"]


def test_el_usuario_lleva_sus_roles_sin_tocar_los_mappings():
    u = accesos.usuario_analista("encuentros-clinicos", "Clave#123abc")
    assert u["password"] == "Clave#123abc"
    assert u["opendistro_security_roles"] == ["encuentros-clinicos-analista", "kibana_user"]
    assert accesos.nombre_del_usuario("x") == "analista-x" and accesos.nombre_del_rol("x") == "x-analista"


@pytest.mark.parametrize("_", range(20))
def test_la_contrasena_cumple_la_politica_de_css(_):
    p = accesos.contrasena()
    assert len(p) == 16
    assert any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p)
    assert any(c in "@#%^*_+-" for c in p) and not set(p) & set("\"'\\/ ")


def test_los_casos_que_declaran_analista():
    con = {v["slug"]: v["analista"]["enmascarados"] for v in verticals.all_verticals() if v.get("analista")}
    assert con == {"encuentros-clinicos": ["patient"],
                   "transacciones-billetera": ["transaction.customer_id", "transaction.account_ref"],
                   "transacciones-alyc": ["comitente"]}
    for slug, campos in con.items():
        assert set(campos) <= set(verticals.get_vertical(slug)["capability"]["fields"]) | {"transaction.account_ref"}
    payload = {v["slug"]: v for v in verticals.front_payload()["verticals"]}
    assert payload["encuentros-clinicos"]["enmascarados"] == ["patient"] and payload["siem"]["enmascarados"] == []


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data or {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


def _cluster(monkeypatch, falla=""):
    pedidos = []

    def req(method, url, user, password, json_body=None, timeout=30):
        pedidos.append((method, url.split(":9200", 1)[1], json_body))
        if falla and falla in url:
            return _R(403, {"error": "no permitido"})
        return _R(200, {"status": "OK"})

    monkeypatch.setattr(main, "_os_req", req)
    return pedidos


def test_alta_del_analista_y_conserva_la_contrasena(monkeypatch, tmp_path):
    pedidos = _cluster(monkeypatch)
    r = main._provisionar_analista("http://x:9200", "admin", "pw", "encuentros-clinicos", "encuentros-clinicos-*",
                                   ["patient"], tmp_path)
    assert r == {"ok": True, "reason": "analista-encuentros-clinicos: ve encuentros-clinicos-* con patient enmascarado"}
    assert [(m, u) for m, u, _ in pedidos] == [
        ("PUT", "/_plugins/_security/api/roles/encuentros-clinicos-analista"),
        ("PUT", "/_plugins/_security/api/internalusers/analista-encuentros-clinicos")]
    reg = json.loads((tmp_path / main._ANALISTAS_NAME).read_text(encoding="utf-8"))["encuentros-clinicos"]
    assert reg["usuario"] == "analista-encuentros-clinicos" and reg["enmascarados"] == ["patient"]
    clave = reg["password"]
    assert pedidos[1][2]["password"] == clave
    # Reprovisionar no cambia la contraseña que el SA ya tiene anotada.
    main._provisionar_analista("http://x:9200", "admin", "pw", "encuentros-clinicos", "encuentros-clinicos-*",
                               ["patient"], tmp_path)
    assert pedidos[-1][2]["password"] == clave
    assert main._read_analistas(tmp_path)["encuentros-clinicos"]["password"] == clave


@pytest.mark.parametrize("falla, motivo", [("/roles/", "rol: "), ("/internalusers/", "usuario: ")])
def test_si_el_cluster_no_deja_se_dice(monkeypatch, tmp_path, falla, motivo):
    _cluster(monkeypatch, falla)
    r = main._provisionar_analista("http://x:9200", "admin", "pw", "x", "x-*", ["a"], tmp_path)
    assert r["ok"] is False and r["reason"].startswith(motivo) and "403" in r["reason"]
    assert not (tmp_path / main._ANALISTAS_NAME).exists()


def test_el_endpoint_devuelve_los_accesos(monkeypatch, tmp_path):
    """Solo los de los casos desplegados: el registro guarda los de entornos anteriores."""
    main._write_analistas(tmp_path, {"transacciones-alyc": {"usuario": "analista-transacciones-alyc",
                                                            "password": "P#1aa", "enmascarados": ["comitente"]},
                                     "de-antes": {"usuario": "analista-de-antes", "password": "x", "enmascarados": ["y"]}})
    main._write_pipelines_registry(tmp_path, {"transacciones-alyc": {"index": "transacciones-alyc-%{+YYYY.MM}"}})
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    assert main.analistas().analistas == [{"slug": "transacciones-alyc", "usuario": "analista-transacciones-alyc",
                                           "password": "P#1aa", "enmascarados": ["comitente"]}]


def test_el_registro_esta_gitignoreado():
    gi = (pathlib.Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "terraform/.analistas.json" in gi


def _funciones(html: str) -> str:
    i = html.index("    function accesoDelCasoHTML(slug, enmascarados) {")
    return html[i:html.index("    async function verAcceso(btn) {", i)]


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SLUG_LABELS = { 'encuentros-clinicos': 'Encuentros clínicos' };
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
// Una línea por caso, como las demás funciones: qué enmascara y, a pedido, el acceso.
const linea = accesoDelCasoHTML('mi-dataset', ['data.dni', 'data.cuenta']);
check('qué enmascara, con el botón', linea.includes('<span>Enmascara data.dni, data.cuenta</span>')
  && linea.includes('class="fn__ver-acceso" data-slug="mi-dataset">Ver usuario y contraseña</button>'), linea);
check('sin contraseña hasta pedirla', !linea.includes('Contraseña'), linea);
const d = credencialesHTML({ slug: 'encuentros-clinicos', usuario: 'analista-encuentros-clinicos', password: 'Ab#1<x>' });
check('usuario y contraseña', d.includes('<code>analista-encuentros-clinicos</code>') && d.includes('<code>Ab#1&lt;x&gt;</code>'), d);
check('sin analista', credencialesHTML(undefined).includes('Activar los plugins'));
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_tarjeta_en_node(tmp_path):
    js = tmp_path / "accesos.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", _funciones(_INDEX.read_text(encoding="utf-8"))), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_va_en_el_panel_de_enmascaramiento():
    """Los accesos de demo, con su función (Enmascaramiento de campos), una
    línea por caso como las demás (no una tarjeta aparte ni en "Más opciones")."""
    html = _INDEX.read_text(encoding="utf-8")
    assert "? accesoDelCasoHTML(s, ((f.pipelines || []).find(p => p.slug === s) || {}).enmascarados) : '';" in html
    assert "accesosHTML" not in html and "infra-accesos" not in html
    assert "panel.querySelectorAll('.fn__numero[data-slug][data-numero]')" in html, "el relleno de números no la vacía"
    assert "env-extras" not in html.split("function renderInfraView(data) {")[1].split("async function hydrateActiveEnv()")[0]
    assert "const acceso = e.target.closest('.fn__ver-acceso');\n        if (acceso) return verAcceso(acceso);" in html
    # "Explicarla con IA", desde el panel de Detección de anomalías (antes, solo en «Detalle por caso»).
    assert "const explicar = e.target.closest('.anomalia__explicar');\n        if (explicar) return explicarAnomalia(explicar);" in html
    assert "const _NUMERO_EN_EL_PANEL = new Set(['anomalias', 'perfil']);" in html and "llenarNumerosDeFuncion(panel);" in html
    # Cada anomalía con su fecha y su botón; la pregunta dice cuál es.
    assert "const lista = ((n.todas || []).length ? n.todas : [n.top]).filter(a => a && a.inicio);" in html
    assert "`¿Qué pasó el ${String(inicio).slice(0, 16)} UTC (grado ${grado} %)? ¿Por qué es una anomalía?`" in html
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert '"todas": top' in src and "_ANOMALIAS_EN_EL_PANEL = 5" in src
