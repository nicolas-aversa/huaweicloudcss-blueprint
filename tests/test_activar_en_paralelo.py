"""Activar los plugins de a varios casos a la vez, sin perder nada del registro.

Pronósticos, anomalías y alertas tardaban unos 30 s por caso, de a uno. Ahora
van de a `_CASOS_EN_PARALELO`. El registro de ids (`.capabilities.json`) lo
escriben todos: cada caso leía el registro al empezar y lo escribía entero, así
que pisaba lo que otro guardó mientras tanto (y el agente, que se asigna a todos
en el medio).
"""
import contextvars
import json
import random
import threading
import time

from fastapi.testclient import TestClient

import main


def test_los_casos_guardan_a_la_vez_sin_pisarse(tmp_path):
    slugs = ["a", "b", "c", "d"]

    def un_caso(slug):
        antes, ids = {}, {"viejo": 1}
        main._guardar_ids_del_caso(tmp_path, slug, antes, ids)
        for i in range(15):
            ids[f"k{i}"] = i
            if i == 7:
                ids.pop("viejo")
            time.sleep(random.random() / 500)
            main._guardar_ids_del_caso(tmp_path, slug, antes, ids)

    hilos = [threading.Thread(target=un_caso, args=(s,)) for s in slugs]
    for h in hilos:
        h.start()
    # Mientras tanto, el agente se asigna a todos los casos (como `_registrar_agente`).
    for _ in range(10):
        with main._REGISTRO_LOCK:
            reg = main._read_capabilities(tmp_path)
            for s in reg:
                reg[s]["agent_id"] = "AG"
            main._write_capabilities(tmp_path, reg)
        time.sleep(0.002)
    for h in hilos:
        h.join()
    with main._REGISTRO_LOCK:
        reg = main._read_capabilities(tmp_path)
        for s in reg:
            reg[s]["agent_id"] = "AG"
        main._write_capabilities(tmp_path, reg)
    final = main._read_capabilities(tmp_path)
    for s in slugs:
        assert {f"k{i}" for i in range(15)} <= set(final[s]), (s, final[s])
        assert "viejo" not in final[s], "lo que el caso sacó, sale"
        assert final[s]["agent_id"] == "AG"


def test_lo_que_guarda_un_caso_no_borra_el_agente_que_se_asigno_despues(tmp_path):
    antes, ids = {}, {"detector_id": "D"}
    main._guardar_ids_del_caso(tmp_path, "a", antes, ids)
    main._write_capabilities(tmp_path, {"a": {**main._read_capabilities(tmp_path)["a"], "agent_id": "AG"}})
    ids["forecaster_ids"] = ["F"]
    main._guardar_ids_del_caso(tmp_path, "a", antes, ids)
    assert main._read_capabilities(tmp_path)["a"] == {"detector_id": "D", "agent_id": "AG", "forecaster_ids": ["F"]}


_PEDIDO = contextvars.ContextVar("pedido", default="sin contexto")


def _endpoint(monkeypatch, agente=""):
    vistos, en_curso, maximo = [], [0], [0]
    traba = threading.Lock()
    registro = {s: {"index": f"{s}-%{{+YYYY.MM}}", "fields": [{"field_path": "x", "type": "keyword"}]}
                for s in ("a", "b", "c", "d")}
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_read_pipelines_registry", lambda td: json.loads(json.dumps(registro)))
    monkeypatch.setattr(main, "_write_pipelines_registry", lambda *a: None)
    monkeypatch.setattr(main, "_esperar_cluster_libre", lambda *a: None)

    def un_caso(c, slug, *a, **k):
        with traba:
            en_curso[0] += 1
            maximo[0] = max(maximo[0], en_curso[0])
        vistos.append((slug, _PEDIDO.get()))
        time.sleep(0.15)
        with traba:
            en_curso[0] -= 1
        return {"forecast": {"ok": True, "reason": "ok"}}

    monkeypatch.setattr(main, "_provision_capabilities", un_caso)
    # Lo primero del pedido, en su hilo: ahí se fija el contexto.
    monkeypatch.setattr(main, "_registrar_capacidades", lambda *a, **k: _PEDIDO.set("el pedido") and "")
    monkeypatch.setattr(main, "_registrar_agente_del_run", lambda *a, **k: agente)
    for f in ("_asegurar_ppl_v3", "_revisar_meses_de_seguridad", "_provisionar_la_busqueda", "_provisionar_canal",
              "_provisionar_perfil", "_provisionar_analista", "_provisionar_reporte", "_provisionar_el_paso_del_tiempo",
              "_rearmar_el_agente_si_cambio", "_ajustar_tiempo_de_dashboards", "_guardar_estados"):
        monkeypatch.setattr(main, f, lambda *a, **k: {"ok": True, "reason": "ok"})
    return vistos, maximo


def test_los_casos_van_de_a_varios_con_el_contexto_del_pedido(monkeypatch):
    vistos, maximo = _endpoint(monkeypatch)
    r = TestClient(main.app).post("/api/v1/onboarding/provision-capabilities", json={
        "opensearch_password": "pw", "slugs": ["a", "b", "c", "d"], "plugins": ["forecasting"]})
    assert r.status_code == 200
    assert sorted(s for s, _ in vistos) == ["a", "b", "c", "d"]
    assert maximo[0] == main._CASOS_EN_PARALELO == 2
    assert {ctx for _, ctx in vistos} == {"el pedido"}, "cada hilo, con el contexto del pedido (el usuario, su key)"


def test_sin_el_asistente_armado_van_de_a_uno(monkeypatch):
    """Cada caso crearía los modelos a la vez que el otro."""
    vistos, maximo = _endpoint(monkeypatch, agente="")
    TestClient(main.app).post("/api/v1/onboarding/provision-capabilities", json={
        "opensearch_password": "pw", "slugs": ["a", "b", "c", "d"], "recomendados": True})
    assert len(vistos) >= 4 and maximo[0] == 1
    vistos, maximo = _endpoint(monkeypatch, agente="AG")
    TestClient(main.app).post("/api/v1/onboarding/provision-capabilities", json={
        "opensearch_password": "pw", "slugs": ["a", "b", "c", "d"], "recomendados": True})
    assert maximo[0] == 2, "con el asistente armado, de a dos"
