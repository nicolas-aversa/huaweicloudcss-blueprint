"""Specs de seguridad, perfiles y analistas con forma real, para probar los
mecanismos genéricos (Security Analytics, Transforms, roles enmascarados).

Son las de las verticales curadas que había antes de que todo caso saliera del
flujo de dataset nuevo: datos de prueba, no catálogo. El producto ya no las
usa; un dataset nuevo arma las suyas (`seguridad_derivada`, `perfiles`,
`accesos`)."""
import json
import pathlib

_DATOS = json.loads((pathlib.Path(__file__).parent / "fixtures" / "specs_de_prueba.json").read_text(encoding="utf-8"))
for _v in _DATOS.values():
    if (_v.get("security") or {}).get("meses"):
        _v["security"]["meses"] = tuple(_v["security"]["meses"])


def all_verticals() -> list[dict]:
    return list(_DATOS.values())


def get_vertical(slug: str) -> "dict | None":
    return _DATOS.get(slug)


def security_specs() -> dict:
    return {s: v["security"] for s, v in _DATOS.items() if v.get("security")}
