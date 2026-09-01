"""Vertical declarativo: fraud.

Detector de fraude basado en IEEE-CIS Fraud Detection (590k transacciones,
3.5% fraude). Datos pre-procesados a JSONL por build_fraud.py.
"""
from __future__ import annotations

_SAMPLE = (
    '{"transaction_id":"2987240","timestamp":"2017-12-01T01:03:13.000Z",'
    '"is_fraud":1,"amount":37.098,"product_cd":"C",'
    '"card":{"number":"13413","brand":"visa","type":"credit"},'
    '"email":{"purchaser":"hotmail.com","recipient":"hotmail.com"},'
    '"counting":{"c1":0.0,"c2":1.0,"c3":0.0,"c4":1.0},'
    '"timedelta":{"d1":0.0,"d4":0.0,"d8":45.04},'
    '"match":{"m4":"M2"},'
    '"device":{"type":"mobile","info":"Redmi Note 4","os":"chrome 54.0 for android"}}'
)

_C_FIELDS = [f"fraud.counting.c{i}" for i in range(1, 15)]
_D_FIELDS = [f"fraud.timedelta.d{i}" for i in range(1, 16)]
_M_FIELDS = [f"fraud.match.m{i}" for i in range(1, 10)]

VERTICAL = {
    'slug': 'fraud-detection',
    'label': 'Fraud Detection',
    'full_label': 'Fraud Detection',
    'group': 'fintech',
    'icon': 'alert-triangle',
    'index_base': 'fraud-detection',
    'description': 'Fraude · 590k tx, 3.5% fraude',
    'dedup_id': '%{[@metadata][generated_id]}',
    'hidden': True,
    'sample': _SAMPLE,
    'filter_code': """filter {
  json {
    source => "message"
    target => "fraud"
  }

  fingerprint {
    source              => ["[fraud][transaction_id]"]
    target              => "[@metadata][generated_id]"
    method              => "SHA256"
    concatenate_sources => true
  }

  date {
    match          => ["[fraud][timestamp]", "ISO8601"]
    target         => "@timestamp"
    remove_field   => ["[fraud][timestamp]"]
    tag_on_failure => ["_dateparsefailure"]
  }

  mutate {
    remove_field => ["message", "@version", "host", "path"]
  }
}""",
    'fields': [
        {'raw_name': 'transaction_id', 'field_path': 'fraud.transaction_id', 'type': 'keyword', 'business_label': 'Transaction ID'},
        {'raw_name': 'is_fraud', 'field_path': 'fraud.is_fraud', 'type': 'integer', 'business_label': 'Is Fraud'},
        {'raw_name': 'amount', 'field_path': 'fraud.amount', 'type': 'float', 'business_label': 'Amount'},
        {'raw_name': 'product_cd', 'field_path': 'fraud.product_cd', 'type': 'keyword', 'business_label': 'Product Code'},
        {'raw_name': 'card_number', 'field_path': 'fraud.card.number', 'type': 'keyword', 'business_label': 'Card Number'},
        {'raw_name': 'card_brand', 'field_path': 'fraud.card.brand', 'type': 'keyword', 'business_label': 'Card Brand'},
        {'raw_name': 'card_type', 'field_path': 'fraud.card.type', 'type': 'keyword', 'business_label': 'Card Type'},
        {'raw_name': 'addr_region1', 'field_path': 'fraud.address.region1', 'type': 'keyword', 'business_label': 'Address Region 1'},
        {'raw_name': 'addr_region2', 'field_path': 'fraud.address.region2', 'type': 'keyword', 'business_label': 'Address Region 2'},
        {'raw_name': 'dist1', 'field_path': 'fraud.distance.dist1', 'type': 'float', 'business_label': 'Distance 1'},
        {'raw_name': 'email_purchaser', 'field_path': 'fraud.email.purchaser', 'type': 'keyword', 'business_label': 'Purchaser Email'},
        {'raw_name': 'email_recipient', 'field_path': 'fraud.email.recipient', 'type': 'keyword', 'business_label': 'Recipient Email'},
        {'raw_name': 'device_type', 'field_path': 'fraud.device.type', 'type': 'keyword', 'business_label': 'Device Type'},
        {'raw_name': 'device_info', 'field_path': 'fraud.device.info', 'type': 'keyword', 'business_label': 'Device Info'},
        {'raw_name': 'device_os', 'field_path': 'fraud.device.os', 'type': 'keyword', 'business_label': 'OS / Browser'},
        {'raw_name': 'device_screen', 'field_path': 'fraud.device.screen', 'type': 'keyword', 'business_label': 'Screen Resolution'},
        {'raw_name': 'match_m4', 'field_path': 'fraud.match.m4', 'type': 'keyword', 'business_label': 'Match M4'},
        {'raw_name': 'counting_c1', 'field_path': 'fraud.counting.c1', 'type': 'float', 'business_label': 'Count C1'},
        {'raw_name': 'timedelta_d1', 'field_path': 'fraud.timedelta.d1', 'type': 'float', 'business_label': 'Timedelta D1'},
        {'raw_name': 'timedelta_d15', 'field_path': 'fraud.timedelta.d15', 'type': 'float', 'business_label': 'Timedelta D15'},
    ],
    'suggested_questions': [
        '¿Cuántas transacciones hay en total?',
        '¿Cuántas transacciones hay por marca de tarjeta?',
        '¿Cuántas transacciones hay por tipo de tarjeta (crédito/débito)?',
        '¿Cuántas transacciones hay por dispositivo (mobile/desktop)?',
        '¿Cuál es el monto total transado?',
        '¿Cuántas transacciones hay por código de producto?',
        '¿Cuáles son los 10 dominios de email más frecuentes?',
        '¿Cuántas transacciones hay por región?',
        '¿Cuántas transacciones son fraude vs legítimas?',
        '¿Cuántas transacciones hay por día?',
    ],
    'industry_fields': {
        'fraud.amount',
        'fraud.card.brand',
        'fraud.card.type',
        'fraud.device.type',
        'fraud.email.purchaser',
        'fraud.is_fraud',
        'fraud.product_cd',
        'fraud.transaction_id',
    },
    'dataset_files': ['fraud-detection.log'],
    'capability': {
        'label': 'Fraud Detection',
        'index_pattern': 'fraud-detection*',
        'operations': ['W', 'C', 'R', 'H', 'S'],
        'success_code': '',
        'fields': {
            'fraud.is_fraud': 'fraud flag (0=legitimate, 1=fraud)',
            'fraud.amount': 'transaction amount',
            'fraud.product_cd': 'product code (W, C, R, H, S)',
            'fraud.card.brand': 'card brand (visa, mastercard, discover, amex)',
            'fraud.card.type': 'card type (credit, debit)',
            'fraud.device.type': 'device type (mobile, desktop)',
            'fraud.email.purchaser': 'purchaser email domain',
            'fraud.address.region1': 'address region 1',
        },
        'volume_field': 'fraud.transaction_id',
        'forecast_interval_minutes': 240,
        'forecast_horizon': 8,
        'forecasts': [
            {
                'name': 'fraud-volume-forecast',
                'feature_name': 'txn_volume',
                'aggregation_query': {
                    'txn_volume': {'value_count': {'field': 'fraud.transaction_id'}},
                },
                'description': 'Forecast de volumen total de transacciones',
            },
            {
                'name': 'fraud-count-forecast',
                'feature_name': 'fraud_count',
                'aggregation_query': {
                    'fraud_count': {'sum': {'field': 'fraud.is_fraud'}},
                },
                'description': 'Forecast de cantidad de transacciones fraudulentas',
            },
            {
                'name': 'fraud-amount-forecast',
                'feature_name': 'fraud_amount',
                'aggregation_query': {
                    'fraud_amount': {'sum': {'field': 'fraud.amount'}},
                },
                'description': 'Forecast de monto total transaccionado',
            },
        ],
    },
    'dashboard': {
        'title': 'Detección de Fraude Fintech',
        'index_fields': (
            [('@timestamp', 'date'),
             ('fraud.transaction_id', 'keyword'),
             ('fraud.is_fraud', 'long'),
             ('fraud.amount', 'double'),
             ('fraud.product_cd', 'keyword'),
             ('fraud.card.number', 'keyword'),
             ('fraud.card.brand', 'keyword'),
             ('fraud.card.type', 'keyword'),
             ('fraud.address.region1', 'keyword'),
             ('fraud.address.region2', 'keyword'),
             ('fraud.distance.dist1', 'double'),
             ('fraud.distance.dist2', 'double'),
             ('fraud.email.purchaser', 'keyword'),
             ('fraud.email.recipient', 'keyword')]
            + [(c, 'double') for c in _C_FIELDS]
            + [(d, 'double') for d in _D_FIELDS]
            + [(m, 'keyword') for m in _M_FIELDS]
            + [('fraud.device.type', 'keyword'),
               ('fraud.device.info', 'keyword'),
               ('fraud.device.os', 'keyword'),
               ('fraud.device.screen', 'keyword')]
        ),
        'panels': [
            {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## Detección de Fraude Fintech — IEEE-CIS Train Set
590k transacciones, 3.5% fraude. Análisis por producto, tarjeta, dispositivo, monto y geografía."""},
            {'type': 'controls', 'title': 'Filtros', 'controls': [
                {'field': 'fraud.is_fraud', 'label': 'Fraude'},
                {'field': 'fraud.product_cd', 'label': 'Producto'},
                {'field': 'fraud.card.brand', 'label': 'Marca de tarjeta'},
                {'field': 'fraud.card.type', 'label': 'Tipo de tarjeta'},
                {'field': 'fraud.device.type', 'label': 'Dispositivo'}]},
            {'type': 'metric', 'title': 'Total Transacciones', 'agg': 'count'},
            {'type': 'metric', 'title': 'Fraudulentas', 'agg': 'count', 'label': 'Fraudulentas', 'query': 'fraud.is_fraud:1'},
            {'type': 'metric', 'title': 'Monto en Fraude', 'agg': 'sum', 'field': 'fraud.amount',
             'label': 'Monto en Fraude', 'query': 'fraud.is_fraud:1'},
            {'type': 'metric', 'title': 'Monto Promedio', 'agg': 'avg', 'field': 'fraud.amount', 'label': 'Monto Promedio'},
            {'type': 'area', 'title': 'Transacciones en el tiempo por estado de fraude', 'metric': 'count',
             'split': 'fraud.is_fraud', 'w': 48},
            {'type': 'bar', 'title': 'Fraude por Producto', 'field': 'fraud.product_cd', 'horizontal': True, 'query': 'fraud.is_fraud:1'},
            {'type': 'bar', 'title': 'Fraude por Marca de Tarjeta', 'field': 'fraud.card.brand', 'horizontal': True, 'query': 'fraud.is_fraud:1'},
            {'type': 'bar', 'title': 'Fraude por Región', 'field': 'fraud.address.region1', 'horizontal': True, 'query': 'fraud.is_fraud:1'},
            {'type': 'bar', 'title': 'Fraude por OS / Browser', 'field': 'fraud.device.os', 'horizontal': True, 'query': 'fraud.is_fraud:1'},
            {'type': 'table', 'title': 'Top Emails en Fraude', 'field': 'fraud.email.purchaser', 'query': 'fraud.is_fraud:1'},
            {'type': 'table', 'title': 'Top Dispositivos en Fraude', 'field': 'fraud.device.info', 'query': 'fraud.is_fraud:1'},
        ],
    },
}
