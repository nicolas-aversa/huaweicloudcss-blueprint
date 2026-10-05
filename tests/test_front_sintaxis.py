"""El JavaScript de la página compila. Un recorte mal hecho dejó dos líneas
sueltas en index.html, la vista dejó de cargar y la suite pasaba igual: los
tests del front prueban funciones sueltas, no la página entera."""
import pathlib
import re
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_el_javascript_de_la_pagina_compila(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    js = "\n".join(re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", html, re.S))
    assert len(js) > 10000, "no encontró los scripts de la página"
    f = tmp_path / "pagina.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr[:600]
