"""Vertical declarativo: media-streaming.

Definición única del vertical (card, filtro, campos, capability spec, dashboard
spec, preguntas del chatbot, vocabulario de industria y datasets). Lo consumen
tanto el backend (capabilities/dashboards/industry/datasets) como el frontend
(inyectado por `GET /` como `window.__VERTICALS__`). Ver `verticals/__init__.py`.
"""

VERTICAL = {
    'slug': 'streaming-ott',
    'label': 'Streaming OTT',
    'full_label': 'Streaming OTT',
    'group': 'media',
    'icon': 'play',
    'index_base': 'streaming-ott',
    'description': 'Streaming · sesiones, rebuffering, CDN',
    'sample': '{"ts":"2025-07-01T00:03:21Z","event":"PLAY_START","session_id":"s-9e0cf02be2","user_id":"u100418","content_id":"d001","title":"Memoria del Fuego","content_type":"DOCUMENTARY","genre":"Documental","device":"SMART_TV","cdn_pop":"SCL1","country":"BR","bitrate_kbps":3600}',
    'filter_code': """filter {
  json { source => "message" }
  date { match => ["ts", "ISO8601"] target => "@timestamp" }
  mutate { remove_field => ["message", "ts"] }
}""",
    'fields': [
        {
            'raw_name': 'event',
            'field_path': 'event',
            'type': 'keyword',
            'business_label': 'Evento',
        },
        {
            'raw_name': 'title',
            'field_path': 'title',
            'type': 'keyword',
            'business_label': 'Título',
        },
        {
            'raw_name': 'content_type',
            'field_path': 'content_type',
            'type': 'keyword',
            'business_label': 'Tipo de contenido',
        },
        {
            'raw_name': 'genre',
            'field_path': 'genre',
            'type': 'keyword',
            'business_label': 'Género',
        },
        {
            'raw_name': 'device',
            'field_path': 'device',
            'type': 'keyword',
            'business_label': 'Dispositivo',
        },
        {
            'raw_name': 'cdn_pop',
            'field_path': 'cdn_pop',
            'type': 'keyword',
            'business_label': 'CDN PoP',
        },
        {
            'raw_name': 'country',
            'field_path': 'country',
            'type': 'keyword',
            'business_label': 'País',
        },
        {
            'raw_name': 'user_id',
            'field_path': 'user_id',
            'type': 'keyword',
            'business_label': 'Usuario',
        },
        {
            'raw_name': 'session_id',
            'field_path': 'session_id',
            'type': 'keyword',
            'business_label': 'Sesión',
        },
        {
            'raw_name': 'bitrate_kbps',
            'field_path': 'bitrate_kbps',
            'type': 'integer',
            'business_label': 'Bitrate (kbps)',
        },
        {
            'raw_name': 'buffering_ms',
            'field_path': 'buffering_ms',
            'type': 'integer',
            'business_label': 'Buffering (ms)',
        },
        {
            'raw_name': 'watch_seconds',
            'field_path': 'watch_seconds',
            'type': 'integer',
            'business_label': 'Segundos vistos',
        },
        {
            'raw_name': 'error_code',
            'field_path': 'error_code',
            'type': 'keyword',
            'business_label': 'Código de error',
        }
    ],
    'suggested_questions': [
        '¿Cuántos eventos de reproducción hay en total?',
        '¿Cuáles son los 10 títulos más reproducidos?',
        '¿Cuántos eventos hay por género?',
        '¿Cuántos eventos hay por dispositivo?',
        '¿Cuántos eventos hay por país?',
        '¿Cuál es el tiempo total visto en segundos?',
        '¿Cuántos eventos hay por tipo de contenido?',
        '¿Cuántos errores de reproducción hay por código?',
        '¿Cuántos eventos hay por CDN pop?',
        '¿Cuántos eventos hay por día?',
    ],
    'industry_fields': {
        'bitrate_kbps',
        'buffering_ms',
        'cdn_pop',
        'content_type',
        'device',
        'error_code',
        'event',
        'session_id',
        'title',
        'watch_seconds',
    },
    'dataset_files': ['streaming-ott.log'],
    'capability': {
        'label': 'Streaming OTT',
        'index_pattern': 'streaming-ott*',
        'operations': ['PLAY_START', 'PLAY_END', 'REBUFFER', 'BITRATE_SWITCH', 'PLAYBACK_ERROR'],
        'success_code': '',
        'fields': {
            'event': 'playback event (see OPERATIONS for the exact names)',
            'title': 'content title',
            'content_type': 'MOVIE, SERIES, DOCUMENTARY or LIVE',
            'genre': 'content genre',
            'device': 'SMART_TV, MOBILE_ANDROID, MOBILE_IOS, WEB or TV_STICK',
            'cdn_pop': 'CDN edge pop (EZE1, GRU2, SCL1, BOG1, MIA1)',
            'country': 'viewer country code (AR, BR, CL, CO, UY, MX)',
            'user_id': 'viewer id (for unique viewers)',
            'session_id': 'playback session id',
            'bitrate_kbps': 'current bitrate in kbps',
            'buffering_ms': 'rebuffer duration in ms; present ONLY on REBUFFER events',
            'watch_seconds': 'seconds watched; present ONLY on PLAY_END — sum it for watch time',
            'error_code': 'E_DRM, E_NETWORK, E_DECODE or E_CDN_TIMEOUT; present ONLY on PLAYBACK_ERROR',
        },
        'volume_field': 'event',
        'forecast_interval_minutes': 240,
        'forecast_horizon': 8,
        'forecasts': [
            {
                'name': 'media-streaming-viewers-forecast',
                'feature_name': 'unique_viewers',
                'aggregation_query': {
                    'unique_viewers': {
                        'cardinality': {
                            'field': 'user_id',
                        },
                    },
                },
                'description': 'Forecast de espectadores unicos por intervalo',
                'history': 2000,
            },
            {
                'name': 'media-streaming-watchtime-forecast',
                'feature_name': 'watch_seconds_sum',
                'aggregation_query': {
                    'watch_seconds_sum': {
                        'sum': {
                            'field': 'watch_seconds',
                        },
                    },
                },
                'description': 'Forecast de segundos reproducidos por intervalo',
            },
            {
                'name': 'media-streaming-errors-forecast',
                'feature_name': 'playback_errors',
                'aggregation_query': {
                    'playback_errors': {
                        'value_count': {
                            'field': 'error_code',
                        },
                    },
                },
                'description': 'Forecast de errores de reproduccion por intervalo',
            }
        ],
    },
    'dashboard': {
        'title': 'Streaming OTT',
        'index_fields': [
            ('@timestamp', 'date'),
            ('event', 'keyword'),
            ('title', 'keyword'),
            ('content_type', 'keyword'),
            ('genre', 'keyword'),
            ('device', 'keyword'),
            ('cdn_pop', 'keyword'),
            ('country', 'keyword'),
            ('user_id', 'keyword'),
            ('session_id', 'keyword'),
            ('bitrate_kbps', 'long'),
            ('buffering_ms', 'long'),
            ('watch_seconds', 'long'),
            ('error_code', 'keyword')
        ],
        'panels': [
            {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## Streaming OTT — audiencia y calidad de reproducción
Espectadores únicos, horas vistas, rebuffering, errores por CDN pop y top contenidos."""},
            {'type': 'controls', 'title': 'Filtros', 'controls': [
                {'field': 'event', 'label': 'Evento'}, {'field': 'content_type', 'label': 'Tipo de contenido'},
                {'field': 'device', 'label': 'Dispositivo'}, {'field': 'country', 'label': 'País'},
                {'field': 'error_code', 'label': 'Código de error'}]},
            {'type': 'metric', 'title': 'Eventos de Reproducción', 'agg': 'count'},
            {'type': 'metric', 'title': 'Espectadores Únicos', 'agg': 'cardinality', 'field': 'user_id', 'label': 'Espectadores Únicos'},
            {'type': 'metric', 'title': 'Segundos Vistos', 'agg': 'sum', 'field': 'watch_seconds', 'label': 'Segundos Vistos'},
            {'type': 'metric', 'title': 'Errores de Reproducción', 'agg': 'count', 'label': 'Errores de Reproducción', 'query': 'event:PLAYBACK_ERROR'},
            {'type': 'area', 'title': 'Eventos en el tiempo por tipo', 'metric': 'count', 'split': 'event', 'w': 48},
            {'type': 'line', 'title': 'Segundos vistos en el tiempo', 'metric': 'sum', 'field': 'watch_seconds'},
            {'type': 'area', 'title': 'Errores en el tiempo', 'metric': 'count', 'query': 'event:PLAYBACK_ERROR'},
            {'type': 'bar', 'title': 'Top Títulos por Segundos Vistos', 'field': 'title', 'metric': 'sum', 'agg_field': 'watch_seconds', 'horizontal': True},
            {'type': 'bar', 'title': 'Top Géneros', 'field': 'genre', 'horizontal': True},
            {'type': 'bar', 'title': 'Rebuffering por PoP (ms)', 'field': 'cdn_pop', 'metric': 'sum', 'agg_field': 'buffering_ms', 'horizontal': True},
            {'type': 'table', 'title': 'Errores por CDN PoP', 'field': 'cdn_pop', 'query': 'event:PLAYBACK_ERROR'},
        ],
    },
}
