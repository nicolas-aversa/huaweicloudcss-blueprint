"""Autenticación y aislamiento por usuario para la app hosteada multi-SA.

La app está pensada para que UN SA la hostee con una URL y otros SAs (grupo
acotado y de confianza) la usen desde su navegador — cada uno desplegando en SU
cuenta (o la del owner). Este módulo agrega, sin dependencias externas (solo
stdlib):

  - login **email + password** (password hasheado con pbkdf2-sha256),
  - **allowlist** de emails (env `SA_ALLOWLIST`),
  - **sesión** por cookie firmada (hmac-sha256),
  - **workspace por usuario** bajo `APP_DATA_DIR/users/<id>/` (settings +
    terraform + state separados), y
  - un **middleware ASGI** que exige sesión y liga el contexto por-request.

Auth se ACTIVA solo si hay `APP_SECRET_KEY` en el entorno. Sin esa var, el
middleware es passthrough y la app se comporta como la herramienta single-user
de siempre (dev local, tests, arranque nativo) → 100% backward compatible.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import maas_integrator as _mi

# ── Config (todo por env, con defaults sensatos) ─────────────────────────────
_SECRET = (os.environ.get("APP_SECRET_KEY") or "").encode("utf-8")
AUTH_ENABLED = bool(_SECRET)

DATA_ROOT = Path(os.environ.get("APP_DATA_DIR") or (Path(__file__).parent / "data"))
TERRAFORM_TEMPLATE = Path(
    os.environ.get("TERRAFORM_TEMPLATE_DIR") or (Path(__file__).parent / "terraform"))

COOKIE_NAME = "sa_session"
SESSION_TTL = int(os.environ.get("SESSION_TTL_SECONDS", str(7 * 24 * 3600)))
# En prod (HTTPS por Caddy) las cookies van Secure. Para probar por HTTP local,
# poné APP_INSECURE_COOKIES=1.
SECURE_COOKIES = os.environ.get("APP_INSECURE_COOKIES", "").lower() not in ("1", "true", "yes")

_USERS_FILE = DATA_ROOT / "users.json"

# Contexto del usuario actual (seteado por el middleware por-request).
current_user_var: "contextvars.ContextVar[UserCtx | None]" = contextvars.ContextVar(
    "current_user", default=None)


# ── Allowlist ────────────────────────────────────────────────────────────────
def _allowlist() -> set[str]:
    raw = os.environ.get("SA_ALLOWLIST", "")
    return {e.strip().lower() for e in re.split(r"[,;\s]+", raw) if e.strip()}


_ALLOWLIST_FILE = DATA_ROOT / "allowlist.json"


def _persisted_allowlist() -> set[str]:
    """Emails agregados en runtime desde el panel admin (además de SA_ALLOWLIST)."""
    try:
        if _ALLOWLIST_FILE.is_file():
            data = json.loads(_ALLOWLIST_FILE.read_text(encoding="utf-8"))
            return {str(e).strip().lower() for e in data if str(e).strip()}
    except (OSError, json.JSONDecodeError):
        pass
    return set()


def _save_persisted_allowlist(emails: set[str]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    _ALLOWLIST_FILE.write_text(json.dumps(sorted(emails), indent=2), encoding="utf-8")


def is_allowed(email: str) -> bool:
    """True si el email puede usar la app. Allowlist = SA_ALLOWLIST (env) ∪ los
    agregados por admin. Vacía = registro abierto (bootstrap; el owner debería
    setear al menos un email o SA_ALLOWLIST)."""
    if not email:
        return False
    combined = _allowlist() | _persisted_allowlist()
    return (not combined) or (email.lower() in combined)


def add_allowed(email: str) -> bool:
    """Agrega un email a la allowlist persistida (panel admin). True si es válido."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    s = _persisted_allowlist()
    s.add(email)
    _save_persisted_allowlist(s)
    return True


def remove_allowed(email: str) -> bool:
    """Quita un email de la allowlist persistida. Los de SA_ALLOWLIST (env) no se
    tocan desde acá. True si existía en la persistida."""
    email = (email or "").strip().lower()
    s = _persisted_allowlist()
    if email in s:
        s.discard(email)
        _save_persisted_allowlist(s)
        return True
    return False


def allowlist_info() -> dict:
    """Para el panel admin: emails de env (no removibles) y agregados (removibles)."""
    return {"env": sorted(_allowlist()), "added": sorted(_persisted_allowlist())}


def _admins() -> set[str]:
    raw = os.environ.get("SA_ADMINS", "")
    return {e.strip().lower() for e in re.split(r"[,;\s]+", raw) if e.strip()}


def is_admin(email: str | None) -> bool:
    """True si el email es admin (env SA_ADMINS). Sin SA_ADMINS no hay admins."""
    return bool(email) and email.lower() in _admins()


# ── Password hashing (pbkdf2-sha256, stdlib) ─────────────────────────────────
_PBKDF2_ITERS = 200_000


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _algo, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ── User store (JSON en disco) ───────────────────────────────────────────────
def _load_users() -> dict:
    try:
        if _USERS_FILE.is_file():
            return json.loads(_USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_users(users: dict) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    _USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")


def user_exists(email: str) -> bool:
    return email.lower() in _load_users()


def create_user(email: str, password: str) -> None:
    users = _load_users()
    users[email.lower()] = {"pw": hash_password(password), "created": int(time.time())}
    _save_users(users)


def list_users() -> list[dict]:
    """Para el panel admin: usuarios registrados con estado (sin exponer hashes)."""
    users = _load_users()
    out = []
    for email, rec in sorted(users.items()):
        out.append({
            "email": email,
            "created": rec.get("created"),
            "has_password": bool(rec.get("pw")),
            "is_admin": is_admin(email),
        })
    return out


def admin_reset_user(email: str) -> bool:
    """Resetea la contraseña de un usuario borrando su registro: al próximo login
    (si sigue en la allowlist) crea una nueva. True si existía."""
    email = (email or "").lower().strip()
    users = _load_users()
    if email in users:
        users.pop(email, None)
        _save_users(users)
        return True
    return False


def check_login(email: str, password: str) -> bool:
    """Verifica credenciales. Si el email está permitido y aún no tiene cuenta,
    la crea con este password (first-login = alta, apto para grupo de confianza)."""
    email = email.lower().strip()
    if not email or not password or not is_allowed(email):
        return False
    users = _load_users()
    rec = users.get(email)
    if rec is None:
        create_user(email, password)
        return True
    return verify_password(password, rec.get("pw", ""))


# ── Sesión (cookie firmada, hmac-sha256) ─────────────────────────────────────
def _sign(msg: str) -> str:
    return hmac.new(_SECRET, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session_token(email: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    b = base64.urlsafe_b64encode(f"{email.lower()}|{exp}".encode("utf-8")).decode().rstrip("=")
    return f"{b}.{_sign(b)}"


def read_session_token(token: str | None) -> str | None:
    """Devuelve el email si la firma es válida y no expiró; si no, None."""
    if not token or "." not in token:
        return None
    try:
        b, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(b)):
            return None
        payload = base64.urlsafe_b64decode(b + "===").decode("utf-8")
        email, exp = payload.split("|")
        if int(exp) < time.time():
            return None
        return email
    except Exception:
        return None


# ── Contexto y workspace por usuario ─────────────────────────────────────────
@dataclass
class UserCtx:
    email: str
    user_id: str
    data_dir: Path
    settings_path: Path
    terraform_dir: Path


def _user_id(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", email.lower()).strip("_") or "user"


# Al sembrar el dir de terraform del usuario, NO copiar estado, secretos ni
# marcadores del template (solo la fuente + providers si el image los trae).
_TF_SEED_IGNORE = shutil.ignore_patterns(
    "terraform.tfstate", "terraform.tfstate.*", "*.backup",
    "*.auto.tfvars.json", "pipeline.conf",
    ".platform_deploy.json", ".pipelines.json",
)


def _ensure_workspace(ctx: "UserCtx") -> None:
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    if not (ctx.terraform_dir / "main.tf").exists():
        # Siembra desde el template baked (con .terraform/providers si el image
        # los trae → `terraform init` no re-descarga). Estado/secretos excluidos.
        shutil.copytree(TERRAFORM_TEMPLATE, ctx.terraform_dir,
                        ignore=_TF_SEED_IGNORE, dirs_exist_ok=True)


def build_user_ctx(email: str) -> "UserCtx":
    uid = _user_id(email)
    ddir = DATA_ROOT / "users" / uid
    ctx = UserCtx(
        email=email.lower(), user_id=uid, data_dir=ddir,
        settings_path=ddir / "platform_settings.json",
        terraform_dir=ddir / "terraform",
    )
    _ensure_workspace(ctx)
    return ctx


def current_terraform_dir() -> Path | None:
    ctx = current_user_var.get()
    return ctx.terraform_dir if ctx is not None else None


# ── Middleware ASGI: auth + binding del contexto por-request ──────────────────
# "/" es público: la SPA se sirve a todos y muestra el overlay de login cuando no
# hay sesión (con la app blureada detrás). Los /api/* siguen protegidos (401).
_PUBLIC_EXACT = {"/", "/login", "/health", "/favicon.ico", "/api/v1/verticals"}
_PUBLIC_PREFIXES = ("/static/", "/auth/")


def _is_public(path: str) -> bool:
    return path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def _cookie(scope, name: str) -> str | None:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                if "=" in part:
                    ck, cv = part.strip().split("=", 1)
                    if ck == name:
                        return cv
    return None


async def _send_json(send, status: int, obj: dict) -> None:
    body = json.dumps(obj).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8")]})
    await send({"type": "http.response.body", "body": body})


async def _send_redirect(send, location: str) -> None:
    await send({"type": "http.response.start", "status": 303,
                "headers": [(b"location", location.encode("latin-1"))]})
    await send({"type": "http.response.body", "body": b""})


class AuthMiddleware:
    """Middleware ASGI puro (no BaseHTTPMiddleware) para que los contextvars que
    setea propaguen bien al endpoint sync (threadpool) y al streaming del deploy."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not AUTH_ENABLED:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if _is_public(path):
            return await self.app(scope, receive, send)

        email = read_session_token(_cookie(scope, COOKIE_NAME))
        if not email or not is_allowed(email) or not user_exists(email):
            if path.startswith("/api/"):
                return await _send_json(send, 401, {"detail": "No autenticado. Iniciá sesión."})
            return await _send_redirect(send, "/login")

        ctx = build_user_ctx(email)
        tok = current_user_var.set(ctx)
        _mi.set_current_settings_path(ctx.settings_path)
        try:
            await self.app(scope, receive, send)
        finally:
            current_user_var.reset(tok)
            _mi.set_current_settings_path(None)
