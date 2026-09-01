"""Vertical declarativo: cts.

Definición única del vertical (card, filtro, campos, capability spec, dashboard
spec, preguntas del chatbot, vocabulario de industria y datasets). Lo consumen
tanto el backend (capabilities/dashboards/industry/datasets) como el frontend
(inyectado por `GET /` como `window.__VERTICALS__`). Ver `verticals/__init__.py`.
"""

VERTICAL = {
    'slug': 'cts',
    'sample': '{"api_version":"v1.0","code":200,"domain_id":"dc6be62ba1bc4d56a87facec4be0e416","event_type":"system","project_id":"ee826ede389344098dde6ed034882580","read_only":false,"record_time":1777898008781,"resource_id":"ee826ede389344098dde6ed034882580","resource_name":"tenant","resource_type":"all","service_type":"DWS","source_ip":"149.104.99.3","time":1777898008781,"trace_id":"741d843d-47b5-11f1-acff-c707b4859a99","trace_name":"getClusterSnapshotsStatistics","trace_rating":"normal","trace_type":"ConsoleAction","tracker_name":"system","user":"{\\"name\\":\\"hwstaff_intl_N50055398\\",\\"account_id\\":\\"dc6be62ba1bc4d56a87facec4be0e416\\",\\"domain\\":{\\"name\\":\\"hwstaff_intl_N50055398\\"},\\"type\\":\\"User\\",\\"user_name\\":\\"hwstaff_intl_N50055398\\"}","user_agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/147.0.0.0"}',
    'filter_code': """filter {
  json { source => "message" target => "trace" }
  if [trace][0] {
    split { field => "trace" }
  }
  ruby { code => "t = event.remove('trace'); if t.is_a?(Hash); t.each { |k, v| event.set(k, v) }; end" }
  json { source => "user" target => "user" }
  date { match => ["record_time", "UNIX_MS"] target => "@timestamp" }
  mutate { remove_field => ["message"] }
}""",
    'fields': [
        {
            'raw_name': 'trace_name',
            'field_path': 'trace_name',
            'type': 'keyword',
            'business_label': 'Action',
        },
        {
            'raw_name': 'trace_type',
            'field_path': 'trace_type',
            'type': 'keyword',
            'business_label': 'Trace Type',
        },
        {
            'raw_name': 'trace_rating',
            'field_path': 'trace_rating',
            'type': 'keyword',
            'business_label': 'Rating',
        },
        {
            'raw_name': 'service_type',
            'field_path': 'service_type',
            'type': 'keyword',
            'business_label': 'Service',
        },
        {
            'raw_name': 'code',
            'field_path': 'code',
            'type': 'integer',
            'business_label': 'Code',
        },
        {
            'raw_name': 'source_ip',
            'field_path': 'source_ip',
            'type': 'ip',
            'business_label': 'Source IP',
        },
        {
            'raw_name': 'resource_type',
            'field_path': 'resource_type',
            'type': 'keyword',
            'business_label': 'Resource Type',
        },
        {
            'raw_name': 'resource_name',
            'field_path': 'resource_name',
            'type': 'keyword',
            'business_label': 'Resource',
        },
        {
            'raw_name': 'event_type',
            'field_path': 'event_type',
            'type': 'keyword',
            'business_label': 'Event Type',
        },
        {
            'raw_name': 'user_name',
            'field_path': 'user.user_name',
            'type': 'keyword',
            'business_label': 'User',
        },
        {
            'raw_name': 'domain',
            'field_path': 'user.domain.name',
            'type': 'keyword',
            'business_label': 'Domain',
        }
    ],
    'dashboard': {
        'title': 'Cloud Trace Service',
        'index_fields': [
            ('@timestamp', 'date'),
            ('trace_name', 'keyword'),
            ('trace_type', 'keyword'),
            ('trace_rating', 'keyword'),
            ('service_type', 'keyword'),
            ('event_type', 'keyword'),
            ('code', 'long'),
            ('source_ip', 'ip'),
            ('resource_type', 'keyword'),
            ('resource_name', 'keyword'),
            ('user.user_name', 'keyword'),
            ('user.domain.name', 'keyword')
        ],
        'panels': [
            {
                'type': 'markdown',
                'title': 'Header',
                'grid': [0, 0, 48, 4],
                'md': """## Cloud Trace Service (CTS)
Audit trail: acciones, ratings, servicios, usuarios y recursos.""",
            },
            {
                'type': 'metric',
                'title': 'Total de Trazas',
                'agg': 'count',
                'grid': [0, 4, 12, 8],
            },
            {
                'type': 'metric',
                'title': 'Usuarios Únicos',
                'agg': 'cardinality',
                'field': 'user.user_name',
                'label': 'Usuarios Únicos',
                'grid': [12, 4, 12, 8],
            },
            {
                'type': 'metric',
                'title': 'IPs de Origen Únicas',
                'agg': 'cardinality',
                'field': 'source_ip',
                'label': 'IPs de Origen Únicas',
                'grid': [24, 4, 12, 8],
            },
            {
                'type': 'metric',
                'title': 'Servicios Distintos',
                'agg': 'cardinality',
                'field': 'service_type',
                'label': 'Servicios Distintos',
                'grid': [36, 4, 12, 8],
            },
            {
                'type': 'area',
                'title': 'Trazas en el tiempo por servicio',
                'metric': 'count',
                'split': 'service_type',
                'grid': [0, 12, 48, 12],
            },
            {
                'type': 'pie',
                'title': 'Calificaciones de Trazas',
                'field': 'trace_rating',
                'grid': [0, 24, 12, 15],
            },
            {
                'type': 'bar',
                'title': 'Top Acciones',
                'field': 'trace_name',
                'horizontal': True,
                'grid': [12, 24, 18, 15],
            },
            {
                'type': 'pie',
                'title': 'Tipos de Recurso',
                'field': 'resource_type',
                'grid': [30, 24, 18, 15],
            },
            {
                'type': 'table',
                'title': 'Top Usuarios',
                'field': 'user.user_name',
                'grid': [0, 39, 24, 15],
            },
            {
                'type': 'table',
                'title': 'Top IPs de Origen',
                'field': 'source_ip',
                'grid': [24, 39, 24, 15],
            },
            {
                'type': 'bar',
                'title': 'Top Servicios',
                'field': 'service_type',
                'horizontal': True,
                'grid': [0, 54, 48, 12],
            }
        ],
    },
    'hidden': True,
}
