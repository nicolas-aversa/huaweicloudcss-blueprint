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
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable

CORRIENDO = "working"
FALLIDA = "failed"
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
        try:
            self.arrancar(self.nombres)
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
