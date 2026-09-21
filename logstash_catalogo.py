"""Qué plugins y qué patrones grok existen en la Logstash 7.10 de CSS.

Un `.conf` con un plugin que no está instalado, o con un `%{PATRON}` que no
existe, **no arranca**: Logstash falla al compilar la pipeline y la deja caída.
Terraform no se entera —para él la configuración se creó bien— y la app tampoco:
el cluster queda vivo, vacío y sin un solo error visible. De ahí que valga la
pena tener el catálogo acá y chequear contra él antes de gastar el deploy.

Las listas son las del bundle estándar de Logstash 7.10 (el que trae CSS),
recortadas a lo que puede aparecer en un `.conf` que generamos nosotros o que
edita un SA. Si falta algo que la Logstash de CSS sí tiene, agregarlo acá es un
renglón — y el error que da el lint dice exactamente eso.
"""

# ── Plugins ──────────────────────────────────────────────────────────────────
INPUTS = {
    "azure_event_hubs", "beats", "couchdb_changes", "dead_letter_queue",
    "elasticsearch", "exec", "file", "ganglia", "gelf", "generator", "graphite",
    "heartbeat", "http", "http_poller", "imap", "jdbc", "jms", "jmx", "kafka",
    "kinesis", "log4j", "lumberjack", "pipe", "rabbitmq", "redis", "s3",
    "salesforce", "snmp", "snmptrap", "sqs", "stdin", "syslog", "tcp",
    "twitter", "udp", "unix", "varnishlog", "websocket", "wmi", "xmpp",
}

FILTROS = {
    "aggregate", "alter", "cidr", "cipher", "clone", "csv", "date", "de_dot",
    "dissect", "dns", "drop", "elapsed", "elasticsearch", "environment",
    "extractnumbers", "fingerprint", "geoip", "grok", "http", "i18n",
    "jdbc_static", "jdbc_streaming", "json", "json_encode", "kv", "memcached",
    "metricize", "metrics", "mutate", "prune", "range", "ruby", "sleep",
    "split", "syslog_pri", "throttle", "tld", "translate", "truncate",
    "urldecode", "useragent", "uuid", "xml",
}

OUTPUTS = {
    "cloudwatch", "csv", "datadog", "datadog_metrics", "elastic_app_search",
    "elasticsearch", "email", "exec", "file", "ganglia", "gelf",
    "google_bigquery", "google_cloud_storage", "google_pubsub", "graphite",
    "graphtastic", "http", "influxdb", "irc", "juggernaut", "kafka", "librato",
    "loggly", "lumberjack", "metriccatcher", "mongodb", "nagios", "nagios_nsca",
    "opentsdb", "pagerduty", "pipe", "rabbitmq", "redis", "redmine", "riak",
    "riemann", "s3", "sns", "solr_http", "sqs", "statsd", "stdout", "stomp",
    "syslog", "tcp", "timber", "udp", "webhdfs", "websocket", "xmpp", "zabbix",
}

# Un codec se escribe como valor (`codec => plain`) o como bloque con opciones
# (`codec => multiline { … }`), y en ese segundo caso aparece como un bloque más.
CODECS = {
    "avro", "cef", "cloudfront", "cloudtrail", "collectd", "dots", "edn",
    "edn_lines", "es_bulk", "fluent", "graphite", "gzip_lines", "json",
    "json_lines", "line", "msgpack", "multiline", "netflow", "nmap", "plain",
    "protobuf", "rubydebug",
}

# Está en el bundle de Logstash pero NO en la Logstash de CSS: el deploy falla
# al crear la configuración. Se descubrió desplegando; si aparecen otros, van acá.
NO_EN_CSS = {"translate"}

POR_SECCION = {"input": INPUTS, "filter": FILTROS, "output": OUTPUTS}


# ── Patrones grok del core ───────────────────────────────────────────────────
# logstash-patterns-core 4.x: los archivos que un `.conf` nuestro puede llegar a
# usar (grok-patterns, linux-syslog, httpd, java, ruby). No están los de
# aparatos específicos (Cisco, Exim, Bacula…): si alguno hace falta, se agrega.
GROK_CORE = {
    # grok-patterns
    "USERNAME", "USER", "EMAILLOCALPART", "EMAILADDRESS", "INT", "BASE10NUM",
    "NUMBER", "BASE16NUM", "BASE16FLOAT", "POSINT", "NONNEGINT", "WORD",
    "NOTSPACE", "SPACE", "DATA", "GREEDYDATA", "QUOTEDSTRING", "QS", "UUID",
    "URN", "MAC", "CISCOMAC", "WINDOWSMAC", "COMMONMAC", "IPV6", "IPV4", "IP",
    "HOSTNAME", "IPORHOST", "HOSTPORT", "PATH", "UNIXPATH", "TTY", "WINPATH",
    "URIPROTO", "URIHOST", "URIPATH", "URIPARAM", "URIPATHPARAM", "URI",
    "MONTH", "MONTHNUM", "MONTHNUM2", "MONTHDAY", "DAY", "YEAR", "HOUR",
    "MINUTE", "SECOND", "TIME", "DATE_US", "DATE_EU", "ISO8601_TIMEZONE",
    "ISO8601_SECOND", "TIMESTAMP_ISO8601", "DATE", "DATESTAMP", "TZ",
    "DATESTAMP_RFC822", "DATESTAMP_RFC2822", "DATESTAMP_OTHER",
    "DATESTAMP_EVENTLOG", "HTTPDERROR_DATE", "SYSLOGTIMESTAMP", "PROG", "PID",
    "SYSLOGPROG", "SYSLOGHOST", "SYSLOGFACILITY", "HTTPDATE", "QUOTEDSTRING",
    "LOGLEVEL", "UNIXTIME",
    # linux-syslog
    "SYSLOG5424PRINTASCII", "SYSLOGBASE2", "SYSLOGPAMSESSION", "CRON_ACTION",
    "CRONLOG", "SYSLOGLINE", "SYSLOG5424PRI", "SYSLOG5424SD", "SYSLOG5424BASE",
    "SYSLOG5424LINE", "SYSLOGBASE",
    # httpd
    "HTTPDUSER", "COMMONAPACHELOG", "COMBINEDAPACHELOG", "HTTPD20_ERRORLOG",
    "HTTPD24_ERRORLOG", "HTTPD_ERRORLOG", "HTTPD_COMMONLOG",
    "HTTPD_COMBINEDLOG",
    # java / ruby
    "JAVACLASS", "JAVAFILE", "JAVAMETHOD", "JAVASTACKTRACEPART",
    "JAVATHREAD", "JAVALOGMESSAGE", "CATALINA_DATESTAMP", "CATALINALOG",
    "TOMCAT_DATESTAMP", "TOMCATLOG", "RUBY_LOGLEVEL", "RUBY_LOGGER",
}
