"""Vertical declarativo: oil-gas.

Definición única del vertical (card, filtro, campos, capability spec, dashboard
spec, preguntas del chatbot, vocabulario de industria y datasets). Lo consumen
tanto el backend (capabilities/dashboards/industry/datasets) como el frontend
(inyectado por `GET /` como `window.__VERTICALS__`). Ver `verticals/__init__.py`.
"""

VERTICAL = {
    'slug': 'produccion-pozos',
    'label': 'Producción de pozos',
    'full_label': 'Producción de pozos',
    'group': 'energia',
    'icon': 'droplet',
    'index_base': 'produccion-pozos',
    'description': 'Pozos · producción, presión, flujo',
    'sample': 'ts=2025-07-01T00:00:00Z well="15/9-F-12 H" well_type=OP flow_kind=production status=FLOWING on_stream_hrs=24.0 avg_dhp=230.8 avg_dht=105.6 avg_whp=71.9 avg_wht=77.1 choke_size=47.0 oil_vol=58.77 gas_vol=6536.52 wat_vol=0.28 wi_vol=0.0',
    'filter_code': """filter {
  kv { source => "message" field_split => " " value_split => "=" }
  date { match => ["ts", "ISO8601"] target => "@timestamp" }
  mutate {
    convert => {
      "on_stream_hrs" => "float"
      "avg_dhp" => "float"
      "avg_dht" => "float"
      "avg_whp" => "float"
      "avg_wht" => "float"
      "choke_size" => "float"
      "oil_vol" => "float"
      "gas_vol" => "float"
      "wat_vol" => "float"
      "wi_vol" => "float"
      "downtime" => "integer"
    }
    remove_field => ["message", "ts"]
  }
}""",
    'fields': [
        {
            'raw_name': 'well',
            'field_path': 'well',
            'type': 'keyword',
            'business_label': 'Well',
        },
        {
            'raw_name': 'well_type',
            'field_path': 'well_type',
            'type': 'keyword',
            'business_label': 'Well Type (OP/WI)',
        },
        {
            'raw_name': 'status',
            'field_path': 'status',
            'type': 'keyword',
            'business_label': 'Status',
        },
        {
            'raw_name': 'on_stream_hrs',
            'field_path': 'on_stream_hrs',
            'type': 'float',
            'business_label': 'On-stream Hours',
        },
        {
            'raw_name': 'avg_dhp',
            'field_path': 'avg_dhp',
            'type': 'float',
            'business_label': 'Downhole Pressure',
        },
        {
            'raw_name': 'avg_whp',
            'field_path': 'avg_whp',
            'type': 'float',
            'business_label': 'Wellhead Pressure',
        },
        {
            'raw_name': 'avg_wht',
            'field_path': 'avg_wht',
            'type': 'float',
            'business_label': 'Wellhead Temp',
        },
        {
            'raw_name': 'choke_size',
            'field_path': 'choke_size',
            'type': 'float',
            'business_label': 'Choke Size %',
        },
        {
            'raw_name': 'oil_vol',
            'field_path': 'oil_vol',
            'type': 'float',
            'business_label': 'Oil Volume (Sm3)',
        },
        {
            'raw_name': 'gas_vol',
            'field_path': 'gas_vol',
            'type': 'float',
            'business_label': 'Gas Volume (Sm3)',
        },
        {
            'raw_name': 'wat_vol',
            'field_path': 'wat_vol',
            'type': 'float',
            'business_label': 'Water Volume (Sm3)',
        },
        {
            'raw_name': 'wi_vol',
            'field_path': 'wi_vol',
            'type': 'float',
            'business_label': 'Water Injected (Sm3)',
        },
        {
            'raw_name': 'downtime',
            'field_path': 'downtime',
            'type': 'integer',
            'business_label': 'Downtime Flag',
        }
    ],
    'suggested_questions': [
        '¿Cuántas lecturas de pozos petroleros están marcadas como DOWN o fuera de servicio?',
        '¿Cuál es el volumen total de petróleo producido por cada pozo?',
        '¿Cuál es el pozo con mayor producción de petróleo en todo el periodo?',
        '¿Cuántas lecturas de pozos registraron tiempo de inactividad (downtime) y en qué pozos?',
        '¿Cuál es la presión promedio de fondo de pozo en las lecturas de petróleo y gas?',
        '¿Cuáles son los 5 pozos con mayor volumen de gas producido?',
        '¿Cuántas horas en producción (on-stream) tiene cada pozo de petróleo y gas?',
        '¿Cuál es el volumen total de agua inyectada por cada pozo inyector?',
        '¿Cuántas lecturas de pozos hay por cada estado de operación (UP, DOWN, etc.)?',
        '¿Cuántas lecturas hay por tipo de pozo (productor, inyector, observador) en los datos de petróleo y gas?'
    ],
    'industry_fields': {
        'avg_dhp',
        'avg_dht',
        'avg_whp',
        'avg_wht',
        'choke_size',
        'downtime',
        'gas_vol',
        'oil_vol',
        'on_stream_hrs',
        'wat_vol',
        'well',
        'well_type',
    },
    'dataset_files': ['produccion-pozos.log'],
    'capability': {
        'label': 'Producción de pozos',
        'index_pattern': 'produccion-pozos*',
        'operations': ['15/9-F-12 H', '15/9-F-14 H', '15/9-F-4 AH', '15/9-F-5 AH'],
        'success_code': '',
        'fields': {
            'well': 'well name (see OPERATIONS for the exact names)',
            'well_type': 'OP = oil producer, WI = water injector',
            'status': 'FLOWING, INJECTING or DOWN',
            'on_stream_hrs': 'hours on stream that day (0 = down)',
            'avg_dhp': 'average downhole pressure',
            'avg_whp': 'average wellhead pressure',
            'avg_wht': 'average wellhead temperature',
            'oil_vol': 'oil volume produced in the reading (Sm3)',
            'gas_vol': 'gas volume produced in the reading (Sm3)',
            'wat_vol': 'water volume produced in the reading (Sm3)',
            'wi_vol': 'water injected in the reading (Sm3)',
            'downtime': 'present (=1) ONLY when the well is down; count it to measure downtime',
        },
        'volume_field': 'well',
        'forecast_interval_minutes': 240,
        'forecast_horizon': 8,
        'forecasts': [
            {
                'name': 'produccion-pozos-oil-vol-forecast',
                'feature_name': 'oil_volume',
                'aggregation_query': {
                    'oil_volume': {
                        'sum': {
                            'field': 'oil_vol',
                        },
                    },
                },
                'description': 'Forecast de produccion de oil (Sm3) por intervalo',
            },
            {
                'name': 'produccion-pozos-gas-vol-forecast',
                'feature_name': 'gas_volume',
                'aggregation_query': {
                    'gas_volume': {
                        'sum': {
                            'field': 'gas_vol',
                        },
                    },
                },
                'description': 'Forecast de produccion de gas (Sm3) por intervalo',
            },
            {
                'name': 'produccion-pozos-downtime-forecast',
                'feature_name': 'downtime_readings',
                'aggregation_query': {
                    'downtime_readings': {
                        'value_count': {
                            'field': 'downtime',
                        },
                    },
                },
                'description': 'Forecast de lecturas con pozo caido (downtime)',
            }
        ],
    },
    'dashboard': {
        'title': 'Producción de pozos',
        'index_fields': [
            ('@timestamp', 'date'),
            ('well', 'keyword'),
            ('well_type', 'keyword'),
            ('status', 'keyword'),
            ('on_stream_hrs', 'double'),
            ('avg_dhp', 'double'),
            ('avg_whp', 'double'),
            ('avg_wht', 'double'),
            ('oil_vol', 'double'),
            ('gas_vol', 'double'),
            ('wat_vol', 'double'),
            ('wi_vol', 'double'),
            ('downtime', 'long')
        ],
        'panels': [
            {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## Oil & Gas — telemetría de pozos (Volve)
Producción de oil/gas/water, presiones, downtime y horas on-stream por pozo."""},
            {'type': 'controls', 'title': 'Filtros', 'controls': [
                {'field': 'status', 'label': 'Estado'}, {'field': 'well_type', 'label': 'Tipo de pozo'},
                {'field': 'well', 'label': 'Pozo'}]},
            {'type': 'metric', 'title': 'Total de Lecturas', 'agg': 'count'},
            {'type': 'metric', 'title': 'Petróleo Producido (Sm3)', 'agg': 'sum', 'field': 'oil_vol', 'label': 'Petróleo Producido (Sm3)'},
            {'type': 'metric', 'title': 'Gas Producido (Sm3)', 'agg': 'sum', 'field': 'gas_vol', 'label': 'Gas Producido (Sm3)'},
            {'type': 'metric', 'title': 'Eventos de Parada', 'agg': 'count', 'label': 'Eventos de Parada', 'query': 'status:DOWN'},
            {'type': 'area', 'title': 'Producción de petróleo en el tiempo por pozo', 'metric': 'sum',
             'field': 'oil_vol', 'split': 'well', 'w': 48},
            {'type': 'line', 'title': 'Presión de fondo en el tiempo', 'metric': 'avg', 'field': 'avg_dhp'},
            {'type': 'line', 'title': 'Presión de cabeza en el tiempo', 'metric': 'avg', 'field': 'avg_whp'},
            {'type': 'table', 'title': 'Top Pozos por Producción de Petróleo', 'field': 'well', 'metric': 'sum', 'agg_field': 'oil_vol'},
            {'type': 'bar', 'title': 'Gas por Pozo', 'field': 'well', 'metric': 'sum', 'agg_field': 'gas_vol', 'horizontal': True},
            {'type': 'bar', 'title': 'Agua Inyectada por Pozo', 'field': 'well', 'metric': 'sum',
             'agg_field': 'wi_vol', 'horizontal': True, 'query': 'well_type:WI'},
            {'type': 'bar', 'title': 'Horas en Producción por Pozo', 'field': 'well', 'metric': 'sum',
             'agg_field': 'on_stream_hrs', 'horizontal': True},
            {'type': 'area', 'title': 'Eventos de parada en el tiempo', 'metric': 'count', 'query': 'status:DOWN', 'w': 48},
        ],
    },
}
