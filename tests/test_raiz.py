"""Los campos de un dataset nuevo llegan a la raíz, como en los casos de ejemplo.

Los generadores arman todo bajo `data` (lo probado contra la gramática) y un
bloque al final lo vacía en la raíz. Lo que se protege acá es que el filter y
los `fields` digan lo mismo: si divergen, el index template, el dashboard y el
chat buscan el dato donde no está.
"""
import json

import pytest
from fastapi.testclient import TestClient

import conf_lint
import main
import perfilador
import raiz

client = TestClient(main.app)

TELEMETRIA = (
    "Fecha y Hora,ID Trabajo,Cliente,Estado,Páginas Totales\n"
    "2026-09-18 11:04:12,TRB-00187,Editorial Sur,COMPLETADO,12000\n"
    "2026-09-18 11:31:55,TRB-00188,Gráfica Andes,FALLIDO,8000\n"
)


def _sin_errores(filtro: str) -> list[str]:
    conf = ('input { s3 { bucket => "b" } }\n' + filtro +
            'output { elasticsearch { hosts => [] index => "x" } }\n')
    return [str(e) for e in conf_lint.lint(conf, marcador_hosts=True)
            if e.nivel == conf_lint.ERROR]


def _no_llamar_al_llm(monkeypatch):
    def _explota(*a, **k):
        raise AssertionError("una tabla no va al LLM")
    monkeypatch.setattr(main, "generate_logstash_filter", _explota)


# ── Los nombres ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("clave, queda", [
    ("sucursal", "sucursal"),
    ("fecha_y_hora", "fecha_y_hora"),
    ("event", "event"),                       # ECS: se deja, se mergea
    ("type", "type"),
    # De Logstash: la línea cruda (se borra al final) y donde anota errores.
    ("message", "message_original"),
    ("tags", "tags_original"),
    ("@timestamp", "timestamp_original"),
    ("@version", "version_original"),
    # OpenSearch rechaza el documento ENTERO con uno de estos en la raíz.
    ("_id", "id_original"),
    ("_index", "index_original"),
    ("a[b]", "a_b_"),
])
def test_un_nombre_que_ya_es_de_otro_se_renombra(clave, queda):
    assert raiz.nombre(clave) == queda


def test_el_ruby_renombra_con_la_misma_regla():
    """Sin ruby en la máquina no se puede correr el bloque: al menos cada
    reservado y cada prefijo tienen que estar en el código que Logstash corre."""
    b = raiz.bloque()
    for r in raiz._RESERVADOS:
        assert f'"{r}"' in b, r
    assert 'start_with?("@", "_")' in b
    assert f'+ "{raiz._SUFIJO}"' in b
    assert 'tr("[]", "__")' in b
    # Un `event` o `geo` que ya esté en la raíz se mergea en vez de pisarse:
    # si no, llegar a la raíz borraba lo que otro paso del filter había puesto.
    assert "actual.merge(v) if actual.is_a?(Hash) && v.is_a?(Hash)" in b


# ── El filter y los fields ──────────────────────────────────────────────────
def test_promover_reescribe_solo_lo_que_esta_bajo_data():
    fields = [
        {"field_path": "data.sucursal", "ecs_path": "data.sucursal", "normalized_path": "data.sucursal"},
        {"field_path": "data.card.number"},
        {"field_path": "data.message"},
        {"field_path": "source.ip", "is_ecs": True},   # ECS del catálogo: ya está en la raíz
        {"field_path": "database.x"},                   # empieza con "data" pero no es `data.`
    ]
    _, nuevos = raiz.promover("filter {\n}\n", fields)

    assert [f["field_path"] for f in nuevos] == [
        "sucursal", "card.number", "message_original", "source.ip", "database.x"]
    assert nuevos[0]["ecs_path"] == nuevos[0]["normalized_path"] == "sucursal"
    assert fields[0]["field_path"] == "data.sucursal", "no toca la lista original"


def test_el_bloque_va_al_final_del_filter_una_sola_vez():
    codigo, _ = raiz.promover("filter {\n  mutate { }\n}\n", [])
    otra_vez, _ = raiz.promover(codigo, [])

    assert codigo.count("ruby {") == 1 and otra_vez.count("ruby {") == 1
    assert codigo.rstrip().endswith(raiz.bloque() + "\n}")
    assert raiz.sacar(codigo).count("ruby {") == 0


def test_un_filter_roto_no_se_promueve_en_silencio():
    """Termina en `}` (el del mutate) pero el `filter {` quedó abierto: el
    bloque iría a parar adentro del plugin."""
    with pytest.raises(ValueError):
        raiz.promover("filter {\n  mutate { }\n", [])


def test_si_no_se_puede_promover_filter_y_campos_siguen_de_acuerdo(monkeypatch):
    def _roto(raw_log, **kw):
        return {"filter_code": "filter {\n  mutate { }\n",
                "fields": [{"raw_name": "x", "field_path": "data.x", "ecs_path": "data.x",
                            "type": "string", "business_label": "X"}]}

    monkeypatch.setattr(main, "generate_logstash_filter", _roto)
    body = client.post("/api/v1/onboarding/generate-filter",
                       json={"raw_log": "algo sin estructura"}).json()

    assert body["fields"][0]["field_path"] == "data.x"
    assert raiz.bloque() not in body["filter_code"]


@pytest.mark.parametrize("texto", [
    TELEMETRIA,
    '{"sucursal": "Palermo", "importe": 10.5, "message": "hola"}\n'
    '{"sucursal": "Belgrano", "importe": 3.2, "message": "chau"}\n',
    'sucursal="Palermo" importe=10 tags=a\nsucursal="Belgrano" importe=3 tags=b\n',
], ids=["csv", "jsonl", "kv"])
def test_el_filter_promovido_pasa_la_gramatica(texto):
    p = perfilador.perfilar(texto.strip("\n").split("\n"))
    codigo, _ = raiz.promover(perfilador.armar_filter(p, "data"), perfilador.campos(p, "data"))
    assert _sin_errores(codigo) == []


# ── De punta a punta ────────────────────────────────────────────────────────
def test_un_dataset_nuevo_llega_a_la_raiz(monkeypatch):
    _no_llamar_al_llm(monkeypatch)
    body = client.post("/api/v1/onboarding/generate-filter",
                       json={"raw_log": TELEMETRIA}).json()

    paths = {f["raw_name"]: f["field_path"] for f in body["fields"]}
    assert paths["cliente"] == "cliente"
    assert paths["paginas_totales"] == "paginas_totales"
    assert not any(p.startswith("data.") for p in paths.values())
    assert body["filter_code"].count(raiz.bloque()) == 1
    assert _sin_errores(body["filter_code"]) == []


def test_una_columna_message_o_tags_no_pisa_lo_de_logstash(monkeypatch):
    _no_llamar_al_llm(monkeypatch)
    csv = "fecha,message,tags,importe\n2026-09-18,hola,a,1\n2026-09-19,chau,b,2\n"
    body = client.post("/api/v1/onboarding/generate-filter", json={"raw_log": csv}).json()

    paths = {f["raw_name"]: f["field_path"] for f in body["fields"]}
    assert paths["message"] == "message_original"
    assert paths["tags"] == "tags_original"
    assert paths["importe"] == "importe"


def test_el_template_sale_con_los_campos_en_la_raiz(monkeypatch):
    _no_llamar_al_llm(monkeypatch)
    fields = client.post("/api/v1/onboarding/generate-filter",
                         json={"raw_log": TELEMETRIA}).json()["fields"]

    props = main.build_index_template(fields, "", "t-%{+YYYY.MM}")["template"]["mappings"]["properties"]
    assert props["paginas_totales"]["type"] in ("integer", "long")
    assert "data" not in props


def test_corregir_con_feedback_no_duplica_el_bloque(monkeypatch):
    """El LLM recibe el filter SIN el bloque —lo agregamos nosotros— y el que
    vuelve lo lleva una sola vez."""
    visto = {}

    def _fake(raw_log, **kw):
        visto["previo"] = kw.get("previous_filter", "")
        return {"filter_code": 'filter {\n  mutate { add_field => { "[data][x]" => "1" } }\n}\n',
                "fields": [{"raw_name": "x", "field_path": "data.x", "ecs_path": "data.x",
                            "type": "string", "business_label": "X"}]}

    monkeypatch.setattr(main, "generate_logstash_filter", _fake)
    previo, _ = raiz.promover('filter {\n  mutate { add_field => { "[data][x]" => "0" } }\n}\n', [])

    body = client.post("/api/v1/onboarding/generate-filter", json={
        "raw_log": "x=0", "feedback": "poné 1", "previous_filter": previo}).json()

    assert raiz.bloque() not in visto["previo"]
    assert body["filter_code"].count(raiz.bloque()) == 1
    assert body["fields"][0]["field_path"] == "x"
