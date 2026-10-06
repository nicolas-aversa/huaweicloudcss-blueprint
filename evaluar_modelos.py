"""Compara modelos de MaaS como generadores de PPL del asistente.

Para cada dataset guardado toma sus preguntas sugeridas y el MISMO system prompt que
usa el chat (`build_ppl_system_prompt` + `_reglas_del_chat`), le pide la
consulta a cada modelo y la revisa:

- sin cluster: que empiece con `source=<índice del caso>`, que no traiga texto
  de más (markdown, explicaciones) y que los campos con punto existan en el caso;
- con `--cluster`: además la ejecuta en `_plugins/_ppl` del entorno desplegado
  y cuenta las que corren.

Uso:  py evaluar_modelos.py [--cluster] [--preguntas N] [--modelos a,b,c]
No imprime credenciales ni datos: solo la consulta generada y si anduvo.
"""
from __future__ import annotations

import argparse
import re
import sys
import time

import capabilities as caps
import custom_cases
import maas_integrator as maas

# MaaS admite 1 pedido por segundo por cuenta (ModelArts.81101): se espera entre
# llamados y se reintenta un 429, o la comparación mide el límite y no al modelo.
_PAUSA_S = 1.2
_REINTENTOS_429 = 4
MODELOS = ["deepseek-v4-flash", "deepseek-v4.1-flash", "deepseek-v4-pro", "glm-5.1", "glm-5.2"]
# Lo que la consulta puede nombrar sin que sea un campo del caso.
_CAMPOS_SIEMPRE = {"@timestamp"}
_CAMPO_CON_PUNTO = re.compile(r"(?<![\w'\"@.])([A-Za-z_][\w]*(?:\.[\w@]+)+)")


def limpiar(texto: str) -> str:
    """La consulta tal como la usaría el chat: sin cercos de código."""
    t = (texto or "").strip()
    t = re.sub(r"^```\w*\s*|```$", "", t).strip()
    return t


def revisar(ppl: str, index_pattern: str, campos: set[str]) -> list[str]:
    """Los problemas que se ven sin ejecutarla (vacío = se ve bien)."""
    problemas = []
    if not re.match(rf"^source\s*=\s*{re.escape(index_pattern)}(\s|\||$)", ppl):
        problemas.append("no arranca con source=" + index_pattern)
    if "\n\n" in ppl or re.search(r"\b(Here|Aquí|Esta consulta|This query)\b", ppl):
        problemas.append("trae texto además de la consulta")
    sin_literales = re.sub(r"'[^']*'|\"[^\"]*\"", "''", ppl)
    desconocidos = sorted({c for c in _CAMPO_CON_PUNTO.findall(sin_literales)
                           if c not in campos and c not in _CAMPOS_SIEMPRE
                           and not re.match(r"^\d", c) and c.split(".")[0] not in {"x", "y", "l", "r"}})
    if desconocidos:
        problemas.append("campos que no existen: " + ", ".join(desconocidos[:4]))
    return problemas


def casos(n_preguntas: int) -> list[dict]:
    """Los datasets guardados con campos y preguntas sugeridas; el spec, el
    mismo que arma el chat de sus campos."""
    fuera = []
    for c in custom_cases.list_cases():
        slug, preguntas = c["slug"], c.get("suggested_questions") or []
        spec = caps.build_spec_from_fields(slug, f"{c.get('index_base') or slug}-*", c.get("fields") or [],
                                           c.get("label", slug))
        if spec.get("fields") and preguntas:
            fuera.append({"slug": slug, "spec": spec, "preguntas": preguntas[:n_preguntas]})
    return fuera


def prompt_de(spec: dict, slug: str) -> str:
    import main
    sp = caps.build_ppl_system_prompt(spec["index_pattern"], spec.get("operations", []), spec.get("fields", {}),
                                      spec.get("success_code", ""), spec.get("label", slug), ppl_v3=True)
    return sp + main._reglas_del_chat(True)


# `modelo:razona` lo pide con el razonamiento prendido; sin sufijo, apagado (como
# los connectors de la plataforma). glm-5.3 razona siempre y no acepta el
# parámetro: se le pide sin él.
_SIEMPRE_RAZONA = {"glm-5.3"}


def _pedir(cliente, modelo: str, sistema: str, pregunta: str):
    nombre, _, modo = modelo.partition(":")
    extra = {} if nombre in _SIEMPRE_RAZONA else {"chat_template_kwargs": {"thinking": modo == "razona"}}
    for intento in range(_REINTENTOS_429 + 1):
        time.sleep(_PAUSA_S)
        try:
            return cliente.chat.completions.create(
                model=nombre, temperature=0, extra_body=extra,
                messages=[{"role": "system", "content": sistema}, {"role": "user", "content": pregunta}])
        except Exception as exc:  # noqa: BLE001
            if "81101" not in str(exc) or intento == _REINTENTOS_429:
                raise
            time.sleep(2 * (intento + 1))


def main_(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cluster", action="store_true", help="ejecutar cada consulta en el entorno desplegado")
    ap.add_argument("--preguntas", type=int, default=3, help="preguntas por caso")
    ap.add_argument("--modelos", default=",".join(MODELOS))
    a = ap.parse_args(argv)
    modelos = [m.strip() for m in a.modelos.split(",") if m.strip()]
    cliente = maas._build_client()

    ejecutar = None
    if a.cluster:
        import main
        td = main._active_terraform_dir()
        cluster = main._cluster_with_public_access(td)
        base = main._os_base(cluster, main._read_https_enabled_from_state(td))
        pw = main._cluster_admin_password(td)

        def ejecutar(ppl: str) -> tuple[bool, str]:
            r = main._os_req("POST", f"{base}/_plugins/_ppl", "admin", pw, json_body={"query": ppl}, timeout=60)
            if r is not None and r.status_code == 200:
                return True, f"{len((r.json() or {}).get('datarows') or [])} filas"
            return False, (getattr(r, "text", "") or "sin respuesta")[:160].replace("\n", " ")

    totales = {m: {"ok": 0, "n": 0, "seg": 0.0} for m in modelos}
    lista = casos(a.preguntas)
    if ejecutar:
        # Solo los casos que este entorno tiene: los otros fallan por "no such
        # index" y eso no dice nada del modelo.
        r = main._os_req("GET", f"{base}/_cat/indices?format=json&h=index", "admin", pw, timeout=30)
        indices = [i["index"] for i in (r.json() if r is not None and r.status_code == 200 else [])]
        lista = [c for c in lista if any(re.fullmatch(c["spec"]["index_pattern"].replace("*", ".*"), i) for i in indices)]
        print("casos en el cluster:", ", ".join(c["slug"] for c in lista))
    for caso in lista:
        spec, slug = caso["spec"], caso["slug"]
        sp = prompt_de(spec, slug)
        campos = set(spec.get("fields", {}))
        print(f"\n## {slug}")
        for q in caso["preguntas"]:
            print(f"  ? {q}")
            for m in modelos:
                t = time.time()
                try:
                    r = _pedir(cliente, m, sp, q)
                    ppl = limpiar(r.choices[0].message.content)
                except Exception as exc:  # noqa: BLE001
                    ppl, problemas = "", [f"el modelo falló: {repr(exc)[:100]}"]
                else:
                    problemas = revisar(ppl, spec["index_pattern"], campos)
                seg = time.time() - t
                if not problemas and ejecutar:
                    ok, detalle = ejecutar(ppl)
                    if not ok:
                        problemas = [f"el cluster la rechazó: {detalle}"]
                tot = totales[m]
                tot["n"] += 1
                tot["seg"] += seg
                tot["ok"] += not problemas
                marca = "OK " if not problemas else "XX "
                print(f"    {marca}{m:20s} {seg:4.1f}s  {ppl[:150]}" + ("" if not problemas else f"\n        -> {'; '.join(problemas)}"))
    print("\n## Resumen" + (" (ejecutadas en el cluster)" if ejecutar else " (revisión sin ejecutar)"))
    for m, t in sorted(totales.items(), key=lambda kv: (-kv[1]["ok"], kv[1]["seg"])):
        if t["n"]:
            print(f"  {m:20s} {t['ok']}/{t['n']} bien · {t['seg'] / t['n']:.1f}s promedio")
    return 0


if __name__ == "__main__":
    sys.exit(main_(sys.argv[1:]))
