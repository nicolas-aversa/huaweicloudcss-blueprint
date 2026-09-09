"""Tests de la resolución de administradores y del reset de contraseña.

El problema que originó esto: en una instancia con `SA_ADMINS` vacía el admin es
el PRIMER usuario registrado, y si te registraste segundo no había forma de
corregirlo desde la app. Ahora los admins se promueven desde el Panel de control
y se persisten, con el bootstrap como último fallback.
"""

import json

import pytest

import auth


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Aísla el store: `DATA_ROOT` y los paths derivados se resuelven al importar,
    así que hay que repuntar los tres."""
    monkeypatch.setattr(auth, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(auth, "_ADMINS_FILE", tmp_path / "admins.json")
    monkeypatch.setattr(auth, "_ALLOWLIST_FILE", tmp_path / "allowlist.json")
    monkeypatch.delenv("SA_ADMINS", raising=False)
    monkeypatch.delenv("SA_ALLOWLIST", raising=False)
    return tmp_path


def _user(email, created):
    users = auth._load_users()
    users[email] = {"pw": auth.hash_password("x"), "created": created}
    auth._save_users(users)


# ── Bootstrap: el primer usuario ────────────────────────────────────────────
def test_first_registered_user_is_admin_when_nothing_declared():
    _user("primero@huawei.com", 1000)
    _user("segundo@huawei.com", 2000)

    assert auth.is_admin("primero@huawei.com") is True
    assert auth.is_admin("segundo@huawei.com") is False
    assert auth.admins_info()["bootstrap"] == "primero@huawei.com"


def test_no_users_means_no_admin():
    assert auth.is_admin("cualquiera@huawei.com") is False
    assert auth.is_admin("") is False
    assert auth.is_admin(None) is False


def test_same_second_ties_break_alphabetically():
    _user("zeta@huawei.com", 1000)
    _user("alfa@huawei.com", 1000)
    assert auth.is_admin("alfa@huawei.com") is True


# ── SA_ADMINS (env) ─────────────────────────────────────────────────────────
def test_env_admins_win_over_bootstrap(monkeypatch):
    _user("primero@huawei.com", 1000)
    monkeypatch.setenv("SA_ADMINS", "jefe@huawei.com")

    assert auth.is_admin("jefe@huawei.com") is True
    assert auth.is_admin("primero@huawei.com") is False, "el bootstrap no aplica si hay declarados"


def test_env_admins_accept_several_separators(monkeypatch):
    monkeypatch.setenv("SA_ADMINS", "a@h.com, b@h.com;c@h.com")
    for e in ("a@h.com", "b@h.com", "c@h.com"):
        assert auth.is_admin(e) is True


def test_email_case_and_spaces_do_not_break_the_match(monkeypatch):
    monkeypatch.setenv("SA_ADMINS", "  Jefe@Huawei.com ")
    assert auth.is_admin("jefe@huawei.com") is True
    assert auth.is_admin("JEFE@HUAWEI.COM") is True


# ── Promoción desde el Panel de control ─────────────────────────────────────
def test_add_admin_promotes_and_persists(isolated):
    _user("primero@huawei.com", 1000)
    _user("nico@huawei.com", 2000)

    assert auth.add_admin("nico@huawei.com") is True
    assert auth.is_admin("nico@huawei.com") is True

    on_disk = json.loads((isolated / "admins.json").read_text(encoding="utf-8"))
    assert "nico@huawei.com" in on_disk


def test_promoting_keeps_the_current_admin(isolated):
    """Al declarar el primer admin se pierde el bootstrap implícito: si no se
    persistiera también el admin actual, quien promueve se quedaría sin permisos
    en el mismo movimiento."""
    _user("primero@huawei.com", 1000)
    _user("nico@huawei.com", 2000)
    assert auth.is_admin("primero@huawei.com") is True

    auth.add_admin("nico@huawei.com")

    assert auth.is_admin("primero@huawei.com") is True, "el que promovía perdió el admin"
    assert auth.is_admin("nico@huawei.com") is True


def test_add_admin_also_allows_the_email():
    """Ser admin sin poder entrar no sirve: la promoción suma a la allowlist."""
    auth.add_allowed("otro@huawei.com")  # allowlist no vacía → restrictiva
    assert auth.is_allowed("nuevo@huawei.com") is False

    auth.add_admin("nuevo@huawei.com")
    assert auth.is_allowed("nuevo@huawei.com") is True


def test_add_admin_rejects_invalid_email():
    assert auth.add_admin("no-es-un-email") is False
    assert auth.add_admin("") is False


def test_env_and_persisted_admins_are_combined(monkeypatch):
    monkeypatch.setenv("SA_ADMINS", "jefe@huawei.com")
    auth.add_admin("nico@huawei.com")

    assert auth.is_admin("jefe@huawei.com") is True
    assert auth.is_admin("nico@huawei.com") is True
    info = auth.admins_info()
    assert info["env"] == ["jefe@huawei.com"]
    assert info["added"] == ["nico@huawei.com"]
    assert info["bootstrap"] == ""


# ── Quitar el rol ───────────────────────────────────────────────────────────
def test_remove_admin_works_with_more_than_one():
    auth.add_admin("uno@huawei.com")
    auth.add_admin("dos@huawei.com")

    ok, reason = auth.remove_admin("dos@huawei.com")
    assert ok is True and reason == ""
    assert auth.is_admin("dos@huawei.com") is False
    assert auth.is_admin("uno@huawei.com") is True


def test_remove_last_admin_is_blocked():
    """Sin este guard la instancia queda sin ningún admin, irreversible desde la UI."""
    auth.add_admin("solo@huawei.com")

    ok, reason = auth.remove_admin("solo@huawei.com")
    assert ok is False
    assert "único administrador" in reason
    assert auth.is_admin("solo@huawei.com") is True


def test_env_admin_cannot_be_removed_from_the_ui(monkeypatch):
    monkeypatch.setenv("SA_ADMINS", "jefe@huawei.com")
    ok, reason = auth.remove_admin("jefe@huawei.com")
    assert ok is False
    assert "SA_ADMINS" in reason
    assert auth.is_admin("jefe@huawei.com") is True


def test_remove_non_admin_reports_it():
    auth.add_admin("uno@huawei.com")
    ok, reason = auth.remove_admin("nadie@huawei.com")
    assert ok is False and "no es admin" in reason


def test_last_persisted_admin_can_go_if_env_has_one(monkeypatch):
    monkeypatch.setenv("SA_ADMINS", "jefe@huawei.com")
    auth.add_admin("temporal@huawei.com")

    ok, _ = auth.remove_admin("temporal@huawei.com")
    assert ok is True, "queda el de env, así que no hay lockout"


# ── Reset de contraseña ─────────────────────────────────────────────────────
def test_reset_keeps_created_and_admin_role():
    """El reset borraba el registro entero: el usuario perdía su antigüedad y con
    ella el admin de bootstrap. Si era el único, la instancia quedaba sin admin."""
    _user("primero@huawei.com", 1000)
    _user("segundo@huawei.com", 2000)
    assert auth.is_admin("primero@huawei.com") is True

    assert auth.admin_reset_user("primero@huawei.com") is True

    rec = auth._load_users()["primero@huawei.com"]
    assert rec["created"] == 1000, "se perdió la antigüedad"
    assert rec["pw"] == "", "la password debe quedar vacía (reset pendiente)"
    assert auth.is_admin("primero@huawei.com") is True, "el reset le quitó el admin"


def test_reset_then_login_sets_the_new_password():
    _user("nico@huawei.com", 1000)
    auth.admin_reset_user("nico@huawei.com")

    assert auth.check_login("nico@huawei.com", "nueva-clave") is True
    assert auth._load_users()["nico@huawei.com"]["created"] == 1000
    # Y ya vale como credencial normal.
    assert auth.check_login("nico@huawei.com", "nueva-clave") is True
    assert auth.check_login("nico@huawei.com", "otra") is False


def test_reset_unknown_user_is_false():
    assert auth.admin_reset_user("nadie@huawei.com") is False


def test_list_users_marks_pending_reset():
    _user("nico@huawei.com", 1000)
    auth.admin_reset_user("nico@huawei.com")
    row = next(u for u in auth.list_users() if u["email"] == "nico@huawei.com")
    assert row["has_password"] is False
