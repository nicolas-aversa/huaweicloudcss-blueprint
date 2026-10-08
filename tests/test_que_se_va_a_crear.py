"""El resumen del paso 4 ("Qué se va a crear"): la infraestructura con el nodo
que va a tener, la ingesta (un pipeline por caso) y lo que queda configurado
en OpenSearch. Se prueba en node con el código real."""
import pathlib
import shutil
import subprocess

import pytest

_INDEX = pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html"

_ARNES = r"""
const icon = (n) => `<svg data-i="${n}"></svg>`;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const SECURITY_SLUGS = new Set(['siem', 'fortianalyzer']);
""" + "{FUNCION}" + r"""
const f = [];
const check = (n, c, x) => { if (!c) f.push(n + ' -> ' + x); };
const c = (id, extra = {}) => ({ id, label: id.toUpperCase(), icon: 'box', ...extra });
let h = queSeVaACrearHTML({ casos: [c('ventas'), c('pozos', { perfil: 'Pozo' })] });
check('infra con el nodo chico', h.includes('<strong>OpenSearch (CSS)</strong><small>1 nodo de 4 vCPU y 8 GB, 40 GB de disco</small>'), h);
check('un pipeline por caso', h.includes('Ingesta · 2 pipelines') && h.includes('<span class="crear__chip"><svg data-i="box"></svg> VENTAS</span>'), h);
check('sin seguridad, sin su mosaico', !h.includes('Security Analytics'), h);
check('perfil donde hay', h.includes('<strong>Perfil por entidad</strong><small>en 1 caso</small>'), h);
check('sin analista, sin su mosaico', !h.includes('Analista enmascarado'), h);
check('la retención', h.includes('retención de 90 días'), h);
h = queSeVaACrearHTML({ casos: [c('siem'), c('cts')], retencion: 30 });
check('con Security Analytics, el nodo grande', h.includes('1 nodo de 8 vCPU y 16 GB, 80 GB de disco'), h);
check('y su mosaico', h.includes('<strong>Security Analytics</strong><small>SIEM: reglas Sigma y correlaciones</small>'), h);
check('la retención pedida', h.includes('retención de 30 días'), h);
h = queSeVaACrearHTML({ casos: [1, 2, 3, 4, 5].map(i => c('x' + i)) });
check('5 casos, nodo grande', h.includes('8 vCPU'), h);
h = queSeVaACrearHTML({ casos: [c('ventas')], reuse: true });
check('con el entorno arriba, sin infra', !h.includes('Infraestructura') && h.includes('Se agrega · 1 pipeline'), h);
h = queSeVaACrearHTML({ casos: [c('ventas')], clusterExistente: true });
check('cluster existente', h.includes('<strong>OpenSearch</strong><small>el cluster existente</small>'), h);
h = queSeVaACrearHTML({ casos: [c('ventas')], conPlugins: false });
check('sin plugins, solo dashboards y ciclo de vida', !h.includes('Forecasting') && h.includes('Dashboards y búsquedas'), h);
h = queSeVaACrearHTML({ casos: [c('<x>')] });
check('escapado', h.includes('&lt;X&gt;') && !h.includes('<X>'), h);
h = queSeVaACrearHTML({ builder: true });
check('el builder', h.includes('Qué guarda el dataset') && h.includes('Los plugins que tus datos justifican'), h);
console.log(f.join('\n')); process.exit(f.length ? 1 : 0);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node no está instalado")
def test_el_resumen_en_node(tmp_path):
    html = _INDEX.read_text(encoding="utf-8")
    i = html.index("    const _CASOS_PESADO = 5;")
    js = tmp_path / "crear.mjs"
    js.write_text(_ARNES.replace("{FUNCION}", html[i:html.index("    // Cambiar el tipo activo del review", i)]), encoding="utf-8")
    r = subprocess.run(["node", str(js)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_la_regla_del_nodo_es_la_del_backend():
    import main
    assert main._CASOS_PESADO == 5, "el front repite la regla: si cambia acá, cambia en queSeVaACrearHTML"
