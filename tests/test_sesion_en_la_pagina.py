"""En modo hosteado la página sale con la cuenta ya puesta en el nav: después
de un F5 no se ve la marca sola (sin usuario ni Panel de control) mientras
vuelve /auth/me."""
from fastapi.testclient import TestClient

import auth
import main


def _pagina(monkeypatch, email, admin=False, hosteado=True):
    monkeypatch.setattr(auth, "AUTH_ENABLED", hosteado)
    monkeypatch.setattr(auth, "read_session_token", lambda token: email)
    monkeypatch.setattr(auth, "is_admin", lambda e: admin)
    return TestClient(main.app).get("/").text


def test_con_sesion_sale_la_cuenta(monkeypatch):
    html = _pagina(monkeypatch, "sa<x>@huawei.com", admin=True)
    assert '<span class="app-nav__user-email" id="nav-user-email">sa&lt;x&gt;@huawei.com</span>' in html
    assert '<strong id="nav-user-email-full">sa&lt;x&gt;@huawei.com</strong>' in html
    assert '<button class="app-nav__cuenta" id="nav-user"' in html
    assert '<img class="app-nav__logo hidden" id="nav-marca-logo"' in html
    assert '<div class="app-nav__brand-text hidden" id="nav-marca-texto">' in html
    assert '<button class="nav-item" data-view="control" id="nav-control"' in html


def test_sin_ser_admin_el_panel_sigue_oculto(monkeypatch):
    html = _pagina(monkeypatch, "sa@huawei.com")
    assert '<button class="nav-item hidden" data-view="control" id="nav-control"' in html
    assert '<button class="app-nav__cuenta" id="nav-user"' in html


def test_sin_sesion_o_single_user_queda_como_estaba(monkeypatch):
    for html in (_pagina(monkeypatch, None), _pagina(monkeypatch, "sa@huawei.com", hosteado=False)):
        assert '<button class="app-nav__cuenta hidden" id="nav-user"' in html
        assert '<img class="app-nav__logo" id="nav-marca-logo"' in html
        assert '<span class="app-nav__user-email" id="nav-user-email"></span>' in html


def test_cada_reemplazo_encuentra_su_lugar():
    """Si cambia el HTML del nav, el reemplazo no puede fallar callado."""
    html = (main.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for viejo, _ in list(main._NAV_SIN_SESION.values()) + [main._NAV_ADMIN]:
        assert html.count(viejo) == 1, viejo
    assert html.count('<span class="app-nav__user-email" id="nav-user-email"></span>') == 1
    assert html.count('<strong id="nav-user-email-full"></strong>') == 1
