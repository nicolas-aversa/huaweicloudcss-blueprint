"""Campañas del SIEM (event.campaign): el dataset las trae con espacios de más
y el filtro de Logstash las limpia. La línea de tiempo que se mostraba en la
plataforma salió: las campañas se ven en Dashboards (correlaciones)."""
import verticals


def test_el_filtro_limpia_la_campana():
    f = verticals.get_vertical("siem")["filter_code"]
    assert 'strip => ["[event][campaign]", "[event][campaign_name]"]' in f
    assert "\\n" not in f.split("strip =>")[0][-200:]
