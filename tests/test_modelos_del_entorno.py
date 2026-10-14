"""El chat de cada caso usa los modelos del entorno (platform-ppl y
platform-llm), que quedan anotados solo en el caso que los creó.

Visto en la ECS: de cinco casos, solo pozos tenía los ids; el chat de los
otros cuatro respondía «El chatbot no está provisionado para este tipo»
aunque el agente los consultaba."""
import main


def test_los_modelos_se_buscan_en_todo_el_entorno():
    registry = {"pozos": {"ppl_model_id": "PM", "llm_model_id": "LM", "agent_id": "A"},
                "envios": {"agent_id": "A"}, "propio": {"ppl_model_id": "P2", "llm_model_id": "L2"},
                "roto": "no es un dict"}
    assert main._modelos_del_entorno(registry, "envios") == ("PM", "LM")
    assert main._modelos_del_entorno(registry, "propio") == ("P2", "L2"), "los del caso, si los tiene"
    assert main._modelos_del_entorno(registry, "no-esta") == ("PM", "LM")
    assert main._modelos_del_entorno({"envios": {"agent_id": "A", "ppl_model_id": "PM"}}, "envios") is None
    assert main._modelos_del_entorno({}) is None


def _chat(monkeypatch, registry):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_cluster_with_public_access", lambda td: {"public_endpoint": "1.2.3.4:9200"})
    monkeypatch.setattr(main, "_read_capabilities", lambda td: registry)
    monkeypatch.setattr(main, "_resolve_capability_spec",
                        lambda *a, **k: {"index_pattern": "envios*", "operations": [], "fields": {}, "label": "Envíos"})
    monkeypatch.setattr(main, "_index_time_window", lambda *a, **k: None)
    monkeypatch.setattr(main, "_read_cluster_features", lambda td: {"ppl_v3": True})
    agente = []
    monkeypatch.setattr(main, "_conversar_con_el_agente",
                        lambda base, u, p, agent_id, req, label, indice: agente.append(agent_id)
                        or main.PplChatResponse(answer="ok"))
    r = TestClient(main.app).post("/api/v1/capabilities/ppl-chat",
                                  json={"question": "¿Cuántos envíos?", "slug": "envios", "opensearch_password": "pw"})
    return r, agente


def test_el_chat_de_un_caso_sin_los_ids_usa_los_del_entorno(monkeypatch):
    r, agente = _chat(monkeypatch, {"pozos": {"ppl_model_id": "PM", "llm_model_id": "LM", "agent_id": "A"},
                                    "envios": {"agent_id": "A"}})
    assert r.status_code == 200 and r.json()["answer"] == "ok"
    assert agente == ["A"]


def test_sin_modelos_en_ningun_caso_sigue_sin_provisionar(monkeypatch):
    r, agente = _chat(monkeypatch, {"envios": {"agent_id": "A"}})
    assert r.status_code == 400 and "no está provisionado" in r.json()["detail"]["message"]
    assert agente == []
