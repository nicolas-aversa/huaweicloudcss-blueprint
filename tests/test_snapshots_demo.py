"""Los datos de una demo en un snapshot del bucket de demos: el próximo
cluster los restaura en vez de ingerirlos. Va contra la API de snapshots del
CSS (con el SDK); acá, con un cliente falso."""
import pathlib
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import main
import maas_integrator as mi


class _Cliente:
    def __init__(self, falla=None):
        self.hechos, self.falla = [], falla or {}

    def _quizas(self, que):
        if que in self.falla:
            raise RuntimeError(self.falla[que])

    def configurar_snapshots(self, cluster_id, bucket, agencia, ruta):
        self._quizas("configurar")
        self.hechos.append(("configurar", cluster_id, bucket, agencia, ruta))

    def crear_snapshot(self, cluster_id, nombre, indices, descripcion):
        self._quizas("crear")
        self.hechos.append(("crear", cluster_id, nombre, indices))
        return "S1"

    def listar_snapshots(self, cluster_id):
        return [{"id": "S0", "nombre": "demo-20261001-1000", "estado": "COMPLETED", "creado": "2026-10-01T10:00:00"},
                {"id": "S1", "nombre": "demo-20261007-1200", "estado": "IN_PROGRESS", "creado": "2026-10-07T12:00:00"}]

    def restaurar_snapshot(self, cluster_id, snapshot_id, destino, indices):
        self._quizas("restaurar")
        self.hechos.append(("restaurar", cluster_id, snapshot_id, destino, indices))


@pytest.fixture
def entorno(monkeypatch, tmp_path):
    cliente = _Cliente()
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"id": "C2", "public_endpoint": "x:9200"})
    monkeypatch.setattr(main, "_cluster_hwc_creds", lambda td: ("AK", "SK"))
    monkeypatch.setattr(main, "get_huawei_project_id", lambda: "P")
    monkeypatch.setattr(main, "_css_client", lambda ak, sk, pid: cliente)
    mi.set_huawei_settings({"demo_bucket": "demoscss", "css_agency": "mi_agencia"})
    main._write_pipelines_registry(tmp_path, {"siem": {}, "produccion-pozos": {}})
    return cliente


def test_guardar_los_datos_de_los_casos(entorno):
    r = TestClient(main.app).post("/api/v1/demo-snapshots")
    d = r.json()
    assert r.status_code == 200 and d["indices"] == "siem-*,produccion-pozos-*" and d["nombre"].startswith("demo-")
    assert entorno.hechos[0] == ("configurar", "C2", "demoscss", "mi_agencia", "css-demos/snapshots")
    assert entorno.hechos[1][:2] == ("crear", "C2") and entorno.hechos[1][3] == "siem-*,produccion-pozos-*"


def test_listar_los_guardados_mas_nuevo_primero(entorno):
    d = TestClient(main.app).get("/api/v1/demo-snapshots").json()
    assert [s["id"] for s in d["snapshots"]] == ["S1", "S0"]


def test_restaurar_en_este_cluster(entorno):
    r = TestClient(main.app).post("/api/v1/demo-snapshots/S0/restaurar")
    assert r.json() == {"restaurando": "S0", "indices": "siem-*,produccion-pozos-*"}
    assert entorno.hechos[-1] == ("restaurar", "C2", "S0", "C2", "siem-*,produccion-pozos-*")


def test_sin_agencia_se_usa_la_de_siempre(entorno):
    mi.set_huawei_settings({"demo_bucket": "demoscss"})
    TestClient(main.app).get("/api/v1/demo-snapshots")
    assert entorno.hechos[0][3] == "css_obs_agency"


def test_lo_que_falta_y_lo_que_falla_se_dice(entorno, monkeypatch):
    mi.set_huawei_settings({})
    r = TestClient(main.app).post("/api/v1/demo-snapshots")
    assert r.status_code == 400 and "el bucket de demos" in r.json()["detail"]["message"]
    mi.set_huawei_settings({"demo_bucket": "demoscss"})
    entorno.falla = {"configurar": "Agency css_obs_agency not found"}
    r = TestClient(main.app).post("/api/v1/demo-snapshots")
    assert r.status_code == 502 and "Agency css_obs_agency not found" in r.json()["detail"]["message"]
    entorno.falla = {"restaurar": "index already exists"}
    r = TestClient(main.app).post("/api/v1/demo-snapshots/S0/restaurar")
    assert r.status_code == 502 and "No se pudo restaurar: index already exists" in r.json()["detail"]["message"]


def test_la_tarjeta_en_el_resumen():
    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert '<div class="env-extras">${accesosHTML(pipelines)}${snapshotsDeDemoHTML()}</div>' in html
    assert "body.querySelector('#infra-snapshots-guardar')?.addEventListener('click', (e) => guardarSnapshot(e.currentTarget));" in html
    assert "confirmLabel: 'Restaurar' });" in html, "restaurar pide confirmación"
    assert "css_agency: 'settings-hw-agency'" in html


_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
""" + "{FUNCIONES}" + r"""
const f = [];
const l = snapshotsListaHTML([{ id: 'S0', nombre: 'demo-1', estado: 'COMPLETED', cluster: 'c1', indices: 'siem-*' },
                              { id: 'S1', nombre: 'demo-2', estado: 'IN_PROGRESS', cluster: 'c1', indices: 'siem-*' }]);
if (!l.includes('<span class="sev sev--ok">Listo</span><span class="snap__nombre">demo-1</span>')) f.push('listo ' + l);
if ((l.match(/snap__restaurar/g) || []).length !== 1) f.push('solo los listos se restauran ' + l);
if (!l.includes('data-id="S0"')) f.push('id');
if (!snapshotsListaHTML([]).includes('Todavía no hay datos')) f.push('vacío');
console.log(f.join('\n')); process.exit(f.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_lista_en_node(tmp_path):
    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    i = html.index("    const _ESTADO_SNAPSHOT = {")
    js = tmp_path / "snap.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", html[i:html.index("    async function verSnapshots() {", i)]), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_con_el_cluster_ocupado_lo_dice_en_palabras():
    """Visto en CSS: "ClientRequestException - {status_code:409, request_id:…,
    error_msg:{"errCode":"CSS.0011",…}}" desbordaba la tarjeta y no decía qué hacer."""
    crudo = ('ClientRequestException - {status_code:409,request_id:5b07,error_code:409,error_msg:'
             '{"errCode":"CSS.0011","externalMessage":"CSS.0011 : This operation cannot be performed because '
             'another operation is being performed on the cluster."}}')
    assert main._motivo_de_css(Exception(crudo)) == \
        "el cluster tiene otra operación en curso en CSS (CSS.0011): probá de nuevo en unos minutos"
    assert main._motivo_de_css(Exception("x" * 500)) == "x" * 300
