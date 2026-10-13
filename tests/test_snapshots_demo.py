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


def test_sin_la_tarjeta_en_la_demo():
    """La tarjeta "Datos de la demo (snapshot)" se fue de Entorno desplegado:
    los backups del cliente van en el runbook (Cómo se opera)."""
    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert '<div class="env-extras">${accesosHTML(pipelines)}</div>' in html
    assert "snapshotsDeDemoHTML" not in html and "infra-snapshots" not in html
    assert "settings-hw-agency" not in html and "Agencia de CSS para OBS" not in html, "sin el campo: va la de siempre"
    assert "const body = { ..._hwSinCampo };" in html, "guardar la infra no borra la agencia que hubiera"


def test_con_el_cluster_ocupado_lo_dice_en_palabras():
    """Visto en CSS: "ClientRequestException - {status_code:409, request_id:…,
    error_msg:{"errCode":"CSS.0011",…}}" desbordaba la tarjeta y no decía qué hacer."""
    crudo = ('ClientRequestException - {status_code:409,request_id:5b07,error_code:409,error_msg:'
             '{"errCode":"CSS.0011","externalMessage":"CSS.0011 : This operation cannot be performed because '
             'another operation is being performed on the cluster."}}')
    assert main._motivo_de_css(Exception(crudo)) == \
        "el cluster tiene otra operación en curso en CSS (CSS.0011): probá de nuevo en unos minutos"
    assert main._motivo_de_css(Exception("x" * 500)) == "x" * 300
