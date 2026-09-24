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
    qs = preguntas.armar(_campos(TELEMETRIA), [])
    assert "¿Cuántos registros hay por Estado (COMPLETADO, FALLIDO)?" in qs
    assert "¿Cuántos registros hay por día?" in qs
    assert "¿Cuáles son los 10 valores de ID Trabajo con más registros?" in qs
    assert "¿Cuántos registros tienen Código Error?" in qs
    assert any("con Estado FALLIDO" in q for q in qs)
    # "Cliente" también tiene rol de entidad, pero la entidad elegida es ID
    # Trabajo: Cliente agrupa, y es la torta principal del dashboard.
    assert "¿Cuál es el total de Páginas Totales por Cliente?" in qs


def test_un_dataset_chico_llega_a_diez_con_las_variantes():
    qs = preguntas.plantillas(_campos(VENTAS))
    assert "¿Cuál es el mínimo de Importe?" in qs
    assert "¿Cuál es el promedio de Importe?" in qs
    assert "¿Qué Sucursal tiene más registros?" in qs


def test_una_velocidad_se_promedia_como_en_el_dashboard():
    fs = [{"raw_name": "velocidad", "business_label": "Velocidad", "type": "integer",
           "unit": "PPM", "role": "measure"},
          {"raw_name": "prensa", "business_label": "Prensa", "type": "string", "dimension": True}]
    qs = preguntas.plantillas(fs)
    assert "¿Cuál es el promedio de Velocidad por Prensa?" in qs


def test_dos_datasets_distintos_dan_preguntas_distintas():
    a, b = preguntas.armar(_campos(TELEMETRIA), []), preguntas.armar(_campos(VENTAS), [])
    assert len(b) == 10
    assert len(set(a) & set(b)) <= 2   # a lo sumo las genéricas ("por día", "en total")
    assert any("Sucursal" in q or "sucursal" in q for q in b)


def test_sin_campos_igual_hay_una_pregunta():
    assert preguntas.armar([], []) == ["¿Cuántos registros hay en total?"]


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
