"""Vigía del arranque de las pipelines de Logstash.

Visto en un deploy de 9 casos: al activar las 9 pipelines juntas, Logstash 7.10
de CSS se cayó entero al cargar los plugins (`NameError: uninitialized constant
Gem::Specification`, una carrera de JRuby cuando arrancan varias pipelines a la
vez) y las 9 quedaron en `failed`. Terraform sigue esperando que pasen a
corriendo hasta su timeout y el paso 2 termina en error, aunque las
configuraciones están bien: volver a arrancarlas desde la consola de CSS
funcionó.

Mientras corre la activación, el vigía mira el estado de las pipelines en CSS
y, si fallaron al arrancar, las vuelve a arrancar (todas juntas, igual que la
consola) hasta `REINTENTOS` veces. Terraform no se entera: sigue esperando y
termina cuando quedan corriendo. Cada reintento queda anotado en Actividad.

Las que se suman a una activación que ya existe no las arranca Terraform sino
`arrancar_las_que_faltan`: el provider lee las activas de una sola página de
CSS (10), así que con 11 "perdía" una, la volvía a arrancar y CSS rechazaba el
apply entero ("CSS.5090 ... the conf[pipeline-x] is being executed"). Pasó en un
deploy de 11 casos. Se arrancan de a una, como hacía el provider, y el vigía
rearranca solo las ya arrancadas (las que CSS lista y no están `stopped`): las
que faltan le tocan a su turno, y arrancarlas antes hacía chocar ese turno.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable

CORRIENDO = "working"
FALLIDA = "failed"
DETENIDA = "stopped"
REINTENTOS = 3
# Después de un reintento CSS tarda en reflejar el nuevo estado: hasta que pase
# este tiempo, un `failed` es el de antes y no amerita otro reintento.
ENFRIAMIENTO_S = 90.0
INTERVALO_S = 15.0


def nombres_activos(tfvars: dict) -> list[str]:
    """Los nombres de pipeline que la activación arranca (como `active_pipeline_names`
    de main.tf: `pipeline-<slug>` cortado a 32)."""
    return [f"pipeline-{k}"[:32] for k, v in ((tfvars or {}).get("pipelines") or {}).items()
            if (v or {}).get("start_ingestion")]


class Vigia:
    """La decisión, sin hilos ni red: `mirar()` lee el estado (`listar` →
    {nombre: status}), rearranca si hace falta (`arrancar(nombres)`) y devuelve
    los pasos a anotar en Actividad (`{name, ok, reason}`)."""

    def __init__(self, listar: Callable[[], dict], arrancar: Callable[[list], None],
                 nombres: list[str], reintentos: int = REINTENTOS,
                 enfriamiento_s: float = ENFRIAMIENTO_S, reloj: Callable[[], float] = time.monotonic):
        self.listar, self.arrancar, self.nombres = listar, arrancar, list(nombres)
        self.reintentos, self.enfriamiento_s, self.reloj = reintentos, enfriamiento_s, reloj
        self.intentos = 0
        self.ultimo: float | None = None
        self.terminado = False

    def mirar(self) -> list[dict]:
        if self.terminado or not self.nombres:
            return []
        try:
            estados = self.listar() or {}
        except Exception:  # noqa: BLE001 — sin estado no se decide nada; se vuelve a mirar
            return []
        fallidas = [n for n in self.nombres if estados.get(n) == FALLIDA]
        if all(estados.get(n) == CORRIENDO for n in self.nombres):
            self.terminado = True
            if self.intentos:
                return [{"name": "Logstash · arranque de las pipelines", "ok": True,
                         "reason": f"corriendo después de {self.intentos} reintento(s)"}]
            return []
        if not fallidas:
            return []            # todavía arrancando
        if self.ultimo is not None and self.reloj() - self.ultimo < self.enfriamiento_s:
            return []            # el failed es el del intento anterior
        if self.intentos >= self.reintentos:
            self.terminado = True
            return [{"name": "Logstash · arranque de las pipelines", "ok": False,
                     "reason": (f"siguen fallando al arrancar después de {self.intentos} reintentos "
                                f"({', '.join(fallidas)}). Volvé a tocar \"Iniciar ingesta\"; si "
                                "se repite, revisá el log de Logstash en la consola de CSS.")}]
        self.intentos += 1
        self.ultimo = self.reloj()
        # Las que Terraform todavía no arrancó las arranca él cuando le toque.
        arrancadas = [n for n in self.nombres if estados.get(n) not in (None, DETENIDA)]
        try:
            self.arrancar(arrancadas)
            ok, detalle = True, ""
        except Exception as exc:  # noqa: BLE001 — se anota y se reintenta en la próxima mirada
            ok, detalle = False, f" · no se pudo pedir el arranque: {str(exc)[:200]}"
        return [{"name": "Logstash · arranque de las pipelines", "ok": ok, "reason": (
            f"{len(fallidas)} de {len(self.nombres)} fallaron al arrancar (error transitorio de "
            f"Logstash 7.10 al cargar los plugins): reintento {self.intentos} de {self.reintentos}"
            + detalle)}]


class VigiaEnSegundoPlano:
    """Corre `Vigia.mirar` cada `intervalo_s` en un hilo; los pasos quedan en
    una cola que el stream del deploy vacía entre línea y línea de Terraform."""

    def __init__(self, vigia: Vigia, intervalo_s: float = INTERVALO_S):
        self.vigia, self.intervalo_s = vigia, intervalo_s
        self.pasos: "queue.Queue[dict]" = queue.Queue()
        self._parar = threading.Event()
        self._hilo = threading.Thread(target=self._correr, name="vigia-logstash", daemon=True)

    def _correr(self):
        while not self._parar.wait(self.intervalo_s):
            for paso in self.vigia.mirar():
                self.pasos.put(paso)
            if self.vigia.terminado:
                return

    def __enter__(self):
        self._hilo.start()
        return self

    def __exit__(self, *_exc):
        self._parar.set()
        self._hilo.join(timeout=5)
        return False

    def pendientes(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self.pasos.get_nowait())
            except queue.Empty:
                return out


# ── Las que se suman a una activación que ya existe ─────────────────────────
ESPERA_S = 10.0
TOPE_POR_PIPELINE_S = 600.0
_PASO = "Logstash · arranque de las pipelines"


def arrancar_las_que_faltan(listar: Callable[[], dict], arrancar_una: Callable[[str], None],
                            nombres: list[str], vigia: Vigia | None = None,
                            espera_s: float | None = None, tope_s: float | None = None,
                            dormir: Callable[[float], None] = time.sleep,
                            reloj: Callable[[], float] = time.monotonic):
    """Generador: arranca (`arrancar_una(nombre)`, el hot-start de CSS) las de
    `nombres` que CSS no lista o lista `stopped`, de a una y esperando que cada
    una corra antes de la siguiente. Da los pasos a anotar en Actividad y
    devuelve las que no quedaron corriendo. `vigia`: mientras se espera,
    rearranca las que se cayeron."""
    espera_s = ESPERA_S if espera_s is None else espera_s
    tope_s = TOPE_POR_PIPELINE_S if tope_s is None else tope_s
    try:
        estados = listar() or {}
    except Exception as exc:  # noqa: BLE001 — sin estado no se arranca nada a ciegas
        yield {"name": _PASO, "ok": False, "reason": f"no se pudo leer el estado de las pipelines: {str(exc)[:200]}"}
        return []
    faltan = [n for n in nombres if estados.get(n) in (None, DETENIDA)]
    for i, nombre in enumerate(faltan):
        try:
            arrancar_una(nombre)
        except Exception as exc:  # noqa: BLE001 — se dice cuál y por qué; las que siguen, en el próximo intento
            yield {"name": _PASO, "ok": False, "reason": f"no se pudo arrancar {nombre}: {str(exc)[:300]}"}
            return faltan[i:]
        limite = reloj() + tope_s
        while True:
            try:
                estados = listar() or {}
            except Exception:  # noqa: BLE001 — se vuelve a mirar
                estados = {}
            yield from (vigia.mirar() if vigia else [])
            if estados.get(nombre) == CORRIENDO:
                break
            if reloj() >= limite:
                yield {"name": _PASO, "ok": False, "reason": (
                    f"{nombre} no quedó corriendo en {tope_s / 60:.0f} minutos "
                    f"(estado: {estados.get(nombre) or 'no figura'})")}
                return faltan[i:]
            dormir(espera_s)
    if faltan:
        yield {"name": _PASO, "ok": True,
               "reason": f"se sumaron {len(faltan)} a las que ya corrían: {', '.join(faltan)}"}
    return []
