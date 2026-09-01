"""Vertical declarativo: siem.

Definición única del vertical (card, filtro, campos, capability spec, dashboard
spec, preguntas del chatbot, vocabulario de industria y datasets). Lo consumen
tanto el backend (capabilities/dashboards/industry/datasets) como el frontend
(inyectado por `GET /` como `window.__VERTICALS__`). Ver `verticals/__init__.py`.
"""

VERTICAL = {
    'slug': 'siem',
    'label': 'SIEM',
    'full_label': 'SIEM',
    'group': 'seguridad',
    'icon': 'layers',
    'index_base': 'siem',
    'description': 'Seguridad · FortiGate, WAF, audit',
    'sample': """date=2025-08-12 time=14:03:11 logid="0419016384" type="utm" subtype="ips" level="alert" severity="critical" srcip="45.155.205.233" srccountry="Netherlands" dstip="10.1.2.50" dstport=443 action="dropped" attack="Apache.Struts.Remote.Code.Execution" service="HTTPS"
<134>2025-10-21T03:14:07Z bastion-01 sshd[41022]: Failed password for invalid user admin from 141.98.10.62 port 51884 ssh2
{"time":1760930047000,"service_type":"IAM","trace_name":"createAccessKey","trace_type":"ApiCall","code":200,"source_ip":"193.169.255.78","user":"contractor_ext","resource_type":"iam"}
{"time":1760930112000,"attack":"sqli","action":"block","severity":"high","clientip":"80.94.95.115","host":"api.miempresa.com","url":"/api/v1/users","method":"POST","rule":"077140","status":403}""",
    'filter_code': """filter {
  # Varias fuentes NATIVAS bajo el mismo prefijo OBS: detectar el formato de cada
  # línea → event.dataset, y ramificar. Cada rama parsea SU propia marca de tiempo.
  if [message] =~ /^date=/ { mutate { add_field => { "[event][dataset]" => "fortigate" } } }
  else if [message] =~ /^<[0-9]+>/ { mutate { add_field => { "[event][dataset]" => "auth" } } }
  else if "trace_name" in [message] { mutate { add_field => { "[event][dataset]" => "cloudaudit" } } }
  else if "attack" in [message] { mutate { add_field => { "[event][dataset]" => "waf" } } }

  # ── FortiGate (traffic / utm-ips / utm-virus / utm-webfilter / event) ──
  if [event][dataset] == "fortigate" {
    kv { source => "message" field_split => " " value_split => "=" trim_value => '"' }
    mutate { add_field => { "_ts" => "%{date} %{time}" } }
    date { match => ["_ts", "yyyy-MM-dd HH:mm:ss"] target => "@timestamp" }
    mutate {
      rename => {
        "srcip" => "[source][ip]"            "dstip" => "[destination][ip]"
        "srcport" => "[source][port]"        "dstport" => "[destination][port]"
        "action" => "[event][action]"        "service" => "[network][protocol]"
        "proto" => "[network][iana_number]"  "app" => "[network][application]"
        "srccountry" => "[source][geo][country_name]"
        "dstcountry" => "[destination][geo][country_name]"
        "sentbyte" => "[source][bytes]"      "rcvdbyte" => "[destination][bytes]"
        "attack" => "[rule][name]"           "level" => "[log][level]"
        "technique" => "[event][technique]"  "campaign" => "[event][campaign]"
        "user" => "[user][name]"             "url" => "[url][path]"
        "hostname" => "[url][domain]"
      }
    }
    if [subtype] in ["ips", "virus", "webfilter"] {
      mutate { add_field => { "[event][category]" => "intrusion_detection" "[event][kind]" => "alert" } }
      if [severity] { mutate { rename => { "severity" => "[event][severity]" } } }
      else if [crlevel] { mutate { rename => { "crlevel" => "[event][severity]" } } }
    } else if [type] == "traffic" {
      mutate { add_field => { "[event][category]" => "network" "[event][kind]" => "event" } }
    } else {
      mutate { add_field => { "[event][category]" => "host" "[event][kind]" => "event" } }
    }
    if [event][action] in ["deny", "block", "blocked", "dropped"] {
      mutate { add_field => { "[security][denied]" => "1" "[event][outcome]" => "failure" } }
    }
  }

  # ── Cloud audit (Huawei Cloud Trace Service) ──
  else if [event][dataset] == "cloudaudit" {
    json { source => "message" }
    date { match => ["time", "UNIX_MS"] target => "@timestamp" }
    mutate {
      rename => {
        "source_ip" => "[source][ip]"        "user" => "[user][name]"
        "trace_name" => "[event][action]"    "trace_type" => "[event][type]"
        "service_type" => "[cloud][service][name]"
        "technique" => "[event][technique]"  "kill_chain_phase" => "[event][kill_chain_phase]"
        "risk_score" => "[event][risk_score]" "campaign" => "[event][campaign]"
        "campaign_name" => "[event][campaign_name]"
      }
      add_field => { "[event][category]" => "iam" "[event][kind]" => "event" }
    }
    if [code] >= 400 {
      mutate { add_field => { "[event][outcome]" => "failure" "[security][denied]" => "1" } }
    } else {
      mutate { add_field => { "[event][outcome]" => "success" } }
    }
  }

  # ── Auth (host Linux: SSH / sudo, syslog) ──
  else if [event][dataset] == "auth" {
    grok { match => { "message" => "^<%{NONNEGINT}>%{TIMESTAMP_ISO8601:_ts} %{HOSTNAME:[host][name]} %{DATA:[process][name]}(?:\\[%{POSINT:[process][pid]}\\])?: %{GREEDYDATA:_authmsg}" } }
    date { match => ["_ts", "ISO8601"] target => "@timestamp" }
    mutate { add_field => { "[event][category]" => "authentication" "[event][kind]" => "event" } }
    if [_authmsg] =~ "^Failed password" {
      grok { match => { "_authmsg" => "for (invalid user )?%{USERNAME:[user][name]} from %{IP:[source][ip]}" } }
      mutate { add_field => { "[event][action]" => "ssh_login" "[event][outcome]" => "failure" "[security][denied]" => "1" } }
    } else if [_authmsg] =~ "^Accepted password" {
      grok { match => { "_authmsg" => "for %{USERNAME:[user][name]} from %{IP:[source][ip]}" } }
      mutate { add_field => { "[event][action]" => "ssh_login" "[event][outcome]" => "success" } }
    } else if [process][name] == "sudo" {
      mutate { add_field => { "[event][action]" => "sudo" } }
    }
    # Extraer technique=Txxxx y campaign=CMP-xxx del final del mensaje
    if [message] =~ /technique=/ {
      grok { match => { "message" => "technique=%{DATA:[event][technique]}(?: campaign=%{DATA:[event][campaign]})?" } }
    }
    mutate { remove_field => ["_authmsg"] }
  }

  # ── WAF (Huawei WAF) ──
  else if [event][dataset] == "waf" {
    json { source => "message" }
    date { match => ["time", "UNIX_MS"] target => "@timestamp" }
    # El JSON crudo trae "rule" como string (ID de regla). Moverlo a rule.id
    # ANTES del rename de attack a rule.name: si "rule" queda escalar, ese
    # rename falla (no se puede colgar un sub-campo de un string) y OpenSearch
    # rechaza el doc entero por conflicto objeto/escalar en el mapping.
    mutate { rename => { "rule" => "[rule][id]" } }
    mutate {
      rename => {
        "clientip" => "[source][ip]"         "attack" => "[rule][name]"
        "url" => "[url][path]"               "host" => "[url][domain]"
        "action" => "[event][action]"        "method" => "[http][request][method]"
        "severity" => "[event][severity]"    "status" => "[http][response][status_code]"
        "technique" => "[event][technique]"  "kill_chain_phase" => "[event][kill_chain_phase]"
        "risk_score" => "[event][risk_score]" "campaign" => "[event][campaign]"
        "campaign_name" => "[event][campaign_name]"
      }
      add_field => { "[event][category]" => "web" "[event][kind]" => "alert" }
    }
    if [event][action] == "block" {
      mutate { add_field => { "[security][denied]" => "1" "[event][outcome]" => "success" } }
    }
  }

  # ── Threat intel (sin plugin translate: no disponible en CSS Logstash 7.10) ──
  if [source][ip] in ["45.155.205.233", "185.220.101.47", "193.169.255.78", "141.98.10.62", "80.94.95.115", "89.248.165.33", "104.244.79.61", "5.188.206.18", "212.70.149.150", "45.135.232.99", "194.165.16.78", "92.63.197.211", "146.70.199.44", "23.129.64.130", "171.25.193.20", "194.147.35.12", "77.247.108.42", "51.158.144.91", "176.10.99.200", "185.244.25.107", "94.232.46.161", "107.189.6.18", "199.249.150.83", "23.154.18.44", "51.79.151.233", "192.42.116.14", "103.143.52.221", "45.83.92.159", "141.95.172.201", "188.166.73.205"] {
    mutate { add_field => { "[threat][matched]" => "true" } }
  }

  mutate {
    convert => {
      "[source][port]" => "integer"        "[destination][port]" => "integer"
      "[source][bytes]" => "integer"       "[destination][bytes]" => "integer"
      "[network][iana_number]" => "integer"
      "[event][risk_score]" => "integer"
    }
    remove_field => ["message", "@version", "_ts", "date", "time", "path", "tz", "vd", "logid", "eventtime", "sessionid", "trace_rating", "api_version", "technique", "campaign", "campaign_name", "kill_chain_phase", "risk_score"]
  }
}""",
    'fields': [
        {
            'raw_name': 'fortigate: kv / auth: syslog / cts+waf: json',
            'field_path': 'event.dataset',
            'type': 'keyword',
            'business_label': 'Tipo de fuente',
            'group': 'Transversales',
        },
        {
            'raw_name': 'fortigate: subtype / cts: iam / auth: authentication / waf: web',
            'field_path': 'event.category',
            'type': 'keyword',
            'business_label': 'Categoría',
            'group': 'Transversales',
        },
        {
            'raw_name': 'action / trace_name',
            'field_path': 'event.action',
            'type': 'keyword',
            'business_label': 'Acción',
            'group': 'Transversales',
        },
        {
            'raw_name': 'success / failure (según code o action)',
            'field_path': 'event.outcome',
            'type': 'keyword',
            'business_label': 'Resultado',
            'group': 'Transversales',
        },
        {
            'raw_name': 'srcip / source_ip / clientip',
            'field_path': 'source.ip',
            'type': 'ip',
            'business_label': 'IP de origen',
            'group': 'Transversales',
        },
        {
            'raw_name': 'geoip(source.ip)',
            'field_path': 'source.geo.country_name',
            'type': 'keyword',
            'business_label': 'País de origen',
            'group': 'Transversales',
        },
        {
            'raw_name': 'threat-intel(source.ip)',
            'field_path': 'threat.matched',
            'type': 'keyword',
            'business_label': 'IP maliciosa conocida',
            'group': 'Transversales',
        },
        {
            'raw_name': '1 si action in [deny, block, dropped]',
            'field_path': 'security.denied',
            'type': 'keyword',
            'business_label': 'Bloqueado',
            'group': 'Transversales',
        },
        {
            'raw_name': 'severity',
            'field_path': 'event.severity',
            'type': 'keyword',
            'business_label': 'Severity (IPS/WAF)',
            'group': 'FortiGate',
        },
        {
            'raw_name': 'dstip',
            'field_path': 'destination.ip',
            'type': 'ip',
            'business_label': 'Destination IP',
            'group': 'FortiGate',
        },
        {
            'raw_name': 'srcport',
            'field_path': 'source.port',
            'type': 'integer',
            'business_label': 'Source Port',
            'group': 'FortiGate',
        },
        {
            'raw_name': 'dstport',
            'field_path': 'destination.port',
            'type': 'integer',
            'business_label': 'Destination Port',
            'group': 'FortiGate',
        },
        {
            'raw_name': 'sentbyte',
            'field_path': 'source.bytes',
            'type': 'integer',
            'business_label': 'Bytes Sent',
            'group': 'FortiGate',
        },
        {
            'raw_name': 'rcvdbyte',
            'field_path': 'destination.bytes',
            'type': 'integer',
            'business_label': 'Bytes Received',
            'group': 'FortiGate',
        },
        {
            'raw_name': 'service',
            'field_path': 'network.protocol',
            'type': 'keyword',
            'business_label': 'Protocol/Service',
            'group': 'FortiGate',
        },
        {
            'raw_name': 'app',
            'field_path': 'network.application',
            'type': 'keyword',
            'business_label': 'Application',
            'group': 'FortiGate',
        },
        {
            'raw_name': 'attack / virus',
            'field_path': 'rule.name',
            'type': 'keyword',
            'business_label': 'Signature / Attack / Virus',
            'group': 'FortiGate',
        },
        {
            'raw_name': 'user',
            'field_path': 'user.name',
            'type': 'keyword',
            'business_label': 'User (CTS / auth)',
            'group': 'Cloud Audit (CTS)',
        },
        {
            'raw_name': 'service_type',
            'field_path': 'cloud.service.name',
            'type': 'keyword',
            'business_label': 'Cloud Service (CTS)',
            'group': 'Cloud Audit (CTS)',
        },
        {
            'raw_name': 'trace_type',
            'field_path': 'event.type',
            'type': 'keyword',
            'business_label': 'Trace Type (CTS)',
            'group': 'Cloud Audit (CTS)',
        },
        {
            'raw_name': '(syslog host)',
            'field_path': 'host.name',
            'type': 'keyword',
            'business_label': 'Host (auth)',
            'group': 'Auth (syslog)',
        },
        {
            'raw_name': '(syslog proc: sshd/sudo)',
            'field_path': 'process.name',
            'type': 'keyword',
            'business_label': 'Process (auth)',
            'group': 'Auth (syslog)',
        },
        {
            'raw_name': 'rule',
            'field_path': 'rule.id',
            'type': 'keyword',
            'business_label': 'Regla WAF (ID)',
            'group': 'WAF',
        },
        {
            'raw_name': 'host',
            'field_path': 'url.domain',
            'type': 'keyword',
            'business_label': 'Web Host (WAF)',
            'group': 'WAF',
        },
        {
            'raw_name': 'url',
            'field_path': 'url.path',
            'type': 'keyword',
            'business_label': 'URL Path (WAF)',
            'group': 'WAF',
        },
        {
            'raw_name': 'method',
            'field_path': 'http.request.method',
            'type': 'keyword',
            'business_label': 'HTTP Method (WAF)',
            'group': 'WAF',
        },
        {
            'raw_name': 'status',
            'field_path': 'http.response.status_code',
            'type': 'integer',
            'business_label': 'HTTP Status (WAF)',
            'group': 'WAF',
        },
        {
            'raw_name': 'technique (Txxxx)',
            'field_path': 'event.technique',
            'type': 'keyword',
            'business_label': 'MITRE Technique ID',
            'group': 'MITRE ATT&CK',
        },
        {
            'raw_name': 'kill_chain_phase',
            'field_path': 'event.kill_chain_phase',
            'type': 'keyword',
            'business_label': 'Kill Chain Phase',
            'group': 'MITRE ATT&CK',
        },
        {
            'raw_name': 'risk_score (0-100)',
            'field_path': 'event.risk_score',
            'type': 'integer',
            'business_label': 'Risk Score',
            'group': 'MITRE ATT&CK',
        },
        {
            'raw_name': 'campaign (CMP-xxx)',
            'field_path': 'event.campaign',
            'type': 'keyword',
            'business_label': 'Campaign ID',
            'group': 'MITRE ATT&CK',
        },
        {
            'raw_name': 'campaign_name',
            'field_path': 'event.campaign_name',
            'type': 'keyword',
            'business_label': 'Campaign Name',
            'group': 'MITRE ATT&CK',
        }
    ],
    'suggested_questions': [
        '¿Cuántos eventos de seguridad hay en total?',
        '¿Cuántos eventos hay por fuente (fortigate, auth, cloudaudit, waf)?',
        '¿Cuáles son las 10 IPs de origen más frecuentes?',
        '¿Cuántos eventos hay por país de origen?',
        '¿Cuántos eventos hay por severidad?',
        '¿Cuántos eventos hay por categoría de seguridad?',
        '¿Cuáles son las 10 técnicas MITRE ATT&CK más frecuentes?',
        '¿Cuántos eventos hay por fase del kill chain?',
        '¿Cuántos logins SSH fallidos hubo y desde qué IPs?',
        '¿Cuántos eventos hay por día?',
    ],
    'industry_fields': {
        'event.action',
        'event.category',
        'event.dataset',
        'event.outcome',
        'event.severity',
        'network.protocol',
        'rule.name',
        'security.denied',
        'source.geo.country_name',
        'source.ip',
    },
    'dataset_files': ['siem-fortigate.log', 'siem-cloudaudit.log', 'siem-auth.log', 'siem-waf.log'],
    'capability': {
        'label': 'SIEM',
        'index_pattern': 'siem*',
        'operations': ['network', 'intrusion_detection', 'authentication', 'iam', 'web', 'host'],
        'success_code': '',
        'fields': {
            'event.dataset': 'source of the event: fortigate, cloudaudit (Huawei CTS), auth (host SSH) or waf',
            'event.category': 'kind of security event (see OPERATIONS)',
            'event.action': 'specific action: accept/deny/dropped (firewall), loginUser/createAccessKey (cloud), ssh_login/sudo (auth), sqli/xss/scanner (waf)',
            'event.outcome': 'success or failure',
            'event.severity': 'critical, high, medium or low (on IPS/WAF alerts)',
            'source.ip': 'source IP of the event',
            'source.geo.country_name': 'country of the source IP (geoip)',
            'destination.ip': 'destination IP',
            'user.name': 'user (cloud audit actor or SSH user)',
            'network.protocol': 'service/protocol (HTTPS, HTTP, DNS...)',
            'rule.name': 'IPS signature, WAF attack name or virus name',
            'url.domain': 'web host targeted (waf)',
            'threat.matched': "'true' ONLY when source.ip is a known-bad IP (threat intel); count it for threat hits",
            'security.denied': "present ('1') ONLY when the event was blocked/denied/failed; count it for blocked events",
            'event.technique': 'MITRE ATT&CK technique ID (T1190, T1110, T1003, etc.)',
            'event.kill_chain_phase': 'kill chain phase (reconnaissance, initial_access, persistence, etc.)',
            'event.risk_score': 'risk score 0-100',
            'event.campaign': 'campaign ID for multi-stage attacks (CMP-001, CMP-002, CMP-003)',
            'event.campaign_name': 'campaign name (Web App Compromise, Credential Theft Chain, Lateral Movement)',
        },
        'volume_field': 'event.dataset',
        'forecast_interval_minutes': 240,
        'forecast_horizon': 8,
        'forecasts': [
            {
                'name': 'siem-events-forecast',
                'feature_name': 'security_events',
                'aggregation_query': {
                    'security_events': {
                        'value_count': {
                            'field': 'event.dataset',
                        },
                    },
                },
                'description': 'Forecast de volumen de eventos de seguridad por intervalo',
            },
            {
                'name': 'siem-denied-forecast',
                'feature_name': 'denied_events',
                'aggregation_query': {
                    'denied_events': {
                        'value_count': {
                            'field': 'security.denied',
                        },
                    },
                },
                'description': 'Forecast de eventos bloqueados/denegados por intervalo',
            },
            {
                'name': 'siem-threat-forecast',
                'feature_name': 'threat_hits',
                'aggregation_query': {
                    'threat_hits': {
                        'value_count': {
                            'field': 'threat.matched',
                        },
                    },
                },
                'description': 'Forecast de eventos desde IPs conocidas-malas (threat intel)',
            }
        ],
    },
    'dashboard': {
        'title': 'SIEM — Seguridad Unificada',
        'index_fields': [
            ('@timestamp', 'date'),
            ('event.dataset', 'keyword'),
            ('event.category', 'keyword'),
            ('event.action', 'keyword'),
            ('event.outcome', 'keyword'),
            ('event.severity', 'keyword'),
            ('event.type', 'keyword'),
            ('source.ip', 'ip'),
            ('source.port', 'long'),
            ('source.geo.country_name', 'keyword'),
            ('destination.ip', 'ip'),
            ('destination.port', 'long'),
            ('network.protocol', 'keyword'),
            ('network.application', 'keyword'),
            ('source.bytes', 'long'),
            ('destination.bytes', 'long'),
            ('rule.name', 'keyword'),
            ('rule.id', 'keyword'),
            ('user.name', 'keyword'),
            ('host.name', 'keyword'),
            ('process.name', 'keyword'),
            ('cloud.service.name', 'keyword'),
            ('url.domain', 'keyword'),
            ('url.path', 'keyword'),
            ('http.request.method', 'keyword'),
            ('http.response.status_code', 'long'),
            ('threat.matched', 'keyword'),
            ('security.denied', 'keyword'),
            ('event.technique', 'keyword'),
            ('event.kill_chain_phase', 'keyword'),
            ('event.risk_score', 'long'),
            ('event.campaign', 'keyword'),
            ('event.campaign_name', 'keyword')
        ],
        'panels': [
            {'type': 'markdown', 'title': 'Header', 'w': 48, 'md': """## SIEM — Seguridad Unificada (FortiGate + Auth + Cloud Audit + WAF)
Eventos de 4 fuentes normalizados a ECS. Tráfico, intrusiones, autenticación, IAM y web attacks con geo-IP, threat-intel y MITRE ATT&CK."""},
            {'type': 'controls', 'title': 'Filtros', 'controls': [
                {'field': 'event.dataset', 'label': 'Fuente'}, {'field': 'event.action', 'label': 'Acción'},
                {'field': 'event.severity', 'label': 'Severidad'}, {'field': 'event.outcome', 'label': 'Resultado'}]},
            {'type': 'metric', 'title': 'Total de Eventos', 'agg': 'count'},
            {'type': 'metric', 'title': 'Eventos Bloqueados', 'agg': 'count', 'label': 'Eventos Bloqueados', 'query': 'security.denied:1'},
            {'type': 'metric', 'title': 'Hits de Threat Intel', 'agg': 'count', 'label': 'Hits de Threat Intel', 'query': 'threat.matched:true'},
            {'type': 'metric', 'title': 'IPs de Origen Únicas', 'agg': 'cardinality', 'field': 'source.ip', 'label': 'IPs de Origen Únicas'},
            {'type': 'area', 'title': 'Eventos en el tiempo por fuente', 'metric': 'count', 'split': 'event.dataset', 'w': 48},
            {'type': 'table', 'title': 'Top IPs Bloqueadas', 'field': 'source.ip', 'query': 'event.dataset:fortigate and security.denied:1'},
            {'type': 'bar', 'title': 'Top Firmas de Ataque', 'field': 'rule.name', 'horizontal': True, 'query': 'event.dataset:fortigate'},
            {'type': 'area', 'title': 'Logins en el tiempo (éxito vs fallo)', 'metric': 'count', 'split': 'event.outcome', 'query': 'event.dataset:auth'},
            {'type': 'table', 'title': 'Top Usuarios con Login Fallido', 'field': 'user.name', 'query': 'event.dataset:auth and event.outcome:failure'},
            {'type': 'bar', 'title': 'Tipos de Ataque WAF', 'field': 'rule.name', 'horizontal': True, 'query': 'event.dataset:waf'},
            {'type': 'bar', 'title': 'Top Acciones Cloud (CTS)', 'field': 'event.action', 'horizontal': True, 'query': 'event.dataset:cloudaudit'},
            {'type': 'line', 'title': 'Hits de Threat Intel en el tiempo', 'metric': 'count', 'query': 'threat.matched:true'},
            {'type': 'table', 'title': 'Top IPs Maliciosas', 'field': 'source.ip', 'query': 'threat.matched:true'},
            {'type': 'bar', 'title': 'Técnicas MITRE ATT&CK', 'field': 'event.technique', 'horizontal': True, 'query': 'event.technique:*'},
        ],
    },
}
