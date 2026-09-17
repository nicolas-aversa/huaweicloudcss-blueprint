"""Tests de `tfstate.py` — la lectura del estado de Terraform.

Lo que se prueba acá vale plata: si `has_resources` se equivoca hacia "no hay
nada", el destroy queda en no-op y los clusters CSS siguen facturando sin que
nadie los pueda dar de baja desde la app. Y si `prepare` no pide la migración
cuando el backend cambia, Terraform arranca contra un estado vacío, no ve los
recursos existentes, y un apply intenta crear todo de nuevo.
"""
import json
import pathlib
import re

import pytest

import tfstate


# Shape v4 real de Terraform, recortado a lo que leen los parsers.
def _state(*recursos):
    return {"version": 4, "terraform_version": "1.9.8", "lineage": "x",
            "resources": list(recursos)}


def _recurso(tipo, **attrs):
    return {"mode": "managed", "type": tipo, "name": "r",
            "instances": [{"attributes": attrs}]}


@pytest.fixture(autouse=True)
def _sin_cache():
    """El cache es global al proceso: limpiarlo entre tests."""
    tfstate._cache.clear()
    yield
    tfstate._cache.clear()


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "terraform"
    d.mkdir()
    return d


def _escribir_state(d, data):
    (d / tfstate.STATE_FILE).write_text(json.dumps(data), encoding="utf-8")


# ── Lectura local (el camino por defecto, que no debe cambiar) ───────────────
def test_lee_el_state_local(ws):
    _escribir_state(ws, _state(_recurso("huaweicloud_css_cluster", id="abc")))

    assert tfstate.read_state(ws)["resources"][0]["type"] == "huaweicloud_css_cluster"


def test_sin_archivo_devuelve_vacio(ws):
    assert tfstate.read_state(ws) == {}


def test_state_corrupto_no_levanta(ws):
    (ws / tfstate.STATE_FILE).write_text("{no es json", encoding="utf-8")

    assert tfstate.read_state(ws) == {}


def test_local_no_invoca_terraform(ws, monkeypatch):
    """El camino local tiene que seguir siendo gratis: `/terraform/status` se
    polea desde el front y no puede pagar un subprocess por cada llamada."""
    def _explota(*a, **k):
        raise AssertionError("no debería invocar terraform con state local")
    monkeypatch.setattr(tfstate.subprocess, "run", _explota)
    _escribir_state(ws, _state(_recurso("huaweicloud_css_cluster")))

    assert tfstate.read_state(ws)


# ── has_resources: el guard del destroy ─────────────────────────────────────
def test_con_recursos(ws):
    _escribir_state(ws, _state(_recurso("huaweicloud_css_cluster")))
    assert tfstate.has_resources(ws) is True


def test_state_vacio_no_tiene_recursos(ws):
    _escribir_state(ws, _state())
    assert tfstate.has_resources(ws) is False


def test_sin_state_no_tiene_recursos(ws):
    assert tfstate.has_resources(ws) is False


def test_state_ilegible_y_grande_asume_que_hay_algo(ws):
    """Ante la duda, decir que SÍ hay recursos: un falso negativo deja clusters
    facturando sin forma de destruirlos; un falso positivo solo hace que
    Terraform corra y no encuentre nada."""
    (ws / tfstate.STATE_FILE).write_text("x" * 512, encoding="utf-8")

    assert tfstate.has_resources(ws) is True


def test_state_ilegible_y_chico_no_cuenta(ws):
    (ws / tfstate.STATE_FILE).write_text("x" * 10, encoding="utf-8")

    assert tfstate.has_resources(ws) is False


# ── resource_attributes ──────────────────────────────────────────────────────
def test_saca_los_atributos_del_tipo_pedido(ws):
    st = _state(_recurso("huaweicloud_vpc_eip", address="1.2.3.4"),
                _recurso("huaweicloud_css_cluster", id="css-1", https_enabled=True))

    assert tfstate.resource_attributes(st, "huaweicloud_vpc_eip")["address"] == "1.2.3.4"
    assert tfstate.resource_attributes(st, "huaweicloud_css_cluster")["id"] == "css-1"


def test_tipo_ausente_devuelve_vacio(ws):
    assert tfstate.resource_attributes(_state(), "huaweicloud_css_cluster") == {}
    assert tfstate.resource_attributes({}, "lo-que-sea") == {}
    assert tfstate.resource_attributes(None, "lo-que-sea") == {}


def test_recurso_sin_instancias_no_rompe():
    st = {"resources": [{"type": "huaweicloud_css_cluster", "instances": []}]}
    assert tfstate.resource_attributes(st, "huaweicloud_css_cluster") == {}


# ── Backend remoto: lectura vía `terraform state pull` ──────────────────────
class FakeRun:
    """Doble de subprocess.run que registra los comandos."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.comandos = []

    def __call__(self, cmd, **kwargs):
        self.comandos.append(cmd)
        return self


def _activar_backend(ws):
    (ws / tfstate.BACKEND_FILE).write_text("# remoto", encoding="utf-8")


def test_con_backend_remoto_usa_state_pull(ws, monkeypatch):
    esperado = _state(_recurso("huaweicloud_css_cluster", id="remoto-1"))
    fake = FakeRun(stdout=json.dumps(esperado))
    monkeypatch.setattr(tfstate.subprocess, "run", fake)
    _activar_backend(ws)

    assert tfstate.read_state(ws) == esperado
    assert fake.comandos == [["terraform", "state", "pull"]]


def test_el_remoto_ignora_un_state_local_viejo(ws, monkeypatch):
    """Un `terraform.tfstate` que quedó del modo local NO puede ganarle al
    remoto: sería operar contra un mundo que ya no existe."""
    _escribir_state(ws, _state(_recurso("huaweicloud_css_cluster", id="VIEJO")))
    _activar_backend(ws)
    monkeypatch.setattr(tfstate.subprocess, "run",
                        FakeRun(stdout=json.dumps(_state(_recurso("x", id="NUEVO")))))

    assert tfstate.read_state(ws)["resources"][0]["instances"][0]["attributes"]["id"] == "NUEVO"


def test_state_pull_que_falla_no_levanta(ws, monkeypatch):
    monkeypatch.setattr(tfstate.subprocess, "run",
                        FakeRun(returncode=1, stderr="backend no inicializado"))
    _activar_backend(ws)

    assert tfstate.read_state(ws) == {}
    assert tfstate.has_resources(ws) is False


def test_terraform_ausente_no_levanta(ws, monkeypatch):
    def _no_existe(*a, **k):
        raise FileNotFoundError("terraform")
    monkeypatch.setattr(tfstate.subprocess, "run", _no_existe)
    _activar_backend(ws)

    assert tfstate.read_state(ws) == {}


def test_remoto_ilegible_no_hereda_el_fallback_de_tamano(ws, monkeypatch):
    """El fallback por tamaño es del archivo local. En remoto no hay archivo, así
    que inventar recursos a partir de uno viejo sería peor que decir que no hay."""
    (ws / tfstate.STATE_FILE).write_text("x" * 512, encoding="utf-8")
    _activar_backend(ws)
    monkeypatch.setattr(tfstate.subprocess, "run", FakeRun(returncode=1))

    assert tfstate.has_resources(ws) is False


# ── Cache ────────────────────────────────────────────────────────────────────
def test_el_cache_colapsa_los_polls(ws, monkeypatch):
    fake = FakeRun(stdout=json.dumps(_state(_recurso("x"))))
    monkeypatch.setattr(tfstate.subprocess, "run", fake)
    _activar_backend(ws)

    for _ in range(5):
        tfstate.read_state(ws)

    assert len(fake.comandos) == 1


def test_invalidate_fuerza_una_lectura_nueva(ws, monkeypatch):
    """Tras un apply o un destroy el mundo cambió: el status no puede mostrar
    hasta 5 segundos el estado anterior."""
    fake = FakeRun(stdout=json.dumps(_state(_recurso("x"))))
    monkeypatch.setattr(tfstate.subprocess, "run", fake)
    _activar_backend(ws)

    tfstate.read_state(ws)
    tfstate.invalidate(ws)
    tfstate.read_state(ws)

    assert len(fake.comandos) == 2


def test_el_cache_no_mezcla_workspaces(tmp_path, monkeypatch):
    """Cada SA tiene su propio state: cachear por proceso y no por directorio
    haría que un SA viera la infra de otro."""
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        _activar_backend(d)

    salidas = {str(a): _state(_recurso("x", id="de-a")),
               str(b): _state(_recurso("x", id="de-b"))}

    def _run(cmd, **kwargs):
        return FakeRun(stdout=json.dumps(salidas[kwargs["cwd"]]))
    monkeypatch.setattr(tfstate.subprocess, "run", _run)

    id_a = tfstate.read_state(a)["resources"][0]["instances"][0]["attributes"]["id"]
    id_b = tfstate.read_state(b)["resources"][0]["instances"][0]["attributes"]["id"]

    assert (id_a, id_b) == ("de-a", "de-b")


# ── prepare(): reconciliar el backend ────────────────────────────────────────
@pytest.fixture
def sin_bucket(monkeypatch):
    monkeypatch.setattr(tfstate, "backend_settings", lambda: {})


@pytest.fixture
def con_bucket(monkeypatch):
    monkeypatch.setattr(tfstate, "backend_settings", lambda: {
        "bucket": "demos-css", "region": "la-south-2",
        "endpoint": "https://obs.la-south-2.myhuaweicloud.com",
        "access_key": "AK", "secret_key": "SK"})


# La config que Terraform registra tras un init contra el backend de `con_bucket`
# con la key "k". Es lo que `prepare` compara para decidir si hay que re-leer.
_CONFIG_REGISTRADA = {
    "bucket": "demos-css", "key": "k", "region": "la-south-2",
    "endpoints": {"s3": "https://obs.la-south-2.myhuaweicloud.com"},
    "access_key": "AK", "secret_key": "SK",
    "skip_credentials_validation": True, "skip_region_validation": True,
    "skip_metadata_api_check": True, "skip_requesting_account_id": True,
    "use_path_style": False, "skip_s3_checksum": True,
}


def _registrar_backend(ws, tipo, config=None):
    """Simula lo que Terraform deja en .terraform/terraform.tfstate tras un init.
    Para s3, con la config tal como la registra Terraform (todas las claves)."""
    d = ws / ".terraform"
    d.mkdir(exist_ok=True)
    if tipo == "s3":
        datos = {"backend": {"type": "s3", "config": dict(_CONFIG_REGISTRADA, **(config or {}))}}
    elif tipo:
        datos = {"backend": {"type": tipo}}
    else:
        datos = {}
    (d / "terraform.tfstate").write_text(json.dumps(datos), encoding="utf-8")


def test_sin_bucket_no_escribe_backend(ws, sin_bucket):
    assert tfstate.prepare(ws, "k") == (True, [])
    assert not (ws / tfstate.BACKEND_FILE).exists()


def test_con_bucket_escribe_el_backend(ws, con_bucket):
    tfstate.prepare(ws, "tfstate/u1/terraform.tfstate")

    hcl = (ws / tfstate.BACKEND_FILE).read_text(encoding="utf-8")
    assert 'bucket = "demos-css"' in hcl
    assert 'key    = "tfstate/u1/terraform.tfstate"' in hcl
    assert 's3 = "https://obs.la-south-2.myhuaweicloud.com"' in hcl
    # Sin estos, Terraform aborta validando APIs de AWS que OBS no tiene.
    for flag in ("skip_credentials_validation", "skip_region_validation",
                 "skip_metadata_api_check", "skip_requesting_account_id"):
        assert re.search(rf"{flag}\s*=\s*true", hcl), flag


def test_el_bucket_va_como_subdominio_no_en_el_path(ws, con_bucket):
    """OBS rechaza el path-style: el primer `init` real murió con
    `403 VirtualHostDomainRequired`. Este test solo chequeaba que la clave
    ESTUVIERA, no su valor — y estaba en `true`."""
    tfstate.prepare(ws, "k")

    hcl = (ws / tfstate.BACKEND_FILE).read_text(encoding="utf-8")
    m = re.search(r"use_path_style\s*=\s*(\w+)", hcl)
    assert m and m.group(1) == "false", hcl


def test_primer_init_no_pide_migracion(ws, con_bucket):
    """Sin init previo ni state local no hay nada que migrar, pero SÍ hay que
    inicializar: el workspace viene del template con los providers cacheados."""
    assert tfstate.prepare(ws, "k") == (True, [])


def test_primer_init_con_state_local_migra(ws, con_bucket):
    """Terraform NO deja registro de backend para el `local` implícito. Un
    workspace que desplegó en el disco y ahora estrena bucket se ve igual que
    uno nuevo —sin `.terraform/terraform.tfstate`— pero tiene un state con
    recursos. Un init pelado con `-input=false` muere con "Can't ask approval
    for state migration when interactive input is disabled". Pasó."""
    _escribir_state(ws, _state(_recurso("huaweicloud_css_cluster")))

    assert tfstate.prepare(ws, "k") == (True, ["-migrate-state", "-force-copy"])


def test_primer_init_con_state_local_vacio_no_migra(ws, con_bucket):
    _escribir_state(ws, _state())          # sin recursos: nada que migrar

    assert tfstate.prepare(ws, "k") == (True, [])


def test_rotar_credenciales_reconfigura(ws, con_bucket):
    """Terraform guarda la config ENTERA del backend —AK/SK incluidas— y si algo
    cambió aborta con "Backend configuration changed". La comparación vieja
    miraba tres claves: rotar las credenciales no disparaba ningún init."""
    _registrar_backend(ws, "s3", {"access_key": "VIEJA"})
    assert tfstate.prepare(ws, "k") == (True, ["-reconfigure"])

    _registrar_backend(ws, "s3", {"region": "otra-region"})
    assert tfstate.prepare(ws, "k") == (True, ["-reconfigure"])


def test_pasar_de_local_a_remoto_migra(ws, con_bucket):
    _registrar_backend(ws, None)          # el init anterior fue local

    assert tfstate.prepare(ws, "k") == (True, ["-migrate-state", "-force-copy"])


def test_pasar_de_remoto_a_local_tambien_migra(ws, sin_bucket):
    """Vaciar el bucket tiene que TRAER el estado de vuelta. Si solo se borrara
    el backend.tf, Terraform arrancaría local y vacío: no vería los recursos y un
    apply intentaría crear todo de nuevo."""
    _activar_backend(ws)
    _registrar_backend(ws, "s3")

    assert tfstate.prepare(ws, "k") == (True, ["-migrate-state", "-force-copy"])
    assert not (ws / tfstate.BACKEND_FILE).exists()


def test_backend_sin_cambios_no_migra(ws, con_bucket):
    _registrar_backend(ws, "s3")

    assert tfstate.prepare(ws, "k") == (False, [])


def test_el_backend_salta_el_checksum_de_s3(ws, con_bucket):
    """OBS rechaza el PutObject firmado por chunks con `XAmzContentSHA256Mismatch`.
    Pasó en un deploy real: los clusters se crearon y el state quedó solo en
    errored.tfstate. `skip_s3_checksum` es el flag de Terraform para los S3
    compatibles que no soportan esa firma."""
    tfstate.prepare(ws, "k")

    hcl = (ws / tfstate.BACKEND_FILE).read_text(encoding="utf-8")
    assert re.search(r"skip_s3_checksum\s*=\s*true", hcl), hcl


def test_cambio_de_config_sin_mudanza_reconfigura(ws, con_bucket):
    """El backend sigue siendo s3 y el state sigue en el mismo bucket/key, pero
    la config cambió (acá: falta `skip_s3_checksum`, que se agregó después del
    init anterior). Sin init, Terraform aborta el apply con "Backend
    configuration changed". `-reconfigure` re-lee la config sin migrar nada."""
    _registrar_backend(ws, "s3", {"skip_s3_checksum": None})

    assert tfstate.prepare(ws, "k") == (True, ["-reconfigure"])


def test_otra_key_en_el_mismo_bucket_es_una_mudanza(ws, con_bucket):
    """Cambiar la key es mover el state: eso sí migra, no reconfigura."""
    _registrar_backend(ws, "s3", {"key": "tfstate/otro/terraform.tfstate"})

    assert tfstate.prepare(ws, "k") == (True, ["-migrate-state", "-force-copy"])


def test_local_estable_no_migra(ws, sin_bucket):
    _registrar_backend(ws, None)

    assert tfstate.prepare(ws, "k") == (False, [])


def test_prepare_invalida_el_cache(ws, con_bucket, monkeypatch):
    monkeypatch.setattr(tfstate.subprocess, "run",
                        FakeRun(stdout=json.dumps(_state(_recurso("x")))))
    _activar_backend(ws)
    tfstate.read_state(ws)
    assert str(ws) in tfstate._cache

    tfstate.prepare(ws, "k")

    assert str(ws) not in tfstate._cache


# ── backend_settings(): cuándo se considera activado ────────────────────────
def test_sin_bucket_de_demos_el_estado_queda_local(monkeypatch):
    """Una instalación recién levantada, antes de que el SA cargue su cuenta: no
    hay dónde poner el state, así que sigue en disco."""
    import maas_integrator as mi
    monkeypatch.setattr(mi, "get_huawei_settings", lambda: {"demo_bucket": ""})

    assert tfstate.state_bucket() == ""
    assert tfstate.backend_settings() == {}


def test_bucket_sin_credenciales_es_un_error_no_una_vuelta_a_local(monkeypatch):
    """**La expectativa cambió a propósito.** Este test afirmaba que sin AK/SK
    "mejor seguir local que romper el deploy". Pero "seguir local" no era
    inocuo: `prepare` borraba el backend.tf y traía el state de OBS al disco
    con `-force-copy`. Un guardado parcial de ⚙ Configuración movía el state de
    lugar sin que nadie lo pidiera, y el init siguiente fallaba "a veces". Ahora
    es un error con nombre, que el deploy muestra y no sigue."""
    import maas_integrator as mi
    monkeypatch.setattr(mi, "get_huawei_settings", lambda: {"demo_bucket": "b"})
    monkeypatch.setattr(mi, "resolve_obs_creds", lambda *a, **k: ("", ""))

    with pytest.raises(tfstate.BackendIncompleto, match="`b`"):
        tfstate.backend_settings()


def test_usa_el_mismo_bucket_que_los_datasets(monkeypatch):
    """Un solo bucket y ninguna perilla: si hay bucket y credenciales, el estado
    va a OBS."""
    import maas_integrator as mi
    monkeypatch.setattr(mi, "get_huawei_settings", lambda: {"demo_bucket": "demos-css"})
    monkeypatch.setattr(mi, "resolve_obs_creds", lambda *a, **k: ("AK", "SK"))
    monkeypatch.setattr(mi, "get_region", lambda: "la-south-2")

    cfg = tfstate.backend_settings()

    assert cfg["bucket"] == "demos-css"
    assert cfg["endpoint"] == "https://obs.la-south-2.myhuaweicloud.com"
    assert (cfg["access_key"], cfg["secret_key"]) == ("AK", "SK")


def test_el_state_key_separa_por_usuario():
    """Dos SAs nunca pueden compartir state: se pisarían la infra."""
    assert tfstate.state_key_for("u1") != tfstate.state_key_for("u2")
    assert tfstate.state_key_for("u1").endswith("terraform.tfstate")
    assert tfstate.state_key_for("") == "tfstate/default/terraform.tfstate"


def test_el_state_vive_bajo_un_prefijo_propio():
    """Comparte bucket con los datasets (`<slug>-logs/`), así que tiene que estar
    bajo un prefijo que ningún pipeline use."""
    assert tfstate.state_key_for("u1").startswith(tfstate.STATE_PREFIX + "/")
    assert not tfstate.STATE_PREFIX.endswith("-logs")


# ── Aislamiento entre SAs ────────────────────────────────────────────────────
def test_el_backend_del_template_no_se_propaga_a_los_workspaces(tmp_path, monkeypatch):
    """`_ensure_workspace` recopia todos los *.tf del template en CADA request.
    Si `backend.tf` entrara por ahí, todos los SAs quedarían apuntando al mismo
    state y se destruirían la infra entre sí."""
    import auth as _auth

    tmpl = tmp_path / "template" / "terraform"
    tmpl.mkdir(parents=True)
    (tmpl / "main.tf").write_text("# infra\n", encoding="utf-8")
    (tmpl / "backend.tf").write_text('# state de OTRO\nkey = "tfstate/otro/x"\n',
                                     encoding="utf-8")
    monkeypatch.setattr(_auth, "TERRAFORM_TEMPLATE", tmpl)
    monkeypatch.setattr(_auth, "DATA_ROOT", tmp_path / "data")

    ctx = _auth.build_user_ctx("sa@huawei.com")
    propio = ctx.terraform_dir / "backend.tf"
    propio.write_text('key = "tfstate/mio/x"\n', encoding="utf-8")

    _auth.build_user_ctx("sa@huawei.com")      # el refresh de cada request

    assert (ctx.terraform_dir / "main.tf").exists(), "el resto del template sí se copia"
    assert propio.read_text(encoding="utf-8") == 'key = "tfstate/mio/x"\n'


def test_workspace_sembrado_con_providers_igual_pide_init(ws, con_bucket):
    """El caso del PRIMER deploy con el bucket cargado.

    `_ensure_workspace` siembra el workspace copiando el template, providers
    incluidos. Si "hace falta init" se dedujera de "faltan providers", acá se
    escribiría el `backend.tf` sin inicializarlo nunca y el apply moriría con
    "Backend initialization required".
    """
    (ws / ".terraform" / "providers").mkdir(parents=True)   # vienen del template

    necesita_init, extra = tfstate.prepare(ws, "tfstate/u1/terraform.tfstate")

    assert necesita_init is True
    assert extra == []                       # nada que migrar: es el primero
    assert (ws / tfstate.BACKEND_FILE).exists()


def test_terraform_corre_sin_color():
    """Terraform colorea aunque no haya terminal. Sin `-no-color`, el error de un
    init fallido llegaba al front con los `[31m` adentro, ilegible."""
    fuente = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    llamadas = re.findall(r'\["terraform", "(init|apply|destroy)"[^\]]*\]', fuente)
    assert len(llamadas) >= 4, "cambió la forma de invocar terraform en main.py"
    sin_flag = [l for l in re.findall(r'\["terraform", "(?:init|apply|destroy)"[^\]]*\]', fuente)
                if "-no-color" not in l]
    assert not sin_flag, sin_flag


# ── errored.tfstate ──────────────────────────────────────────────────────────
# Cuando el backend rechaza la escritura, Terraform deja el state en este
# archivo. Otro apply sin subirlo crea un state bifurcado: los clusters que ya
# existen no están en ningún state y se crea OTRO par, facturando los dos.
def test_sin_errored_no_hace_nada(ws, monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(tfstate.subprocess, "run", run)

    assert tfstate.push_errored_state(ws) == (True, "")
    assert run.comandos == []


def test_errored_se_sube_y_se_borra(ws, monkeypatch):
    (ws / tfstate.ERRORED_FILE).write_text("{}", encoding="utf-8")
    run = FakeRun()
    monkeypatch.setattr(tfstate.subprocess, "run", run)

    ok, detalle = tfstate.push_errored_state(ws)

    assert ok and "recuperado" in detalle
    assert run.comandos == [["terraform", "state", "push", "-no-color", "errored.tfstate"]]
    assert "-force" not in run.comandos[0], "sin -force: un push rechazado por linaje es una señal, no un obstáculo"
    assert not (ws / tfstate.ERRORED_FILE).exists(), "subido → ya no hace falta"


def test_si_el_push_falla_el_archivo_se_queda(ws, monkeypatch):
    """Si no se pudo subir, el archivo es lo ÚNICO que sabe qué recursos existen:
    no se borra, y el que llama tiene que parar en vez de aplicar."""
    (ws / tfstate.ERRORED_FILE).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tfstate.subprocess, "run",
                        FakeRun(returncode=1, stderr="lineage mismatch"))

    ok, detalle = tfstate.push_errored_state(ws)

    assert not ok and "lineage" in detalle
    assert (ws / tfstate.ERRORED_FILE).exists()
