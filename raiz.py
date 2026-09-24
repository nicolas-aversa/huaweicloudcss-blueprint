"""Los campos de un dataset nuevo van a la raíz del documento, como en los casos
de ejemplo.

Los casos de fábrica guardan sus campos en la raíz (`category`, `customer_id`,
`sucursal`), y así los ve el chat y así se escriben las consultas. Los datasets
nuevos caían bajo `data.` (`data.sucursal`), que no suma nada y obliga a
anteponerlo en cada búsqueda, en cada panel y en cada pregunta.

Los generadores (el perfilador, el LLM, los del catálogo) siguen armando todo
bajo `data`: ahí está todo lo que ya se probó contra la gramática de Logstash
—las referencias `[data][x]` del `mutate`, del `date`, del ruby de geo— y no
se toca. `data` queda como área de trabajo, y al final del filter un solo
bloque ruby la vacía en la raíz. Los `fields` se reescriben con la misma regla,
así el index template, el dashboard y el chat apuntan adonde el dato llega.

Un nombre que en la raíz ya es de otro se renombra en vez de pisarlo:
`@timestamp`, `@version` y `@metadata` son de Logstash, `message` es la línea
cruda (se borra al final del filter), `tags` es donde Logstash anota los
errores de parseo, y OpenSearch rechaza el documento entero si trae un campo
como `_id` o `_index` en la raíz. Esos quedan como `<nombre>_original`.
"""
from __future__ import annotations

AREA = "data"
_RESERVADOS = frozenset({"message", "tags"})
_SUFIJO = "_original"


def nombre(clave: str) -> str:
    """El nombre con que `clave` queda en la raíz (igual que el ruby de abajo)."""
    k = str(clave)
    if k.startswith(("@", "_")) or k in _RESERVADOS:
        k = k.lstrip("@_") + _SUFIJO
    return k.replace("[", "_").replace("]", "_")


# Comillas dobles adentro a propósito: el bloque va entre simples, y con
# `config.support_escapes` apagado no hay forma de escapar una simple. La lista
# de reservados sale de la misma constante que usa `nombre()`: si divergieran,
# los `fields` dirían un path y el dato llegaría a otro.
_RESERVADOS_RB = "[" + ", ".join(f'"{r}"' for r in sorted(_RESERVADOS)) + "]"
_BLOQUE = f"""  ruby {{
    code => '
      d = event.get("{AREA}")
      if d.is_a?(Hash)
        event.remove("{AREA}")
        d.each do |k, v|
          destino = k.to_s
          if destino.start_with?("@", "_") || {_RESERVADOS_RB}.include?(destino)
            destino = destino.sub(/\\A[@_]+/, "") + "{_SUFIJO}"
          end
          destino = destino.tr("[]", "__")
          actual = event.get("[" + destino + "]")
          v = actual.merge(v) if actual.is_a?(Hash) && v.is_a?(Hash)
          event.set("[" + destino + "]", v)
        end
      end
    '
  }}"""


def bloque() -> str:
    return _BLOQUE


def sacar(filter_code: str) -> str:
    """El filter sin el bloque de promoción (para devolvérselo al LLM a
    corregir: si no, el bloque se duplicaba en cada vuelta)."""
    return (filter_code or "").replace("\n" + _BLOQUE, "").replace(_BLOQUE, "")


def _path(path: str) -> str:
    prefijo = AREA + "."
    if not isinstance(path, str) or not path.startswith(prefijo):
        return path
    primero, _, resto = path[len(prefijo):].partition(".")
    return nombre(primero) + ("." + resto if resto else "")


def promover(filter_code: str, fields: list[dict]) -> tuple[str, list[dict]]:
    """Agrega el bloque al final del `filter { }` y reescribe los paths.

    El bloque va antes de la última `}`, así que el filter tiene que estar bien
    formado: se lo pasa por la gramática real en vez de mirar si termina en `}`
    (un `filter {` sin cerrar que termina en el `}` de un `mutate` pasaba, y el
    bloque quedaba adentro del plugin). Si no parsea, `ValueError`.
    """
    import conf_lint

    codigo = sacar(filter_code).rstrip()
    try:
        conf_lint.parse(codigo + "\n")
    except conf_lint.ErrorDeSintaxis as exc:
        raise ValueError(f"el filter no parsea, no sé dónde va el bloque: {exc}") from exc
    codigo = codigo[:-1].rstrip() + "\n\n" + _BLOQUE + "\n}\n"

    nuevos = []
    for f in fields or []:
        g = dict(f)
        for k in ("field_path", "ecs_path", "normalized_path", "ecs_overlay_path"):
            if g.get(k):
                g[k] = _path(g[k])
        nuevos.append(g)
    return codigo, nuevos
