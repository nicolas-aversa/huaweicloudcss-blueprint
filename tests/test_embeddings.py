"""El modelo de embeddings de la búsqueda híbrida: NVIDIA NIM por un conector
de ml-commons (un modelo remoto). El CSS no deja desplegar modelos locales
(TorchScript y ONNX fallaban: DJL baja PyTorch de internet y el nodo no lo
puede guardar) y MaaS no tiene embeddings en ap-southeast-1."""
import json

import pytest

import busqueda
import capabilities
import main
import maas_integrator
import plugins_vista as pv

CLAVE = "clave-de-prueba-1234"


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


def _cluster(monkeypatch, existentes=None, deploy_ok=True, predict_ok=True, clave=CLAVE):
    """Un ml-commons falso. `existentes`: {nombre: [(model_id, estado)]}; un
    estado None es un modelo que no se puede leer (el cluster no contesta)."""
    pedidos = []
    estados = {mid: est for lista in (existentes or {}).values() for mid, est in lista}
    por_nombre = {n: [mid for mid, _ in lista] for n, lista in (existentes or {}).items()}
    contador = {"conectores": 0}

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        pedidos.append((m, url, json_body))
        if url.endswith("/_plugins/_ml/models/_search"):
            nombre = json_body["query"]["bool"]["must"][0]["term"]["name.keyword"]
            return _R(200, {"hits": {"hits": [{"_id": i} for i in por_nombre.get(nombre, []) if i in estados]}})
        if url.endswith("/_plugins/_ml/connectors/_create"):
            contador["conectores"] += 1
            return _R(200, {"connector_id": f"C{contador['conectores']}"})
        if "/_plugins/_ml/models/_register" in url:
            mid = f"M-{json_body['name']}"
            estados[mid] = "DEPLOYED" if deploy_ok else "DEPLOY_FAILED"
            return _R(200, {"task_id": f"T-{mid}", "status": "CREATED"})
        if "/_plugins/_ml/tasks/T-" in url:
            return _R(200, {"state": "COMPLETED", "model_id": url.rsplit("/T-", 1)[1]})
        if url.endswith("/_deploy"):
            mid = url.split("/models/")[1].split("/")[0]
            if deploy_ok:
                estados[mid] = "DEPLOYED"
            return _R(200, {})
        if url.endswith("/_predict"):
            return (_R(200, {"inference_results": [{"output": [{"data": [0.1] * busqueda.DIMENSION}]}]}) if predict_ok
                    else _R(500, {"error": {"reason": "Connection timed out"}}))
        if "/_plugins/_ml/models/" in url and m == "DELETE":
            estados.pop(url.rsplit("/", 1)[-1], None)
            return _R(200, {})
        if "/_plugins/_ml/models/" in url and m == "GET":
            mid = url.rsplit("/", 1)[-1]
            if mid not in estados:
                return _R(404)
            return None if estados[mid] is None else _R(200, {"model_state": estados[mid], "connector_id": f"CX-{mid}"})
        return _R(200, {})

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(maas_integrator, "get_nvidia_api_key", lambda: clave)
    return pedidos


def _creados(pedidos, sufijo):
    return [b for m, u, b in pedidos if m == "POST" and sufijo in u]


# ── Los modelos remotos ─────────────────────────────────────────────────────
def test_crea_el_conector_y_los_dos_modelos(monkeypatch):
    pedidos = _cluster(monkeypatch)
    ids, motivo = main._preparar_los_modelos("http://x", "a", "p", "")
    assert motivo == "" and ids == {"passage": "M-plataforma-embeddings-textos", "query": "M-plataforma-embeddings-consultas"}
    conectores = _creados(pedidos, "/connectors/_create")
    assert [c["parameters"]["input_type"] for c in conectores] == ["passage", "query"], "uno para cargar y otro para buscar"
    for c in conectores:
        accion = c["actions"][0]
        assert accion["url"] == "https://integrate.api.nvidia.com/v1/embeddings" and c["credential"] == {"api_key": CLAVE}
        assert c["parameters"]["model"] == "nvidia/nemotron-3-embed-1b"
        assert accion["pre_process_function"] == "connector.pre_process.openai.embedding"
        assert "${credential.api_key}" in accion["headers"]["Authorization"]
    registros = _creados(pedidos, "/models/_register?deploy=true")
    assert [r["function_name"] for r in registros] == ["remote", "remote"], "nada se instala en el nodo"
    assert [r["connector_id"] for r in registros] == ["C1", "C2"]
    assert len(_creados(pedidos, "/_predict")) == 1, "un vector de prueba: ¿llega el cluster a NVIDIA?"


def test_reusa_los_que_ya_estan(monkeypatch):
    pedidos = _cluster(monkeypatch, existentes={"plataforma-embeddings-textos": [("T1", "DEPLOYED")],
                                                "plataforma-embeddings-consultas": [("Q1", "DEPLOYED")]})
    assert main._preparar_los_modelos("http://x", "a", "p", main._huella(CLAVE)) == ({"passage": "T1", "query": "Q1"}, "")
    assert not _creados(pedidos, "/connectors/_create") and not _creados(pedidos, "/_register")


def test_registrado_sin_desplegar_se_despliega(monkeypatch):
    """Un reinicio del nodo los deja UNDEPLOYED: se despliegan, no se crean otros."""
    pedidos = _cluster(monkeypatch, existentes={"plataforma-embeddings-textos": [("T1", "UNDEPLOYED")],
                                                "plataforma-embeddings-consultas": [("Q1", "DEPLOYED")]})
    assert main._preparar_los_modelos("http://x", "a", "p", "")[0] == {"passage": "T1", "query": "Q1"}
    assert ("POST", "http://x/_plugins/_ml/models/T1/_deploy", None) in pedidos
    assert not _creados(pedidos, "/connectors/_create")


def test_sin_clave_no_hace_nada(monkeypatch):
    pedidos = _cluster(monkeypatch, clave="")
    assert main._preparar_los_modelos("http://x", "a", "p", "") == ({}, main.SIN_CLAVE_DE_NVIDIA)
    assert not pedidos and "⚙ Configuración" in main.SIN_CLAVE_DE_NVIDIA


def test_con_otra_clave_los_rehace(monkeypatch):
    """El conector lleva la clave: si cambió, el viejo seguiría con la anterior."""
    pedidos = _cluster(monkeypatch, existentes={"plataforma-embeddings-textos": [("T1", "DEPLOYED")],
                                                "plataforma-embeddings-consultas": [("Q1", "DEPLOYED")]})
    ids, motivo = main._preparar_los_modelos("http://x", "a", "p", "otra-huella")
    assert motivo == "" and ids["passage"] == "M-plataforma-embeddings-textos"
    borrados = [u for m, u, _ in pedidos if m == "DELETE"]
    assert "http://x/_plugins/_ml/models/T1" in borrados and "http://x/_plugins/_ml/connectors/CX-T1" in borrados
    assert len(_creados(pedidos, "/connectors/_create")) == 2


def test_si_nvidia_no_responde_lo_dice(monkeypatch):
    _cluster(monkeypatch, predict_ok=False)
    ids, motivo = main._preparar_los_modelos("http://x", "a", "p", "")
    assert ids == {} and "no obtuvo un vector de NVIDIA" in motivo and "ruta del CSS" in motivo


def test_si_no_se_puede_leer_no_crea_otro(monkeypatch):
    pedidos = _cluster(monkeypatch, existentes={"plataforma-embeddings-textos": [("T1", None)]})
    ids, motivo = main._preparar_los_modelos("http://x", "a", "p", "")
    assert ids == {} and "no se crea otra copia" in motivo and not _creados(pedidos, "/connectors/_create")


# ── La salida del cluster a NVIDIA ──────────────────────────────────────────
def test_la_salida_a_nvidia_son_dos_rutas_y_un_destino(monkeypatch, tmp_path):
    rutas, pedidos = [], []
    monkeypatch.setattr(main, "_cluster_hwc_creds", lambda td: ("AK", "SK"))
    monkeypatch.setattr(main, "get_huawei_project_id", lambda: "PID")
    monkeypatch.setattr(main, "_add_css_cluster_routes", lambda cid, ak, sk, pid, ips=None: rutas.append((cid, ips)) or {})
    monkeypatch.setattr(main, "_os_req", lambda m, url, u, p, json_body=None, **k: pedidos.append((m, url, json_body)))
    main._salida_original({"id": "CL1"}, "http://x", "a", "p", tmp_path)
    assert rutas == [("CL1", ["75.2.113.119", "99.83.136.103"])], "solo las IPs de NVIDIA: no se abre internet"
    (m, url, cuerpo), = pedidos
    destinos = cuerpo["persistent"]["plugins.ml_commons.trusted_connector_endpoints_regex"]
    assert url == "http://x/_cluster/settings" and destinos == capabilities.destinos_permitidos()


def test_maas_y_nvidia_siempre_juntos():
    """Una lista con un solo destino pisaba al otro al reprovisionar."""
    destinos = capabilities.build_cluster_settings()["persistent"]["plugins.ml_commons.trusted_connector_endpoints_regex"]
    assert any("modelarts-maas" in d for d in destinos) and busqueda.DESTINO_PERMITIDO in destinos
    assert busqueda.DESTINO_PERMITIDO == r"^https://integrate\.api\.nvidia\.com/.*$"


# ── La clave, en Configuración ──────────────────────────────────────────────
def test_la_clave_se_guarda_cifrada_y_no_vuelve(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(maas_integrator, "_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    c = TestClient(main.app)
    assert c.get("/api/v1/settings/nvidia").json()["configured"] is False
    assert c.post("/api/v1/settings/nvidia", json={"api_key": CLAVE}).json() == {"configured": True, "source": "settings"}
    estado = c.get("/api/v1/settings/nvidia").json()
    assert estado["masked"] == "••••1234" and CLAVE not in json.dumps(estado)
    assert c.post("/api/v1/settings/nvidia", json={"api_key": "con espacios no"}).status_code == 400
    assert c.post("/api/v1/settings/nvidia", json={"api_key": ""}).json()["configured"] is False


def test_probar_la_clave(monkeypatch):
    import requests
    monkeypatch.setattr(maas_integrator, "get_nvidia_api_key", lambda: CLAVE)
    monkeypatch.setattr(requests, "post", lambda url, **k: _R(200, {"data": [{"embedding": [0.0] * 2048}]}))
    assert maas_integrator.probar_nvidia_api_key() == {"ok": True, "detail": "Funciona: nvidia/nemotron-3-embed-1b devolvió un vector de 2048 dimensiones."}
    monkeypatch.setattr(requests, "post", lambda url, **k: _R(401, {"error": "Unauthorized"}))
    assert "rechazó la API key" in maas_integrator.probar_nvidia_api_key()["detail"]
    monkeypatch.setattr(maas_integrator, "get_nvidia_api_key", lambda: "")
    assert not maas_integrator.probar_nvidia_api_key()["ok"]


# ── La tarjeta y el candado ─────────────────────────────────────────────────
def test_la_tarjeta_del_modelo():
    sin = {t["plugin"]: t for t in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="")}["embeddings"]
    assert sin["titulo"] == "Modelo de embeddings" and sin["estado"] == pv.SIN_PROBAR and "API key de NVIDIA" in sin["que"]
    ok = {t["plugin"]: t for t in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="",
                                                          embeddings={"ok": True, "dimensiones": 2048})}["embeddings"]
    assert ok["estado"] == pv.OK and "2048 dimensiones" in ok["que"] and "viajan a NVIDIA" in ok["que"]
    mal = {t["plugin"]: t for t in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="",
                                                           embeddings={"ok": False, "motivo": "sin salida"})}["embeddings"]
    assert mal["estado"] == pv.FALLA and mal["motivo"].startswith("sin salida: se reintenta con «Completar»")
    assert not sin.get("accion") and not ok.get("accion") and not mal.get("accion")


def test_el_modelo_no_tiene_boton_propio():
    import pathlib
    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "probarEmbeddings" not in html and "probar-embeddings" not in html
    assert "probar-embeddings" not in pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert "&& (t.estado !== 'ok' || t.plugin === 'query_insights'));" in html


def test_ya_no_hay_modelo_local():
    """Ni zip en el bucket ni descarga: el CSS no lo puede correr."""
    import pathlib
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    for viejo in ("_modelo_en_obs", "_subir_el_modelo", "clave_en_obs", "TORCH_SCRIPT", "artifacts.opensearch.org"):
        assert viejo not in src, viejo
    assert not hasattr(busqueda, "MODELO")


def test_uno_a_la_vez(monkeypatch, tmp_path):
    _cluster(monkeypatch)
    assert main._preparando_el_modelo.acquire(blocking=False)
    try:
        assert main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path) == ({}, main.MODELO_OCUPADO)
    finally:
        main._preparando_el_modelo.release()


def test_la_provision_espera_al_que_lo_esta_preparando(monkeypatch, tmp_path):
    import threading
    _cluster(monkeypatch, existentes={"plataforma-embeddings-textos": [("T1", "DEPLOYED")],
                                      "plataforma-embeddings-consultas": [("Q1", "DEPLOYED")]})
    assert main._preparando_el_modelo.acquire(blocking=False)
    threading.Timer(0.2, main._preparando_el_modelo.release).start()
    assert main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path, espera_s=5) == ({"passage": "T1", "query": "Q1"}, "")


def test_mientras_se_prepara_la_tarjeta_dice_en_curso(monkeypatch, tmp_path):
    vistos = []
    _cluster(monkeypatch)
    real = main._preparar_los_modelos
    monkeypatch.setattr(main, "_preparar_los_modelos", lambda *a: vistos.append(
        (main._read_estados(tmp_path).get("_cluster") or {}).get("embeddings")) or real(*a))
    main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path)
    assert vistos[0]["en_curso"] > 0
    cluster = lambda e: {t["plugin"]: t for t in pv.tarjetas_del_cluster(  # noqa: E731
        agente=False, text2viz={}, base="", embeddings=e)}["embeddings"]
    en_curso = cluster({"ok": False, "motivo": "lo de antes", "en_curso": int(main.time.time())})
    assert en_curso["estado"] == pv.EN_CURSO and "se está preparando" in en_curso["motivo"]
    cortada = cluster({"ok": True, "en_curso": int(main.time.time()) - 3 * 3600})
    assert cortada["estado"] == pv.FALLA and "se interrumpió" in cortada["motivo"] and "«Completar»" in cortada["motivo"]


def test_ocupado_no_pisa_el_estado_con_un_fallo(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_asegurar_modelo_de_embeddings", lambda *a, **k: ({}, main.MODELO_OCUPADO))
    main._guardar_estados(tmp_path, "_cluster", {"embeddings": {"en_curso": 123}})
    pasos = main.runs.start("capabilities")
    pipe = {"a": {"fields": [{"field_path": "msg", "type": "text"}]}}
    main._busqueda_original({"public_endpoint": "x:9200"}, "a", "p", False, tmp_path, ["a"], pipe, False, pasos)
    assert "a" not in main._read_estados(tmp_path)
    assert main._read_estados(tmp_path)["_cluster"]["embeddings"] == {"en_curso": 123}


# ── Subir archivos grandes a OBS (lo usa cualquier dataset pesado) ──────────
def test_la_subida_grande_reintenta_desde_donde_quedo(monkeypatch, tmp_path):
    import obs_client
    llamadas = []

    class _Resp:
        def __init__(self, status):
            self.status, self.errorMessage = status, "x"

    class _SDK:
        def uploadFile(self, bucket, key, ruta, **kw):
            llamadas.append(kw)
            if len(llamadas) == 1:
                raise OSError("getaddrinfo failed")
            return _Resp(500 if len(llamadas) == 2 else 200)

    c = obs_client.OBSClient.__new__(obs_client.OBSClient)
    c._client, c._bucket = _SDK(), "b"
    c.put_file_grande("k", str(tmp_path / "m.zip"), espera_s=0)
    assert len(llamadas) == 3 and all(k["enableCheckpoint"] and k["checkpointFile"].endswith(".subida") for k in llamadas)
    llamadas.clear()
    _SDK.uploadFile = lambda self, *a, **kw: (_ for _ in ()).throw(OSError("sin red"))
    with pytest.raises(obs_client.OBSUploadError, match="4 intentos"):
        c.put_file_grande("k", str(tmp_path / "m.zip"), espera_s=0)
