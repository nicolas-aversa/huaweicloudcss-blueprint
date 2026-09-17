"""Aislamiento de la suite respecto de la máquina donde corre.

`maas_integrator` lee ⚙ Configuración de `.platform_settings.json`, junto al
código. Si quien corre los tests tiene su cuenta Huawei guardada ahí (project
id, VPC, AK/SK…), tres tests que asumen "sin cuenta configurada" fallan sin que
haya nada roto — pasó al correr la suite en una máquina donde la app local se
había usado de verdad. Cada test arranca con un archivo de settings propio y
vacío; el que necesite valores los monkeypatcha, como ya hacen varios.
"""
import pytest


@pytest.fixture(autouse=True)
def _settings_aislados(tmp_path, monkeypatch):
    import auth as _auth
    import maas_integrator as _mi

    monkeypatch.setattr(_mi, "_SETTINGS_PATH", tmp_path / ".platform_settings.json")
    monkeypatch.setattr(_mi, "_fernet_cache", None, raising=False)
    # Lo mismo para `data/`: un caso creado desde la app local (`data/cases/`)
    # sumaba el grupo "Mis casos" al registro y un test que cuenta grupos
    # pasaba a fallar. Los tests que necesitan un store lo vuelven a mover.
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(_auth, "DATA_ROOT", tmp_path / "data")
