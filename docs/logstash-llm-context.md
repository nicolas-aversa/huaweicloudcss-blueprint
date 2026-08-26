# CONTEXTO PARA GENERACIÓN DE CONFIGURACIÓN LOGSTASH

## INSTRUCCIONES PRINCIPALES

Cuando el usuario proporcione logs, debes generar una configuración completa de Logstash que:

1. **Analice la estructura del log** → identificar formato, delimitadores, campos
2. **Seleccione filtros apropiados** → grok, json, kv, csv, dissect
3. **Genere patterns correctos** → usar built-in patterns cuando sea posible
4. **Configure timestamp parsing** → siempre incluir date filter
5. **Limpie campos innecesarios** → usar mutate filter
6. **Configure output correcto** → index dinámico, SSL, autenticación

**Output obligatorio:**
- Configuración input completa
- Configuración filter completa con comentarios
- Configuración output completa
- Lista de campos searchables resultantes

---

## CONFIGURACIÓN VALIDADA EN CSS (HUAWEI CLOUD)

### Input S3/OBS - CONFIGURACIÓN QUE FUNCIONA
```ruby
input {
  s3 {
    access_key_id => "TU_AK"                    # OBLIGATORIO
    secret_access_key => "TU_SK"                 # OBLIGATORIO
    bucket => "nombre-de-tu-bucket"             # OBLIGATORIO
    region => "la-south-2"                      # OBLIGATORIO
    endpoint => "https://obs.la-south-2.myhuaweicloud.com"  # OBLIGATORIO
    prefix => "logs/"                           # Opcional: carpeta dentro del bucket
    interval => 60                              # Opcional: segundos entre checks
    codec => plain                              # Opcional: plain, json, line
    temporary_directory => "/opt/data/tmp/"     # Opcional: dir temporal
    delete => true                              # Opcional: borrar después de procesar
    watch_for_new_files => true                 # Opcional: monitorear archivos nuevos
  }
}
```

**IMPORTANTE:**
- Usar `access_key_id` (NO `access_key`)
- Usar `secret_access_key` (NO `secret_key`)
- El plugin es `s3` (OBS es compatible con S3)
- `delete => true` evita duplicados
- `temporary_directory` es necesario en CSS

### Output Elasticsearch/OpenSearch - CONFIGURACIÓN QUE FUNCIONA
```ruby
output {
  elasticsearch {
    hosts => ["192.168.0.119:9200"]             # SIN protocolo cuando se usa cacert
    index => "logs-%{+YYYY.MM.dd}"              # Index dinámico por fecha
    action => "index"                           # Acción de indexación
    user => "admin"                             # Usuario del cluster
    password => "tu_password"                   # Password del cluster
    ssl => true                                 # Habilitar SSL
    cacert => "/rds/datastore/logstash/v7.10.0/package/logstash-7.10.0/extend/certs"  # Ruta certificados
    manage_template => false                    # DESHABILITAR para CSS
    ilm_enabled => false                        # DESHABILITAR para CSS
  }
}
```

**IMPORTANTE:**
- `hosts` SIN protocolo (https://) cuando se usa `cacert`
- `cacert` apunta al DIRECTORIO de certificados, no a un archivo específico
- Ruta de certificados en CSS: `/rds/datastore/logstash/v7.10.0/package/logstash-7.10.0/extend/certs`
- `manage_template => false` es OBLIGATORIO para CSS
- `ilm_enabled => false` es OBLIGATORIO para CSS
- Si NO se tiene certificado, usar `ssl_certificate_verification => false`

---

## INPUT PLUGINS

### OBS (Object Storage Service) - S3 Compatible
```ruby
input {
  s3 {
    access_key_id => "TU_AK"
    secret_access_key => "TU_SK"
    bucket => "nombre-bucket"
    region => "la-south-2"
    endpoint => "https://obs.la-south-2.myhuaweicloud.com"
    prefix => "logs/"
    interval => 60
    codec => plain
    temporary_directory => "/opt/data/tmp/"
    delete => true
    watch_for_new_files => true
  }
}
```

### BEATS (Filebeat, Metricbeat)
```ruby
input {
  beats {
    port => 5044
    host => "0.0.0.0"
    
    # Para logs multiline (stack traces)
    codec => multiline {
      pattern => "^%{TIMESTAMP_ISO8601}"
      negate => true
      what => "previous"
    }
  }
}
```

### KAFKA
```ruby
input {
  kafka {
    bootstrap_servers => "kafka1:9092,kafka2:9092"
    topics => ["app-logs", "error-logs"]
    group_id => "logstash-consumer"
    auto_offset_reset => "earliest"
    consumer_threads => 3
    codec => "json"
  }
}
```

### JDBC (MySQL, MariaDB, PostgreSQL)
```ruby
input {
  jdbc {
    jdbc_driver_library => "/path/to/driver.jar"
    jdbc_driver_class => "org.mariadb.jdbc.Driver"
    jdbc_connection_string => "jdbc:mariadb://db-server:3306/database"
    jdbc_user => "user"
    jdbc_password => "password"
    statement => "SELECT * FROM logs WHERE id > :sql_last_value ORDER BY id"
    use_column_value => true
    tracking_column => "id"
    tracking_column_type => "numeric"
    schedule => "* * * * *"
  }
}
```

**Drivers:**
- MariaDB: `org.mariadb.jdbc.Driver`
- MySQL: `com.mysql.cj.jdbc.Driver`
- PostgreSQL: `org.postgresql.Driver`

---

## GROK PATTERNS - REFERENCIA

### Timestamps
```
%{TIMESTAMP_ISO8601}      # 2024-01-15T10:30:45.123Z o 2024-01-15 10:30:45
%{DATE}                   # 2024-01-15
%{TIME}                   # 10:30:45
%{HTTPDATE}               # 15/Jan/2024:10:30:45 +0000
%{SYSLOGTIMESTAMP}        # Jan 15 10:30:45
```

### Red
```
%{IP}                     # 192.168.1.1
%{IPV4}                   # 192.168.1.1
%{HOSTNAME}               # server01.example.com
```

### Identificadores
```
%{UUID}                   # 550e8400-e29b-41d4-a716-446655440000
%{NUMBER}                 # 123
%{INT}                    # -123
%{POSINT}                 # 123
%{BASE10NUM}              # 123.45
```

### URLs
```
%{URI}                    # http://example.com/path?query=value
%{URIPATH}                # /path/to/resource
%{URIPATHPARAM}           # /path?query=value
```

### HTTP
```
%{HTTPDATE}               # 15/Jan/2024:10:30:45 +0000
%{HTTPMETHOD}             # GET, POST, PUT, DELETE
%{HTTPVERSION}            # HTTP/1.1
```

### Log levels
```
%{LOGLEVEL}               # ERROR, WARN, INFO, DEBUG, TRACE, FATAL
```

### Java
```
%{JAVACLASS}              # com.example.MyClass
%{JAVAFILE}               # MyClass.java
```

### Datos generales
```
%{WORD}                   # palabra
%{DATA}                   # datos cortos
%{GREEDYDATA}             # todo el resto
%{QUOTEDSTRING}           # "texto" o 'texto'
%{EMAILADDRESS}           # user@example.com
```

---

## FILTROS

### DATE Filter
```ruby
filter {
  date {
    match => ["timestamp", "yyyy-MM-dd HH:mm:ss", "ISO8601"]
    target => "@timestamp"
    timezone => "UTC"
    remove_field => ["timestamp"]
  }
}
```

**Formatos comunes:**
```ruby
match => ["timestamp", "ISO8601"]
match => ["timestamp", "yyyy-MM-dd HH:mm:ss"]
match => ["timestamp", "yyyy-MM-dd HH:mm:ss.SSS"]
match => ["timestamp", "dd/MMM/yyyy:HH:mm:ss Z"]  # Apache
```

### JSON Filter
```ruby
filter {
  json {
    source => "message"
    remove_field => ["message"]
  }
}
```

### KV (Key-Value) Filter
```ruby
filter {
  kv {
    source => "message"
    field_split => " "
    value_split => "="
  }
}
```

### DISSECT Filter (más eficiente que grok)
```ruby
filter {
  dissect {
    mapping => {
      "message" => "%{timestamp} %{kv_data}"
    }
  }
}
```

### MUTATE Filter
```ruby
filter {
  mutate {
    rename => {
      "host" => "[host][name]"
      "level" => "[log][level]"
      "user" => "[user][name]"
    }
    convert => {
      "amount" => "float"
      "status_code" => "integer"
    }
    remove_field => ["message", "@version"]
  }
}
```

---

## EJEMPLO COMPLETO VALIDADO

### Log de entrada:
```
2024-01-15T10:30:45.123Z host=server01 level=INFO user=admin action=login src_ip=192.168.1.50 dst_ip=10.0.0.1 txn_id=abc123 amount=1500.00 currency=USD card_bin=411111 mcc=5812 auth_code=OK123 resp=200 latency_ms=45 msg=Transaction processed
```

### Configuración generada:
```ruby
input {
  s3 {
    access_key_id => "TU_AK"
    secret_access_key => "TU_SK"
    bucket => "origen"
    region => "la-south-2"
    endpoint => "https://obs.la-south-2.myhuaweicloud.com"
    prefix => "logs/"
    interval => 60
    codec => plain
    temporary_directory => "/opt/data/tmp/"
    delete => true
  }
}

filter {
  dissect {
    mapping => {
      "message" => "%{timestamp} %{kv_data}"
    }
  }

  kv {
    source => "kv_data"
    field_split => " "
    value_split => "="
  }

  date {
    match => ["timestamp", "ISO8601"]
    target => "@timestamp"
    remove_field => ["timestamp"]
  }

  mutate {
    rename => {
      "host" => "[host][name]"
      "level" => "[log][level]"
      "user" => "[user][name]"
      "action" => "[event][action]"
      "src_ip" => "[source][ip]"
      "dst_ip" => "[destination][ip]"
      "txn_id" => "[transaction][id]"
      "amount" => "[transaction][amount]"
      "currency" => "[transaction][currency]"
      "card_bin" => "[transaction][card_bin]"
      "mcc" => "[transaction][mcc]"
      "auth_code" => "[transaction][auth_code]"
      "resp" => "[http][response][status_code]"
      "latency_ms" => "[event][duration]"
      "msg" => "[event][original]"
    }
    convert => {
      "[transaction][amount]" => "float"
      "[http][response][status_code]" => "integer"
      "[event][duration]" => "integer"
      "[transaction][mcc]" => "integer"
    }
    remove_field => ["message", "kv_data"]
  }
}

output {
  elasticsearch {
    hosts => ["192.168.0.119:9200"]
    index => "logs-%{+YYYY.MM.dd}"
    action => "index"
    manage_template => false
    ilm_enabled => false
    user => "admin"
    password => "tu_password"
    ssl => true
    cacert => "/rds/datastore/logstash/v7.10.0/package/logstash-7.10.0/extend/certs"
  }
}
```

### Resultado en OpenSearch:
```json
{
  "_source": {
    "destination": { "ip": "10.0.0.1" },
    "user": { "name": "admin" },
    "http": { "response": { "status_code": 200 } },
    "host": { "name": "server01" },
    "transaction": {
      "card_bin": "411111",
      "id": "abc123",
      "mcc": 5812,
      "amount": 1500,
      "auth_code": "OK123",
      "currency": "USD"
    },
    "@timestamp": "2024-01-15T10:30:45.123Z",
    "event": {
      "action": "login",
      "duration": 45,
      "original": "Transaction processed"
    },
    "log": { "level": "INFO" },
    "source": { "ip": "192.168.1.50" }
  }
}
```

**Campos searchables:**
- `user.name`
- `transaction.amount`
- `transaction.id`
- `source.ip`
- `destination.ip`
- `log.level`
- `http.response.status_code`
- `event.action`
- `event.duration`
- `@timestamp`

---

## MEJORES PRÁCTICAS

1. **Siempre usar date filter** para que @timestamp sea el timestamp del log
2. **Usar dissect** en lugar de grok cuando sea posible (más eficiente)
3. **Remover campos innecesarios** para ahorrar espacio
4. **Usar ECS field names** para compatibilidad (user.name, source.ip, etc.)
5. **Convertir tipos** explícitamente (integer, float)
6. **Para CSS:** siempre usar `manage_template => false` e `ilm_enabled => false`
7. **Para HTTPS:** usar cacert apuntando al directorio de certificados
8. **Para OBS:** usar `delete => true` para evitar duplicados

---

## ALGORITMO DE GENERACIÓN

**Cuando recibas logs del usuario:**

1. **Detectar formato:**
   - Si empieza con `{` → JSON
   - Si tiene `=` → key=value (usar dissect + kv)
   - Si tiene comas → CSV
   - Si tiene estructura → grok

2. **Identificar campos:**
   - Timestamp (obligatorio)
   - Log level
   - User/session
   - IPs
   - Transaction data

3. **Generar filter:**
   - dissect para separar timestamp del resto
   - kv para parsear key=value
   - date para timestamp
   - mutate para renombrar a ECS

4. **Generar output:**
   - hosts SIN protocolo
   - ssl => true
   - cacert => ruta directorio
   - manage_template => false
   - ilm_enabled => false

---

## REFERENCIA DE PRODUCCIÓN — PIPELINE NAMESPACED (CAPA 1 + CAPA 2)

Este es el pipeline gold real de producción (dominio financiero, campos bajo
`transaction.*`). Es la **referencia de estilo canónica**: namespacing con
`target => "transaction"`, grok envelope, `date` con patrón compacto
`yyyyMMddHHmmssSSS`, `document_id` por `fingerprint`, output `elasticsearch` + `s3`.
Fuente: `docs/reference_pipeline.conf`.

**IMPORTANTE — qué genera el wizard (capa 1) vs qué NO (capa 2):**
- El generador del wizard produce SOLO la **capa 1** (baseline indexable): input +
  grok envelope opcional + `kv ... target => "<namespace>"` + `date => @timestamp` +
  `mutate { remove_field => [...] }` de limpieza. Tipado conservador (solo medidas
  agregables a numérico; el resto queda keyword).
- NO emitas en el baseline los bloques de **capa 2** (reglas de negocio): el `ruby`
  de whitelist/prune, la normalización de `"null"`→null, la clave compuesta `txn_key`,
  el `funnel` de steps, `trxl_geo` (geo_point) ni el sub-parsing extra de
  `trxl_tech_detail`/`trxl_detail`. Esos se construyen conversando con el **copiloto**
  sobre el negocio — son referencia de estilo, NO parte del baseline.
- El **tipado** de los campos vive en el index template de OpenSearch
  (`docs/reference_index_template.json`), NO en `mutate convert` (salvo una medida
  puntual que una regla de negocio necesite).

```ruby
input {
  s3 {
    access_key_id       => "${OBS_AK}"
    secret_access_key   => "${OBS_SK}"
    bucket              => "bucket-origen"
    endpoint            => "https://obs.la-south-2.myhuaweicloud.com"
    region              => "la-south-2"
    prefix              => "loggg/"
    interval            => 300
    codec               => plain { charset => "ISO-8859-1" }
    temporary_directory => "/opt/data/tmp/"
    watch_for_new_files => true
    delete            => true
    backup_to_bucket  => "bucket-origen"
    backup_add_prefix => "backup/"
  }
}

filter {
  if "table=" not in [message] { drop {} }

  grok {
    match => {
      "message" => "^\[%{WORD:log_status}\]\s+%{NOTSPACE:raw_timestamp}\s+-\s+%{DATA:thread_info}\s+\[%{DATA:component}\]\s+%{GREEDYDATA:kv_payload}"
    }
    remove_field   => ["message"]
    tag_on_failure => ["_grokparsefailure"]
  }

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
    trim_value  => "\s"
    trim_key    => "\s"
    target      => "transaction"
  }

  if [transaction][trxl_entry_tim] {
    date {
      match    => ["[transaction][trxl_entry_tim]", "yyyyMMddHHmmssSSS"]
      target   => "@timestamp"
      timezone => "America/Argentina/Buenos_Aires"
    }
  }

  # ── CAPA 2 (referencia — la arma el copiloto, NO el baseline) ──────────────
  # ruby { init/code => whitelist + prune + nulls + txn_key }
  # json  { source => "[transaction][trxl_tech_detail]" target => idem skip_on_invalid_json => true }
  # kv    { source => "[transaction][trxl_detail]" field_split => "~" target => idem }
  # ruby  { ... funnel de steps -> [transaction][funnel] }
  # ruby  { ... GEO_LAT/GEO_LONG -> [transaction][trxl_geo] (geo_point) }

  mutate {
    remove_field => [
      "kv_payload", "raw_timestamp", "thread_info",
      "log_status", "component", "@version", "[tags]"
    ]
  }
}

output {
  elasticsearch {
    hosts           => []
    index           => "logs-hoje-%{+YYYY.MM.dd}"
    action          => "index"
    document_id     => "%{[@metadata][generated_id]}"
    manage_template => false
    ilm_enabled     => false
    user            => "admin"
    password        => "${PASSWORD}"
  }
}
```
