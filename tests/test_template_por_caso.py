"""El index template de cada caso se llama como el caso, también desplegado de
a uno, y el de antes del mismo caso (con el nombre del proyecto solo) se
reemplaza en vez de hacer fallar el template nuevo."""
import json

import main

CAMPOS = [{"raw_name": "ip", "field_path": "source.ip", "type": "ip"}]
CHOQUE = ('{"error":{"root_cause":[{"type":"illegal_argument_exception","reason":"index template '
          '[log-analytics-peaje] has index patterns [peaje-*] matching patterns from existing templates '
          '[%s] with patterns (%s => [peaje-*]) that have the same priority [0]"}]},"status":400}')


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text or json.dumps(body or {})

    def json(self):
        return self._body


class _Cluster:
    """Un cluster con templates: el PUT choca si otro tiene los mismos patrones."""
    def __init__(self, templates):
        self.templates = dict(templates)
        self.borrados = []

    def put(self, url, **kw):
        nombre = url.rsplit("/", 1)[-1]
        pats = set(kw["json"].get("index_patterns") or [])
        otros = [n for n, p in self.templates.items() if n != nombre and set(p) & pats]
        if otros:
            return _Resp(400, text=CHOQUE % (", ".join(otros), otros[0]))
        self.templates[nombre] = sorted(pats)
        return _Resp(200, {"acknowledged": True})

    def get(self, url, **kw):
        n = url.rsplit("/", 1)[-1]
        if n not in self.templates:
            return _Resp(404, {})
        return _Resp(200, {"index_templates": [{"name": n, "index_template": {"index_patterns": self.templates[n]}}]})

    def delete(self, url, **kw):
        n = url.rsplit("/", 1)[-1]
        self.borrados.append(n)
        self.templates.pop(n, None)
        return _Resp(200, {"acknowledged": True})


def _aplicar(monkeypatch, cluster, **kw):
    monkeypatch.setattr("requests.put", cluster.put)
    monkeypatch.setattr("requests.get", cluster.get)
    monkeypatch.setattr("requests.delete", cluster.delete)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    req = main.TerraformDeployRequest(pipeline_conf="x", project_name="log-analytics", opensearch_password="pw",
                                      **kw)
    return main._apply_index_templates(req, {"public_endpoint": "1.2.3.4:9200"})


def test_desplegado_solo_el_template_lleva_el_nombre_del_caso(monkeypatch):
    c = _Cluster({"log-analytics-regalos": ["regalos-*"]})
    assert _aplicar(monkeypatch, c, pipeline_slug="peaje", opensearch_index="peaje-%{+YYYY.MM}", fields=CAMPOS)
    assert c.templates == {"log-analytics-regalos": ["regalos-*"], "log-analytics-peaje": ["peaje-*"]}, \
        "el de otro caso no se pisa"


def test_el_template_de_antes_del_mismo_caso_se_reemplaza(monkeypatch):
    c = _Cluster({"log-analytics": ["peaje-*"]})
    caso = main.PipelineCase(slug="peaje", filter_code="filter {}", index_name="peaje-%{+YYYY_MM}", fields=CAMPOS)
    assert _aplicar(monkeypatch, c, cases=[caso])
    assert c.borrados == ["log-analytics"] and c.templates == {"log-analytics-peaje": ["peaje-*"]}


def test_uno_ajeno_o_con_otros_patrones_no_se_toca(monkeypatch):
    caso = main.PipelineCase(slug="peaje", filter_code="filter {}", index_name="peaje-%{+YYYY.MM}", fields=CAMPOS)
    ajeno = _Cluster({"del-cliente": ["peaje-*"]})
    assert not _aplicar(monkeypatch, ajeno, cases=[caso]) and ajeno.borrados == []
    mas_amplio = _Cluster({"log-analytics": ["peaje-*", "otro-*"]})
    assert not _aplicar(monkeypatch, mas_amplio, cases=[caso]) and mas_amplio.borrados == []
