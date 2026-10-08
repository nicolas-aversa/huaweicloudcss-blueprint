"""El resumen del paso 4 ("Qué se va a crear"), en tres filas compactas: la
infraestructura con el nodo que va a tener, lo que va en Logstash (un pipeline por caso)
y lo que queda configurado en OpenSearch. Se prueba en node con el código
real."""
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
check('infra con el nodo chico', h.includes('OpenSearch (CSS) <small>· 1 nodo, 4 vCPU y 8 GB, 40 GB de disco</small>'), h);
check('y el de Logstash', h.includes('Logstash (CSS) <small>· 1 nodo, 4 vCPU y 8 GB, 40 GB de disco</small>'), h);
check('un pipeline por caso', h.includes('En Logstash</span>') && (h.match(/class="crear__chip"/g) || []).length === 2 && h.includes('<span class="crear__chip"><svg data-i="box"></svg><span>VENTAS</span></span>'), h);
check('sin seguridad, no aparece', !h.includes('Security Analytics'), h);
check('perfil donde hay', h.includes('Perfil por entidad <small>· 1 caso</small>'), h);
check('sin analista, no aparece', !h.includes('Analista enmascarado'), h);
check('la retención', h.includes('retención de 90 días'), h);
h = queSeVaACrearHTML({ casos: [c('siem'), c('cts')], retencion: 30 });
check('con Security Analytics, el nodo grande', h.includes('OpenSearch (CSS) <small>· 1 nodo, 8 vCPU y 16 GB, 80 GB de disco</small>'), h);
check('Logstash sigue en el chico', h.includes('Logstash (CSS) <small>· 1 nodo, 4 vCPU y 8 GB, 40 GB de disco</small>'), h);
check('y para qué casos', h.includes('Security Analytics <small>· SIEM, con correlaciones</small>'), h);
check('lo que hace, al pasar el mouse', h.includes('title="reglas Sigma y detectores por fuente"'), h);
check('la retención pedida', h.includes('retención de 30 días'), h);
h = queSeVaACrearHTML({ casos: [1, 2, 3, 4, 5].map(i => c('x' + i)) });
check('5 casos, nodo grande', h.includes('8 vCPU'), h);
h = queSeVaACrearHTML({ casos: [c('ventas')], reuse: true });
check('con el entorno arriba, sin infra', !h.includes('Infraestructura') && h.includes('En Logstash'), h);
check('tres filas', (queSeVaACrearHTML({ casos: [c('ventas')] }).match(/class="crear__fila"/g) || []).length === 3, h);
h = queSeVaACrearHTML({ casos: [c('ventas')], clusterExistente: true });
check('cluster existente', h.includes('OpenSearch <small>· el cluster existente</small>'), h);
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
    """El front repite la regla y los tamaños (`_NODO_CHICO`, `_NODO_GRANDE`):
    si cambian acá, cambian en queSeVaACrearHTML."""
    import main
    assert main._CASOS_PESADO == 5
    chico, grande = main._capacity_for(1), main._capacity_for(1, pesado=True)
    assert (chico["logstash_flavor"], chico["logstash_volume_size"]) == ("ess.spec-4u8g", 40)
    assert (chico["opensearch_flavor"], chico["opensearch_volume_size"]) == ("ess.spec-4u8g", 40)
    assert (grande["opensearch_flavor"], grande["opensearch_volume_size"]) == ("ess.spec-8u16g", 80)
    assert (grande["logstash_flavor"], grande["logstash_volume_size"]) == ("ess.spec-4u8g", 40)
    html = _INDEX.read_text(encoding="utf-8")
    assert "const _NODO_CHICO = '1 nodo, 4 vCPU y 8 GB, 40 GB de disco';" in html
    assert "const _NODO_GRANDE = '1 nodo, 8 vCPU y 16 GB, 80 GB de disco';" in html


def test_el_cluster_existente_es_un_plegable():
    """El endpoint de un cluster que ya existe: plegado, con lo elegido en el
    renglón (se lee aunque esté cerrado). Su texto de ayuda lleva estilo propio;
    `form-hint` no tiene regla global a propósito: una con el gris tenue bajaba
    el contraste de los avisos del resto de la app (el error de "CSS rechazó el
    configuration file" en la puesta en marcha quedaba en 2,9:1)."""
    html = _INDEX.read_text(encoding="utf-8")
    assert '<details id="demo-cluster-field" class="cluster-existente hidden">' in html
    assert 'id="demo-cluster-endpoint"' in html and 'id="demo-cluster-resumen"' in html
    assert "classList.toggle('en-uso', !!valor)" in html
    assert ".cluster-existente__cuerpo .form-hint {" in html
    assert "\n    .form-hint {" not in html
