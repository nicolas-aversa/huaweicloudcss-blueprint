"""El dataset de pedidos omnicanal (Retail) que se carga por el Builder: un
evento JSON por cambio de estado del pedido, como lo avisa el OMS de la
plataforma de comercio, completado con el detalle del pedido."""
import json
from datetime import date

import build_retail_dataset as retail


def _eventos(desde=date(2025, 11, 1), hasta=date(2025, 11, 10), por_dia=12):
    filas, cuenta = retail.generar(desde, hasta, por_dia)
    return [json.loads(retail.a_json(f)) for f in filas], cuenta


def test_cada_evento_es_un_cambio_de_estado_con_el_pedido():
    ev, cuenta = _eventos()
    assert cuenta["Pedido creado"] > 50 and cuenta["Pago aprobado"] and cuenta["Facturado"]
    e = ev[0]
    assert {"timestamp", "evento", "pedido", "destino", "producto", "importe", "pago", "entrega"} <= set(e)
    assert e["timestamp"].endswith("-03:00") and e["pedido"]["id"].endswith("-01")
    assert ev == sorted(ev, key=lambda x: x["timestamp"]), "en orden de tiempo"
    estados = {x["pedido"].get("estado_oms") for x in ev} - {None}
    assert estados <= {"payment-pending", "payment-approved", "payment-denied", "handling", "invoiced", "canceled"}


def test_los_datos_de_contacto_solo_al_crear_el_pedido():
    ev, _ = _eventos()
    creados = [x for x in ev if x["evento"] == "Pedido creado"]
    assert all({"dni", "email", "telefono"} <= set(x["cliente"]) for x in creados)
    assert not [x for x in ev if x["evento"] != "Pedido creado" and set(x.get("cliente", {})) - {"id"}]


def test_la_noche_del_cybermonday_se_cae_el_procesador():
    """Un error de comunicación aislado puede pasar cualquier día; la caída los
    concentra en esa noche."""
    from collections import Counter
    ev, _ = _eventos(por_dia=25)
    por_dia = Counter(x["timestamp"][:10] for x in ev if x["evento"] == "Pago rechazado"
                      and x["pago"].get("motivo_rechazo") == "Error de comunicación con el procesador")
    dia, cuantos = por_dia.most_common(1)[0]
    assert dia == "2025-11-04" and cuantos >= 3 * max([n for d, n in por_dia.items() if d != dia] or [1])


def test_la_posventa_trae_el_comentario_del_cliente():
    ev, _ = _eventos(date(2025, 11, 1), date(2025, 12, 31), 15)
    comentarios = {x["posventa"]["comentario"] for x in ev if x.get("posventa", {}).get("comentario")}
    assert len(comentarios) >= 20, "muchas formas de decir lo mismo: para la búsqueda por significado"
    assert all(x["posventa"].get("motivo") for x in ev if x.get("posventa", {}).get("comentario"))
