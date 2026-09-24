"""El chat con memoria, honesto y que se corrige solo.

Tres fallas de una conversación real sobre reseñas:
  * "¿en qué idioma están?" → inventó `review_language` (no existe);
  * "¿se quejan de la tardanza?" → contó puntaje 1, y quien redactaba —que no
    veía la consulta— afirmó que eran quejas por tardanza;
  * "¿cómo sabés?" → sin memoria, repitió la consulta e inventó una razón.
"""
import pathlib

import pytest
from fastapi import HTTPException

import capabilities
import main

T = main.ChatTurno
CAMPOS = {"review_score": "Puntaje (1-5)", "review_comment_message": "Comentario"}


class _Fakes:
    """Modelo y cluster de mentira, con lo que les llegó."""

    def __init__(self, ppls, respuesta="ok", ejecuciones=None):
        self.ppls = list(ppls)
        self.respuesta = respuesta
        self.ejecuciones = list(ejecuciones or [(True, {"schema": [{"name": "total"}], "datarows": [[1]]})])
        self.prompts_ppl, self.prompts_llm, self.ejecutadas = [], [], []

    def ppl(self, prompt):
        self.prompts_ppl.append(prompt)
        return self.ppls.pop(0)

    def llm(self, prompt):
        self.prompts_llm.append(prompt)
        return self.respuesta

    def ejecutar(self, ppl):
        self.ejecutadas.append(ppl)
        return self.ejecuciones.pop(0)


def _charlar(fakes, pregunta="¿cuántas?", historial=()):
    return main._conversar(pregunta, list(historial), CAMPOS, fakes.ppl, fakes.llm, fakes.ejecutar)


TARDANZA = T(pregunta="hay alguna reseña que se queja de la tardanza?",
             ppl="source=reviews-* | where review_score = 1 | stats count() as total",
             respuesta="Sí, hay 10,407 reseñas que se quejan de la tardanza.")


# ── Memoria ─────────────────────────────────────────────────────────────────
def test_la_memoria_son_los_ultimos_turnos_con_su_consulta():
    turnos = [T(pregunta=f"p{i}", ppl=f"source=x | where a={i}", respuesta=f"r{i}") for i in range(6)]
    memoria = main._memoria(turnos)
    assert "p0" not in memoria and "p1" not in memoria and "p5" in memoria
    assert memoria.count("Consulta PPL:") == main._TURNOS_DE_MEMORIA
    assert main._memoria([]) == ""


def test_la_pregunta_nueva_llega_con_la_conversacion():
    f = _Fakes(["source=reviews-* | stats count() as total by review_score"])
    _charlar(f, "¿y por puntaje?", [TARDANZA])
    assert "CONVERSATION" in f.prompts_ppl[0] and "review_score = 1" in f.prompts_ppl[0]
    assert f.prompts_ppl[0].endswith("PREGUNTA ACTUAL: ¿y por puntaje?")


# ── "¿Cómo sabés?" ──────────────────────────────────────────────────────────
def test_como_sabes_explica_la_consulta_sin_volver_a_consultar():
    f = _Fakes([main._SIN_CONSULTA], respuesta="Conté reseñas con puntaje 1; eso no dice si fue por demora.")
    res = _charlar(f, "pero como sabes que el score de 1 es por la tardanza?", [TARDANZA])

    assert not f.ejecutadas, "no se ejecuta nada nuevo"
    assert res.ppl == TARDANZA.ppl
    assert res.answer.startswith("Conté reseñas con puntaje 1")
    pedido = f.prompts_llm[0]
    assert TARDANZA.ppl in pedido and "qué NO se puede concluir" in pedido
    assert "reconocelo y corregilo" in pedido


def test_como_sabes_sin_nada_antes():
    res = _charlar(_Fakes([main._SIN_CONSULTA]), "¿cómo sabés?")
    assert "No tengo una respuesta anterior" in res.answer


# ── El dato que no está ─────────────────────────────────────────────────────
def test_si_el_dato_no_esta_lo_dice_con_lo_que_si_hay():
    f = _Fakes([f"{main._SIN_DATO}: el idioma de la reseña"])
    res = _charlar(f, "en que idioma está escrita las reseñas?")
    assert not f.ejecutadas
    assert res.answer.startswith("Ese dato no está en los datos (el idioma de la reseña)")
    assert "review_score" in res.answer and "review_comment_message" in res.answer


# ── Autocorrección ──────────────────────────────────────────────────────────
_NO_EXISTE = (False, '{"error":{"details":"Field [review_language] not found."},"status":400}')


def test_un_campo_inventado_se_corrige_con_el_error():
    bien = "source=reviews-* | stats count() as total by review_score"
    f = _Fakes(["source=reviews-* | stats count() as total by review_language", bien],
               ejecuciones=[_NO_EXISTE, (True, {"schema": [], "datarows": [[5, 10]]})])
    res = _charlar(f)
    assert f.ejecutadas[-1] == bien and res.ppl == bien
    assert "review_language] not found" in f.prompts_ppl[1] and "ONLY the fields" in f.prompts_ppl[1]


def test_si_al_corregir_ve_que_el_dato_no_esta_lo_dice():
    f = _Fakes(["source=reviews-* | stats count() by review_language", f"{main._SIN_DATO}: idioma"],
               ejecuciones=[_NO_EXISTE])
    assert _charlar(f).answer.startswith("Ese dato no está en los datos (idioma)")


def test_si_tampoco_anda_devuelve_el_error_y_la_consulta():
    f = _Fakes(["source=x | a", "source=x | b"], ejecuciones=[_NO_EXISTE, _NO_EXISTE])
    with pytest.raises(HTTPException) as exc:
        _charlar(f)
    assert exc.value.detail["ppl"] == "source=x | b" and "not found" in exc.value.detail["message"]


def test_una_salida_que_no_es_ppl_es_un_error():
    with pytest.raises(HTTPException):
        _charlar(_Fakes(["no sé"]))


# ── Honestidad al redactar ──────────────────────────────────────────────────
def test_quien_redacta_ve_la_consulta_y_no_atribuye_causas():
    f = _Fakes([TARDANZA.ppl])
    _charlar(f, TARDANZA.pregunta)
    pedido = f.prompts_llm[0]
    assert TARDANZA.ppl in pedido
    assert "Decí qué se midió" in pedido and "NO atribuyas causas" in pedido


def test_sin_redaccion_quedan_los_datos():
    f = _Fakes([TARDANZA.ppl], respuesta=None)
    assert _charlar(f).answer == "Resultado: [[1]]"


# ── Los prompts ─────────────────────────────────────────────────────────────
def test_los_vacios_se_cuentan_con_isnull():
    sp = capabilities.build_ppl_system_prompt("reviews-*", [], CAMPOS)
    assert "isnull(field)" in sp and "NEVER compare a field with ''" in sp


def test_el_chat_agrega_sus_reglas_al_prompt_del_indice():
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    i = src.index("def ppl_chat(")
    cuerpo = src[i:src.index("\nclass ", i)]
    assert "_sp += _REGLAS_DEL_CHAT" in cuerpo
    assert "return _conversar(request.question, request.history" in cuerpo
    assert main._SIN_CONSULTA in main._REGLAS_DEL_CHAT and main._SIN_DATO in main._REGLAS_DEL_CHAT


def test_el_request_trae_la_memoria():
    req = main.PplChatRequest(question="¿y?", history=[{"pregunta": "a", "ppl": "source=x", "respuesta": "b"}])
    assert req.history[0].ppl == "source=x"
    assert main.PplChatRequest(question="¿y?").history == []
