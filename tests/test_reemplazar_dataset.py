"""Reemplazar el dataset de un caso por otro con la misma estructura.

Un SA armó su caso, le corrigió dashboards y los guardó; después consiguió
mejores datos (los primeros eran "muy lineales"). Rehacer el caso lo vuelve a
pasar por la IA y los nombres pueden cambiar: lo guardado deja de encontrar sus
visualizaciones. Esto le cambia SOLO el archivo, si tiene la misma estructura.
"""
import json

import pytest
from fastapi.testclient import TestClient

import auth
import custom_cases
import dashboards_guardados as dg
import main


def _jsonl(n, demanda=lambda i: 100 + i, extra_desde=None, **cambios):
    filas = []
    for i in range(n):
        fila = {"ts": f"2026-10-{1 + i % 28:02d}T{i % 24:02d}:00:00Z", "zona": ["Córdoba", "Rosario"][i % 2],
                "demanda_mw": demanda(i), "carga_pct": 40 + i % 50}
        if extra_desde is not None and i >= extra_desde:
            fila["evento"] = "pico"
        for k, v in cambios.items():
            if v is None:
                fila.pop(k, None)
            else:
                fila[k] = v(i) if callable(v) else v
        filas.append(json.dumps(fila, ensure_ascii=False))
    return "\n".join(filas) + "\n"


ACTUAL = _jsonl(300, demanda=lambda i: 100.0 + i * 0.5)           # "muy lineal"


# ── La comparación ──────────────────────────────────────────────────────────
def test_mismos_campos_con_otros_valores_es_compatible():
    nuevo = _jsonl(900, demanda=lambda i: 80.0 + (i % 24) * 3.7 + (i % 7) * 1.3)
    comp = custom_cases.comparar_estructura(ACTUAL, nuevo)
    assert comp["verificable"] and comp["compatible"], comp


def test_un_campo_que_falta_o_sobra_no_es_compatible():
    sin_carga = custom_cases.comparar_estructura(ACTUAL, _jsonl(300, carga_pct=None))
    assert not sin_carga["compatible"] and sin_carga["faltan"] == ["carga_pct"]
    con_temp = custom_cases.comparar_estructura(ACTUAL, _jsonl(300, temperatura=lambda i: 20 + i % 10))
    assert not con_temp["compatible"] and con_temp["sobran"] == ["temperatura"]
    assert "carga_pct" in custom_cases.explicar_diferencias(sin_carga)


def test_un_numero_que_pasa_a_texto_no_es_compatible_y_lo_explica():
    comp = custom_cases.comparar_estructura(ACTUAL, _jsonl(300, carga_pct=lambda i: f"alto-{i}"))
    assert not comp["compatible"]
    assert comp["cambian"][0]["campo"] == "carga_pct"
    frase = custom_cases.explicar_diferencias(comp)
    assert "carga_pct era número entero y ahora es texto" in frase


def test_un_numero_que_ahora_trae_simbolos_no_es_compatible():
    """El perfilador lee "55%" como número, pero el filtro del caso (armado con
    números limpios) no lo limpia: esos documentos no se indexarían."""
    comp = custom_cases.comparar_estructura(ACTUAL, _jsonl(300, carga_pct=lambda i: f"{i}%"))
    assert not comp["compatible"]
    assert "carga_pct era número entero y ahora es número con símbolos" in custom_cases.explicar_diferencias(comp)


def test_lo_que_el_conf_del_caso_igual_procesa_es_compatible():
    """Una columna decimal que ahora trae enteros, o una de texto que ahora trae
    solo números: el .conf del caso las procesa igual."""
    actual = _jsonl(200, demanda=lambda i: 10.5 + i, zona=lambda i: f"Z{i}")
    nuevo = _jsonl(200, demanda=lambda i: 10 + i, zona=lambda i: str(i))
    assert custom_cases.comparar_estructura(actual, nuevo)["compatible"]


def test_un_entero_que_ahora_trae_decimales_no_es_compatible():
    comp = custom_cases.comparar_estructura(_jsonl(200, carga_pct=lambda i: i), _jsonl(200, carga_pct=lambda i: i + 0.5))
    assert [c["campo"] for c in comp["cambian"]] == ["carga_pct"]


def test_una_clave_que_aparece_recien_al_final_cuenta():
    """La muestra se reparte en todo el archivo: si solo se miraran las primeras
    líneas, `evento` (que aparece al final) daría 'sobra' o 'falta' en falso."""
    actual = _jsonl(6000, extra_desde=1000)                     # aparece en las primeras 2000
    nuevo = _jsonl(6000, extra_desde=4000, demanda=lambda i: 50 + i % 17)   # aparece recién al final
    comp = custom_cases.comparar_estructura(actual, nuevo)
    assert comp["compatible"], comp


def test_otro_formato_es_otro_caso():
    csv = "ts,zona,demanda_mw,carga_pct\n" + "\n".join(f"2026-10-01T00:00:00Z,Córdoba,{i},{i}" for i in range(50))
    comp = custom_cases.comparar_estructura(ACTUAL, csv)
    assert not comp["compatible"] and comp["formato_nuevo"] == "delimitado"
    assert "caso nuevo" in custom_cases.explicar_diferencias(comp)


def test_un_log_sin_estructura_no_se_puede_verificar():
    syslog = "\n".join(f"Oct 21 10:0{i % 10}:00 host sshd[{i}]: Accepted password for user{i}" for i in range(20))
    assert custom_cases.comparar_estructura(syslog, syslog)["verificable"] is False


def test_una_columna_vacia_en_el_nuevo_se_avisa_sin_bloquear():
    comp = custom_cases.comparar_estructura(ACTUAL, _jsonl(300, carga_pct=""))
    assert comp["compatible"] and comp["vacias"] == ["carga_pct"]


# ── El endpoint ─────────────────────────────────────────────────────────────
FIELDS = [{"raw_name": "zona", "field_path": "zona", "type": "keyword", "business_label": "Zona", "entity": True},
          {"raw_name": "demanda_mw", "field_path": "demanda_mw", "type": "float", "business_label": "Demanda (MW)"}]


@pytest.fixture
def caso(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    subidos = []
    monkeypatch.setattr(main, "_upload_case_dataset", lambda slug: (subidos.append(slug) or True, ""))
    custom_cases.save_case({"label": "Energetica", "filter_code": "filter { json { source => \"message\" } }",
                            "fields": FIELDS}, ACTUAL, created_by="matias@x.com", filename="demanda-cordoba.jsonl")
    return subidos


def _post(cuerpo):
    return TestClient(main.app).post("/api/v1/cases/energetica/dataset", json=cuerpo)


def test_reemplaza_solo_el_archivo_y_la_definicion_queda(caso):
    antes = custom_cases.get_case("energetica")
    nuevo = _jsonl(900, demanda=lambda i: 80.0 + (i % 24) * 3.7)

    r = _post({"log_content": nuevo})

    assert r.status_code == 200, r.text
    d = r.json()
    assert d["lineas_antes"] == 300 and d["case"]["lines"] == 900 and d["uploaded"] is True
    ruta = custom_cases.dataset_path("energetica")
    assert ruta.name == "demanda-cordoba.jsonl", "con otro nombre, el bucket se quedaría con los dos archivos"
    assert ruta.read_bytes() == nuevo.encode("utf-8")
    despues = custom_cases.get_case("energetica")
    for clave in ("fields", "filter_code", "label", "dataset_files", "created_by"):
        assert despues[clave] == antes[clave], clave
    assert despues["dataset_reemplazado"]["por"] == ""        # sin login no hay a quién atribuirlo
    assert caso == ["energetica"]


def test_si_la_estructura_no_coincide_no_toca_nada(caso):
    r = _post({"log_content": _jsonl(300, carga_pct=None)})
    assert r.status_code == 400
    assert r.json()["detail"]["stage"] == "dataset_incompatible"
    assert "carga_pct" in r.json()["detail"]["message"]
    assert custom_cases.dataset_path("energetica").read_text(encoding="utf-8") == ACTUAL
    assert caso == []


def test_si_no_se_puede_verificar_pide_confirmar(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(main, "_upload_case_dataset", lambda slug: (True, ""))
    log = "\n".join(f"Oct 21 10:00:0{i} host app: evento {i}" for i in range(9)) + "\n"
    custom_cases.save_case({"label": "Logs app", "filter_code": "filter { grok {} }", "fields": FIELDS}, log)
    url = "/api/v1/cases/logs-app/dataset"
    r = TestClient(main.app).post(url, json={"log_content": log + "Oct 21 10:00:09 host app: evento 9\n"})
    assert r.status_code == 409 and r.json()["detail"]["stage"] == "dataset_sin_verificar"
    r = TestClient(main.app).post(url, json={"log_content": log + "Oct 21 10:00:09 host app: evento 9\n", "forzar": True})
    assert r.status_code == 200 and r.json()["case"]["lines"] == 10


def test_solo_quien_lo_creo_o_un_admin(caso, monkeypatch):
    monkeypatch.setattr(custom_cases, "can_delete", lambda slug, email: False)
    r = _post({"log_content": _jsonl(300)})
    assert r.status_code == 403
    assert custom_cases.dataset_path("energetica").read_text(encoding="utf-8") == ACTUAL


def test_un_caso_que_no_existe(caso):
    assert TestClient(main.app).post("/api/v1/cases/no-existe/dataset", json={"log_content": "x"}).status_code == 404


def test_lo_guardado_del_caso_sigue_y_el_deploy_sube_el_archivo_nuevo(caso, monkeypatch):
    """Los dashboards guardados no dependen del archivo; y el bucket que tenía
    el viejo recibe el nuevo en el próximo deploy (el tamaño ya no coincide)."""
    dg.guardar("energetica", [{"type": "dashboard", "id": "mio", "attributes": {"title": "mio"},
                               "references": [], "titulo": "mio", "estado": "nuevo"}], "matias@x.com")
    _post({"log_content": _jsonl(900, demanda=lambda i: 80.0 + (i % 24) * 3.7)})
    assert set(dg.leer("energetica")) == {"dashboard:mio"}

    clave = "energetica-logs/demanda-cordoba.jsonl"
    subidos = []

    class _Obs:
        def __init__(self, **kw):
            pass

        def objetos_bajo(self, prefix):
            return {clave: len(ACTUAL.encode("utf-8"))}       # el viejo

        def put_file(self, key, path):
            subidos.append(key)

        def close(self):
            pass

    monkeypatch.setattr("obs_client.OBSClient", _Obs)
    main._check_demo_datasets_present(main.TerraformDeployRequest(
        pipeline_conf="input {}", obs_access_key="AK", obs_secret_key="SK", obs_bucket="b",
        obs_endpoint="https://obs.x.com", opensearch_password="pw", pipeline_slug="energetica",
        obs_prefix="energetica-logs/", read_existing_bucket=True))
    assert subidos == [clave]
