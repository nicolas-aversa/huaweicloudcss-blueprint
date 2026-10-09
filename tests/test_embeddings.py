"""El modelo de embeddings de la búsqueda híbrida. El CSS no sale a internet
(registrar el preentrenado por nombre daba "Connection timed out" a
artifacts.opensearch.org): la plataforma lo sube una vez al bucket de demos
y el cluster lo registra desde un link firmado de OBS."""
import hashlib
import json

import pytest

import busqueda
import main
import plugins_vista as pv


class _R:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data if data is not None else {}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


def _cluster(monkeypatch, tmp_path, registro="COMPLETED", deploy="COMPLETED", estados_modelo=None, previos=()):
    """Un cluster de ml-commons falso. `estados_modelo`: el `model_state` de
    cada modelo; los registrados acá quedan DEPLOYED si el deploy completa."""
    pedidos = []
    estados_modelo = dict(estados_modelo or {})

    def req(m, url, user, password, json_body=None, timeout=30, headers=None):
        pedidos.append((m, url, json_body))
        if url.endswith("/_plugins/_ml/models/_register"):
            return _R(200, {"task_id": "T1"})
        if url.endswith("/_deploy"):
            return _R(200, {"task_id": "T2"})
        if url.endswith("/tasks/T1"):
            return _R(200, {"state": registro, "model_id": "M1", "error": "Connection timed out: obs"})
        if url.endswith("/tasks/T2"):
            if deploy == "COMPLETED":
                estados_modelo["M1"] = "DEPLOYED"
                for k in list(estados_modelo):
                    if estados_modelo[k] == "REGISTERED":
                        estados_modelo[k] = "DEPLOYED"
            return _R(200, {"state": deploy, "error": "Native Memory Circuit Breaker is open"})
        if "/_plugins/_ml/models/" in url and m == "GET":
            mid = url.rsplit("/", 1)[-1]
            if mid in estados_modelo and estados_modelo[mid] is None:
                return None                                   # el cluster no contestó
            return _R(200, {"model_state": estados_modelo.get(mid, "")}) if mid in estados_modelo else _R(404)
        return _R(200)

    monkeypatch.setattr(main, "_os_req", req)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "_modelos_por_nombre", lambda *a, **k: list(previos))
    monkeypatch.setattr(main, "_modelo_en_obs", lambda: ("https://obs/firmado", ""))
    return pedidos


def test_lo_registra_desde_obs_y_lo_despliega(monkeypatch, tmp_path):
    pedidos = _cluster(monkeypatch, tmp_path)
    assert main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path) == ("M1", "")
    assert ("PUT", "http://x/_cluster/settings", busqueda.ajustes_del_cluster()) in pedidos, "registrar desde un link"
    registro = next(b for m, u, b in pedidos if u.endswith("/_register"))
    assert registro["url"] == "https://obs/firmado" and registro["model_content_hash_value"] == busqueda.MODELO["hash"]
    assert registro["model_config"]["embedding_dimension"] == 384
    assert not [u for m, u, _ in pedidos if m == "DELETE"], "se queda: es el de la búsqueda"


def test_reusa_el_que_ya_esta(monkeypatch, tmp_path):
    pedidos = _cluster(monkeypatch, tmp_path, estados_modelo={"VIEJO": "DEPLOYED"}, previos=["VIEJO"])
    assert main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path) == ("VIEJO", "")
    assert not [u for _, u, _ in pedidos if u.endswith("/_register")], "no lo vuelve a subir ni registrar"
    # Registrado pero sin desplegar (un reinicio del nodo): solo se despliega.
    pedidos = _cluster(monkeypatch, tmp_path, estados_modelo={"R": "REGISTERED"}, previos=["R"])
    assert main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path) == ("R", "")
    assert ("POST", "http://x/_plugins/_ml/models/R/_deploy", None) in pedidos
    assert not [u for _, u, _ in pedidos if u.endswith("/_register")]


@pytest.mark.parametrize("kw, motivo", [
    ({"registro": "FAILED"}, "el cluster no pudo cargar el modelo desde OBS (Connection timed out: obs)"),
    ({"deploy": "FAILED"}, "se cargó pero no se pudo desplegar: Native Memory Circuit Breaker is open"),
])
def test_si_no_anda_dice_por_que(monkeypatch, tmp_path, kw, motivo):
    _cluster(monkeypatch, tmp_path, **kw)
    modelo, dice = main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path)
    assert modelo == "" and motivo in dice


def test_sin_el_modelo_en_obs_no_registra(monkeypatch, tmp_path):
    pedidos = _cluster(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_modelo_en_obs", lambda: ("", "faltan AK/SK o el bucket de demos en ⚙ Configuración"))
    assert main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path) == \
        ("", "faltan AK/SK o el bucket de demos en ⚙ Configuración")
    assert not [u for _, u, _ in pedidos if u.endswith("/_register")]


class _OBS:
    def __init__(self, existe):
        self.existe, self.subidos = existe, []

    def __call__(self, **kw):
        return self

    def object_exists(self, clave):
        return self.existe

    def put_file(self, clave, ruta):
        with open(ruta, "rb") as f:
            self.subidos.append((clave, f.read()))

    def signed_url(self, clave, expires_s=0):
        return f"https://obs/{clave}?firmado"

    def close(self):
        pass


class _Descarga:
    def __init__(self, contenido):
        self.contenido = contenido

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        yield self.contenido


def _obs(monkeypatch, existe, contenido=b"modelo"):
    import maas_integrator
    import obs_client
    import requests
    obs = _OBS(existe)
    bajadas = []
    monkeypatch.setattr(obs_client, "OBSClient", obs)
    monkeypatch.setattr(maas_integrator, "get_obs_creds", lambda: {"ak": "AK", "sk": "SK"})
    monkeypatch.setattr(main, "get_huawei_settings", lambda: {"demo_bucket": "demos"})
    monkeypatch.setattr(requests, "get", lambda url, **k: bajadas.append(url) or _Descarga(contenido))
    return obs, bajadas


def test_en_obs_se_sube_una_vez_y_con_su_hash(monkeypatch):
    obs, bajadas = _obs(monkeypatch, existe=True)
    url, motivo = main._modelo_en_obs()
    assert (url, motivo) == (f"https://obs/{busqueda.clave_en_obs()}?firmado", "") and not bajadas, "ya estaba: no se baja"
    obs, bajadas = _obs(monkeypatch, existe=False, contenido=b"modelo")
    monkeypatch.setitem(busqueda.MODELO, "hash", hashlib.sha256(b"modelo").hexdigest())
    url, motivo = main._modelo_en_obs()
    assert motivo == "" and bajadas == [busqueda.MODELO["origen"] + busqueda.MODELO["archivo"]]
    assert obs.subidos == [(busqueda.clave_en_obs(), b"modelo")]
    obs, _ = _obs(monkeypatch, existe=False, contenido=b"otra cosa")
    url, motivo = main._modelo_en_obs()
    assert url == "" and "otro hash" in motivo and not obs.subidos, "uno incompleto no se sube"


def test_el_endpoint_lo_prepara_y_guarda_el_estado(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_cluster_del_entorno", lambda stage: ("http://x", "a", "p"))
    monkeypatch.setattr(main, "_asegurar_modelo_de_embeddings", lambda b, u, p, td: ("", "sin salida a OBS"))
    r = TestClient(main.app).post("/api/v1/plugins/probar-embeddings").json()
    assert not r["ok"] and r["reason"] == "sin salida a OBS"
    assert main._read_estados(tmp_path)["_cluster"]["embeddings"] == {"ok": False, "motivo": "sin salida a OBS",
                                                                     "model_id": "", "dimensiones": 0}


def test_la_tarjeta_del_modelo():
    sin = {t["plugin"]: t for t in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="")}["embeddings"]
    assert sin["titulo"] == "Modelo de embeddings" and sin["estado"] == pv.SIN_PROBAR
    assert sin["accion"] == {"id": "probar_embeddings", "texto": "Prepararlo ahora"}
    ok = {t["plugin"]: t for t in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="",
                                                          embeddings={"ok": True, "dimensiones": 384})}["embeddings"]
    assert ok["estado"] == pv.OK and "384 dimensiones" in ok["que"] and ok["accion"]["texto"] == "Volver a prepararlo"
    mal = {t["plugin"]: t for t in pv.tarjetas_del_cluster(agente=False, text2viz={}, base="",
                                                           embeddings={"ok": False, "motivo": "sin salida"})}["embeddings"]
    assert (mal["estado"], mal["motivo"]) == (pv.FALLA, "sin salida")


def test_el_boton_en_la_tarjeta():
    import pathlib
    html = (pathlib.Path(main.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "sin_probar: ['Sin probar', 'sev--low']" in html
    assert 'class="btn btn-secondary btn-sm plug__accion" data-accion="${escapeHtml(t.accion.id)}"' in html
    assert "if (accion) return probarEmbeddings(accion);" in html
    assert "fetch('/api/v1/plugins/probar-embeddings', { method: 'POST' })" in html


def test_si_no_se_puede_leer_o_se_esta_cargando_no_registra_otro(monkeypatch, tmp_path):
    """Con el cluster cargado, un GET sin respuesta se tomaba como "no hay
    modelo" y se registraba otra copia de ~490 MB al lado del que andaba."""
    monkeypatch.setattr(main, "_read_estados", lambda td: {"_cluster": {"embeddings": {"model_id": "ANDABA"}}})
    pedidos = _cluster(monkeypatch, tmp_path, estados_modelo={"ANDABA": None})
    modelo, motivo = main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path)
    assert modelo == "" and "no se registra otra copia" in motivo
    assert not [u for _, u, _ in pedidos if u.endswith("/_register")]
    pedidos = _cluster(monkeypatch, tmp_path, estados_modelo={"C": "DEPLOYING"}, previos=["C"])
    modelo, motivo = main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path)
    assert modelo == "" and "se está cargando (deploying)" in motivo
    assert not [u for _, u, _ in pedidos if u.endswith(("/_register", "/_deploy"))]


def test_uno_a_la_vez(monkeypatch, tmp_path):
    _cluster(monkeypatch, tmp_path)
    assert main._preparando_el_modelo.acquire(blocking=False)
    try:
        assert main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path) ==             ("", "el modelo ya se está preparando (otro pedido): en unos minutos queda")
    finally:
        main._preparando_el_modelo.release()


def test_el_motivo_no_lleva_el_link_firmado(monkeypatch, tmp_path):
    """El error de ml-commons repite la URL entera (con el AccessKeyId y la
    firma) y el motivo va a Actividad, a la auditoría y a la pantalla."""
    pedidos = _cluster(monkeypatch, tmp_path, registro="FAILED")
    error = ("Server returned HTTP response code: 403 for URL: "
             "https://demos.obs.la-south-2.myhuaweicloud.com/css-demos/modelos/m.zip?AccessKeyId=AKSECRETO&Signature=abc")
    main_req = main._os_req
    monkeypatch.setattr(main, "_os_req", lambda m, url, *a, **k:
                        _R(200, {"state": "FAILED", "error": error}) if url.endswith("/tasks/T1") else main_req(m, url, *a, **k))
    modelo, motivo = main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path)
    assert modelo == "" and "<link de OBS>" in motivo and "AKSECRETO" not in motivo and "Signature" not in motivo


def test_la_provision_espera_al_que_lo_esta_preparando(monkeypatch, tmp_path):
    """"Prepararlo ahora" y después "Aplicar los recomendados": la provisión
    llegaba a la búsqueda con el modelo todavía bajándose y marcaba como
    fallida la de cada caso. Ahora lo espera y lo encuentra desplegado."""
    import threading
    _cluster(monkeypatch, tmp_path, estados_modelo={"YA": "DEPLOYED"}, previos=["YA"])
    assert main._preparando_el_modelo.acquire(blocking=False)
    threading.Timer(0.2, main._preparando_el_modelo.release).start()
    assert main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path, espera_s=5) == ("YA", "")
    # Sin espera (el botón), dice que lo prepara otro.
    assert main._preparando_el_modelo.acquire(blocking=False)
    try:
        assert main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path) == ("", main.MODELO_OCUPADO)
    finally:
        main._preparando_el_modelo.release()


def test_mientras_se_prepara_la_tarjeta_dice_en_curso(monkeypatch, tmp_path):
    vistos = []
    _cluster(monkeypatch, tmp_path)
    real = main._preparar_el_modelo
    monkeypatch.setattr(main, "_preparar_el_modelo", lambda *a: vistos.append(
        (main._read_estados(tmp_path).get("_cluster") or {}).get("embeddings")) or real(*a))
    main._asegurar_modelo_de_embeddings("http://x", "a", "p", tmp_path)
    assert vistos[0]["en_curso"] > 0
    cluster = lambda e: {t["plugin"]: t for t in pv.tarjetas_del_cluster(  # noqa: E731
        agente=False, text2viz={}, base="", embeddings=e)}["embeddings"]
    en_curso = cluster({"ok": False, "motivo": "lo de antes", "en_curso": int(main.time.time())})
    assert en_curso["estado"] == pv.EN_CURSO and "se está preparando" in en_curso["motivo"] and not en_curso.get("accion")
    cortada = cluster({"ok": True, "en_curso": int(main.time.time()) - 3 * 3600})
    assert cortada["estado"] == pv.FALLA and "se interrumpió" in cortada["motivo"] and cortada["accion"]


def test_ocupado_no_pisa_el_estado_con_un_fallo(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_active_terraform_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_cluster_del_entorno", lambda stage: ("http://x", "a", "p"))
    monkeypatch.setattr(main, "_asegurar_modelo_de_embeddings", lambda *a, **k: ("", main.MODELO_OCUPADO))
    main._guardar_estados(tmp_path, "_cluster", {"embeddings": {"en_curso": 123}})
    r = TestClient(main.app).post("/api/v1/plugins/probar-embeddings").json()
    assert r["en_curso"] and not r["ok"]
    assert main._read_estados(tmp_path)["_cluster"]["embeddings"] == {"en_curso": 123}, "lo deja el otro pedido"
    # La provisión, si después de esperar sigue ocupado: ni el modelo ni la búsqueda fallaron.
    pasos = main.runs.start("capabilities")
    pipe = {"a": {"fields": [{"field_path": "msg", "type": "text"}]}}
    main._busqueda_original({"public_endpoint": "x:9200"}, "a", "p", False, tmp_path, ["a"], pipe, False, pasos)
    assert "a" not in main._read_estados(tmp_path)
    assert main._read_estados(tmp_path)["_cluster"]["embeddings"] == {"en_curso": 123}
