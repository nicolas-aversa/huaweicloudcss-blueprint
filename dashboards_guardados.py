"""Los dashboards que los SAs mejoran en un entorno, guardados por caso.

La plataforma carga sus dashboards en cada deploy (y los vuelve a escribir, con
el mismo id, cada vez que aplica el index template). Lo que un SA arma o
corrige en OpenSearch Dashboards vive en el índice `.kibana` del cluster y se
perdía al destruir el entorno. "Guardar cambios" (vista del entorno) lo copia
acá, por caso y para toda la instancia: el próximo deploy de ese caso, de
cualquier SA, lo carga después de los de la plataforma.

Qué cuenta como cambio, sin registro previo (anda también en entornos
desplegados antes de esto):
  - la plataforma escribe todos los objetos de un caso en un solo `_bulk` con
    el mismo `updated_at`, y OpenSearch Dashboards lo actualiza al guardar. Un
    objeto de la plataforma con otra fecha lo tocó alguien;
  - un objeto con un id que la plataforma no genera es nuevo. Es del caso si
    apunta (por `references`, o por nombre, como TSVB y Vega) al index pattern
    del caso o a otro objeto del caso.
Al comparar se ignora el rango de fechas del dashboard: la plataforma lo ajusta
sola a los datos (`_update` que no toca `updated_at`).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import auth

TIPOS = ("dashboard", "visualization", "search", "visualization-visbuilder", "index-pattern")
# Lo que la plataforma reescribe sola en un dashboard: no es un cambio de nadie.
_CLAVES_DE_TIEMPO = ("timeRestore", "timeFrom", "timeTo", "refreshInterval")


def _dir() -> Path:
    return auth.DATA_ROOT / "dashboards_de_casos"


def _ruta(slug: str) -> Path:
    return _dir() / f"{re.sub(r'[^a-z0-9_-]+', '-', slug.lower())}.json"


def leer(slug: str) -> dict[str, dict]:
    """`{"<tipo>:<id>": objeto}` de lo guardado para el caso."""
    try:
        return (json.loads(_ruta(slug).read_text(encoding="utf-8")) or {}).get("objetos") or {}
    except (OSError, ValueError):
        return {}


def guardar(slug: str, objetos: list[dict], quien: str) -> None:
    """Suma (o reemplaza, por id) `objetos` a lo guardado del caso."""
    actuales = leer(slug)
    cuando = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for o in objetos:
        actuales[f"{o['type']}:{o['id']}"] = {**o, "guardado_por": quien, "guardado_en": cuando}
    _dir().mkdir(parents=True, exist_ok=True)
    _ruta(slug).write_text(json.dumps({"slug": slug, "objetos": actuales}, ensure_ascii=False, indent=1),
                           encoding="utf-8")


def ndjson(slug: str) -> str:
    """Lo guardado del caso, como NDJSON de saved objects (para cargarlo)."""
    lineas = [json.dumps({"type": o["type"], "id": o["id"], "attributes": o.get("attributes") or {},
                          "references": o.get("references") or []}, ensure_ascii=False)
              for o in leer(slug).values()]
    return "\n".join(lineas) + ("\n" if lineas else "")


def reemplaza_al_principal(slug: str, plataforma: str, id_principal: str) -> bool:
    """Si lo guardado del caso trae su propio dashboard principal (el mismo id
    que el de la plataforma) y no lleva a los de al lado de la plataforma
    (Problemas, Tiempos): entonces nadie llega a ellos y repiten lo que armó el
    SA. Uno que editó el principal generado conserva el encabezado con los
    links, y ahí los de al lado siguen."""
    guardados = leer(slug)
    principal = guardados.get(f"dashboard:{id_principal}")
    if not principal:
        return False
    de_la_plataforma = objetos_del_ndjson(plataforma)
    al_lado = [o["id"] for o in de_la_plataforma.values() if o["type"] == "dashboard" and o["id"] != id_principal]
    usados = [guardados.get(f"{r.get('type')}:{r.get('id')}") or de_la_plataforma.get(f"{r.get('type')}:{r.get('id')}")
              for r in principal.get("references") or []]
    texto = json.dumps([principal] + usados)
    return not any(i in texto for i in al_lado)


def objetos_del_ndjson(contenido: str) -> dict[str, dict]:
    """`{"<tipo>:<id>": {type, id, attributes, references}}` de un NDJSON."""
    out: dict[str, dict] = {}
    for linea in (contenido or "").splitlines():
        try:
            o = json.loads(linea)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("type") and o.get("id"):
            out[f"{o['type']}:{o['id']}"] = {"type": o["type"], "id": o["id"],
                                             "attributes": o.get("attributes") or {},
                                             "references": o.get("references") or []}
    return out


def de_kibana(hits: list[dict]) -> dict[str, dict]:
    """Los docs de `.kibana` (`_id` = `<tipo>:<id>`, el objeto bajo `<tipo>`)
    con la misma forma que `objetos_del_ndjson`, más `updated_at`."""
    out: dict[str, dict] = {}
    for h in hits:
        src, clave = h.get("_source") or {}, str(h.get("_id") or "")
        tipo = src.get("type")
        if tipo not in TIPOS or ":" not in clave:
            continue
        out[clave] = {"type": tipo, "id": clave.split(":", 1)[1], "attributes": src.get(tipo) or {},
                      "references": src.get("references") or [], "updated_at": src.get("updated_at") or ""}
    return out


def _forma(o: dict) -> str:
    """Lo que se compara de un objeto: atributos (sin el rango de fechas) y referencias."""
    attrs = {k: v for k, v in (o.get("attributes") or {}).items()
             if not (o.get("type") == "dashboard" and k in _CLAVES_DE_TIEMPO)}
    refs = sorted((r.get("type", ""), r.get("id", ""), r.get("name", "")) for r in o.get("references") or [])
    return json.dumps([attrs, refs], sort_keys=True, ensure_ascii=False)


def titulo(o: dict) -> str:
    return str((o.get("attributes") or {}).get("title") or o.get("id"))


def _limpio(o: dict, estado: str) -> dict:
    return {"type": o["type"], "id": o["id"], "attributes": o["attributes"], "references": o["references"],
            "titulo": titulo(o), "estado": estado}


def _nombra(o: dict, patrones: list[str]) -> bool:
    """El objeto nombra un index pattern del caso dentro de sus atributos (TSVB,
    Vega y timeline no usan `references`)."""
    texto = json.dumps(o.get("attributes") or {}, ensure_ascii=False)
    return any(re.search(r"(?<![\w-])" + re.escape(p), texto) for p in patrones)


def del_caso(en_kibana: dict[str, dict], plataforma: dict[str, dict]) -> dict[str, dict]:
    """Lo que alguien armó o cambió en UN caso: lo nuevo del caso y lo de la
    plataforma con otro contenido. Cada objeto lleva `estado` ("nuevo" o
    "modificado") y `titulo`."""
    # La fecha del lote de la plataforma: la que comparten casi todos sus objetos.
    fechas = Counter(o["updated_at"] for k, o in en_kibana.items() if k in plataforma)
    lote = fechas.most_common(1)[0][0] if fechas else None
    elegidos: dict[str, dict] = {}
    for k, o in en_kibana.items():
        if k not in plataforma or o["type"] == "index-pattern" or o["updated_at"] == lote:
            continue
        if _forma(o) != _forma(plataforma[k]):
            elegidos[k] = _limpio(o, "modificado")
    # Lo nuevo: lo que apunta al caso, directo o a través de otro objeto del caso.
    patrones = [titulo(o) for o in plataforma.values() if o["type"] == "index-pattern"]
    del_caso = set(plataforma) | set(elegidos)
    agregado = True
    while agregado:
        agregado = False
        for k, o in en_kibana.items():
            if k in del_caso or o["type"] == "index-pattern":
                continue
            refs = {f"{r.get('type')}:{r.get('id')}" for r in o["references"]}
            if refs & del_caso or _nombra(o, patrones):
                elegidos[k] = _limpio(o, "nuevo")
                del_caso.add(k)
                agregado = True
    # Lo que usa algo de lo elegido viaja con ello, aunque no apunte al caso: un
    # index pattern que creó un SA, o un panel de texto (markdown, sin
    # referencias) que un SA puso en un dashboard del caso. Sin esto, el
    # dashboard guardado quedaba con un panel roto en el próximo deploy.
    pendientes = list(elegidos.values())
    while pendientes:
        o = pendientes.pop()
        for r in o["references"]:
            k = f"{r.get('type')}:{r.get('id')}"
            if k in en_kibana and k not in plataforma and k not in elegidos:
                elegidos[k] = _limpio(en_kibana[k], "nuevo")
                pendientes.append(elegidos[k])
    return elegidos


def pendientes(elegidos: dict[str, dict], guardados: dict[str, dict]) -> list[dict]:
    """De `del_caso`, lo que todavía no está guardado así."""
    return [o for k, o in elegidos.items() if k not in guardados or _forma(guardados[k]) != _forma(o)]


def sueltos(en_kibana: dict[str, dict], de_la_plataforma: set[str], de_algun_caso: set[str]) -> list[str]:
    """Títulos de lo que un SA armó y no es de ningún caso del entorno (no se guarda)."""
    return sorted(titulo(o) for k, o in en_kibana.items()
                  if o["type"] in ("dashboard", "visualization", "search", "visualization-visbuilder")
                  and k not in de_la_plataforma and k not in de_algun_caso)
