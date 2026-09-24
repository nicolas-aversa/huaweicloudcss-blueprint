"""El index template de cada caso de ejemplo nombra TODOS los campos que llegan.

El template sale de la lista `fields` de cada caso, y esas listas estaban
armadas a mano: FortiAnalyzer mandaba 82 campos al índice y declaraba 19. Lo que
faltaba igual se indexaba (keyword por el dynamic template), pero sin tipo
propio: `duration`, un número que el filter convierte, entraba sin declarar, y
el `proto` que el filter convierte a entero figuraba como keyword en los
dashboards.

Estos tests leen el dataset REAL de cada caso (una muestra repartida por todo el
archivo), lo parsean como lo hace su filter y exigen que cada campo que llega
esté declarado.
"""
import csv
import json
import pathlib
import re

import pytest

import verticals
from index_template import build_index_template

_DATASETS = pathlib.Path(__file__).resolve().parent.parent / "datasets"
_KV = re.compile(r'([A-Za-z_][\w.\-]*)=("([^"]*)"|\S*)')
_PASO = 25   # una línea de cada 25: todos los tipos de evento, en segundos


def _aplanar(o, pre=()):
    out = {}
    for k, v in o.items():
        if isinstance(v, dict):
            out.update(_aplanar(v, pre + (k,)))
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            for x in v:   # lista de objetos: sus campos, como los mapea OpenSearch
                out.update(_aplanar(x, pre + (k,)))
        else:
            out[".".join(pre + (k,))] = v
    return out


def _filas(code: str, lineas):
    columnas = re.search(r'columns\s*=>\s*\[([^\]]*)\]', code)
    kv = re.search(r'kv\s*\{[^}]*source\s*=>\s*"([^"]+)"', code)
    for linea in lineas:
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        if columnas:
            nombres = re.findall(r'"([^"]+)"', columnas.group(1))
            yield dict(zip(nombres, next(csv.reader([linea]))))
        elif "json {" in code and linea.startswith("{"):
            yield _aplanar(json.loads(linea))
        elif kv:
            if kv.group(1) != "message":   # un grok antes del kv (alyc): lo que sigue
                partes = linea.split(" ", 2)
                linea = partes[2] if len(partes) == 3 else linea
            yield {m.group(1): m.group(3) if m.group(3) is not None else m.group(2)
                   for m in _KV.finditer(linea)}


def _muestra(slug):
    archivos = verticals.demo_dataset_files().get(slug) or []
    ruta = _DATASETS / archivos[0] if archivos else None
    if not ruta or not ruta.exists():
        pytest.skip(f"sin dataset local para {slug}")
    with ruta.open(encoding="utf-8", errors="replace") as f:
        return [l for i, l in enumerate(f) if i % _PASO == 0]


def _declarados(v):
    nombres = set()
    for f in v.get("fields") or []:
        for k in ("field_path", "raw_name"):
            if f.get(k):
                nombres.add(f[k])
    return nombres


# Los que se parsean con kv / json / csv directo. Billetera tiene su propio test
# (arma estructuras en el filter) y SIEM junta cuatro fuentes que no están en el repo.
_DIRECTOS = ["fortianalyzer", "transacciones-alyc", "streaming-ott", "produccion-pozos",
             "ventas-ecommerce", "encuentros-clinicos"]


@pytest.mark.parametrize("slug", _DIRECTOS)
def test_cada_campo_que_llega_esta_declarado(slug):
    v = verticals.get_vertical(slug)
    code = v["filter_code"]
    borrados = set()
    for m in re.finditer(r'remove_field\s*=>\s*\[([^\]]*)\]', code):
        borrados |= {x.strip("[]").replace("][", ".") for x in re.findall(r'"([^"]+)"', m.group(1))}
    llegan = set()
    for fila in _filas(code, _muestra(slug)):
        llegan |= {k for k in fila if k not in borrados and not k.startswith(("_", "@"))}

    faltan = sorted(llegan - _declarados(v))
    assert not faltan, f"{slug}: llegan al índice sin declarar en `fields`: {faltan}"


def test_billetera_declara_lo_que_arma_su_filter():
    """El filter de billetera parte el payload, parsea los pasos (JSON), el
    dispositivo (`~`) y arma el embudo: se replica eso sobre el dataset real."""
    v = verticals.get_vertical("transacciones-billetera")
    llegan = set()
    for linea in _muestra("transacciones-billetera"):
        m = re.match(r"^(\S+)\s+-\s+(\S+)\s+(.*)$", linea.strip())
        if not m:
            continue
        trx = dict(p.split("=", 1) for p in m.group(3).split("|") if "=" in p)
        if trx.get("operation_code") == "HEALTH_CHECK":
            continue
        pasos, detalle = trx.pop("steps", None), trx.pop("detail", None)
        llegan |= {f"transaction.{k}" for k in trx}
        if detalle:
            llegan |= {f"transaction.device.{p.split('=', 1)[0]}" for p in detalle.split("~") if "=" in p}
        if pasos:
            for paso in (json.loads(pasos).get("data") or {}).get("steps") or []:
                llegan |= {f"transaction.steps_parsed.data.steps.{k}" for k in paso}
    faltan = sorted(llegan - _declarados(v))
    assert not faltan, faltan


@pytest.mark.parametrize("slug", [v["slug"] for v in verticals.front_payload()["verticals"]])
def test_lo_que_el_filter_convierte_esta_declarado_con_ese_tipo(slug):
    """Un campo que el filter convierte a número tiene que estar en el template
    como número: si no, `duration` entraba sin declarar."""
    v = verticals.get_vertical(slug)
    tipos = {}
    for f in v.get("fields") or []:
        for k in ("field_path", "raw_name"):
            if f.get(k):
                tipos[f[k]] = f.get("type")
    equivalente = {"integer": {"integer", "long"}, "float": {"float", "double"},
                   "boolean": {"boolean"}, "string": {"string", "keyword", "text"}}
    mal = []
    for campo, tipo in re.findall(r'"(\[?[\w.\]\[]+)"\s*=>\s*"(integer|float|boolean|string)"',
                                  v.get("filter_code") or ""):
        campo = campo.strip("[]").replace("][", ".")
        if tipos.get(campo) not in equivalente[tipo]:
            mal.append(f"{campo}: filter={tipo} fields={tipos.get(campo)}")
    assert not mal, f"{slug}: {mal}"


@pytest.mark.parametrize("slug", [v["slug"] for v in verticals.front_payload()["verticals"]])
def test_los_dashboards_indexan_con_el_tipo_del_template(slug):
    """El index pattern de los dashboards curados dice el tipo de cada campo:
    `proto` figuraba como keyword con el filter convirtiéndolo a entero, y
    `latency` como long con valores de 0.013."""
    v = verticals.get_vertical(slug)
    props = build_index_template(v.get("fields") or [], "", "x-%{+YYYY.MM}")["template"]["mappings"]["properties"]

    def tipo(path):
        nodo = props
        for parte in path.split("."):
            if parte not in nodo:
                return None
            nodo = nodo[parte].get("properties", nodo[parte])
        return nodo.get("type")

    mal = []
    for d in [v.get("dashboard") or {}] + list((v.get("extra_dashboards") or {}).values()):
        for path, es in d.get("index_fields") or []:
            t = tipo(path)
            if t and t != es and not (t == "text" and es == "keyword"):
                mal.append(f"{path}: template={t} dashboard={es}")
    assert not mal, f"{slug}: {sorted(set(mal))}"


def test_las_metricas_sdwan_llegan_como_numero():
    """`packetloss` venía como "0.000%" y los anchos de banda como "0kbps": los
    dashboards los graficaban como números y no podían. El filter los limpia
    antes de convertir (en otro mutate: convert corre antes que gsub)."""
    code = verticals.get_vertical("fortianalyzer")["filter_code"]
    gsub = code.index('"packetloss", "%", ""')
    convert = code.index('"packetloss" => "float"')
    assert gsub < convert
    entre = code[gsub:convert]
    assert "}\n  mutate {" in entre, "gsub y convert en mutates distintos"
