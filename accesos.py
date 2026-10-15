"""
accesos.py
==========

Builders PUROS (sin I/O) del analista de demo con datos enmascarados: un
usuario interno de solo lectura sobre los índices de UN caso, que ve los campos
sensibles (paciente, cliente, comitente) enmascarados por el plugin de
seguridad de OpenSearch (`masked_fields`: el valor se reemplaza por un hash
estable, así se puede seguir contando y agrupando por paciente sin verlo).

Lo declara cada vertical en su clave `analista` (`enmascarados`). La
orquestación REST vive en `main.py`.
"""

from __future__ import annotations

import re
import secrets
import string


def nombre_del_rol(slug: str) -> str:
    return f"{slug}-analista"


def nombre_del_usuario(slug: str) -> str:
    return f"analista-{slug}"


def rol_analista(index_pattern: str, enmascarados: list[str]) -> dict:
    """`PUT _plugins/_security/api/roles/<slug>-analista`: lectura sobre los
    índices del caso, con los campos sensibles enmascarados."""
    return {
        "description": "Analista de demo: solo lectura, con datos sensibles enmascarados (plataforma)",
        "cluster_permissions": ["cluster_composite_ops_ro"],
        "index_permissions": [{
            "index_patterns": [index_pattern],
            "allowed_actions": ["read", "indices:admin/mappings/get", "indices:admin/resolve/index"],
            "masked_fields": list(enmascarados),
        }],
        "tenant_permissions": [{"tenant_patterns": ["global_tenant"], "allowed_actions": ["kibana_all_read"]}],
    }


def usuario_analista(slug: str, password: str) -> dict:
    """`PUT _plugins/_security/api/internalusers/analista-<slug>`: los roles van
    en el usuario (`opendistro_security_roles`), sin tocar los role mappings
    compartidos del cluster. `kibana_user` para poder entrar a Dashboards."""
    return {"password": password, "opendistro_security_roles": [nombre_del_rol(slug), "kibana_user"],
            "attributes": {"creado_por": "plataforma", "caso": slug}}


# La política de contraseñas de CSS: 8 a 32 caracteres, con mayúscula,
# minúscula, número y un especial. Sin comillas ni barras: van en JSON y en la UI.
_ESPECIALES = "@#%^*_+-"


def contrasena(largo: int = 16) -> str:
    alfabeto = string.ascii_letters + string.digits + _ESPECIALES
    while True:
        p = "".join(secrets.choice(alfabeto) for _ in range(largo))
        if (any(c.islower() for c in p) and any(c.isupper() for c in p)
                and any(c.isdigit() for c in p) and any(c in _ESPECIALES for c in p)):
            return p


# Quién es la persona: su usuario, su documento, cómo contactarla, su cuenta.
# Para los campos que no traen la marca de sensible (las verticales, que no
# pasan por el análisis del paso 2): sin esto, cuatro se quedaban sin analista.
_DE_UNA_PERSONA = {"user", "users", "usuario", "username", "email", "mail", "correo", "dni", "cuit", "cuil",
                   "documento", "document", "passport", "pasaporte", "phone", "telefono", "tel", "celular",
                   "mobile", "customer", "cliente", "client", "comitente", "account", "cuenta", "patient",
                   "paciente", "afiliado", "card", "tarjeta", "iban", "cbu"}
# Lo que nombra algo de la persona sin identificarla: su navegador, su tipo.
_NO_LA_IDENTIFICA = {"agent", "type", "tipo", "role", "rol", "group", "grupo"}


def _palabras(tramo: str) -> set[str]:
    """`user_agent` → {user, agent}; `accountId` → {account, id}."""
    return {w.lower() for w in re.split(r"[_\-\s]+|(?<=[a-z0-9])(?=[A-Z])", tramo) if w}


def _es_sensible(ruta: str) -> bool:
    """Por las palabras del último tramo del nombre (`operador_dni`,
    `user.user_name`); un `name` o un `id` a secas, si el tramo de antes es de
    una persona (`user.name` sí; `user.domain.name` no). `user_agent` y
    `user.type` no identifican a nadie."""
    tramos = [t for t in ruta.split(".") if t]
    if not tramos:
        return False
    hoja = _palabras(tramos[-1])
    if hoja & _NO_LA_IDENTIFICA:
        return False
    if hoja & _DE_UNA_PERSONA:
        return True
    return hoja <= {"name", "nombre", "id"} and len(tramos) > 1 and bool(_palabras(tramos[-2]) & _DE_UNA_PERSONA)


def _ruta(f: dict) -> str:
    return f.get("field_path") or f.get("ecs_path") or f.get("raw_name") or ""


def enmascarados_desde_campos(fields: list[dict]) -> list[str]:
    """Los campos sensibles: los que se marcaron en el paso 2 o, si ningún
    campo trae la marca (una vertical), los que por el nombre identifican a una
    persona. Si alguno la trae, manda la marca, aunque no haya ninguno sensible."""
    fields = [f for f in fields or [] if _ruta(f)]
    if any("sensitive" in f for f in fields):
        return [_ruta(f) for f in fields if f.get("sensitive")]
    return [_ruta(f) for f in fields if (f.get("type") or "") in ("string", "keyword", "text") and _es_sensible(_ruta(f))]
