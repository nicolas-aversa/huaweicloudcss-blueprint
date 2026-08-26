"""Vertical declarativo: fortianalyzer.

Definición única del vertical (card, filtro, campos, capability spec, dashboard
spec, preguntas del chatbot, vocabulario de industria y datasets). Lo consumen
tanto el backend (capabilities/dashboards/industry/datasets) como el frontend
(inyectado por `GET /` como `window.__VERTICALS__`). Ver `verticals/__init__.py`.
"""

VERTICAL = {
    'slug': 'fortianalyzer',
    'label': 'FortiAnalyzer',
    'full_label': 'FortiAnalyzer',
    'group': 'seguridad',
    'icon': 'key',
    'index_base': 'fortianalyzer',
    'description': 'Firewall · tráfico, UTM, apps',
    'sample': 'date=2026-04-13 time=21:08:17 logid="0000000013" type="traffic" subtype="forward" level="notice" vd="vdom1" srcip="10.1.100.121" srcport=34331 srcintf="port12" dstip="132.23.184.20" dstport=80 sessionid=575948 proto=6 action="server-rst" policyid=1 service="HTTP" dstcountry="Canada" srccountry="Reserved" app="HTTP.BROWSER_Firefox" appcat="Web.Client" apprisk="elevated" duration=179 sentbyte=2101 rcvdbyte=19445 utmaction="allow"',
    'filter_code': """filter {
  kv { source => "message" field_split => " " value_split => "=" trim_value => '"' }
  mutate { add_field => { "_ts" => "%{date} %{time}" } }
  date { match => ["_ts", "yyyy-MM-dd HH:mm:ss"] target => "@timestamp" }
  mutate {
    convert => {
      "srcport" => "integer"
      "dstport" => "integer"
      "proto" => "integer"
      "sentbyte" => "integer"
      "rcvdbyte" => "integer"
      "sentpkt" => "integer"
      "rcvdpkt" => "integer"
      "duration" => "integer"
      "policyid" => "integer"
    }
    remove_field => ["message", "_ts", "date", "time", "eventtime", "tz", "vd", "logid"]
  }
}""",
    'fields': [
        {
            'raw_name': 'type',
            'field_path': 'type',
            'type': 'keyword',
            'business_label': 'Tipo (traffic/utm/event)',
        },
        {
            'raw_name': 'subtype',
            'field_path': 'subtype',
            'type': 'keyword',
            'business_label': 'Subtipo',
        },
        {
            'raw_name': 'level',
            'field_path': 'level',
            'type': 'keyword',
            'business_label': 'Severidad de log',
        },
        {
            'raw_name': 'action',
            'field_path': 'action',
            'type': 'keyword',
            'business_label': 'Acción',
        },
        {
            'raw_name': 'utmaction',
            'field_path': 'utmaction',
            'type': 'keyword',
            'business_label': 'Acción UTM',
        },
        {
            'raw_name': 'srcip',
            'field_path': 'srcip',
            'type': 'ip',
            'business_label': 'IP de origen',
        },
        {
            'raw_name': 'srcport',
            'field_path': 'srcport',
            'type': 'integer',
            'business_label': 'Puerto origen',
        },
        {
            'raw_name': 'dstip',
            'field_path': 'dstip',
            'type': 'ip',
            'business_label': 'IP de destino',
        },
        {
            'raw_name': 'dstport',
            'field_path': 'dstport',
            'type': 'integer',
            'business_label': 'Puerto destino',
        },
        {
            'raw_name': 'service',
            'field_path': 'service',
            'type': 'keyword',
            'business_label': 'Servicio',
        },
        {
            'raw_name': 'app',
            'field_path': 'app',
            'type': 'keyword',
            'business_label': 'Aplicación',
        },
        {
            'raw_name': 'appcat',
            'field_path': 'appcat',
            'type': 'keyword',
            'business_label': 'Categoría de app',
        },
        {
            'raw_name': 'attack',
            'field_path': 'attack',
            'type': 'keyword',
            'business_label': 'Firma IPS',
        },
        {
            'raw_name': 'severity',
            'field_path': 'severity',
            'type': 'keyword',
            'business_label': 'Severidad IPS',
        },
        {
            'raw_name': 'srccountry',
            'field_path': 'srccountry',
            'type': 'keyword',
            'business_label': 'País origen',
        },
        {
            'raw_name': 'dstcountry',
            'field_path': 'dstcountry',
            'type': 'keyword',
            'business_label': 'País destino',
        },
        {
            'raw_name': 'sentbyte',
            'field_path': 'sentbyte',
            'type': 'integer',
            'business_label': 'Bytes enviados',
        },
        {
            'raw_name': 'rcvdbyte',
            'field_path': 'rcvdbyte',
            'type': 'integer',
            'business_label': 'Bytes recibidos',
        },
        {
            'raw_name': 'policyid',
            'field_path': 'policyid',
            'type': 'integer',
            'business_label': 'Política',
        }
    ],
    'suggested_questions': [
        '¿Cuántas sesiones de firewall hay por cada acción (accept, deny, close)?',
        '¿Cuáles son las 10 aplicaciones con más tráfico en el firewall?',
        '¿Cuántas amenazas IPS se detectaron y cuáles son las firmas más frecuentes?',
        '¿Cuáles son las 10 IPs de origen con más sesiones bloqueadas?',
        '¿Cuántos bytes se enviaron en total por categoría de aplicación?',
        '¿Cuáles son los países de origen con más tráfico hacia el firewall?',
        '¿Cuántos eventos hay por cada subtipo de log (forward, ips, virus, webfilter)?',
        '¿Qué puertos de destino registran más intentos bloqueados?',
        '¿Cuántas IPs de origen únicas atravesaron el firewall?',
        '¿Cuál es la distribución de severidad de los eventos UTM?'
    ],
    'industry_fields': {
        'app',
        'appcat',
        'attack',
        'dstip',
        'dstport',
        'policyid',
        'rcvdbyte',
        'sentbyte',
        'srccountry',
        'srcip',
        'srcport',
        'subtype',
    },
    'dataset_files': ['fortianalyzer.log'],
    'capability': {
        'label': 'FortiAnalyzer',
        'index_pattern': 'fortianalyzer*',
        'operations': ['forward', 'local', 'sniffer', 'ips', 'virus', 'webfilter', 'rest-api', 'system', 'sdwan'],
        'success_code': '',
        'fields': {
            'type': 'log type: traffic, utm or event',
            'subtype': 'log subtype (see OPERATIONS for the exact names)',
            'level': 'log severity: notice, warning, alert, ...',
            'action': 'session action: accept, deny, close, server-rst, ...',
            'utmaction': 'UTM verdict: allow, block, ...',
            'srcip': 'source IP',
            'dstip': 'destination IP',
            'srcport': 'source port',
            'dstport': 'destination port',
            'service': 'service/protocol name (HTTP, DNS, ...)',
            'app': 'detected application',
            'appcat': 'application category',
            'attack': 'IPS signature name; present ONLY on ips events — count it for threats',
            'severity': 'IPS severity; present on utm events',
            'srccountry': 'source country',
            'dstcountry': 'destination country',
            'sentbyte': 'bytes sent; sum it for bandwidth',
            'rcvdbyte': 'bytes received; sum it for bandwidth',
            'policyid': 'firewall policy id',
        },
        'volume_field': 'type',
        'forecast_interval_minutes': 240,
        'forecast_horizon': 8,
        'forecasts': [
            {
                'name': 'fortianalyzer-bytes-forecast',
                'feature_name': 'bytes_sent',
                'aggregation_query': {
                    'bytes_sent': {
                        'sum': {
                            'field': 'sentbyte',
                        },
                    },
                },
                'description': 'Forecast de ancho de banda (bytes enviados) por intervalo',
            },
            {
                'name': 'fortianalyzer-threats-forecast',
                'feature_name': 'ips_threats',
                'aggregation_query': {
                    'ips_threats': {
                        'value_count': {
                            'field': 'attack',
                        },
                    },
                },
                'description': 'Forecast de amenazas IPS detectadas por intervalo',
            },
            {
                'name': 'fortianalyzer-srcips-forecast',
                'feature_name': 'unique_sources',
                'aggregation_query': {
                    'unique_sources': {
                        'cardinality': {
                            'field': 'srcip',
                        },
                    },
                },
                'description': 'Forecast de IPs de origen unicas por intervalo',
                'history': 2000,
            }
        ],
    },
    'dashboard': {
        'title': 'FortiAnalyzer — Firewall FortiGate',
        'index_fields': [
            ('@timestamp', 'date'),
            ('type', 'keyword'),
            ('subtype', 'keyword'),
            ('level', 'keyword'),
            ('action', 'keyword'),
            ('utmaction', 'keyword'),
            ('srcip', 'ip'),
            ('dstip', 'ip'),
            ('srcport', 'long'),
            ('dstport', 'long'),
            ('service', 'keyword'),
            ('app', 'keyword'),
            ('appcat', 'keyword'),
            ('attack', 'keyword'),
            ('severity', 'keyword'),
            ('srccountry', 'keyword'),
            ('dstcountry', 'keyword'),
            ('sentbyte', 'long'),
            ('rcvdbyte', 'long'),
            ('policyid', 'long')
        ],
        'panels': [
            {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## FortiAnalyzer — tráfico, UTM y amenazas (naming Fortinet)
Sesiones por acción, ancho de banda, top aplicaciones, firmas IPS y países de origen."""},
            {'type': 'controls', 'title': 'Filtros', 'controls': [
                {'field': 'action', 'label': 'Acción'}, {'field': 'subtype', 'label': 'Subtipo'},
                {'field': 'appcat', 'label': 'Categoría de app'}, {'field': 'severity', 'label': 'Severidad'}]},
            {'type': 'metric', 'title': 'Total de Sesiones', 'agg': 'count'},
            {'type': 'metric', 'title': 'Bloqueados', 'agg': 'count', 'label': 'Bloqueados', 'query': 'action:(deny or blocked or dropped)'},
            {'type': 'metric', 'title': 'Bytes Enviados', 'agg': 'sum', 'field': 'sentbyte', 'label': 'Bytes Enviados'},
            {'type': 'metric', 'title': 'Amenazas IPS', 'agg': 'count', 'label': 'Amenazas IPS', 'query': 'attack:*'},
            {'type': 'area', 'title': 'Sesiones en el tiempo por acción', 'metric': 'count', 'split': 'action', 'w': 48},
            {'type': 'line', 'title': 'Ancho de banda en el tiempo (bytes enviados)', 'metric': 'sum', 'field': 'sentbyte'},
            {'type': 'area', 'title': 'Eventos UTM en el tiempo', 'metric': 'count', 'query': 'type:utm'},
            {'type': 'bar', 'title': 'Top Aplicaciones', 'field': 'app', 'horizontal': True},
            {'type': 'bar', 'title': 'Top Firmas IPS', 'field': 'attack', 'horizontal': True},
            {'type': 'table', 'title': 'Top IPs de Origen', 'field': 'srcip'},
            {'type': 'bar', 'title': 'Top Países de Origen', 'field': 'srccountry', 'horizontal': True},
            {'type': 'bar', 'title': 'Top Puertos de Destino Bloqueados', 'field': 'dstport', 'horizontal': True, 'query': 'action:(deny or blocked or dropped)'},
            {'type': 'table', 'title': 'Top Apps por Bytes', 'field': 'app', 'metric': 'sum', 'agg_field': 'sentbyte'},
        ],
    },
    'extra_capabilities': {
        'fortianalyzer-soc': {
            'label': 'FortiAnalyzer (SOC)',
            'index_pattern': 'fortianalyzer*',
            'operations': ['ips', 'virus', 'webfilter', 'blocked', 'dropped'],
            'success_code': '',
            'fields': {
                'type': 'log type',
                'subtype': 'utm subtype: ips, virus, webfilter',
                'action': 'session action',
                'utmaction': 'UTM verdict',
                'attack': 'IPS signature name',
                'severity': 'IPS severity',
                'virus': 'virus name',
                'url': 'blocked URL',
                'srcip': 'source IP',
                'dstip': 'destination IP',
                'crscore': 'IPS credit score',
            },
            'volume_field': 'subtype',
            'forecast_interval_minutes': 240,
            'forecast_horizon': 8,
            'forecasts': [
                {
                    'name': 'fa-soc-threats-forecast',
                    'feature_name': 'utm_events',
                    'aggregation_query': {
                        'utm_events': {
                            'filter': {
                                'term': {
                                    'type': 'utm',
                                },
                            },
                        },
                    },
                    'description': 'Forecast de amenazas UTM por intervalo',
                },
                {
                    'name': 'fa-soc-ips-forecast',
                    'feature_name': 'ips_attacks',
                    'aggregation_query': {
                        'ips_attacks': {
                            'filter': {
                                'term': {
                                    'subtype': 'ips',
                                },
                            },
                        },
                    },
                    'description': 'Forecast de eventos IPS por intervalo',
                },
                {
                    'name': 'fa-soc-blocked-forecast',
                    'feature_name': 'blocked_sessions',
                    'aggregation_query': {
                        'blocked_sessions': {
                            'filter': {
                                'terms': {
                                    'action': ['blocked', 'dropped'],
                                },
                            },
                        },
                    },
                    'description': 'Forecast de sesiones bloqueadas por intervalo',
                }
            ],
        },
        'fortianalyzer-traffic': {
            'label': 'FortiAnalyzer (Traffic)',
            'index_pattern': 'fortianalyzer*',
            'operations': ['forward', 'local', 'sniffer'],
            'success_code': '',
            'fields': {
                'action': 'session action: accept, blocked, dropped',
                'srcip': 'source IP',
                'dstip': 'destination IP',
                'app': 'application',
                'appcat': 'application category',
                'sentbyte': 'bytes sent',
                'rcvdbyte': 'bytes received',
                'srccountry': 'source country',
                'dstcountry': 'destination country',
                'proto': 'protocol',
                'dstport': 'destination port',
                'policyid': 'policy ID',
            },
            'volume_field': 'action',
            'forecast_interval_minutes': 240,
            'forecast_horizon': 8,
            'forecasts': [
                {
                    'name': 'fa-traffic-bytes-forecast',
                    'feature_name': 'bytes_sent',
                    'aggregation_query': {
                        'bytes_sent': {
                            'sum': {
                                'field': 'sentbyte',
                            },
                        },
                    },
                    'description': 'Forecast de bytes enviados por intervalo',
                },
                {
                    'name': 'fa-traffic-sessions-forecast',
                    'feature_name': 'sessions',
                    'aggregation_query': {
                        'sessions': {
                            'value_count': {
                                'field': 'action',
                            },
                        },
                    },
                    'description': 'Forecast de sesiones por intervalo',
                },
                {
                    'name': 'fa-traffic-srcips-forecast',
                    'feature_name': 'unique_sources',
                    'aggregation_query': {
                        'unique_sources': {
                            'cardinality': {
                                'field': 'srcip',
                            },
                        },
                    },
                    'description': 'Forecast de IPs de origen unicas por intervalo',
                    'history': 2000,
                }
            ],
        },
        'fortianalyzer-utm': {
            'label': 'FortiAnalyzer (UTM)',
            'index_pattern': 'fortianalyzer*',
            'operations': ['ips', 'virus', 'webfilter'],
            'success_code': '',
            'fields': {
                'subtype': 'utm subtype: ips, virus, webfilter',
                'utmaction': 'UTM verdict',
                'attack': 'IPS signature',
                'virus': 'virus name',
                'catdesc': 'web filter category',
                'url': 'filtered URL',
                'hostname': 'target host',
                'severity': 'IPS severity',
                'crscore': 'IPS credit score',
            },
            'volume_field': 'subtype',
            'forecast_interval_minutes': 240,
            'forecast_horizon': 8,
            'forecasts': [
                {
                    'name': 'fa-utm-ips-forecast',
                    'feature_name': 'ips_events',
                    'aggregation_query': {
                        'ips_events': {
                            'filter': {
                                'term': {
                                    'subtype': 'ips',
                                },
                            },
                        },
                    },
                    'description': 'Forecast de eventos IPS por intervalo',
                },
                {
                    'name': 'fa-utm-virus-forecast',
                    'feature_name': 'virus_events',
                    'aggregation_query': {
                        'virus_events': {
                            'filter': {
                                'term': {
                                    'subtype': 'virus',
                                },
                            },
                        },
                    },
                    'description': 'Forecast de detecciones de virus por intervalo',
                },
                {
                    'name': 'fa-utm-webfilter-forecast',
                    'feature_name': 'webfilter_events',
                    'aggregation_query': {
                        'webfilter_events': {
                            'filter': {
                                'term': {
                                    'subtype': 'webfilter',
                                },
                            },
                        },
                    },
                    'description': 'Forecast de bloques de web filter por intervalo',
                }
            ],
        },
        'fortianalyzer-event': {
            'label': 'FortiAnalyzer (Event)',
            'index_pattern': 'fortianalyzer*',
            'operations': ['system', 'sdwan', 'rest-api'],
            'success_code': '',
            'fields': {
                'subtype': 'event subtype: system, sdwan, rest-api',
                'level': 'event level',
                'status': 'event status',
                'msg': 'system message',
                'user': 'user',
                'path': 'REST API path',
                'latency': 'SD-WAN latency',
                'jitter': 'SD-WAN jitter',
                'packetloss': 'SD-WAN packet loss',
                'healthcheck': 'SD-WAN health check target',
            },
            'volume_field': 'subtype',
            'forecast_interval_minutes': 240,
            'forecast_horizon': 8,
            'forecasts': [
                {
                    'name': 'fa-event-sdwan-latency-forecast',
                    'feature_name': 'avg_latency',
                    'aggregation_query': {
                        'avg_latency': {
                            'avg': {
                                'field': 'latency',
                            },
                        },
                    },
                    'description': 'Forecast de latencia SD-WAN por intervalo',
                },
                {
                    'name': 'fa-event-system-forecast',
                    'feature_name': 'system_events',
                    'aggregation_query': {
                        'system_events': {
                            'filter': {
                                'term': {
                                    'subtype': 'system',
                                },
                            },
                        },
                    },
                    'description': 'Forecast de eventos de sistema por intervalo',
                },
                {
                    'name': 'fa-event-sdwan-forecast',
                    'feature_name': 'sdwan_events',
                    'aggregation_query': {
                        'sdwan_events': {
                            'filter': {
                                'term': {
                                    'subtype': 'sdwan',
                                },
                            },
                        },
                    },
                    'description': 'Forecast de eventos SD-WAN por intervalo',
                }
            ],
        },
    },
    'extra_dashboards': {
        'fortianalyzer-soc': {
            'title': 'FortiAnalyzer — SOC',
            'ip_id': 'fortianalyzer-*',
            'index_fields': [
                ('@timestamp', 'date'),
                ('type', 'keyword'),
                ('subtype', 'keyword'),
                ('level', 'keyword'),
                ('action', 'keyword'),
                ('utmaction', 'keyword'),
                ('srcip', 'ip'),
                ('dstip', 'ip'),
                ('srcport', 'long'),
                ('dstport', 'long'),
                ('service', 'keyword'),
                ('proto', 'keyword'),
                ('app', 'keyword'),
                ('appcat', 'keyword'),
                ('attack', 'keyword'),
                ('severity', 'keyword'),
                ('attackid', 'long'),
                ('crscore', 'long'),
                ('craction', 'keyword'),
                ('crlevel', 'keyword'),
                ('srccountry', 'keyword'),
                ('dstcountry', 'keyword'),
                ('sentbyte', 'long'),
                ('rcvdbyte', 'long'),
                ('policyid', 'long'),
                ('url', 'keyword'),
                ('hostname', 'keyword'),
                ('catdesc', 'keyword'),
                ('virus', 'keyword'),
                ('virusid', 'long'),
                ('filename', 'keyword'),
                ('msg', 'keyword'),
                ('status', 'keyword'),
                ('user', 'keyword')
            ],
            'panels': [
                {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## FortiAnalyzer — SOC Dashboard
Vista de operaciones de seguridad: amenazas IPS, virus, web filter, tráfico bloqueado y source IPs maliciosas."""},
                {'type': 'controls', 'title': 'Filtros', 'controls': [
                    {'field': 'subtype', 'label': 'Subtipo'}, {'field': 'action', 'label': 'Acción'},
                    {'field': 'severity', 'label': 'Severidad'}, {'field': 'utmaction', 'label': 'UTM Action'}]},
                {'type': 'metric', 'title': 'Total Amenazas', 'agg': 'count', 'label': 'Total Amenazas', 'query': 'type:utm'},
                {'type': 'metric', 'title': 'Sesiones Bloqueadas', 'agg': 'count', 'label': 'Sesiones Bloqueadas', 'query': 'action:(blocked or dropped)'},
                {'type': 'metric', 'title': 'IPS Attacks', 'agg': 'count', 'label': 'IPS Attacks', 'query': 'subtype:ips'},
                {'type': 'metric', 'title': 'Virus Detections', 'agg': 'count', 'label': 'Virus Detections', 'query': 'subtype:virus'},
                {'type': 'metric', 'title': 'Web Filter Blocks', 'agg': 'count', 'label': 'Web Filter Blocks', 'query': 'subtype:webfilter'},
                {'type': 'area', 'title': 'Amenazas en el tiempo por subtype', 'metric': 'count', 'split': 'subtype', 'query': 'type:utm', 'w': 48},
                {'type': 'area', 'title': 'Tráfico bloqueado en el tiempo', 'metric': 'count', 'split': 'action', 'query': 'action:(blocked or dropped)'},
                {'type': 'bar', 'title': 'Top Firmas IPS', 'field': 'attack', 'horizontal': True, 'query': 'subtype:ips'},
                {'type': 'table', 'title': 'Top IPs con Amenazas', 'field': 'srcip', 'query': 'type:utm'},
                {'type': 'bar', 'title': 'Top Virus', 'field': 'virus', 'horizontal': True, 'query': 'subtype:virus'},
                {'type': 'table', 'title': 'Top URLs Bloqueadas', 'field': 'url', 'query': 'subtype:webfilter'},
                {'type': 'bar', 'title': 'Top Países Origen (amenazas)', 'field': 'srccountry', 'horizontal': True, 'query': 'type:utm'},
            ],
        },
        'fortianalyzer-traffic': {
            'title': 'FortiAnalyzer — Traffic',
            'ip_id': 'fortianalyzer-*',
            'index_fields': [
                ('@timestamp', 'date'),
                ('type', 'keyword'),
                ('subtype', 'keyword'),
                ('level', 'keyword'),
                ('action', 'keyword'),
                ('utmaction', 'keyword'),
                ('srcip', 'ip'),
                ('dstip', 'ip'),
                ('srcport', 'long'),
                ('dstport', 'long'),
                ('service', 'keyword'),
                ('proto', 'keyword'),
                ('app', 'keyword'),
                ('appcat', 'keyword'),
                ('srccountry', 'keyword'),
                ('dstcountry', 'keyword'),
                ('sentbyte', 'long'),
                ('rcvdbyte', 'long'),
                ('policyid', 'long'),
                ('srcintf', 'keyword'),
                ('dstintf', 'keyword'),
                ('duration', 'long'),
                ('sessionid', 'keyword')
            ],
            'panels': [
                {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## FortiAnalyzer — Traffic Dashboard
Tráfico de red: sesiones por acción, ancho de banda, top aplicaciones, países, IPs, puertos y políticas."""},
                {'type': 'controls', 'title': 'Filtros', 'controls': [
                    {'field': 'action', 'label': 'Acción'}, {'field': 'proto', 'label': 'Protocolo'},
                    {'field': 'appcat', 'label': 'Categoría de app'}, {'field': 'srcintf', 'label': 'Interfaz origen'}]},
                {'type': 'metric', 'title': 'Total Sesiones', 'agg': 'count'},
                {'type': 'metric', 'title': 'Aceptadas', 'agg': 'count', 'label': 'Aceptadas', 'query': 'action:accept'},
                {'type': 'metric', 'title': 'Bloqueadas', 'agg': 'count', 'label': 'Bloqueadas', 'query': 'action:(blocked or dropped)'},
                {'type': 'metric', 'title': 'Bytes Enviados', 'agg': 'sum', 'field': 'sentbyte', 'label': 'Bytes Enviados'},
                {'type': 'metric', 'title': 'Bytes Recibidos', 'agg': 'sum', 'field': 'rcvdbyte', 'label': 'Bytes Recibidos'},
                {'type': 'area', 'title': 'Sesiones en el tiempo por acción', 'metric': 'count', 'split': 'action', 'w': 48},
                {'type': 'line', 'title': 'Ancho de banda (bytes enviados)', 'metric': 'sum', 'field': 'sentbyte'},
                {'type': 'table', 'title': 'Top IPs Origen', 'field': 'srcip'},
                {'type': 'table', 'title': 'Top IPs Destino', 'field': 'dstip'},
                {'type': 'bar', 'title': 'Top Países Origen', 'field': 'srccountry', 'horizontal': True},
                {'type': 'bar', 'title': 'Top Puertos Destino', 'field': 'dstport', 'horizontal': True},
                {'type': 'table', 'title': 'Top Apps por Bytes', 'field': 'app', 'metric': 'sum', 'agg_field': 'sentbyte'},
            ],
        },
        'fortianalyzer-utm': {
            'title': 'FortiAnalyzer — UTM',
            'ip_id': 'fortianalyzer-*',
            'index_fields': [
                ('@timestamp', 'date'),
                ('type', 'keyword'),
                ('subtype', 'keyword'),
                ('level', 'keyword'),
                ('action', 'keyword'),
                ('utmaction', 'keyword'),
                ('srcip', 'ip'),
                ('dstip', 'ip'),
                ('dstport', 'long'),
                ('attack', 'keyword'),
                ('severity', 'keyword'),
                ('attackid', 'long'),
                ('crscore', 'long'),
                ('craction', 'keyword'),
                ('url', 'keyword'),
                ('hostname', 'keyword'),
                ('catdesc', 'keyword'),
                ('virus', 'keyword'),
                ('virusid', 'long'),
                ('filename', 'keyword'),
                ('method', 'keyword'),
                ('agent', 'keyword')
            ],
            'panels': [
                {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## FortiAnalyzer — UTM Dashboard
Unified Threat Management: IPS, web filter y virus. Firmas, categorías, URLs bloqueadas y detecciones."""},
                {'type': 'controls', 'title': 'Filtros', 'controls': [
                    {'field': 'subtype', 'label': 'Subtipo'}, {'field': 'severity', 'label': 'Severidad'},
                    {'field': 'utmaction', 'label': 'UTM Action'}]},
                {'type': 'metric', 'title': 'Total UTM', 'agg': 'count', 'label': 'Total UTM', 'query': 'type:utm'},
                {'type': 'metric', 'title': 'IPS Events', 'agg': 'count', 'label': 'IPS Events', 'query': 'subtype:ips'},
                {'type': 'metric', 'title': 'Web Filter', 'agg': 'count', 'label': 'Web Filter', 'query': 'subtype:webfilter'},
                {'type': 'metric', 'title': 'Virus', 'agg': 'count', 'label': 'Virus', 'query': 'subtype:virus'},
                {'type': 'area', 'title': 'UTM events por subtype', 'metric': 'count', 'split': 'subtype', 'query': 'type:utm', 'w': 48},
                {'type': 'line', 'title': 'crscore en el tiempo', 'metric': 'sum', 'field': 'crscore', 'query': 'subtype:ips'},
                {'type': 'bar', 'title': 'Top Firmas IPS', 'field': 'attack', 'horizontal': True, 'query': 'subtype:ips'},
                {'type': 'bar', 'title': 'Top Virus', 'field': 'virus', 'horizontal': True, 'query': 'subtype:virus'},
                {'type': 'bar', 'title': 'Top Categorías Web Filter', 'field': 'catdesc', 'horizontal': True, 'query': 'subtype:webfilter'},
                {'type': 'table', 'title': 'Top URLs Bloqueadas', 'field': 'url', 'query': 'subtype:webfilter'},
                {'type': 'table', 'title': 'Top Hosts', 'field': 'hostname', 'query': 'subtype:webfilter'},
                {'type': 'table', 'title': 'Top IPs (UTM)', 'field': 'srcip', 'query': 'type:utm'},
            ],
        },
        'fortianalyzer-event': {
            'title': 'FortiAnalyzer — Event',
            'ip_id': 'fortianalyzer-*',
            'index_fields': [
                ('@timestamp', 'date'),
                ('type', 'keyword'),
                ('subtype', 'keyword'),
                ('level', 'keyword'),
                ('action', 'keyword'),
                ('status', 'keyword'),
                ('msg', 'keyword'),
                ('logdesc', 'keyword'),
                ('user', 'keyword'),
                ('ui', 'keyword'),
                ('path', 'keyword'),
                ('vdom', 'keyword'),
                ('method', 'keyword'),
                ('reqtype', 'keyword'),
                ('reason', 'keyword'),
                ('healthcheck', 'keyword'),
                ('interface', 'keyword'),
                ('latency', 'long'),
                ('jitter', 'long'),
                ('packetloss', 'long'),
                ('inbandwidth', 'long'),
                ('outbandwidth', 'long'),
                ('slatargetid', 'keyword'),
                ('slamap', 'keyword'),
                ('metric', 'keyword')
            ],
            'panels': [
                {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## FortiAnalyzer — Event Dashboard
Eventos de sistema, SD-WAN y REST API: mensajes, status, latencia, jitter, packet loss y health checks."""},
                {'type': 'controls', 'title': 'Filtros', 'controls': [
                    {'field': 'subtype', 'label': 'Subtipo'}, {'field': 'level', 'label': 'Nivel'},
                    {'field': 'status', 'label': 'Status'}]},
                {'type': 'metric', 'title': 'Total Events', 'agg': 'count', 'label': 'Total Events', 'query': 'type:event'},
                {'type': 'metric', 'title': 'System', 'agg': 'count', 'label': 'System', 'query': 'subtype:system'},
                {'type': 'metric', 'title': 'SD-WAN', 'agg': 'count', 'label': 'SD-WAN', 'query': 'subtype:sdwan'},
                {'type': 'metric', 'title': 'REST API', 'agg': 'count', 'label': 'REST API', 'query': 'subtype:rest-api'},
                {'type': 'area', 'title': 'Events por subtype', 'metric': 'count', 'split': 'subtype', 'query': 'type:event', 'w': 48},
                {'type': 'line', 'title': 'SD-WAN latency en el tiempo', 'metric': 'sum', 'field': 'latency', 'query': 'subtype:sdwan'},
                {'type': 'line', 'title': 'SD-WAN packet loss', 'metric': 'sum', 'field': 'packetloss', 'query': 'subtype:sdwan'},
                {'type': 'table', 'title': 'Top Mensajes de Sistema', 'field': 'msg', 'query': 'subtype:system'},
                {'type': 'table', 'title': 'Top Usuarios', 'field': 'user', 'query': 'type:event'},
                {'type': 'table', 'title': 'Top REST API Paths', 'field': 'path', 'query': 'subtype:rest-api'},
                {'type': 'table', 'title': 'Top Health Checks', 'field': 'healthcheck', 'query': 'subtype:sdwan'},
            ],
        },
    },
    'extra_industry_fields': {
        'fortianalyzer-soc': {
            'attack',
            'crscore',
            'dstip',
            'dstport',
            'severity',
            'srccountry',
            'srcip',
            'subtype',
            'url',
            'utmaction',
            'virus',
        },
        'fortianalyzer-traffic': {
            'action',
            'app',
            'appcat',
            'dstcountry',
            'dstip',
            'dstport',
            'policyid',
            'proto',
            'rcvdbyte',
            'sentbyte',
            'srccountry',
            'srcintf',
            'srcip',
        },
        'fortianalyzer-utm': {
            'attack',
            'attackid',
            'catdesc',
            'crscore',
            'filename',
            'hostname',
            'severity',
            'srcip',
            'subtype',
            'url',
            'utmaction',
            'virus',
        },
        'fortianalyzer-event': {
            'healthcheck',
            'jitter',
            'latency',
            'level',
            'msg',
            'packetloss',
            'path',
            'status',
            'subtype',
            'user',
        },
    },
}
