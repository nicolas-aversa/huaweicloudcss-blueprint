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
    # Ni una llamada real al MaaS desde la suite: con la key del `.env` de quien
    # corre los tests, la semántica de un dataset nuevo iba a la red. Los tests
    # que necesitan una key la ponen ellos.
    monkeypatch.delenv("MAAS_API_KEY", raising=False)
    monkeypatch.setattr(_mi, "_fernet_cache", None, raising=False)
    # Lo mismo para `data/`: un caso creado desde la app local (`data/cases/`)
    # sumaba el grupo "Mis casos" al registro y un test que cuenta grupos
    # pasaba a fallar. Los tests que necesitan un store lo vuelven a mover.
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(_auth, "DATA_ROOT", tmp_path / "data")
    # Y la Actividad: sin usuario, los runs iban a `runs/` del repo, la misma
    # carpeta que lee la app local; la suite le llenaba el historial de corridas
    # falsas (y el recorte a 50 le borraba las de verdad).
    import runs as _runs
    monkeypatch.setattr(_runs, "_BASE_SIN_USUARIO", tmp_path)
    # Las esperas entre sondeos al cluster (backtest de forecast, análisis
    # histórico de AD) son de segundos: con un fake que nunca termina, un test
    # dormía minutos de verdad. Los que las miden, las miden igual.
    import main as _main
    monkeypatch.setattr(_main, "_FORECAST_ESPERA_S", 0.0)
    monkeypatch.setattr(_main, "_AD_ESPERA_S", 0.0)
    monkeypatch.setattr(_main, "_RANGO_ESPERA_S", 0.0)
    monkeypatch.setattr(_main, "_ESPERA_CLUSTER_S", 0.0)
    monkeypatch.setattr(_main, "_ESPERA_CLUSTER_MAX_S", 0.0)
    # Sin cluster de verdad: la cola de búsquedas no se mide (se sigue de largo).
    monkeypatch.setattr(_main, "_cola_de_busquedas", lambda *a, **k: None)
    # El ciclo de vida y el rollup van contra el cluster (descubren las
    # dimensiones con una agregación): en los tests del endpoint de provisión,
    # con una IP de prueba, cada caso esperaba los timeouts. Los tests que lo
    # prueban usan el original (`_paso_del_tiempo_original`).
    monkeypatch.setattr(_main, "_paso_del_tiempo_original", _main._provisionar_el_paso_del_tiempo, raising=False)
    monkeypatch.setattr(_main, "_provisionar_el_paso_del_tiempo", lambda *a, **k: None)
    # El estado de cada plugin (vista Plugins): un test del endpoint de
    # provisión lo escribía en `terraform/` del repo, la carpeta que lee la app
    # local. Ahí va a la carpeta del test; en cualquier otra, donde dijo.
    import pathlib
    repo_tf = (pathlib.Path(_main.__file__).parent / "terraform").resolve()
    guardar = _main._guardar_estados
    monkeypatch.setattr(_main, "_guardar_estados", lambda td, slug, nuevo: guardar(
        tmp_path if pathlib.Path(td).resolve() == repo_tf else td, slug, nuevo))
