"""El chatbot tiene que aparecer para un caso creado desde el Builder.

El bug: un caso custom se desplegaba, el agente conversacional SÍ se creaba en el
cluster, y aun así la card del asistente se pintaba sin chat. La causa era una
línea de JS:

    const qs = SUGGESTED_BY_SLUG[s] || _genericQuestions(s);
    if (!qs.length) continue;

Un array vacío es **truthy** en JavaScript, así que el `||` nunca caía al
fallback. Los tres tipos de slug se comportaban distinto:

  * built-in  → `questions` con 8-10 preguntas          → chat OK
  * productivo→ no está en el registro → `undefined`    → falsy → fallback → OK
  * del Builder → SÍ está en el registro, `questions: []` → truthy → 0 preguntas
                  → `continue` → sin chat

O sea que el único roto era justo el que no tenía cobertura: los tests de
capabilities usan `my-log`, un slug productivo, que es el caso que funcionaba.

Estos tests extraen las funciones REALES del HTML y las corren en node, igual que
tests/test_front_case_meta.py.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node no está instalado")


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _sin_comentarios(texto: str) -> str:
    """Quita comentarios JS/CSS. Los dos `sub` van separados: con DOTALL, el `.*`
    de la variante `//` se come del primer comentario de línea hasta el final."""
    texto = re.sub(r"/\*.*?\*/", "", texto, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", texto, flags=re.MULTILINE)


def _extraer(nombre: str, texto: str) -> str:
    """Devuelve el cuerpo de `const <nombre> = (…) => { … };` tal cual está.

    El cierre se ancla a la indentación (`\\n      };`) y no al primer `;`: el
    cuerpo tiene sus propios `;` y un `.*?;` cortaba la función por la mitad.
    """
    m = re.search(rf"const {nombre} = (.*?\n      \}});", texto, re.DOTALL)
    assert m, f"no encontré `const {nombre}` en el front"
    return m.group(1)


def _correr(js: str) -> str:
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, f"el JS extraído no corre:\n{r.stderr}"
    return r.stdout.strip()


def test_un_caso_del_builder_recibe_preguntas_y_por_lo_tanto_chat():
    """`_questionsFor` es la función que decide si un slug entra al chat: abajo,
    todo slug sin preguntas se descarta con `continue`."""
    html = _html()
    cuerpo = _extraer("_questionsFor", html)

    # Los tres tipos de slug, con el shape que realmente tiene cada uno.
    js = f"""
      const state = {{ fields: [] }};
      const SUGGESTED_BY_SLUG = {{
        siem: ['¿Cuántos eventos hay?', '¿Y por día?'],   // built-in
        'firewall-de-acme': [],                            // creado en el Builder
      }};
      const _genericQuestions = (slug) => ['generica 1', 'generica 2'];
      const _questionsFor = {cuerpo};
      console.log(JSON.stringify({{
        builtin:    _questionsFor('siem').length,
        builder:    _questionsFor('firewall-de-acme').length,
        productivo: _questionsFor('my-log').length,
      }}));
    """
    got = json.loads(_correr(js))

    assert got["builder"] > 0, (
        "un caso creado desde el Builder se queda sin preguntas → sin chatbot. "
        "Es el bug original: `[] || fallback` devuelve `[]` porque un array vacío "
        "es truthy.")
    assert got["builtin"] == 2, "un built-in tiene que conservar SUS preguntas curadas"
    assert got["productivo"] > 0, "un slug productivo tiene que caer al fallback"


def test_el_agente_se_busca_en_todos_los_casos_no_solo_en_el_primero():
    """El agente conversacional es uno solo para el cluster. Buscándolo en
    `entries[0]`, alcanzaba con que el primer caso del registro no lo tuviera
    para que el chat desapareciera aunque los demás estuvieran provisionados."""
    html = _html()
    m = re.search(r"const conAgente = entries\.find\((.*?)\);\n", html, re.DOTALL)
    assert m, ("desapareció la búsqueda del agente sobre todos los casos; si volvió "
               "a `entries[0]`, un caso sin agente al principio apaga el chat")

    js = f"""
      const entries = [
        ['sin-agente', {{ conversational: {{ ok: false }} }}],
        ['con-agente', {{ conversational: {{ ok: true, agent_id: 'abc123' }} }}],
      ];
      const conAgente = entries.find({m.group(1)});
      console.log(JSON.stringify({{
        encontrado: !!conAgente,
        id: conAgente ? conAgente[1].conversational.agent_id : '',
      }}));
    """
    got = json.loads(_correr(js))
    assert got["encontrado"], "no encontró el agente cuando NO está en el primer caso"
    assert got["id"] == "abc123"


def test_no_vuelve_el_or_sobre_el_array_de_preguntas():
    """El patrón exacto que causó el bug, para que no reaparezca copiado.

    Se mira el código sin comentarios: el comentario que explica el bug cita el
    patrón viejo a propósito, y esa cita no es una regresión."""
    malos = re.findall(r"SUGGESTED_BY_SLUG\[\w+\]\s*\|\|", _sin_comentarios(_html()))
    assert not malos, (
        f"volvió `SUGGESTED_BY_SLUG[x] || …` ({len(malos)} veces). Un array vacío "
        f"es truthy: usá `_questionsFor(slug)`, que chequea `.length`.")
