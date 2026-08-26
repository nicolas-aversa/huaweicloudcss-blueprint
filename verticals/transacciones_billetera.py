"""Vertical declarativo: fintech-transactions.

Definición única del vertical (card, filtro, campos, capability spec, dashboard
spec, preguntas del chatbot, vocabulario de industria y datasets). Lo consumen
tanto el backend (capabilities/dashboards/industry/datasets) como el frontend
(inyectado por `GET /` como `window.__VERTICALS__`). Ver `verticals/__init__.py`.
"""

VERTICAL = {
    'slug': 'transacciones-billetera',
    'label': 'Transacciones billetera',
    'full_label': 'Transacciones billetera',
    'group': 'fintech',
    'icon': 'activity',
    'index_base': 'transacciones-billetera',
    'description': 'Billetera · operaciones, funnel, geo',
    'dedup_id': '%{[@metadata][generated_id]}',
    'sample': '20251113014806:726000000 - 7846:1787478390 operation_code=TRANSFER|message_type=210|response_code=000|sequence_number=1015|channel=M|customer_id=15761618|entry_time=20251113014806726|account_ref=4|steps={"data":{"steps":[{"code":"validate_session","index":"0","counter":"1","stepType":"Req & Resp","status":"C","timedout":"False","lateResp":"False"},{"code":"check_account","index":"1","counter":"2","stepType":"Internal","status":"C","timedout":"False","lateResp":"False"},{"code":"confirm","index":"2","counter":"3","stepType":"Internal","status":"C","timedout":"False","lateResp":"False"}]}}|detail=carrier=Personal~platform=ANDROID~os_version=10~app_version=3.2.1~device_model=SM-A115M~device_platform=samsung/SM-A115M/samsung/sm-a115m~geo_lat=-34.9041327~geo_long=-57.9935279~geo_date=20251113014806',
    'filter_code': """filter {
  grok {
    match => {
      "message" => "^%{NOTSPACE:raw_timestamp}\\s+-\\s+%{NOTSPACE:thread_info}\\s+%{GREEDYDATA:kv_payload}"
    }
    remove_field   => ["message"]
    tag_on_failure => ["_grokparsefailure"]
  }

  if "_grokparsefailure" in [tags] { drop {} }

  fingerprint {
    source              => ["raw_timestamp", "thread_info", "kv_payload"]
    target              => "[@metadata][generated_id]"
    method              => "SHA256"
    concatenate_sources => true
  }

  kv {
    source      => "kv_payload"
    field_split => "|"
    value_split => "="
    target      => "transaction"
  }

  if [transaction][operation_code] == "HEALTH_CHECK" {
    drop {}
  }

  date {
    match          => ["[transaction][entry_time]", "yyyyMMddHHmmssSSS"]
    target         => "@timestamp"
    timezone       => "America/Argentina/Buenos_Aires"
    tag_on_failure => ["_dateparsefailure"]
  }

  mutate {
    convert => {
      "[transaction][message_type]"    => "integer"
      "[transaction][sequence_number]" => "integer"
    }
  }

  ruby {
    code => '
      trx = event.get("transaction")
      next unless trx.is_a?(Hash)
      sid = trx["session_id"] || event.get("[transaction][session_id]")
      # txn_key = session (thread) + sequence
      seq = trx["sequence_number"]
      sess = event.get("thread_info")
      event.set("[transaction][operation_key]", "#{sess}:#{seq}") if seq
    '
  }

  if [transaction][steps] {
    json {
      source               => "[transaction][steps]"
      target               => "[transaction][steps_parsed]"
      skip_on_invalid_json => true
    }
    mutate { remove_field => ["[transaction][steps]"] }
  }

  if [transaction][detail] {
    kv {
      source      => "[transaction][detail]"
      field_split => "~"
      value_split => "="
      target      => "[transaction][device]"
    }
    mutate { remove_field => ["[transaction][detail]"] }
  }

  if [transaction][device][geo_lat] and [transaction][device][geo_long] {
    ruby {
      code => '
        lat = event.get("[transaction][device][geo_lat]").to_s
        lon = event.get("[transaction][device][geo_long]").to_s
        if lat =~ /\\A-?\\d+(\\.\\d+)?\\z/ && lon =~ /\\A-?\\d+(\\.\\d+)?\\z/
          latf, lonf = lat.to_f, lon.to_f
          if latf.abs<=90 && lonf.abs<=180 && !(latf==0.0 && lonf==0.0)
            event.set("[transaction][geo_location]", "#{lat},#{lon}")
          end
        end
      '
    }
  }

  if [transaction][steps_parsed][data][steps] {
    ruby {
      code => '
        steps = event.get("[transaction][steps_parsed][data][steps]")
        next unless steps.is_a?(Array) && !steps.empty?
        ordered = steps.sort_by { |s| s["index"].to_i }
        total     = ordered.size
        completed = ordered.count { |s| s["status"] == "C" }
        reached   = ordered.map { |s| s["code"] }
        oks       = ordered.select { |s| s["status"]=="C" }.map { |s| s["code"] }
        last      = ordered.last
        first_bad = ordered.find { |s| s["status"] != "C" }
        event.set("[transaction][funnel]", {
          "steps_total"     => total,
          "steps_completed" => completed,
          "steps_reached"   => reached,
          "steps_ok"        => oks,
          "last_step_code"  => last ? last["code"] : nil,
          "failed"          => completed < total,
          "failed_at_code"  => first_bad ? first_bad["code"] : nil
        })
      '
    }
  }

  mutate {
    remove_field => ["kv_payload","raw_timestamp","thread_info","@version","host","path"]
  }
}""",
    'fields': [
        {
            'raw_name': 'operation_code',
            'field_path': 'transaction.operation_code',
            'type': 'keyword',
            'business_label': 'Operation',
        },
        {
            'raw_name': 'message_type',
            'field_path': 'transaction.message_type',
            'type': 'integer',
            'business_label': 'Message Type',
        },
        {
            'raw_name': 'response_code',
            'field_path': 'transaction.response_code',
            'type': 'keyword',
            'business_label': 'Response Code',
        },
        {
            'raw_name': 'sequence_number',
            'field_path': 'transaction.sequence_number',
            'type': 'integer',
            'business_label': 'Sequence',
        },
        {
            'raw_name': 'channel',
            'field_path': 'transaction.channel',
            'type': 'keyword',
            'business_label': 'Channel',
        },
        {
            'raw_name': 'customer_id',
            'field_path': 'transaction.customer_id',
            'type': 'keyword',
            'business_label': 'Customer',
        },
        {
            'raw_name': 'account_ref',
            'field_path': 'transaction.account_ref',
            'type': 'keyword',
            'business_label': 'Account Ref',
        },
        {
            'raw_name': 'operation_key',
            'field_path': 'transaction.operation_key',
            'type': 'keyword',
            'business_label': 'Operation Key',
        },
        {
            'raw_name': 'geo_location',
            'field_path': 'transaction.geo_location',
            'type': 'geo_point',
            'business_label': 'Geo Location',
        },
        {
            'raw_name': 'platform',
            'field_path': 'transaction.device.platform',
            'type': 'keyword',
            'business_label': 'Platform (OS)',
        },
        {
            'raw_name': 'carrier',
            'field_path': 'transaction.device.carrier',
            'type': 'keyword',
            'business_label': 'Carrier',
        },
        {
            'raw_name': 'os_version',
            'field_path': 'transaction.device.os_version',
            'type': 'keyword',
            'business_label': 'OS Version',
        },
        {
            'raw_name': 'app_version',
            'field_path': 'transaction.device.app_version',
            'type': 'keyword',
            'business_label': 'App Version',
        },
        {
            'raw_name': 'device_model',
            'field_path': 'transaction.device.device_model',
            'type': 'keyword',
            'business_label': 'Device Model',
        },
        {
            'raw_name': 'device_platform',
            'field_path': 'transaction.device.device_platform',
            'type': 'keyword',
            'business_label': 'Device Platform',
        },
        {
            'raw_name': 'steps_total',
            'field_path': 'transaction.funnel.steps_total',
            'type': 'integer',
            'business_label': 'Steps Total',
        },
        {
            'raw_name': 'steps_completed',
            'field_path': 'transaction.funnel.steps_completed',
            'type': 'integer',
            'business_label': 'Steps Completed',
        },
        {
            'raw_name': 'failed',
            'field_path': 'transaction.funnel.failed',
            'type': 'boolean',
            'business_label': 'Failed',
        },
        {
            'raw_name': 'last_step_code',
            'field_path': 'transaction.funnel.last_step_code',
            'type': 'keyword',
            'business_label': 'Last Step',
        },
        {
            'raw_name': 'failed_at_code',
            'field_path': 'transaction.funnel.failed_at_code',
            'type': 'keyword',
            'business_label': 'Failed At',
        }
    ],
    'suggested_questions': [
        '¿Cuántas transacciones financieras fallaron, o sea tienen un código de respuesta distinto de 000?',
        '¿Cuántas transacciones financieras hay por cada tipo de operación bancaria?',
        '¿Cuántos clientes únicos realizaron transacciones en el sistema financiero?',
        '¿Qué tipos de operación bancaria tienen más transacciones fallidas?',
        '¿Cuántas transacciones financieras hay por canal de acceso (web, mobile, etc.)?',
        '¿Cuál es la distribución de los códigos de respuesta en las transacciones financieras?',
        '¿Cuántas transferencias se completaron correctamente con código de respuesta 000?',
        '¿Cuántas transacciones financieras hay por plataforma del dispositivo (iOS, Android, web)?',
        '¿Cuál es el tipo de operación bancaria más común entre todas las transacciones?',
        '¿Qué canales de acceso presentan más transacciones fallidas en la banca digital?'
    ],
    'industry_fields': {
        'transaction.account_ref',
        'transaction.channel',
        'transaction.customer_id',
        'transaction.funnel',
        'transaction.message_type',
        'transaction.operation_code',
        'transaction.response_code',
        'transaction.sequence_number',
    },
    'dataset_files': ['transacciones-billetera.log'],
    'capability': {
        'label': 'Transacciones billetera',
        'index_pattern': 'transacciones-billetera*',
        'operations': [
            'LOGIN',
            'TRANSFER',
            'BALANCE_INQUIRY',
            'QR_PAYMENT',
            'BILL_PAYMENT',
            'MONEY_DEPOSIT',
            'CARD_MANAGEMENT'
        ],
        'success_code': '000',
        'fields': {
            'transaction.operation_code': 'operation type name',
            'transaction.response_code': 'response code',
            'transaction.channel': 'channel (M=mobile, W=web)',
            'transaction.customer_id': 'customer id (for unique/active users)',
            'transaction.device.platform': 'ANDROID or IOS',
            'transaction.funnel.failed': 'boolean: transaction failed in the step funnel',
            'transaction.funnel.failed_at_code': 'step where it failed',
        },
        'volume_field': 'transaction.operation_code',
        'forecast_interval_minutes': 240,
        'forecast_horizon': 8,
        'forecasts': [
            {
                'name': 'fintech-volume-forecast',
                'feature_name': 'txn_volume',
                'aggregation_query': {
                    'txn_volume': {
                        'value_count': {
                            'field': 'transaction.operation_code',
                        },
                    },
                },
                'description': 'Forecast de volumen total de transacciones',
            },
            {
                'name': 'fintech-failed-forecast',
                'feature_name': 'failed_count',
                'aggregation_query': {
                    'failed_count': {
                        'value_count': {
                            'field': 'transaction.funnel.failed_at_code',
                        },
                    },
                },
                'description': 'Forecast de volumen de transacciones fallidas',
            },
            {
                'name': 'fintech-customers-forecast',
                'feature_name': 'active_customers',
                'aggregation_query': {
                    'active_customers': {
                        'cardinality': {
                            'field': 'transaction.customer_id',
                        },
                    },
                },
                'description': 'Forecast de clientes activos unicos por intervalo',
                'history': 2000,
            }
        ],
    },
    'dashboard': {
        'title': 'Transacciones billetera',
        'index_fields': [
            ('@timestamp', 'date'),
            ('transaction.operation_code', 'keyword'),
            ('transaction.message_type', 'long'),
            ('transaction.response_code', 'keyword'),
            ('transaction.sequence_number', 'long'),
            ('transaction.channel', 'keyword'),
            ('transaction.customer_id', 'keyword'),
            ('transaction.account_ref', 'keyword'),
            ('transaction.operation_key', 'keyword'),
            ('transaction.geo_location', 'geo_point'),
            ('transaction.device.platform', 'keyword'),
            ('transaction.device.carrier', 'keyword'),
            ('transaction.device.os_version', 'keyword'),
            ('transaction.device.app_version', 'keyword'),
            ('transaction.device.device_model', 'keyword'),
            ('transaction.device.device_platform', 'keyword'),
            ('transaction.funnel.steps_total', 'long'),
            ('transaction.funnel.steps_completed', 'long'),
            ('transaction.funnel.steps_reached', 'keyword'),
            ('transaction.funnel.failed', 'boolean'),
            ('transaction.funnel.last_step_code', 'keyword'),
            ('transaction.funnel.failed_at_code', 'keyword')
        ],
        'panels': [
            {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## Transacciones billetera — actividad, clientes y funnel
Cuentas activas, recurrencia (DAU), transacciones fallidas, funnel de transferencias y geolocalización."""},
            {'type': 'controls', 'title': 'Filtros', 'controls': [
                {'field': 'transaction.operation_code', 'label': 'Operación'},
                {'field': 'transaction.channel', 'label': 'Canal'},
                {'field': 'transaction.device.platform', 'label': 'Plataforma'}]},
            {'type': 'metric', 'title': 'Total de Transacciones', 'agg': 'count'},
            {'type': 'metric', 'title': 'Cuentas Activas', 'agg': 'cardinality', 'field': 'transaction.customer_id', 'label': 'Cuentas Activas'},
            {'type': 'metric', 'title': 'Transacciones Fallidas', 'agg': 'count', 'label': 'Transacciones Fallidas', 'query': 'transaction.funnel.failed:true'},
            {'type': 'metric', 'title': 'Pasos Completados (Promedio)', 'agg': 'avg', 'field': 'transaction.funnel.steps_completed', 'label': 'Pasos Completados (Promedio)'},
            {'type': 'area', 'title': 'Transacciones en el tiempo por operación', 'metric': 'count', 'split': 'transaction.operation_code', 'w': 48},
            {'type': 'line', 'title': 'Cuentas Activas por Día (DAU)', 'metric': 'cardinality', 'field': 'transaction.customer_id'},
            {'type': 'area', 'title': 'Éxitos vs Fallos en el tiempo', 'metric': 'count', 'split': 'transaction.funnel.failed'},
            {'type': 'bar', 'title': 'Códigos de Respuesta', 'field': 'transaction.response_code', 'horizontal': True},
            {'type': 'bar', 'title': 'Embudo de Transferencias', 'field': 'transaction.funnel.steps_reached',
             'horizontal': True, 'query': 'transaction.operation_code:TRANSFER'},
            {'type': 'bar', 'title': 'Dónde Fallan las Transacciones', 'field': 'transaction.funnel.failed_at_code', 'horizontal': True},
            {'type': 'table', 'title': 'Top Clientes por Actividad', 'field': 'transaction.customer_id'},
            {'type': 'map', 'title': 'Mapa de Transacciones', 'field': 'transaction.geo_location'},
        ],
    },
}
