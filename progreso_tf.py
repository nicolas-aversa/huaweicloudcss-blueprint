"""El progreso de un `terraform apply`, por componente y global, que solo avanza.

El parser anterior mapeaba cada línea a un porcentaje fijo del total. Terraform
crea los recursos EN PARALELO, así que mientras el cluster de OpenSearch iba por
16%, un "NAT gateway: Creating..." devolvía la barra a 3%, y un "EIP: Creation
complete" la subía a 7%. Además la duración solo se leía en segundos
(`[70s elapsed]`), y Terraform escribe `[1m10s elapsed]`: pasado el primer
minuto, cada línea del cluster —que tarda quince— volvía la barra al inicio de
su tramo. Y un recurso que se modifica escribe `[id=…, 10s elapsed]`, que no
se leía nunca.

Acá el porcentaje sale de otro lado:

  * `terraform apply` imprime el PLAN antes de aplicar (`# x will be created`):
    ese es el conjunto de recursos, fijo desde el principio;
  * cada recurso tiene su propio avance, que solo sube (por tiempo transcurrido
    contra una duración estimada, y 100% al terminar);
  * cada componente (cada cluster, cada pieza de la red —NAT, EIP, cada
    regla DNAT, el SNAT, las reglas de SG—, cada pipeline) es el promedio de
    sus recursos, pesado por lo que tardan. Van por separado para que se vea
    TODO lo que levanta Terraform, no "la red" como una caja cerrada;
  * el global es la suma pesada de todos, sobre un denominador que ya no
    cambia. Si igual apareciera un recurso que el plan no anunció, el global
    se queda donde estaba en vez de bajar.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# tipo → (componente, segundos creando, segundos eliminando). Las duraciones son
# las que se ven en Huawei Cloud; solo reparten el peso y mueven la barra
# mientras Terraform no dice nada más que "Still creating".
_TIPOS: dict[str, tuple[str, float, float]] = {
    "huaweicloud_css_cluster":                ("opensearch", 900, 300),
    "huaweicloud_css_logstash_cluster":       ("logstash",   600, 300),
    "huaweicloud_css_logstash_configuration": ("pipeline",    60,  30),
    "huaweicloud_css_logstash_pipeline":      ("activar",     60,  30),
    "huaweicloud_nat_gateway":                ("nat",         30,  20),
    "huaweicloud_vpc_eip":                    ("eip",         15,  10),
    "huaweicloud_nat_dnat_rule":              ("dnat",        15,  10),
    "huaweicloud_nat_snat_rule":              ("snat",        15,  10),
    "huaweicloud_networking_secgroup_rule":   ("sg",           5,   5),
}
_POR_DEFECTO = ("otros", 30, 20)
_ETIQUETAS = {
    "opensearch": "CSS OpenSearch cluster",
    "logstash": "CSS Logstash cluster",
    "nat": "NAT gateway",
    "eip": "EIP pública",
    "snat": "SNAT (salida)",
    "sg": "Reglas de SG",
    "activar": "Activar pipelines",
    "otros": "Otros recursos",
}
# Una fila por regla DNAT: son las que dan acceso al cluster privado, y cuando
# falta una conviene verlo en la lista. Cada una con su puerto. Las etiquetas
# son cortas a propósito: la lista va en dos columnas y "DNAT → OpenSearch
# Dashboards" se cortaba. 5601 es `kibana_port`, que el backend no cambia; el
# de Beats sí varía, así que ese no se nombra.
_DNAT = {
    "opensearch": "DNAT :9200 · OpenSearch",
    "kibana": "DNAT :5601 · Dashboards",
    "logstash_beats": "DNAT · Logstash Beats",
}
# Orden en pantalla: los clusters, la red que les da acceso y la ingesta.
_ORDEN = ["opensearch", "logstash", "nat", "eip", "dnat", "snat", "sg",
          "pipeline", "activar", "otros"]

_PLAN = re.compile(
    r"^\s*# (?P<dir>\S+) (?P<acc>will be created|will be updated in-place|must be replaced|"
    r"will be replaced|will be destroyed)")
_APLICA = re.compile(
    r"^(?P<dir>\S+): (?P<ev>Creating\.\.\.|Still creating|Creation complete|"
    r"Modifying\.\.\.|Still modifying|Modifications complete|"
    r"Destroying\.\.\.|Still destroying|Destruction complete)")
_TRANSCURRIDO = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(\d+)s elapsed")
_CLAVE = re.compile(r'\["([^"]+)"\]')

_ACCION = {"will be created": "crear", "will be updated in-place": "modificar",
           "must be replaced": "reemplazar", "will be replaced": "reemplazar",
           "will be destroyed": "eliminar"}
_VERBO = {"crear": "Creando", "modificar": "Actualizando",
          "reemplazar": "Reemplazando", "eliminar": "Eliminando"}


def transcurrido(linea: str) -> int:
    """Segundos de `[15m30s elapsed]`, `[id=…, 1h2m3s elapsed]` o `[40s elapsed]`."""
    m = _TRANSCURRIDO.search(linea)
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def _curva(t: float, estimado: float) -> float:
    """Avance de una etapa por tiempo: lineal hasta 90% al llegar a lo
    estimado, y después se sigue moviendo hacia 97% sin llegar nunca (el 100%
    lo da Terraform al terminar). Así un cluster que tarda más de lo previsto
    no se ve congelado, ni se da por terminado antes de tiempo."""
    if estimado <= 0:
        return 0.9
    if t <= estimado:
        return 0.9 * t / estimado
    return 0.9 + 0.07 * (1 - math.exp(-(t - estimado) / estimado))


def _tipo(direccion: str) -> str:
    partes = [p for p in direccion.split(".") if not p.startswith("module")]
    # module.x.tipo.nombre → tipo; los `data.` no son recursos que se crean.
    return partes[0] if partes else ""


@dataclass
class _Recurso:
    direccion: str
    componente: str
    accion: str
    seg_crear: float
    seg_eliminar: float
    avance: float = 0.0
    listo: bool = False
    # Recién dijo "Creating..." y va por 0%: igual está en curso, y la pantalla
    # tiene que decir "Creando" y no "En espera".
    empezado: bool = False

    @property
    def peso(self) -> float:
        if self.accion == "reemplazar":
            return self.seg_eliminar + self.seg_crear
        if self.accion == "eliminar":
            return self.seg_eliminar
        if self.accion == "modificar":
            return self.seg_crear * 0.5
        return self.seg_crear

    def _tramo(self, etapa: str) -> tuple[float, float, float]:
        """(desde, hasta, segundos) de la etapa dentro del avance del recurso."""
        if self.accion == "reemplazar":
            corte = self.seg_eliminar / self.peso
            return (0.0, corte, self.seg_eliminar) if etapa == "eliminar" else (corte, 1.0, self.seg_crear)
        segundos = {"eliminar": self.seg_eliminar, "modificar": self.seg_crear * 0.5}.get(etapa, self.seg_crear)
        return 0.0, 1.0, segundos

    def evento(self, ev: str, linea: str) -> None:
        etapa = ("eliminar" if "Destr" in ev else "modificar" if "Modif" in ev else "crear")
        desde, hasta, segundos = self._tramo(etapa)
        self.empezado = True
        if ev.startswith(("Creating", "Modifying", "Destroying")):
            nuevo = desde
        elif ev.startswith("Still"):
            nuevo = desde + (hasta - desde) * _curva(transcurrido(linea), segundos)
        else:   # "... complete": termina la etapa; el recurso, si era la última
            nuevo = hasta
            if hasta >= 1.0:
                self.listo = True
        self.avance = max(self.avance, min(nuevo, 1.0))


@dataclass
class ProgresoApply:
    """Se le pasan las líneas del apply; devuelve los eventos a emitir."""
    recursos: dict[str, _Recurso] = field(default_factory=dict)
    plan_listo: bool = False
    global_: float = 0.0
    _ultimo: dict[str, tuple] = field(default_factory=dict)

    # ── Componentes ──────────────────────────────────────────────────────
    def _componente_de(self, direccion: str, tipo_comp: str) -> str:
        if tipo_comp == "pipeline":
            m = _CLAVE.search(direccion)
            return f"pipeline:{m.group(1)}" if m else "pipeline"
        if tipo_comp == "dnat":
            partes = [p for p in direccion.split(".") if not p.startswith("module")]
            return "dnat:" + partes[1].split("[")[0] if len(partes) > 1 else "dnat"
        return tipo_comp

    def _registrar(self, direccion: str, accion: str) -> _Recurso | None:
        tipo = _tipo(direccion)
        if not tipo or tipo == "data":
            return None
        comp, crear, eliminar = _TIPOS.get(tipo, _POR_DEFECTO)
        r = self.recursos.get(direccion)
        if r is None:
            r = _Recurso(direccion, self._componente_de(direccion, comp), accion, crear, eliminar)
            self.recursos[direccion] = r
        return r

    @staticmethod
    def etiqueta(componente: str) -> str:
        if componente.startswith("pipeline:"):
            return f"Pipeline · {componente.split(':', 1)[1]}"
        if componente == "pipeline":
            return "Pipelines"
        if componente.startswith("dnat:"):
            nombre = componente.split(":", 1)[1]
            return _DNAT.get(nombre, f"DNAT · {nombre}")
        return _ETIQUETAS.get(componente, componente)

    def componentes(self) -> list[dict]:
        grupos: dict[str, list[_Recurso]] = {}
        for r in self.recursos.values():
            grupos.setdefault(r.componente, []).append(r)

        def _orden(k: str) -> tuple[int, str]:
            base = k.split(":", 1)[0]
            return (_ORDEN.index(base) if base in _ORDEN else len(_ORDEN), k)

        fuera = []
        for k in sorted(grupos, key=_orden):
            rs = grupos[k]
            peso = sum(r.peso for r in rs) or 1.0
            avance = sum(r.peso * r.avance for r in rs) / peso
            listo = all(r.listo for r in rs)
            empezado = any(r.empezado for r in rs) or listo
            accion = max(rs, key=lambda r: r.peso).accion
            estado = "listo" if listo else (_VERBO.get(accion, "Aplicando") if empezado else "En espera")
            fuera.append({"key": k, "label": self.etiqueta(k), "percent": round(100 * avance, 1),
                          "done": listo, "estado": estado, "peso": round(peso, 1)})
        return fuera

    # ── Global ───────────────────────────────────────────────────────────
    def fraccion(self) -> float:
        """Avance del apply entero (0..1). Nunca baja."""
        total = sum(r.peso for r in self.recursos.values())
        if total > 0:
            actual = sum(r.peso * r.avance for r in self.recursos.values()) / total
            self.global_ = max(self.global_, actual)
        return self.global_

    def _fase(self) -> tuple[str, str]:
        """Lo que está en curso. Con uno solo, cuál; con varios, cuántos en
        paralelo y cuántos terminaron. Antes se nombraba solo el que más
        faltaba (casi siempre el cluster de OpenSearch), y "Creando OpenSearch
        cluster…" escondía la red y el Logstash creándose al mismo tiempo."""
        comps = self.componentes()
        activos = [c for c in comps if not c["done"] and c["estado"] != "En espera"]
        if not activos:
            return "Terraform apply", "Aplicando infraestructura…"
        if len(activos) == 1:
            c = activos[0]
            return c["label"], f"{c['estado']} {c['label']}…"
        listos = sum(1 for c in comps if c["done"])
        return ("En paralelo",
                f"{len(activos)} servicios en paralelo · {listos} de {len(comps)} listos")

    # ── Entrada ──────────────────────────────────────────────────────────
    def linea(self, linea: str) -> list[dict]:
        """Eventos para esta línea: `plan` una vez, `item` por componente que
        cambió, y el avance global (0..1) en un `apply`."""
        texto = linea.rstrip()
        eventos: list[dict] = []

        m = _PLAN.match(texto)
        if m and not self.plan_listo:
            self._registrar(m.group("dir"), _ACCION[m.group("acc")])
            return []
        if not self.plan_listo and (texto.startswith("Plan:") or texto.startswith("No changes.")
                                    or _APLICA.match(texto)):
            self.plan_listo = True
            items = self.componentes()
            for c in items:
                self._ultimo[c["key"]] = (round(c["percent"]), c["done"], c["estado"])
            eventos.append({"type": "plan", "items": [{k: v for k, v in c.items() if k != "peso"}
                                                      for c in items]})

        m = _APLICA.match(texto)
        if m:
            ev = m.group("ev")
            accion = ("eliminar" if "Destr" in ev else "modificar" if "Modif" in ev else "crear")
            r = self._registrar(m.group("dir"), accion)
            if r is not None:
                r.evento(ev, texto)
        elif texto.startswith("Apply complete!"):
            for r in self.recursos.values():
                r.avance, r.listo, r.empezado = 1.0, True, True
            self.global_ = 1.0
        else:
            return eventos

        for c in self.componentes():
            firma = (round(c["percent"]), c["done"], c["estado"])
            if self._ultimo.get(c["key"]) != firma:
                self._ultimo[c["key"]] = firma
                eventos.append({"type": "item", **{k: v for k, v in c.items() if k != "peso"}})
        fase, mensaje = self._fase()
        eventos.append({"type": "apply", "fraccion": self.fraccion(), "phase": fase, "message": mensaje})
        return eventos
