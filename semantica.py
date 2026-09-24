"""La semántica de una tabla: etiquetas, roles, unidades y preguntas.

El perfilador decide todo lo que puede romper la pipeline —separador, tipos,
formato de fecha, decimales— mirando las filas. Lo que queda es lo que una
persona entiende de los datos y una regex no: que "Estado" es el resultado de
cada trabajo, que "Cliente" sirve para agrupar, que la columna 7 de un CSV sin
header son litros. Eso lo pone el LLM, y está armado para que **no pueda romper
nada**:

  * nunca toca un tipo, un formato de fecha ni el .conf (solo el nombre de las
    columnas de un CSV sin header, y ese nombre se valida y el filter se arma
    después con él);
  * cada cosa que devuelve se valida contra el perfil: roles del vocabulario,
    medidas solo sobre números, dimensiones solo donde la cardinalidad real lo
    permite, columnas que existen;
  * si falla, tarda o no hay API key, quedan las heurísticas por nombre (en
    castellano y en inglés) y el flujo sigue igual.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

# El modelo de la semántica NO es el del .conf. glm-5.3 razona siempre y para
# esto tardaba ~95 s (medido con la telemetría de 14 columnas); glm-5.2 sin
# thinking contesta en ~25 s. Acá no hace falta razonar: son etiquetas, roles y
# preguntas, todo validado después, y un error no rompe nada. La sintaxis del
# .conf —lo que sí puede romper la pipeline— sigue con glm-5.3 y thinking.
_MODELO = "glm-5.2"
# Tope del llamado: el paso 1 → 2 no puede quedar colgado por la semántica. Si
# el modelo no contesta en este tiempo, siguen las heurísticas.
_TIMEOUT_S = 60
_FILAS_DE_MUESTRA = 5
_MAX_ETIQUETA = 60
_MAX_UNIDAD = 12

_ROLES = ("primary_dimension", "success_indicator", "critical_indicator",
          "entity_id", "measure", "timestamp")
_NUMERICOS = ("integer", "float")
_NOMBRE_VALIDO = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_SIN_HEADER = re.compile(r"^columna_\d+$")


@dataclass
class Resultado:
    fuente: str                                   # "llm" | "heuristica"
    preguntas: list[str] = field(default_factory=list)
    nota: str = ""                                # por qué no se usó el LLM


# ── El llamado ──────────────────────────────────────────────────────────────
_PROMPT = """Sos analista de datos. Te paso las columnas de un dataset (ya tipadas: \
los tipos NO se discuten) y unas filas de muestra. Devolvé SOLO un JSON así:

{{"columnas": {{"<nombre>": {{"etiqueta": "...", "rol": "...", "dimension": true, \
"unidad": "..."}}}}, "nombres": {{"<columna_N>": "<nombre_snake_case>"}}, \
"preguntas": ["..."]}}

- "etiqueta": cómo le diría una persona a la columna, en castellano, corta.
- "rol" (o null), uno de:
  primary_dimension = LA categoría principal para agrupar (una sola);
  success_indicator = el resultado de cada registro (estado, código de respuesta);
  critical_indicator = campo que solo aparece o importa cuando algo salió mal (código o motivo de error);
  entity_id = identifica a una entidad que se repite (cliente, usuario, equipo, trabajo);
  measure = número que se suma o promedia (monto, cantidad, duración);
  timestamp = la fecha del evento.
- "dimension": true si sirve para agrupar (pocos valores que se repiten).
- "unidad": solo para números, si se deduce (ml, kg, ms, ARS, %…); si no, null.
- "nombres": SOLO para las columnas que se llaman columna_N, un nombre snake_case \
ASCII que diga qué son.
- "preguntas": 10 preguntas en castellano que un analista le haría a estos datos. \
Cada una se tiene que poder contestar con UNA agregación sobre el índice (contar, \
sumar, promediar, máximo/mínimo, top N, por día) y nombrar una columna por su \
etiqueta. Podés usar un valor real para filtrar ("con Estado FALLIDO") o como \
ejemplo de las categorías ("por Estado (COMPLETADO, FALLIDO)"), pero NUNCA \
escribas la respuesta en la pregunta. Nada de "por qué", predicciones ni \
correlaciones.

Columnas:
{columnas}

Filas de muestra:
{filas}
"""


def _payload_columnas(perfil) -> str:
    fuera = []
    for c in perfil.columnas:
        fuera.append({
            "nombre": c.nombre,
            "etiqueta_original": c.etiqueta,
            "tipo": c.tipo,
            "unidad": c.unidad,
            "valores_distintos": c.distintos,
            "valores_frecuentes": c.frecuentes[:6],
        })
    return json.dumps(fuera, ensure_ascii=False)


def _llamar_al_llm(prompt: str) -> str:
    from maas_integrator import _build_client, _chat

    client = _build_client().with_options(timeout=_TIMEOUT_S, max_retries=0)
    respuesta = _chat(
        client,
        model=_MODELO,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return respuesta.choices[0].message.content or ""


def _json_de(texto: str) -> dict:
    """El objeto JSON de la respuesta, aunque venga con `<think>`, entre
    fences de Markdown o con una frase antes (glm-5.2 las usa pese al
    `response_format`)."""
    texto = re.sub(r"<think>.*?</think>", "", texto or "", flags=re.DOTALL)
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio < 0 or fin < inicio:
        raise ValueError("la respuesta no trae un objeto JSON")
    datos = json.loads(texto[inicio:fin + 1])
    if not isinstance(datos, dict):
        raise ValueError("la respuesta no es un objeto JSON")
    return datos


def enriquecer(perfil, llamar: Callable[[str], str] | None = None) -> Resultado:
    """Pone la semántica sobre `perfil` (lo modifica) y devuelve las preguntas
    que propuso el LLM, todavía sin validar (eso es de `preguntas.armar`).

    `llamar` existe para los tests; en la app es el LLM de la plataforma.
    """
    filas = [l for l in perfil.lineas_datos if l.strip()][:_FILAS_DE_MUESTRA]
    prompt = _PROMPT.format(columnas=_payload_columnas(perfil),
                            filas="\n".join(filas))
    try:
        crudo = (llamar or _llamar_al_llm)(prompt)
        datos = _json_de(crudo)
    except Exception as exc:  # noqa: BLE001 — cualquier falla deja las heurísticas
        return Resultado("heuristica", nota=f"{type(exc).__name__}: {str(exc)[:160]}")
    aplicar(perfil, datos)
    preguntas = datos.get("preguntas")
    return Resultado("llm", [str(p) for p in preguntas if isinstance(p, str)]
                     if isinstance(preguntas, list) else [])


# ── La validación ───────────────────────────────────────────────────────────
def _sin_tildes(texto: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", texto)
                   if not unicodedata.combining(ch))


def _rol_valido(rol: Any, tipo: str) -> str | None:
    if not isinstance(rol, str):
        return None
    rol = rol.strip().lower()
    if rol not in _ROLES:
        return None
    if rol == "measure" and tipo not in _NUMERICOS:
        return None
    if rol == "timestamp" and tipo != "date":
        return None
    if rol in ("primary_dimension", "entity_id", "critical_indicator") and tipo in ("date", "float"):
        return None
    if rol == "success_indicator" and tipo not in ("string", "boolean", "integer"):
        return None
    return rol


def aplicar(perfil, datos: dict) -> None:
    """Aplica al perfil lo que haya de válido en la respuesta del LLM."""
    columnas = datos.get("columnas")
    columnas = columnas if isinstance(columnas, dict) else {}
    # La etiqueta original es el vocabulario del cliente: si el CSV trae header,
    # se respeta. La del LLM se usa donde no hay nada mejor (un `columna_3`, una
    # clave `src_ip` de un JSON).
    etiquetas_propias = perfil.formato == "delimitado" and bool(perfil.header)
    unicos: set[str] = set()   # roles que tienen que ser de un solo campo

    for c in perfil.columnas:
        info = columnas.get(c.nombre)
        if not isinstance(info, dict):
            continue

        etiqueta = info.get("etiqueta")
        if (not etiquetas_propias and isinstance(etiqueta, str)
                and 0 < len(etiqueta.strip()) <= _MAX_ETIQUETA):
            c.etiqueta = etiqueta.strip()

        # Una dimensión se puede apagar siempre (un texto que el perfilador no
        # reconoció como tal). Prenderla, solo si los datos lo permiten: un
        # string con valores que se repiten. Si no, el dashboard pinta una
        # barra por documento.
        dim = info.get("dimension")
        if dim is False:
            c.dimension = False
        elif dim is True and c.tipo in ("string", "boolean") and not c.dimension:
            llenos = max(1, len(c.valores) - c.vacios)
            c.dimension = c.distintos < 0.9 * llenos

        rol = _rol_valido(info.get("rol"), c.tipo)
        if rol == "primary_dimension" and not c.dimension:
            rol = None   # la torta principal de un campo que no agrupa
        if rol in ("primary_dimension", "success_indicator", "critical_indicator"):
            if rol in unicos:
                rol = None
            else:
                unicos.add(rol)
        if rol:
            c.rol = rol

        unidad = info.get("unidad")
        if (c.unidad is None and c.tipo in _NUMERICOS and isinstance(unidad, str)
                and 0 < len(unidad.strip()) <= _MAX_UNIDAD):
            c.unidad = unidad.strip()

    # Nombres para un CSV sin header: `columna_7` → `consumo_litros`. El
    # nombre se valida (snake_case ASCII, único) y el .conf se arma después con
    # él, así que la gramática no corre ningún riesgo.
    nombres = datos.get("nombres")
    if isinstance(nombres, dict) and not perfil.header and perfil.formato == "delimitado":
        tomados = {c.nombre for c in perfil.columnas}
        for c in perfil.columnas:
            nuevo = nombres.get(c.nombre)
            if not (isinstance(nuevo, str) and _SIN_HEADER.match(c.nombre)):
                continue
            nuevo = _sin_tildes(nuevo.strip().lower()).replace(" ", "_")
            if not _NOMBRE_VALIDO.match(nuevo) or nuevo in tomados or nuevo == "message":
                continue
            _renombrar(perfil, c, nuevo)
            tomados.add(nuevo)


def _renombrar(perfil, c, nuevo: str) -> None:
    viejo = c.nombre
    c.nombre = nuevo
    c.path = (nuevo,)
    if perfil.fecha_evento == viejo:
        perfil.fecha_evento = nuevo
    if perfil.fecha_compuesta:
        perfil.fecha_compuesta = tuple(nuevo if n == viejo else n
                                       for n in perfil.fecha_compuesta)
