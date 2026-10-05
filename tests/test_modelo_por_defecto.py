"""deepseek-v4-flash se retira de MaaS el 2026-10-08: los entornos (efímeros)
se crean con su sucesor."""
import capabilities as caps


def test_el_modelo_por_defecto_es_el_sucesor():
    assert caps.DEFAULT_MAAS_LLM_MODEL == caps.DEFAULT_MAAS_PPL_MODEL == "deepseek-v4.1-flash"
    assert caps.build_llm_connector("K")["parameters"]["model"] == "deepseek-v4.1-flash"
    assert caps.build_ppl_connector("K", "p")["parameters"]["model"] == "deepseek-v4.1-flash"
