"""Un dataset nuevo (Builder) también tiene perfil por entidad, analista con
datos enmascarados y mapa por país: salen de sus campos y de lo que se marca
en el paso 2 (Entidad, Sensible), no de un vertical."""
import json
import pathlib
import shutil
import subprocess

import pytest

import accesos
import dashboards
import main
import perfiles

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"
CAMPOS = [
    {"field_path": "data.status", "type": "string", "business_label": "Estado", "dimension": True},
    {"field_path": "data.customer_id", "type": "string", "business_label": "Cliente", "dimension": True},
    {"field_path": "data.dni", "type": "string", "business_label": "DNI", "dimension": True, "sensitive": True},
    {"field_path": "data.price", "type": "float", "business_label": "Precio"},
    {"field_path": "data.qty", "type": "integer", "business_label": "Cantidad"},
    {"field_path": "data.country", "type": "string", "business_label": "País", "dimension": True},
]


def test_la_entidad_propuesta_y_la_marcada():
    assert perfiles.entidad_propuesta(CAMPOS) == "data.customer_id"
    marcado = [dict(f, entity=(f["field_path"] == "data.status")) for f in CAMPOS]
    assert perfiles.entidad_propuesta(marcado) == "data.status", "lo que marca el usuario manda"
    assert perfiles.entidad_propuesta([{"field_path": "data.status", "type": "string"}]) == ""
    assert perfiles.entidad_propuesta([{"field_path": "src.ip", "type": "ip"}]) == "src.ip"


def test_el_perfil_sale_de_los_campos():
    p = perfiles.perfil_desde_campos(CAMPOS)
    assert p["campo"] == "data.customer_id" and p["etiqueta"] == "Cliente"
    # La cantidad se suma; el precio se promedia (el "precio total" no dice nada).
    assert p["medidas"] == {"eventos": {"value_count": {"field": "@timestamp"}},
                            "data_qty": {"sum": {"field": "data.qty"}},
                            "data_price": {"avg": {"field": "data.price"}},
                            "ultimo": {"max": {"field": "@timestamp"}}}
    assert p["nombres"]["data_price"] == "Precio (promedio)" and p["fechas"] == ["ultimo"]
    t = perfiles.build_transform("mi-dataset", "mi-dataset-*", p)["transform"]
    assert t["target_index"] == "perfil-mi-dataset" and t["groups"][0]["terms"]["source_field"] == "data.customer_id"
    assert perfiles.perfil_desde_campos([{"field_path": "x", "type": "float"}]) is None


def test_el_perfil_no_suma_coordenadas_ni_edades_y_promedia_los_tiempos():
    """Emergencias médicas: entraban latitud, longitud y edad, sumadas (-2012 de
    latitud para 58 llamadas). Ahora, las medidas del análisis: el costo sumado
    y los tiempos promediados."""
    F = lambda path, tipo, **kw: {"field_path": path, "type": tipo, "business_label": path, **kw}  # noqa: E731
    campos = [F("afiliado_id", "string", entity=True), F("edad_paciente", "integer"), F("latitud", "float"),
              F("longitud", "float"), F("tiempo_respuesta_min", "float", role="measure"),
              F("costo_servicio_ars", "integer", role="measure"), F("satisfaccion", "integer")]
    p = perfiles.perfil_desde_campos(campos)
    assert p["medidas"] == {"eventos": {"value_count": {"field": "@timestamp"}},
                            "costo_servicio_ars": {"sum": {"field": "costo_servicio_ars"}},
                            "tiempo_respuesta_min": {"avg": {"field": "tiempo_respuesta_min"}},
                            "ultimo": {"max": {"field": "@timestamp"}}}
    assert p["nombres"]["tiempo_respuesta_min"] == "tiempo_respuesta_min (promedio)"
    # Sin medidas marcadas: los números que no son coordenadas, edades ni ids.
    sin_roles = [dict(f, role=None) for f in campos]
    assert [f["field_path"] for f, _ in perfiles.medidas_de_la_entidad(sin_roles, "afiliado_id")] == \
        ["costo_servicio_ars", "satisfaccion", "tiempo_respuesta_min"]



def test_el_perfil_trae_los_criticos_las_duraciones_y_no_la_fila_sin_entidad():
    """Un OMS: entraban monto, cantidad ejecutada y cantidad ordenada (tres
    sumas) y nunca la latencia; no había rechazos por participante; y los 1.172
    eventos del propio sistema, sin participante, eran la fila más grande."""
    F = lambda path, tipo, **kw: {"field_path": path, "type": tipo, "business_label": path, **kw}  # noqa: E731
    campos = [F("fix.comp_id_propio", "keyword", entity=True),
              F("fix.rechazo_motivo", "text", role="critical_indicator", business_label="Motivo del rechazo"),
              F("fix.monto_ejecutado", "float", role="measure"), F("fix.last_qty", "float", role="measure"),
              F("fix.order_qty", "float", role="measure"), F("fix.latencia_ms", "long", role="measure"),
              F("fix.demora_ms", "long", role="measure")]
    p = perfiles.perfil_desde_campos(campos)
    assert list(p["medidas"]) == ["eventos", "criticos", "fix_monto_ejecutado", "fix_last_qty",
                                  "fix_latencia_ms", "fix_demora_ms", "ultimo"]
    assert p["medidas"]["criticos"] == {"value_count": {"field": "fix.rechazo_motivo.keyword"}}
    assert p["nombres"]["criticos"] == "Motivo del rechazo (cantidad)"
    assert p["medidas"]["fix_latencia_ms"] == {"avg": {"field": "fix.latencia_ms"}}
    t = perfiles.build_transform("oms", "oms-*", p)["transform"]
    assert t["data_selection_query"] == {"exists": {"field": "fix.comp_id_propio"}}
    # Un indicador 0/1 está en todos los eventos: se suma.
    fraude = [F("cliente", "keyword", entity=True), F("is_fraud", "integer", role="critical_indicator")]
    assert perfiles.perfil_desde_campos(fraude)["medidas"]["criticos"] == {"sum": {"field": "is_fraud"}}
    # De una sola clase, tres como antes.
    sumas = [F("a", "float", role="measure"), F("b", "float", role="measure"), F("c", "float", role="measure"),
             F("d", "float", role="measure")]
    assert [f["field_path"] for f, _ in perfiles.medidas_de_la_entidad(sumas)] == ["a", "b", "c"]


def test_los_sensibles_son_los_marcados():
    assert accesos.enmascarados_desde_campos(CAMPOS) == ["data.dni"]
    assert accesos.enmascarados_desde_campos([]) == []


def test_un_vertical_usa_lo_suyo_y_un_dataset_nuevo_lo_derivado():
    assert main._perfil_de("siem", {"fields": CAMPOS})["campo"] == "source.ip"
    # Una vertical que no declara perfil ni sensibles los deriva de SUS campos,
    # como un dataset nuevo (cinco verticales se quedaban sin perfil y cuatro sin analista).
    assert main._perfil_de("cts", {"fields": CAMPOS})["campo"] == "user.user_name"
    assert main._perfil_de("transacciones-alyc", {})["campo"] == "comitente", "el comitente, no la orden"
    assert main._perfil_de("mi-dataset", {"fields": CAMPOS})["campo"] == "data.customer_id"
    assert main._perfil_de("mi-dataset", {}) is None
    assert main._enmascarados_de("encuentros-clinicos", {"fields": CAMPOS}) == ["patient"]
    assert main._enmascarados_de("mi-dataset", {"fields": CAMPOS}) == ["data.dni"]
    assert main._enmascarados_de("siem", {"fields": CAMPOS}) == ["user.name"]
    assert main._enmascarados_de("cts", {}) == ["user.user_name", "user.name", "user.account_id"]
    assert main._perfil_de("cts", {"excluir": ["perfil"]}) is None and main._enmascarados_de("cts", {"excluir": ["analista"]}) == []


def test_lo_sensible_por_el_nombre_si_nadie_lo_marco():
    """Sin la marca en ningún campo (una vertical), lo que identifica a una
    persona; con la marca en alguno, manda la marca (aunque no haya ninguno)."""
    F = lambda p, t="keyword", **kw: {"field_path": p, "type": t, **kw}  # noqa: E731
    campos = [F("user.name"), F("user.domain.name"), F("user_agent"), F("user.type"), F("operador_dni"),
              F("destinatario.telefono"), F("accountId"), F("trace_name"), F("monto", "float")]
    assert accesos.enmascarados_desde_campos(campos) == ["user.name", "operador_dni", "destinatario.telefono", "accountId"]
    marcados = [dict(f, sensitive=False) for f in campos]
    assert accesos.enmascarados_desde_campos(marcados) == [], "el análisis dijo que ninguno"


def test_el_perfil_elige_a_quien_antes_que_al_equipo_o_un_id():
    F = lambda p, t="keyword": {"field_path": p, "type": t}  # noqa: E731
    assert perfiles.entidad_propuesta([F("order_id"), F("device"), F("comitente")]) == "comitente"
    assert perfiles.entidad_propuesta([F("order_id"), F("source_ip", "ip")]) == "source_ip"
    assert perfiles.entidad_propuesta([F("session_id"), F("order_id")]) == "session_id", "sin nada mejor, el primero"
    medidas = [f["field_path"] for f, _ in perfiles.medidas_de_la_entidad(
        [F("srcport", "integer"), F("policyid", "integer"), F("proto", "integer"), F("message_type", "integer"),
         F("steps.index", "integer"), F("sentbyte", "long"), F("price", "float")])]
    assert medidas == ["sentbyte", "price"], "puertos, ids, tipos y órdenes no son medidas; el precio va al final (promedio)"


def test_el_mapa_por_pais_desde_los_campos():
    # El código va ANTES: sin el filtro de códigos, se elegiría ese.
    campos = [{"field_path": "data.country_code", "type": "string", "business_label": "Código de país", "dimension": True}] + CAMPOS
    objs = [json.loads(l) for l in dashboards.build_ndjson_from_fields("mi-dataset", "mi-dataset-%{+YYYY.MM}", campos).splitlines() if l.strip()]
    mapas = [json.loads(o["attributes"]["visState"]) for o in objs
             if o.get("type") == "visualization" and json.loads(o["attributes"]["visState"]).get("type") == "region_map"]
    assert len(mapas) == 1 and mapas[0]["aggs"][1]["params"]["field"] == "data.country", "el código ISO no une por nombre"
    sin_pais = [f for f in CAMPOS if f["field_path"] != "data.country"]
    objs = [json.loads(l) for l in dashboards.build_ndjson_from_fields("x", "x-%{+YYYY.MM}", sin_pais).splitlines() if l.strip()]
    assert not [o for o in objs if o.get("type") == "visualization" and '"region_map"' in o["attributes"]["visState"]]


def _funciones(html: str) -> str:
    i = html.index("    const _PISTA_DE_ENTIDAD = ")
    return html[i:html.index("    function renderMappingTable(fields) {", i)]


_ARNES = r"""
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
const icon = (n) => `<i data-icon="${n}"></i>`;
const state = {};
""" + "{FUNCIONES}" + r"""
const fallos = [];
const check = (n, c, x) => { if (!c) fallos.push(n + (x === undefined ? '' : ' -> ' + x)); };
const campos = [
  { field_path: 'data.status', type: 'string', dimension: true },
  { field_path: 'data.customer_id', type: 'string', dimension: true },
  { field_path: 'data.price', type: 'float' },
];
check('propone la entidad', entidadPropuesta(campos) === 'data.customer_id');
campos[0].entity = true;
check('la marcada (por el descubrimiento) manda', entidadPropuesta(campos) === 'data.status');
check('sin pistas, ninguna', entidadPropuesta([{ field_path: 'x', type: 'string' }]) === '');
console.log(fallos.join('\n'));
process.exit(fallos.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_entidad_en_el_paso_2_en_node(tmp_path):
    js = tmp_path / "entidad.mjs"
    js.write_text(_ARNES.replace("{FUNCIONES}", _funciones(_INDEX.read_text(encoding="utf-8"))), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_la_tabla_del_paso_2_no_pide_marcas():
    """Rol, Entidad y Sensible eran columnas para marcar a mano y confundían al
    desplegar un caso: los propone el descubrimiento y viajan con el campo."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    function renderMappingTable(fields) {")
    fn = html[i:html.index("    function renderMultiCaseMapping(", i)]
    for fuera in (">Rol</th>", ">Entidad</th>", ">Sensible</th>", "mapping-entidad", "mapping-sensible", "mapping-rol"):
        assert fuera not in html, fuera
    assert "selectorDeRol" not in html and "marcarRol" not in html and "marcarEntidad" not in html
    assert '<td colspan="5">' in fn
    assert "dimensionamiento" not in html.lower(), "sin «Para producción» en el paso 2"
    # La entidad propuesta queda marcada en el campo: viaja con el deploy.
    assert "if (!fields.some(f => f.entity)) {" in fn


def test_el_paso_2_no_elige_plugins():
    """Los plugins se eligen con el entorno desplegado (Plugins para el
    cluster), con sus comandos a la vista: el paso 2 ya no los lista."""
    html = _INDEX.read_text(encoding="utf-8")
    for fuera in ("Lo que va a tener tu cluster", "planDelClusterHTML", "plan-cluster", "planExcluidos", "/api/v1/onboarding/plan-del-cluster"):
        assert fuera not in html, fuera
    assert "plan-del-cluster" not in pathlib.Path(main.__file__).read_text(encoding="utf-8")


def test_el_estado_informa_perfil_y_enmascarados():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert '"perfil": (_perfil_de(slug, entry) or {}).get("etiqueta", ""),' in src
    assert '"enmascarados": _enmascarados_de(slug, entry),' in src
