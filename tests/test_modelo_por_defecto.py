"""deepseek-v4-flash se retira de MaaS el 2026-10-08: los entornos (efímeros)
se crean con su sucesor."""
import capabilities as caps


def test_el_modelo_por_defecto_es_el_sucesor():
    assert caps.DEFAULT_MAAS_LLM_MODEL == caps.DEFAULT_MAAS_PPL_MODEL == "deepseek-v4.1-flash"
    assert caps.build_llm_connector("K")["parameters"]["model"] == "deepseek-v4.1-flash"
    assert caps.build_ppl_connector("K", "p")["parameters"]["model"] == "deepseek-v4.1-flash"


def test_los_rankings_descartan_los_vacios():
    """El "top 5 IPs" del SIEM salía encabezado por un null: los eventos sin
    source.ip. Vale para cualquier dataset, también en las tools del agente."""
    p = caps.build_ppl_system_prompt("x*", [], {"a": "b"}, ppl_v3=True)
    assert "When ranking or grouping BY a field (top N, most frequent, count by X), first filter where isnotnull(<field>)" in p
    assert "unless the user asks about missing values" in caps.build_ppl_system_prompt("x*", [], {})


def test_razona_solo_con_deepseek():
    """deepseek-v4.1-flash con razonamiento: 25/25 en el mismo tiempo. glm: de ~3 a ~8 s."""
    for armar in (lambda m: caps.build_llm_connector("K", model=m), lambda m: caps.build_ppl_connector("K", "p", model=m),
                  lambda m: caps.build_agent_connector("K", model=m)):
        assert '"chat_template_kwargs": {"thinking": true}' in armar("deepseek-v4.1-flash")["actions"][0]["request_body"]
        assert '"chat_template_kwargs": {"thinking": false}' in armar("glm-5.2")["actions"][0]["request_body"]
