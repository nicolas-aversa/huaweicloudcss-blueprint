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


def enmascarados_desde_campos(fields: list[dict]) -> list[str]:
    """Los campos que el usuario marcó como sensibles en el paso 2."""
    return [f.get("field_path") or f.get("ecs_path") or f.get("raw_name") or ""
            for f in fields or [] if f.get("sensitive") and (f.get("field_path") or f.get("raw_name"))]
