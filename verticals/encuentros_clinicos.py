"""Vertical declarativo: health.

Definición única del vertical (card, filtro, campos, capability spec, dashboard
spec, preguntas del chatbot, vocabulario de industria y datasets). Lo consumen
tanto el backend (capabilities/dashboards/industry/datasets) como el frontend
(inyectado por `GET /` como `window.__VERTICALS__`). Ver `verticals/__init__.py`.
"""

VERTICAL = {
    'slug': 'encuentros-clinicos',
    'label': 'Encuentros clínicos',
    'full_label': 'Encuentros clínicos',
    'group': 'salud',
    'icon': 'heart',
    'index_base': 'encuentros-clinicos',
    'description': 'Salud · consultas, pacientes, triage',
    'sample': '2025-07-01T07:14:09Z,emergency,183460006,Obstetric emergency hospital admission,d286528e,Hamilton,129.16,129.16,69.16,Normal pregnancy,YELLOW',
    'filter_code': """filter {
  csv {
    source => "message"
    columns => ["ts","class","code","desc","patient","city","cost","claim","covered","reason","triage"]
  }
  date { match => ["ts", "ISO8601"] target => "@timestamp" }
  mutate { convert => { "cost" => "float" "claim" => "float" "covered" => "float" } }
  if [triage] == "" { mutate { remove_field => ["triage"] } }
  if [reason] == "" { mutate { remove_field => ["reason"] } }
  mutate { remove_field => ["message", "ts"] }
}""",
    'fields': [
        {
            'raw_name': 'class',
            'field_path': 'class',
            'type': 'keyword',
            'business_label': 'Encounter Class',
        },
        {
            'raw_name': 'code',
            'field_path': 'code',
            'type': 'keyword',
            'business_label': 'SNOMED Code',
        },
        {
            'raw_name': 'desc',
            'field_path': 'desc',
            'type': 'keyword',
            'business_label': 'Description',
        },
        {
            'raw_name': 'patient',
            'field_path': 'patient',
            'type': 'keyword',
            'business_label': 'Patient ID',
        },
        {
            'raw_name': 'city',
            'field_path': 'city',
            'type': 'keyword',
            'business_label': 'City',
        },
        {
            'raw_name': 'cost',
            'field_path': 'cost',
            'type': 'float',
            'business_label': 'Base Cost',
        },
        {
            'raw_name': 'claim',
            'field_path': 'claim',
            'type': 'float',
            'business_label': 'Total Claim',
        },
        {
            'raw_name': 'covered',
            'field_path': 'covered',
            'type': 'float',
            'business_label': 'Payer Coverage',
        },
        {
            'raw_name': 'reason',
            'field_path': 'reason',
            'type': 'keyword',
            'business_label': 'Reason',
        },
        {
            'raw_name': 'triage',
            'field_path': 'triage',
            'type': 'keyword',
            'business_label': 'Triage Level',
        }
    ],
    'suggested_questions': [
        '¿Cuántos encuentros clínicos hay en total?',
        '¿Cuántos encuentros hay por clase (ambulatorio, internación, emergencia)?',
        '¿Cuáles son los 10 diagnósticos más frecuentes?',
        '¿Cuántos encuentros hay por ciudad?',
        '¿Cuál es el costo total de los reclamos?',
        '¿Cuál es la cobertura total de los pagadores?',
        '¿Cuántos encuentros hay por nivel de triage?',
        '¿Cuáles son los 10 motivos clínicos más frecuentes?',
        '¿Cuál es el costo promedio por clase de encuentro?',
        '¿Cuántos encuentros hay por día?',
    ],
    'industry_fields': {
        'claim',
        'cost',
        'department',
        'diagnosis',
        'patient.id',
        'procedure',
        'provider',
        'visit_date',
    },
    'dataset_files': ['encuentros-clinicos.log'],
    'capability': {
        'label': 'Encuentros clínicos',
        'index_pattern': 'encuentros-clinicos*',
        'operations': ['ambulatory', 'wellness', 'outpatient', 'inpatient', 'emergency', 'urgentcare'],
        'success_code': '',
        'fields': {
            'class': 'encounter class (see OPERATIONS for the exact names)',
            'desc': 'encounter description (SNOMED)',
            'patient': 'patient id (for unique patients)',
            'city': 'patient city',
            'cost': 'base encounter cost',
            'claim': 'total claim cost; sum it for total spend',
            'covered': 'payer coverage amount',
            'reason': 'clinical reason, when recorded',
            'triage': 'RED, YELLOW or GREEN; present ONLY on emergency/urgentcare visits',
        },
        'volume_field': 'class',
        'forecast_interval_minutes': 240,
        'forecast_horizon': 8,
        'forecasts': [
            {
                'name': 'health-encounters-forecast',
                'feature_name': 'encounters_volume',
                'aggregation_query': {
                    'encounters_volume': {
                        'value_count': {
                            'field': 'class',
                        },
                    },
                },
                'description': 'Forecast de volumen de consultas por intervalo',
            },
            {
                'name': 'health-emergency-forecast',
                'feature_name': 'critical_visits',
                'aggregation_query': {
                    'critical_visits': {
                        'value_count': {
                            'field': 'triage',
                        },
                    },
                },
                'description': 'Forecast de visitas criticas (emergencia/urgencia) por intervalo',
            },
            {
                'name': 'health-patients-forecast',
                'feature_name': 'unique_patients',
                'aggregation_query': {
                    'unique_patients': {
                        'cardinality': {
                            'field': 'patient',
                        },
                    },
                },
                'description': 'Forecast de pacientes unicos por intervalo',
                'history': 2000,
            }
        ],
    },
    'dashboard': {
        'title': 'Encuentros clínicos',
        'index_fields': [
            ('@timestamp', 'date'),
            ('class', 'keyword'),
            ('desc', 'keyword'),
            ('patient', 'keyword'),
            ('city', 'keyword'),
            ('cost', 'double'),
            ('claim', 'double'),
            ('covered', 'double'),
            ('reason', 'keyword'),
            ('triage', 'keyword')
        ],
        'panels': [
            {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## Encuentros clínicos — consultas y emergencias
Volumen de consultas, costos, triage, pacientes únicos y ciudades."""},
            {'type': 'controls', 'title': 'Filtros', 'controls': [
                {'field': 'class', 'label': 'Clase de consulta'}, {'field': 'triage', 'label': 'Triaje'}]},
            {'type': 'metric', 'title': 'Total de Consultas', 'agg': 'count'},
            {'type': 'metric', 'title': 'Pacientes Únicos', 'agg': 'cardinality', 'field': 'patient', 'label': 'Pacientes Únicos'},
            {'type': 'metric', 'title': 'Costo Total de Reclamos', 'agg': 'sum', 'field': 'claim', 'label': 'Costo Total de Reclamos'},
            {'type': 'metric', 'title': 'Visitas Críticas', 'agg': 'count', 'label': 'Visitas Críticas', 'query': 'triage:*'},
            {'type': 'area', 'title': 'Consultas en el tiempo por clase', 'metric': 'count', 'split': 'class', 'w': 48},
            {'type': 'line', 'title': 'Costo de reclamos en el tiempo', 'metric': 'sum', 'field': 'claim'},
            {'type': 'area', 'title': 'Visitas de emergencia en el tiempo', 'metric': 'count', 'query': 'class:(emergency or urgentcare)'},
            {'type': 'bar', 'title': 'Costo por Clase', 'field': 'class', 'metric': 'sum', 'agg_field': 'claim', 'horizontal': True},
            {'type': 'bar', 'title': 'Top Ciudades por Consultas', 'field': 'city', 'horizontal': True},
            {'type': 'bar', 'title': 'Top Motivos Clínicos', 'field': 'reason', 'horizontal': True},
            {'type': 'table', 'title': 'Top Pacientes por Costo', 'field': 'patient', 'metric': 'sum', 'agg_field': 'cost'},
            {'type': 'line', 'title': 'Cobertura vs Costo en el tiempo', 'metric': 'sum', 'field': 'covered', 'w': 48},
        ],
    },
}
