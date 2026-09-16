"""La política de contraseña de OpenSearch: que exista, y que las dos copias coincidan.

Dos cosas que este archivo blinda:

1. **El campo no viene prellenado.** Venía con `value="Huawei1234"` en el markup y el
   mismo literal como fallback en JS. Todo despliegue salía con la misma contraseña,
   escrita en claro en un repo que se publica.

2. **La política vive duplicada** —en el `test` del wizard y en el bloque `validation`
   de `terraform/main.tf`— y tiene que decir lo mismo. Si se separan, el front acepta
   algo que el `apply` rechaza a los diez minutos, que es exactamente el fallo que la
   validación venía a evitar. Acá se corre el `test` REAL extraído del HTML contra una
   tabla de casos; `scripts` aparte verifica el lado de Terraform con `terraform console`.

El caso que ordena todo es `Huawei1234`: tiene minúscula, mayúscula y dígito pero NO
símbolo. Huawei lo acepta (pide 3 de 4 clases), así que una validación que exija las
cuatro rompe el flujo que hoy funciona.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

_RAIZ = pathlib.Path(__file__).resolve().parent.parent
_INDEX = _RAIZ / "static" / "index.html"
_MAIN_TF = _RAIZ / "terraform" / "main.tf"

# (password, valida, por qué)
CASOS = [
    ("Huawei1234", True, "min+may+dígito = 3 clases; la que se venía usando"),
    ("Huawei12!", True, "las 4 clases"),
    ("Abcdefg1", True, "3 clases, 8 caracteres justos"),
    ("Abcdefg!", True, "min+may+símbolo, sin dígito"),
    ("Passw0rd", True, "3 clases"),
    ("Abcdefgh1jklmnopqrstuvwxyz012345", True, "32 justos"),
    ("abcd1234", False, "solo 2 clases"),
    ("ABCD1234", False, "solo 2 clases"),
    ("aaaaaaaa", False, "una sola clase"),
    ("Abc1!", False, "5 caracteres"),
    ("A" * 33 + "b1", False, "35 > 32"),
    ("", False, "vacía"),
]


def test_el_campo_de_password_no_viene_prellenado():
    html = _INDEX.read_text(encoding="utf-8")
    m = re.search(r'<input[^>]*id="output-password"[^>]*>', html)
    assert m, "desapareció el input #output-password"
    campo = m.group(0)
    assert "value=" not in campo, (
        f"el campo de contraseña volvió a traer un valor por defecto: {campo}")
    assert 'type="password"' in campo


def test_no_queda_ninguna_password_hardcodeada_en_el_front():
    """Había dos: el `value` del markup y `state.osPassword || 'Huawei1234'`, que
    se usaba al reconstruir el body del deploy después de un reload."""
    html = _INDEX.read_text(encoding="utf-8")
    # Los comentarios explican el cambio y pueden nombrar el literal; el código no.
    # Los dos `sub` van por separado a propósito: con DOTALL, el `.*` de la variante
    # `//` se come desde el primer comentario de línea hasta el final del archivo y
    # el test pasa comparando contra la nada.
    codigo = re.sub(r"/\*.*?\*/", "", html, flags=re.DOTALL)
    codigo = re.sub(r"^\s*//.*$", "", codigo, flags=re.MULTILINE)
    assert "Huawei1234" not in codigo, (
        "volvió una contraseña por defecto al front. Un deploy tiene que usar la que "
        "el usuario escribe, no una que está en un repo público.")


@pytest.fixture(scope="module")
def politica_js() -> str:
    """El cuerpo del `test:` de output-password, tal cual está en el HTML."""
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("id: 'output-password'")
    bloque = html[i:html.index("msg:", i)]
    m = re.search(r"test:\s*(v\s*=>.*?),\s*$", bloque, re.DOTALL | re.MULTILINE)
    assert m, f"no encontré el `test:` de output-password en:\n{bloque}"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_la_validacion_del_wizard_acepta_y_rechaza_lo_que_debe(politica_js):
    """Corre la función REAL del HTML, no una reimplementación: si se reescribe el
    regex y se rompe el escapado de la clase de símbolos, se ve acá."""
    script = (f"const test = {politica_js};\n"
              f"const casos = {json.dumps([c[0] for c in CASOS])};\n"
              "console.log(JSON.stringify(casos.map(p => !!test(p))));")
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert r.returncode == 0, f"la validación no es JS válido:\n{r.stderr}"
    got = json.loads(r.stdout.strip())

    malos = [f"{pw!r} dio {g} y debía dar {esperado} ({porque})"
             for (pw, esperado, porque), g in zip(CASOS, got) if g != esperado]
    assert not malos, "la política del wizard no coincide con Huawei CSS:\n" + "\n".join(malos)


def test_terraform_valida_la_misma_politica():
    """El bloque `validation` tiene que existir y pedir lo mismo: 8-32 y 3 de 4.

    No alcanza con que el front valide — un deploy puede no pasar por el formulario,
    y `terraform/main.tf` declaraba la variable sin ninguna restricción."""
    tf = _MAIN_TF.read_text(encoding="utf-8")
    i = tf.index('variable "opensearch_password"')
    bloque = tf[i:tf.index("\n}\n", i)]
    # El bloque, anclado: un `in bloque` a secas da verdadero para cualquier cosa
    # que contenga la palabra, incluido un `x_validation` que Terraform ignora.
    assert re.search(r"^\s*validation\s*\{", bloque, re.MULTILINE), (
        "terraform/main.tf declara opensearch_password sin `validation`: una "
        "contraseña inválida se descubre recién cuando el apply crea el cluster.")
    assert ">= 8" in bloque and "<= 32" in bloque
    assert ">= 3" in bloque, "la condición dejó de pedir 3 de las 4 clases"
