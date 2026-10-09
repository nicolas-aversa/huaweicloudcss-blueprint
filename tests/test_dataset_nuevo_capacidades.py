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
    assert p["medidas"] == {"eventos": {"value_count": {"field": "@timestamp"}},
                            "data_price": {"sum": {"field": "data.price"}},
                            "data_qty": {"sum": {"field": "data.qty"}},
                            "ultimo": {"max": {"field": "@timestamp"}}}
    assert p["nombres"]["data_price"] == "Precio" and p["fechas"] == ["ultimo"]
    t = perfiles.build_transform("mi-dataset", "mi-dataset-*", p)["transform"]
    assert t["target_index"] == "perfil-mi-dataset" and t["groups"][0]["terms"]["source_field"] == "data.customer_id"
    assert perfiles.perfil_desde_campos([{"field_path": "x", "type": "float"}]) is None


def test_los_sensibles_son_los_marcados():
    assert accesos.enmascarados_desde_campos(CAMPOS) == ["data.dni"]
    assert accesos.enmascarados_desde_campos([]) == []


def test_un_vertical_usa_lo_suyo_y_un_dataset_nuevo_lo_derivado():
    assert main._perfil_de("siem", {"fields": CAMPOS})["campo"] == "source.ip"
    assert main._perfil_de("cts", {"fields": CAMPOS}) is None, "un vertical sin perfil no lo deriva"
    assert main._perfil_de("mi-dataset", {"fields": CAMPOS})["campo"] == "data.customer_id"
    assert main._perfil_de("mi-dataset", {}) is None
    assert main._enmascarados_de("encuentros-clinicos", {"fields": CAMPOS}) == ["patient"]
    assert main._enmascarados_de("mi-dataset", {"fields": CAMPOS}) == ["data.dni"]
    assert main._enmascarados_de("siem", {"fields": CAMPOS}) == []


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
const state = { planExcluidos: ['perfil'] };
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
// El plan: lo que aplica con su switch (y el apagado, apagado); lo global aparte.
const html = planDelClusterHTML([
  { plugin: 'perfil', titulo: 'Perfil', aplica: true, motivo: 'por <Cliente>', opcional: true, config: {} },
  { plugin: 'mapa', titulo: 'Mapa', aplica: false, motivo: 'no hay país', opcional: false, config: null },
  { plugin: 'agente', titulo: 'Asistente', aplica: true, motivo: 'm', opcional: false, config: {} },
  { plugin: 'text2viz', titulo: 'Gráficos', aplica: true, motivo: 'm', opcional: false, config: { alcance: 'cluster' } },
]);
check('cuenta lo que aplica', html.includes('2 de 3'), html);
check('lo que no se puede apagar no tiene switch', !html.includes('data-plugin="agente"'));
check('escapa el motivo', html.includes('por &lt;Cliente>'));
check('el apagado sale apagado', html.includes('is-apagado') && !/data-plugin="perfil" checked/.test(html));
check('lo que no aplica no tiene switch', !html.includes('data-plugin="mapa"') && html.includes('is-off'));
check('lo global va aparte', html.includes('Y para todo el cluster: Gráficos.') && !html.includes('data-plugin="text2viz"'));
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
    assert '<td colspan="5">' in fn and '<div class="plan-cluster" id="plan-cluster"' in fn and "cargarPlanDelCluster();" in fn
    # La entidad propuesta queda marcada en el campo: viaja con el deploy.
    assert "if (!fields.some(f => f.entity)) {" in fn


def test_el_estado_informa_perfil_y_enmascarados():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert '"perfil": (_perfil_de(slug, entry) or {}).get("etiqueta", ""),' in src
    assert '"enmascarados": _enmascarados_de(slug, entry),' in src
