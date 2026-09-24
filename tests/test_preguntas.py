"""Diez preguntas de ejemplo que el chat siempre puede contestar."""
import pathlib

import pytest

import custom_cases
import dashboards
import perfilador
import preguntas

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"

TELEMETRIA = """Fecha y Hora,ID Trabajo,Cliente,Prensa / Máquina,Estado,Páginas Totales,Velocidad (PPM),Código Error
2026-09-18 11:04:12,TRB-00187,Editorial Sur,PRENSA-03,COMPLETADO,12000,142,
2026-09-18 11:31:55,TRB-00188,Gráfica Andes,PRENSA-01,FALLIDO,8000,118,E-0412
2026-09-18 12:02:10,TRB-00189,Editorial Sur,PRENSA-03,COMPLETADO,5000,150,
2026-09-18 12:30:10,TRB-00190,Imprenta Norte,PRENSA-02,COMPLETADO,3000,150,
"""
VENTAS = """fecha;sucursal;medio_pago;importe
18/09/2026;Palermo;Débito;1.250,50
18/09/2026;Belgrano;Efectivo;980,00
19/09/2026;Palermo;Crédito;2.100,00
19/09/2026;Caballito;Débito;450,25
"""


def _campos(texto):
    return perfilador.campos(perfilador.perfilar(texto.strip("\n").split("\n")), "data")


@pytest.fixture
def vocab():
    return preguntas._vocabulario(_campos(TELEMETRIA))


# ── Validación ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cruda, prolija", [
    ("¿Cuántos trabajos hay por Estado?", "¿Cuántos trabajos hay por Estado?"),
    ("Cuántos trabajos hay por cliente", "¿Cuántos trabajos hay por cliente?"),
    ("3. ¿Cuál es la prensa con más páginas?", "¿Cuál es la prensa con más páginas?"),
    ("  ¿Qué   cliente tiene más FALLIDO?  ", "¿Qué cliente tiene más FALLIDO?"),
    # "por que" adentro de otra palabra no es un "por qué".
    ("¿Cuántos trabajos hay por quemador en la Prensa?", "¿Cuántos trabajos hay por quemador en la Prensa?"),
])
def test_una_buena_pregunta_entra_prolija(vocab, cruda, prolija):
    assert preguntas.validar(cruda, vocab) == prolija


@pytest.mark.parametrize("cruda", [
    "¿Por qué fallan los trabajos del Cliente?",             # no es una agregación
    "¿Cuántas Páginas Totales se van a imprimir mañana? Predecí",
    "¿Hay correlación entre Velocidad y Páginas Totales?",
    "¿Cuántos registros hay por día?",                       # no cita ningún campo
    "¿Cuántos por Estado? ¿Y por Cliente?",                  # dos preguntas en una
    "How many jobs per Estado?",                             # no es castellano
    "¿Estado?",                                              # corta
    None, 42,
])
def test_una_pregunta_que_el_chat_no_puede_contestar_no_entra(vocab, cruda):
    assert preguntas.validar(cruda, vocab) is None


# ── Armado ──────────────────────────────────────────────────────────────────
def test_siempre_son_diez_aunque_el_llm_no_mande_ninguna():
    qs = preguntas.armar(_campos(TELEMETRIA), [])
    assert len(qs) == 10 and len(set(qs)) == 10
    assert all(q.startswith("¿") and q.endswith("?") for q in qs)


def test_las_del_llm_van_primero_y_las_plantillas_completan():
    llm = ["¿Qué Cliente imprimió más Páginas Totales?",
           "¿Por qué falla la PRENSA-01?",
           "¿Qué Cliente imprimió más Páginas Totales?"]      # repetida
    qs = preguntas.armar(_campos(TELEMETRIA), llm)
    assert qs[0] == llm[0]
    assert qs.count(llm[0]) == 1 and llm[1] not in qs
    assert len(qs) == 10


def test_nunca_mas_de_diez():
    llm = [f"¿Cuántos trabajos hay con Estado COMPLETADO en la PRENSA-0{i}?" for i in range(15)]
    assert len(preguntas.armar(_campos(TELEMETRIA), llm)) == 10


def test_las_plantillas_usan_los_roles_y_los_valores_reales():
    qs = preguntas.armar(_campos(TELEMETRIA), [], "los trabajos")
    assert "¿Cuántos trabajos hay de cada estado?" in qs
    assert "¿Cuántos trabajos hubo por día?" in qs
    assert "¿Cuáles son los 10 valores de ID trabajo que más se repiten?" in qs
    assert "¿Cuántos trabajos tienen código error?" in qs
    # El valor real del estado de falla, como lo diría una persona (el valor
    # va como está: es lo que hay que escribir para filtrar).
    assert "¿Cuántos trabajos terminaron en FALLIDO?" in qs
    assert "¿Qué cliente tuvo más trabajos en FALLIDO?" in qs
    # "Cliente" también tiene rol de entidad, pero la entidad elegida es ID
    # Trabajo: Cliente agrupa, y es la torta principal del dashboard. Y sin
    # repetir "total": "el total de páginas", no "de páginas totales".
    assert "¿Cuál es el total de páginas por cliente?" in qs


def test_las_preguntas_suenan_a_persona_y_no_a_titulo_de_reporte():
    """Se leen en voz alta: la etiqueta en minúscula en medio de la frase,
    nada de "registros" cuando sabemos que cada fila es una factura, y nada
    de títulos de reporte."""
    qs = preguntas.armar(_campos(VENTAS), [], "las facturas")

    assert not any("Distribución" in q for q in qs), qs
    assert not any("registros" in q for q in qs), qs
    assert not any(" Importe" in q or " Sucursal" in q for q in qs), (
        "una etiqueta con mayúscula en medio de la frase se lee como un reporte")
    assert "¿Cuántas facturas hubo por día?" in qs
    assert "¿Cuál es el total de importe por sucursal?" in qs


@pytest.mark.parametrize("etiqueta, en_frase", [
    ("Páginas Totales", "páginas totales"),
    ("ID Trabajo", "ID trabajo"),            # las siglas no se tocan
    ("Velocidad PPM", "velocidad PPM"),
    ("Prensa / Máquina", "prensa / máquina"),
    ("StockCode", "StockCode"),              # mezclado: no sé qué es, se deja
])
def test_una_etiqueta_en_medio_de_la_frase_va_en_minuscula(etiqueta, en_frase):
    assert preguntas._en_frase(etiqueta) == en_frase


def test_el_total_no_se_repite():
    fs = [{"raw_name": "facturacion_total", "business_label": "Facturación total",
           "type": "float", "role": "measure"},
          {"raw_name": "pais", "business_label": "País", "type": "string", "dimension": True}]
    qs = preguntas.plantillas(fs, "las ventas")
    assert "¿Cuál es el total de facturación por país?" in qs
    assert not any("total de facturación total" in q for q in qs), qs


@pytest.mark.parametrize("filas, esperada", [
    ("las facturas", "¿Cuántas facturas hay en total?"),
    ("los trabajos", "¿Cuántos trabajos hay en total?"),
    # Basura o nada: las filas son "registros", que concuerda en masculino.
    ("", "¿Cuántos registros hay en total?"),
    ("facturas", "¿Cuántos registros hay en total?"),
])
def test_el_genero_sale_del_articulo(filas, esperada):
    assert esperada in preguntas.plantillas(_campos(VENTAS), filas)


def test_con_muchas_dimensiones_las_diez_no_son_la_misma_pregunta():
    """Seis "¿… hay por X?" seguidas se leen como una lista de columnas. La
    medida se intercala en las primeras, y después siguen los conteos."""
    muchas = ("Fecha y Hora,Cliente,Prensa,Papel,Turno,Estado,Páginas\n"
              "2026-09-18 11:04:12,Editorial Sur,PRENSA-03,Couché,Mañana,COMPLETADO,12000\n"
              "2026-09-18 11:31:55,Gráfica Andes,PRENSA-01,Obra,Tarde,FALLIDO,8000\n"
              "2026-09-18 12:02:10,Editorial Sur,PRENSA-03,Couché,Mañana,COMPLETADO,5000\n"
              "2026-09-18 12:30:10,Imprenta Norte,PRENSA-02,Obra,Noche,COMPLETADO,3000\n")
    qs = preguntas.armar(_campos(muchas), [], "los trabajos")

    assert len(qs) == 10
    # La medida acompaña a las primeras dimensiones, no a todas.
    assert 2 <= sum(1 for q in qs if q.startswith("¿Cuál es el total de páginas por")) <= 3, qs
    # Y ninguna dimensión se queda sin su conteo por culpa de eso.
    assert "¿Cuántos trabajos hay por turno?" in qs, qs


def test_un_dataset_chico_llega_a_diez_con_las_variantes():
    qs = preguntas.plantillas(_campos(VENTAS), "las facturas")
    assert "¿Cuál es el valor más bajo de importe?" in qs
    assert "¿Qué sucursal tiene más facturas?" in qs
    assert "¿Cuántas facturas corresponden a Palermo?" in qs


def test_una_velocidad_se_promedia_como_en_el_dashboard():
    fs = [{"raw_name": "velocidad", "business_label": "Velocidad", "type": "integer",
           "unit": "PPM", "role": "measure"},
          {"raw_name": "prensa", "business_label": "Prensa", "type": "string", "dimension": True}]
    qs = preguntas.plantillas(fs)
    assert "¿Cuál es el promedio de velocidad por prensa?" in qs
    assert not any("total de velocidad" in q for q in qs), "142 PPM y 118 PPM no suman 260"


def test_dos_datasets_distintos_dan_preguntas_distintas():
    a, b = preguntas.armar(_campos(TELEMETRIA), []), preguntas.armar(_campos(VENTAS), [])
    assert len(b) == 10
    assert len(set(a) & set(b)) <= 2   # a lo sumo las genéricas ("por día", "en total")
    assert any("sucursal" in q for q in b)


def test_sin_campos_igual_hay_una_pregunta():
    assert preguntas.armar([], []) == ["¿Cuántos registros hay en total?"]


def test_nombrar_las_filas_alcanza_para_que_una_pregunta_del_llm_entre():
    """"¿Cuántas facturas hubo en cada País?" no cita ninguna columna con su
    etiqueta (la columna se llama Sucursal), pero habla de estos datos."""
    vocab = preguntas._vocabulario(_campos(VENTAS))
    assert preguntas.validar("¿Cuántas facturas hubo?", vocab) is None

    qs = preguntas.armar(_campos(VENTAS), ["¿Cuántas facturas hubo?"], "las facturas")
    assert qs[0] == "¿Cuántas facturas hubo?"


# ── El dashboard y las preguntas dicen lo mismo ─────────────────────────────
def test_el_dashboard_de_telemetria():
    spec = dashboards._spec_from_fields("telemetria", "telemetria-2026.09", _campos(TELEMETRIA))
    titulos = [(p["type"], p["title"]) for p in spec["panels"]]
    metricas = [t for tipo, t in titulos if tipo == "metric"]
    # La fila de métricas entra entera: con `< 36` la medida quedaba afuera.
    assert metricas == ["Total", "Únicos ID Trabajo", "Con Código Error", "Suma Páginas Totales"]
    assert ("area", "Estado en el tiempo") in titulos
    assert ("table", "Top ID Trabajo") in titulos
    assert ("line", "Páginas Totales en el tiempo") in titulos


# ── Viajan con el caso ──────────────────────────────────────────────────────
def test_el_modal_manda_las_preguntas_al_guardar():
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("async function submitSaveCase(e)")
    assert "questions: state.preguntas || []" in html[i:html.index("_closeSaveCase();", i)]


def test_las_genericas_del_chat_usan_los_campos_del_caso_y_no_los_del_wizard():
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("const _genericQuestions = (slug) => {")
    cuerpo = html[i:html.index("const _questionsFor", i)]
    assert "state.fields" not in cuerpo
    assert "_VVIS.find(v => v.slug === slug)" in cuerpo


def test_un_caso_guardado_sin_preguntas_las_arma_de_sus_campos(monkeypatch):
    caso = {"slug": "telemetria", "label": "Telemetría", "fields": _campos(TELEMETRIA),
            "suggested_questions": []}
    guardadas = {"slug": "otro", "label": "Otro", "fields": [],
                 "suggested_questions": ["¿Cuántos pedidos hay?"]}
    monkeypatch.setattr(custom_cases, "list_cases", lambda: [caso, guardadas])
    entradas = {e["slug"]: e["questions"] for e in custom_cases.front_entries()}
    assert len(entradas["telemetria"]) == 10
    assert entradas["otro"] == ["¿Cuántos pedidos hay?"]
