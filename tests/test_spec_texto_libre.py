"""El chat ve el texto libre de un caso creado desde el Builder.

`build_spec_from_fields` dejaba afuera los campos de texto ("no aportan al
PPL"), y el modelo no sabía que existía el comentario de una reseña:
"¿a qué se deben las malas reseñas?" terminaba en "Ese dato no está".
"""
import capabilities

RESENAS = [
    {"field_path": "review_id", "type": "string", "business_label": "Id de reseña", "dimension": False},
    {"field_path": "order_id", "type": "string", "business_label": "Orden", "dimension": False},
    {"field_path": "review_score", "type": "integer", "business_label": "Puntaje", "dimension": True},
    {"field_path": "review_comment_title", "type": "string", "business_label": "Título", "dimension": False},
    {"field_path": "review_comment_message", "type": "text", "business_label": "Comentario", "dimension": False},
    {"field_path": "review_creation_date", "type": "date", "business_label": "Fecha"},
]


def test_el_comentario_entra_como_texto_libre():
    spec = capabilities.build_spec_from_fields("reviews-ordenes", "reviews-ordenes-*", RESENAS, "Reseñas")
    campos = spec["fields"]
    assert "free text" in campos["review_comment_message"]
    assert "match(review_comment_message, 'words')" in campos["review_comment_message"]
    assert "fields review_comment_message | head 30" in campos["review_comment_message"]
    # Un string (keyword) se lee, pero `match` es para campos de texto.
    assert "free text" in campos["review_comment_title"] and "match(" not in campos["review_comment_title"]


def test_los_ids_siguen_afuera():
    campos = capabilities.build_spec_from_fields("r", "r-*", RESENAS, "R")["fields"]
    assert "review_id" not in campos and "order_id" not in campos
    assert "review_score" in campos and "review_creation_date" in campos


def test_una_dimension_no_es_texto_libre():
    f = {"field_path": "estado", "type": "string", "dimension": True}
    assert capabilities._es_texto_libre(f) is False
    assert capabilities._es_texto_libre({"field_path": "comentario", "type": "text"}) is True
    assert capabilities._es_texto_libre({"field_path": "monto", "type": "float"}) is False
