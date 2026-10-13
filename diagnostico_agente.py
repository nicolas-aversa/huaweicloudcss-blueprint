"""
diagnostico_agente.py
=====================

Revisa, contra un cluster desplegado, la búsqueda por significado del agente
conversacional: si el agente tiene su herramienta (`BuscarTextos-<caso>`), el
modelo de consultas de NVIDIA, la herramienta sola (`VectorDBTool`) y el agente
completo con una pregunta, con las trazas de lo que usó.

Corre donde se alcance el cluster (su IP privada, desde la ECS). La contraseña
de admin va por la variable OSP y no se imprime. Desde la ECS, sin rearmar el
contenedor:

    OSP='<contraseña>' docker compose -f docker-compose.hosted.yml exec -T -e OSP app \
        python - https://<ip>:9200 < diagnostico_agente.py
"""
import json
import os
import sys

import requests
import urllib3

urllib3.disable_warnings()
AUTH = ("admin", os.environ.get("OSP", ""))
# Por caso: el tema para la herramienta sola y la pregunta para el agente.
TEMAS = {
    "envios": ("paquetes rotos", "¿Cuántos paquetes llegaron rotos en junio y en qué centro?"),
    "pozos": ("choke congelado", "¿Cuántas veces se congeló el choke en julio y en qué pozos?"),
    "prepaga": ("dolor de espalda", "¿Cuántas autorizaciones hubo por dolor de espalda y en qué especialidad?"),
    "acarreo": ("operador dormido", "¿Cuántas veces se durmió un operador en septiembre y en qué turno?"),
    "emergencias": ("falta de aire", "¿Cuántos llamados hubo por falta de aire en julio?"),
}


def base_del_cluster(destino: str) -> str:
    """La URL que contesta: la dada o, con solo la IP, https y después http."""
    candidatas = [destino] if destino.startswith("http") else [f"https://{destino}", f"http://{destino}"]
    for b in candidatas:
        try:
            if requests.get(b, auth=AUTH, verify=False, timeout=10).status_code == 200:
                return b.rstrip("/")
        except requests.RequestException:
            pass
    return ""


def main() -> None:
    destino = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("OS_URL", "")).strip()
    base = base_del_cluster(destino)
    print("1 cluster:", base or f"no contesta ({destino})")
    if not base:
        return

    def q(metodo, ruta, cuerpo=None, t=60):
        r = requests.request(metodo, base + ruta, auth=AUTH, json=cuerpo, verify=False, timeout=t)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text[:300]

    _, a = q("POST", "/_plugins/_ml/agents/_search",
             {"query": {"match_phrase": {"name": "Platform Conversational Root"}}, "size": 5})
    agentes = a.get("hits", {}).get("hits", []) if isinstance(a, dict) else []
    for h in agentes:
        print("2 agente", h["_id"], [(t.get("type"), t.get("name")) for t in h["_source"].get("tools", [])])
    if not agentes:
        print("2 agente: no hay")
    _, m = q("POST", "/_plugins/_ml/models/_search",
             {"query": {"match_phrase": {"name": "plataforma-embeddings-consultas"}}, "size": 3})
    modelos = [(h["_id"], h["_source"].get("model_state")) for h in (m.get("hits", {}).get("hits", []) if isinstance(m, dict) else [])]
    print("3 modelo de consultas:", modelos)
    _, ix = q("GET", "/_cat/indices/busqueda-*?format=json&h=index,docs.count")
    print("4 índices:", ix)
    indice = ix[0]["index"] if isinstance(ix, list) and ix else ""
    tema, pregunta = next((v for k, v in TEMAS.items() if k in indice), TEMAS["envios"])

    if indice and modelos:
        s, r = q("POST", "/_plugins/_ml/tools/_execute/VectorDBTool", {"parameters": {
            "input": tema, "index": indice, "embedding_field": "embedding", "source_field": '["texto", "cuenta"]',
            "model_id": modelos[0][0], "k": "10", "doc_size": "10"}})
        print(f"5 VectorDBTool «{tema}»:", s, json.dumps(r, ensure_ascii=False)[:700])
    if agentes:
        s, r = q("POST", f"/_plugins/_ml/agents/{agentes[0]['_id']}/_execute", {"parameters": {"question": pregunta}}, t=240)
        if s == 200:
            salida = {o.get("name"): (o.get("result") or (o.get("dataAsMap") or {}).get("response"))
                      for o in r["inference_results"][0]["output"]}
        else:
            salida = r
        print(f"6 agente «{pregunta}»:", s, json.dumps(salida, ensure_ascii=False)[:900])
        pid = salida.get("parent_interaction_id") if isinstance(salida, dict) else None
        if pid:
            _, tr = q("GET", f"/_plugins/_ml/memory/message/{pid}/traces")
            for t in (tr.get("traces") or []) if isinstance(tr, dict) else []:
                print("  traza", t.get("trace_number"), t.get("origin"), "|", str(t.get("input"))[:150],
                      "=>", str(t.get("response"))[:250])


if __name__ == "__main__":
    main()
