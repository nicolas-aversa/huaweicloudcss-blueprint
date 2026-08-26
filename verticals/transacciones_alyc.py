"""Vertical declarativo: alyc.

Definición única del vertical (card, filtro, campos, capability spec, dashboard
spec, preguntas del chatbot, vocabulario de industria y datasets). Lo consumen
tanto el backend (capabilities/dashboards/industry/datasets) como el frontend
(inyectado por `GET /` como `window.__VERTICALS__`). Ver `verticals/__init__.py`.
"""

VERTICAL = {
    'slug': 'transacciones-alyc',
    'label': 'Transacciones ALyC',
    'full_label': 'Transacciones ALyC',
    'group': 'fintech',
    'icon': 'activity',
    'index_base': 'transacciones-alyc',
    'description': 'Capitales · órdenes, liquidación, comitentes',
    'sample': '20250701-11:00:26.461 BYMA evt=PARTIAL_FILL order_id=O-20250701-000060 comitente=C10020 especie=BMA side=SELL qty=100 price=11306.6 notional=1130660.0 currency=ARS plazo=T+1 channel=API status=PARTIAL fee=1695.99',
    'filter_code': """filter {
  grok { match => { "message" => "^%{NOTSPACE:ts} %{WORD:market} %{GREEDYDATA:kv_payload}" } }
  kv { source => "kv_payload" field_split => " " value_split => "=" }
  date { match => ["ts", "yyyyMMdd-HH:mm:ss.SSS"] target => "@timestamp" }
  mutate {
    convert => { "qty" => "integer" "price" => "float" "notional" => "float" "fee" => "float" }
    remove_field => ["message", "kv_payload", "ts"]
  }
}""",
    'fields': [
        {
            'raw_name': 'evt',
            'field_path': 'evt',
            'type': 'keyword',
            'business_label': 'Evento',
        },
        {
            'raw_name': 'market',
            'field_path': 'market',
            'type': 'keyword',
            'business_label': 'Mercado',
        },
        {
            'raw_name': 'order_id',
            'field_path': 'order_id',
            'type': 'keyword',
            'business_label': 'Orden',
        },
        {
            'raw_name': 'comitente',
            'field_path': 'comitente',
            'type': 'keyword',
            'business_label': 'Comitente',
        },
        {
            'raw_name': 'especie',
            'field_path': 'especie',
            'type': 'keyword',
            'business_label': 'Especie',
        },
        {
            'raw_name': 'side',
            'field_path': 'side',
            'type': 'keyword',
            'business_label': 'Compra/Venta',
        },
        {
            'raw_name': 'qty',
            'field_path': 'qty',
            'type': 'integer',
            'business_label': 'Cantidad',
        },
        {
            'raw_name': 'price',
            'field_path': 'price',
            'type': 'float',
            'business_label': 'Precio',
        },
        {
            'raw_name': 'notional',
            'field_path': 'notional',
            'type': 'float',
            'business_label': 'Monto operado',
        },
        {
            'raw_name': 'currency',
            'field_path': 'currency',
            'type': 'keyword',
            'business_label': 'Moneda',
        },
        {
            'raw_name': 'plazo',
            'field_path': 'plazo',
            'type': 'keyword',
            'business_label': 'Plazo (CI/T+1)',
        },
        {
            'raw_name': 'channel',
            'field_path': 'channel',
            'type': 'keyword',
            'business_label': 'Canal',
        },
        {
            'raw_name': 'status',
            'field_path': 'status',
            'type': 'keyword',
            'business_label': 'Estado',
        },
        {
            'raw_name': 'fee',
            'field_path': 'fee',
            'type': 'float',
            'business_label': 'Arancel',
        },
        {
            'raw_name': 'reject_reason',
            'field_path': 'reject_reason',
            'type': 'keyword',
            'business_label': 'Motivo de rechazo',
        },
        {
            'raw_name': 'fail_reason',
            'field_path': 'fail_reason',
            'type': 'keyword',
            'business_label': 'Motivo de fallo de liquidación',
        }
    ],
    'suggested_questions': [
        '¿Cuál es el volumen total operado (notional) en el mercado de capitales?',
        '¿Qué especie concentró el mayor volumen operado?',
        '¿Cuántas órdenes fueron rechazadas y por qué motivo?',
        '¿Cuántos comitentes únicos operaron en el período?',
        '¿Cuántos fallos de liquidación hubo y cuáles fueron sus motivos?',
        '¿Cuál es la distribución de órdenes por canal (WEB, API, MOBILE, MESA)?',
        '¿Cuánto se operó en plazo CI versus T+1?',
        '¿Cuáles son los 5 comitentes con mayor volumen operado?',
        '¿Cuántas operaciones hubo por mercado (BYMA vs MAE)?',
        '¿Cuál es el total de aranceles (fees) cobrados por especie?'
    ],
    'industry_fields': {
        'comitente',
        'especie',
        'evt',
        'fee',
        'market',
        'notional',
        'plazo',
        'price',
        'qty',
        'reject_reason',
        'side',
    },
    'dataset_files': ['transacciones-alyc.log'],
    'capability': {
        'label': 'Transacciones ALyC',
        'index_pattern': 'transacciones-alyc*',
        'operations': [
            'ORDER_NEW',
            'ORDER_FILL',
            'PARTIAL_FILL',
            'ORDER_CANCEL',
            'ORDER_REJECT',
            'SETTLEMENT_OK',
            'SETTLEMENT_FAIL'
        ],
        'success_code': '',
        'fields': {
            'evt': 'event type (see OPERATIONS for the exact names)',
            'market': 'BYMA or MAE',
            'especie': 'ticker/security (AL30, GD30, GGAL, YPFD, ...)',
            'side': 'BUY or SELL',
            'plazo': 'settlement term: CI (immediate) or T+1',
            'channel': 'WEB, API, MOBILE or MESA',
            'status': 'NEW, FILLED, PARTIAL, CANCELLED, REJECTED, SETTLED or FAILED',
            'comitente': 'client account id (for unique/active clients)',
            'qty': 'quantity (nominals/shares)',
            'price': 'execution price; do not sum it',
            'notional': 'traded amount (price x qty); sum it for traded volume',
            'fee': 'commission charged; sum it for fee revenue',
            'currency': 'ARS or USD',
            'reject_reason': 'SALDO_INSUFICIENTE, LIMITE_EXCEDIDO or ESPECIE_SUSPENDIDA; present ONLY on rejected orders',
            'fail_reason': 'FALTA_ESPECIES, FALTA_FONDOS or CONTRAPARTE; present ONLY on failed settlements',
        },
        'volume_field': 'evt',
        'forecast_interval_minutes': 240,
        'forecast_horizon': 8,
        'forecasts': [
            {
                'name': 'alyc-notional-forecast',
                'feature_name': 'traded_notional',
                'aggregation_query': {
                    'traded_notional': {
                        'sum': {
                            'field': 'notional',
                        },
                    },
                },
                'description': 'Forecast de volumen operado (notional) por intervalo',
            },
            {
                'name': 'alyc-orders-forecast',
                'feature_name': 'orders_volume',
                'aggregation_query': {
                    'orders_volume': {
                        'value_count': {
                            'field': 'evt',
                        },
                    },
                },
                'description': 'Forecast de eventos de ordenes por intervalo',
            },
            {
                'name': 'alyc-comitentes-forecast',
                'feature_name': 'active_comitentes',
                'aggregation_query': {
                    'active_comitentes': {
                        'cardinality': {
                            'field': 'comitente',
                        },
                    },
                },
                'description': 'Forecast de comitentes activos unicos por intervalo',
                'history': 2000,
            }
        ],
    },
    'dashboard': {
        'title': 'Transacciones ALyC',
        'index_fields': [
            ('@timestamp', 'date'),
            ('evt', 'keyword'),
            ('market', 'keyword'),
            ('order_id', 'keyword'),
            ('comitente', 'keyword'),
            ('especie', 'keyword'),
            ('side', 'keyword'),
            ('qty', 'long'),
            ('price', 'double'),
            ('notional', 'double'),
            ('currency', 'keyword'),
            ('plazo', 'keyword'),
            ('channel', 'keyword'),
            ('status', 'keyword'),
            ('fee', 'double'),
            ('reject_reason', 'keyword'),
            ('fail_reason', 'keyword')
        ],
        'panels': [
            {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## Transacciones ALyC — órdenes, ejecuciones y liquidación
Volumen operado por especie, rechazos, fallos de liquidación, comitentes activos y aranceles."""},
            {'type': 'controls', 'title': 'Filtros', 'controls': [
                {'field': 'side', 'label': 'Compra/Venta'}, {'field': 'plazo', 'label': 'Plazo'},
                {'field': 'channel', 'label': 'Canal'}, {'field': 'market', 'label': 'Mercado'},
                {'field': 'evt', 'label': 'Tipo de evento'}]},
            {'type': 'metric', 'title': 'Eventos de Órdenes', 'agg': 'count'},
            {'type': 'metric', 'title': 'Volumen Operado', 'agg': 'sum', 'field': 'notional', 'label': 'Volumen Operado'},
            {'type': 'metric', 'title': 'Comitentes Activos', 'agg': 'cardinality', 'field': 'comitente', 'label': 'Comitentes Activos'},
            {'type': 'metric', 'title': 'Rechazos + Fallos', 'agg': 'count', 'label': 'Rechazos + Fallos',
             'query': 'evt:(ORDER_REJECT or SETTLEMENT_FAIL)'},
            {'type': 'area', 'title': 'Eventos en el tiempo por tipo', 'metric': 'count', 'split': 'evt', 'w': 48},
            {'type': 'line', 'title': 'Volumen operado en el tiempo', 'metric': 'sum', 'field': 'notional'},
            {'type': 'line', 'title': 'Aranceles en el tiempo', 'metric': 'sum', 'field': 'fee'},
            {'type': 'bar', 'title': 'Top Especies por Volumen', 'field': 'especie', 'metric': 'sum',
             'agg_field': 'notional', 'horizontal': True},
            {'type': 'table', 'title': 'Top Comitentes por Volumen', 'field': 'comitente', 'metric': 'sum', 'agg_field': 'notional'},
            {'type': 'bar', 'title': 'Motivos de Rechazo', 'field': 'reject_reason', 'horizontal': True},
            {'type': 'bar', 'title': 'Fallos de Liquidación por Motivo', 'field': 'fail_reason', 'horizontal': True},
            {'type': 'area', 'title': 'Rechazos en el tiempo', 'metric': 'count', 'query': 'evt:ORDER_REJECT'},
        ],
    },
}
