"""
plugin_rag.py
==============

Extrae información de plugins de Logstash (input, filter, output, codec)
del archivo de documentación para usar como RAG al generar configuración.

También incluye ejemplos de las user guides de CSS (Huawei Cloud Search Service)
para generar configuraciones más completas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path


@dataclass
class PluginInfo:
    """Información de un plugin de Logstash."""

    name: str
    plugin_type: str
    description: str
    options: list[str]
    example: str | None = None


class PluginRAG:
    """Extrae y busca información de plugins de Logstash + CSS examples."""

    def __init__(self, docs_path: str | Path | None = None):
        if docs_path is None:
            docs_path = Path(__file__).parent.parent / "logstash" / "docs" / "logstash_7.10_completo.txt"
        self.docs_path = Path(docs_path)
        self.plugins: dict[str, list[PluginInfo]] = {
            "input": [],
            "filter": [],
            "output": [],
            "codec": [],
        }
        self.css_examples: list[str] = []
        self._loaded = False

    def load(self) -> None:
        """Carga la información de plugins desde la documentación."""
        if self._loaded:
            return

        try:
            content = self.docs_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            content = ""

        self._extract_plugins(content)
        self._load_css_examples()
        self._loaded = True

    def _load_css_examples(self) -> None:
        """Carga ejemplos de las user guides de CSS."""
        base_path = Path(__file__).parent.parent
        
        css_examples = []
        
        css_example_1 = """
EJEMPLO CSS - Pipeline completo para logs financieros con OBS:

input {
  s3 {
    bucket => "financial-logs-bucket"
    prefix => "transactions/"
    region => "la-south-2"
    endpoint => "https://obs.la-south-2.myhuaweicloud.com"
    access_key_id => "${OBS_ACCESS_KEY}"
    secret_access_key => "${OBS_SECRET_KEY}"
    codec => "json_lines"
    interval => 300
  }
}

filter {
  grok {
    match => { "message" => "%{TIMESTAMP_ISO8601:timestamp} %{IP:source_ip} user=%{WORD:user} txn_id=%{WORD:txn_id} amount=%{NUMBER:amount} status=%{NUMBER:status}" }
  }
  
  date {
    match => ["timestamp", "ISO8601"]
    target => "@timestamp"
  }
  
  mutate {
    convert => { "amount" => "float" }
    convert => { "status" => "integer" }
    rename => { "source_ip" => "[source][ip]" }
    rename => { "user" => "[user][name]" }
    rename => { "txn_id" => "[transaction][id]" }
    rename => { "amount" => "[transaction][amount]" }
  }
}

output {
  elasticsearch {
    hosts => ["https://css-cluster.la-south-2.myhuaweicloud.com:9200"]
    index => "financial-transactions-%{+YYYY.MM}"
    user => "${CSS_USER}"
    password => "${CSS_PASSWORD}"
    ssl => true
    cacert => "/path/to/css-ca.crt"
  }
}
"""
        css_examples.append(css_example_1)
        
        css_example_2 = """
EJEMPLO CSS - Index mapping para logs financieros:

PUT /financial-transactions
{
  "settings": {
    "index": {
      "number_of_shards": 3,
      "number_of_replicas": 1
    }
  },
  "mappings": {
    "properties": {
      "@timestamp": { "type": "date" },
      "source": {
        "properties": {
          "ip": { "type": "ip" }
        }
      },
      "user": {
        "properties": {
          "name": { "type": "keyword" }
        }
      },
      "transaction": {
        "properties": {
          "id": { "type": "keyword" },
          "amount": { "type": "float" }
        }
      },
      "status": { "type": "integer" }
    }
  }
}
"""
        css_examples.append(css_example_2)
        
        css_example_3 = """
EJEMPLO CSS - Pipeline con particionado por fecha en OBS:

output {
  s3 {
    bucket => "archive-bucket"
    prefix => "transactions/year=%{+YYYY}/month=%{+MM}/day=%{+dd}/"
    region => "la-south-2"
    endpoint => "https://obs.la-south-2.myhuaweicloud.com"
    access_key_id => "${OBS_ACCESS_KEY}"
    secret_access_key => "${OBS_SECRET_KEY}"
    codec => "json_lines"
    encoding => "gzip"
    time_file => 1
    size_file => 100
  }
}
"""
        css_examples.append(css_example_3)
        
        self.css_examples = css_examples

    def _extract_plugins(self, content: str) -> None:
        """Extrae información de plugins del contenido."""
        self.plugins["input"] = self._extract_input_plugins(content)
        self.plugins["filter"] = self._extract_filter_plugins(content)
        self.plugins["output"] = self._extract_output_plugins(content)
        self.plugins["codec"] = self._extract_codec_plugins(content)

    def _extract_input_plugins(self, content: str) -> list[PluginInfo]:
        """Extrae plugins de input (solo los disponibles en el frontend)."""
        plugins = []
        input_plugins = [
            ("s3", "OBS", "Huawei Cloud Object Storage. Opciones: bucket, prefix, region, endpoint, access_key_id, secret_access_key, codec, interval, delete."),
            ("beats", "Beats", "Filebeat / Metricbeat / Heartbeat / Packetbeat. Opciones: port, host, ssl, ssl_certificate, ssl_key."),
            ("file", "File", "Archivos locales. Opciones: path, start_position (beginning/end), sincedb_path, mode, codec."),
            ("kafka", "Kafka", "Apache Kafka consumer. Opciones: bootstrap_servers, topics, group_id, consumer_threads, auto_offset_reset (earliest/latest)."),
            ("http", "HTTP", "Endpoint REST para recibir datos. Opciones: port, host, path, ssl, codec."),
            ("jdbc", "JDBC", "Query periódica a base de datos SQL. Opciones: jdbc_driver_library, jdbc_driver_class, jdbc_connection_string, jdbc_user, jdbc_password, schedule, statement."),
        ]
        for plugin_id, name, desc in input_plugins:
            plugins.append(PluginInfo(
                name=name,
                plugin_type="input",
                description=desc,
                options=self._extract_options(desc),
                example=self._get_input_example(plugin_id),
            ))
        return plugins

    def _extract_filter_plugins(self, content: str) -> list[PluginInfo]:
        """Extrae plugins de filter."""
        plugins = []
        filter_plugins = [
            ("grok", "Grok", "Parsea logs con patrones. Opciones: match, patterns_dir, patterns_definitions, overwrite, remove_field, tag_on_failure."),
            ("mutate", "Mutate", "Transforma campos. Opciones: rename, add_field, remove_field, replace, copy, convert, uppercase, lowercase, strip, split, join."),
            ("date", "Date", "Parsea fechas a @timestamp. Opciones: match, target, timezone, locale."),
            ("json", "JSON", "Parsea campos JSON. Opciones: source, target, skip_on_invalid_json."),
            ("kv", "Key-Value", "Parsea pares clave=valor. Opciones: source, target, field_split, value_split, trim_key, trim_value."),
            ("geoip", "GeoIP", "Agrega geolocalización por IP. Opciones: source, target, database, fields."),
            ("csv", "CSV", "Parsea datos CSV. Opciones: source, target, columns, separator, quote_char."),
            ("dissect", "Dissect", "Parsea con patrones simples. Opciones: mapping, datatype, convert_datatype."),
            ("drop", "Drop", "Descarta eventos. Opciones: percentage."),
            ("fingerprint", "Fingerprint", "Genera hash de campos. Opciones: source, target, method, concatenate_sources."),
            ("ruby", "Ruby", "Ejecuta código Ruby arbitrario. Opciones: code, init."),
            ("useragent", "User Agent", "Parsea user-agent HTTP. Opciones: source, target."),
        ]
        for plugin_id, name, desc in filter_plugins:
            plugins.append(PluginInfo(
                name=name,
                plugin_type="filter",
                description=desc,
                options=self._extract_options(desc),
                example=self._get_filter_example(plugin_id),
            ))
        return plugins

    def _extract_output_plugins(self, content: str) -> list[PluginInfo]:
        """Extrae plugins de output (solo los disponibles en el frontend)."""
        plugins = []
        output_plugins = [
            ("elasticsearch", "OpenSearch / Elasticsearch", "Huawei Cloud CSS / OpenSearch cluster. Opciones: hosts, index, user, password, action (index/create/update/delete), document_id, pipeline."),
            ("s3", "OBS", "Huawei Cloud Object Storage. Opciones: bucket, prefix, region, endpoint, access_key_id, secret_access_key, codec, encoding (gzip/none), time_file, size_file."),
            ("stdout", "Stdout", "Salida a consola para debugging. Opciones: codec (rubydebug/json/line)."),
            ("kafka", "Kafka", "Apache Kafka producer. Opciones: bootstrap_servers, topic_id, compression_type (none/gzip/snappy), key, message_key."),
            ("mongodb", "MongoDB", "Documentos NoSQL. Opciones: uri, database, collection."),
            ("postgresql", "PostgreSQL", "Base de datos relacional. Opciones: connection_string, driver_jar_path, user, password, statement."),
        ]
        for plugin_id, name, desc in output_plugins:
            plugins.append(PluginInfo(
                name=name,
                plugin_type="output",
                description=desc,
                options=self._extract_options(desc),
                example=self._get_output_example(plugin_id),
            ))
        return plugins

    def _extract_codec_plugins(self, content: str) -> list[PluginInfo]:
        """Extrae plugins de codec."""
        plugins = []
        codec_plugins = [
            ("json", "JSON", "Codifica/decodifica JSON. Opciones: charset."),
            ("json_lines", "JSON Lines", "JSON delimitado por líneas. Opciones: charset."),
            ("plain", "Plain", "Texto plano sin procesar. Opciones: charset."),
            ("line", "Line", "Una línea por evento. Opciones: charset, delimiter."),
            ("multiline", "Multiline", "Agrupa líneas múltiples. Opciones: pattern, what, negate, max_lines, max_bytes."),
            ("cef", "CEF", "ArcSight Common Event Format. Opciones: delimiter, fields."),
        ]
        for plugin_id, name, desc in codec_plugins:
            plugins.append(PluginInfo(
                name=name,
                plugin_type="codec",
                description=desc,
                options=self._extract_options(desc),
            ))
        return plugins

    def _extract_options(self, desc: str) -> list[str]:
        """Extrae opciones de la descripción."""
        match = re.search(r"Opciones?:\s*(.+?)\.$", desc)
        if match:
            return [o.strip() for o in match.group(1).split(",")]
        return []

    def _get_input_example(self, plugin_id: str) -> str | None:
        """Devuelve ejemplo de input."""
        examples = {
            "s3": '''s3 {
  bucket => "my-bucket"
  prefix => "logs/"
  region => "la-south-2"
  endpoint => "https://obs.la-south-2.myhuaweicloud.com"
  access_key_id => "YOUR_ACCESS_KEY"
  secret_access_key => "YOUR_SECRET_KEY"
  codec => "json"
}''',
            "kafka": '''kafka {
  bootstrap_servers => "localhost:9092"
  topics => ["logs-topic"]
  group_id => "logstash-consumer"
}''',
            "file": '''file {
  path => "/var/log/application.log"
  start_position => "beginning"
  sincedb_path => "/dev/null"
}''',
            "beats": '''beats {
  port => 5044
}''',
        }
        return examples.get(plugin_id)

    def _get_filter_example(self, plugin_id: str) -> str | None:
        """Devuelve ejemplo de filter."""
        examples = {
            "grok": '''grok {
  match => { "message" => "%{COMBINEDAPACHELOG}" }
}''',
            "mutate": '''mutate {
  rename => { "old_field" => "new_field" }
  remove_field => ["unnecessary_field"]
}''',
            "date": '''date {
  match => ["timestamp", "ISO8601"]
  target => "@timestamp"
}''',
            "json": '''json {
  source => "message"
  target => "parsed"
}''',
        }
        return examples.get(plugin_id)

    def _get_output_example(self, plugin_id: str) -> str | None:
        """Devuelve ejemplo de output."""
        examples = {
            "elasticsearch": '''elasticsearch {
  hosts => ["https://localhost:9200"]
  index => "logs-%{+YYYY.MM}"
  user => "admin"
  password => "secret"
}''',
            "s3": '''s3 {
  bucket => "output-bucket"
  prefix => "processed/"
  region => "la-south-2"
  access_key_id => "YOUR_ACCESS_KEY"
  secret_access_key => "YOUR_SECRET_KEY"
  codec => "json_lines"
}''',
            "kafka": '''kafka {
  bootstrap_servers => "localhost:9092"
  topic_id => "output-topic"
}''',
        }
        return examples.get(plugin_id)

    def search(self, query: str, plugin_type: str | None = None, top_k: int = 5) -> list[tuple[PluginInfo, float]]:
        """Busca plugins relevantes para una query."""
        if not self._loaded:
            self.load()

        query_lower = query.lower()
        query_words = set(re.findall(r"\w+", query_lower))
        scored = []

        for ptype, plugin_list in self.plugins.items():
            if plugin_type and ptype != plugin_type:
                continue
            for plugin in plugin_list:
                score = 0.0
                name_lower = plugin.name.lower()
                desc_lower = plugin.description.lower()

                if query_lower in name_lower:
                    score += 2.0
                if query_lower in desc_lower:
                    score += 1.0

                for word in query_words:
                    if word in name_lower:
                        score += 0.5
                    if word in desc_lower:
                        score += 0.3
                    for opt in plugin.options:
                        if word in opt.lower():
                            score += 0.2

                if score > 0:
                    scored.append((plugin, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def get_context_for_llm(self, query: str) -> str:
        """Genera contexto de plugins relevantes para que el LLM escriba el
        bloque filter.

        Ojo: el caller es `generate_logstash_filter` — el modelo solo escribe
        el `filter { }`, así que pre-filtramos por `plugin_type='filter'` y
        bajamos `top_k` a 5. Antes traíamos top 10 sobre todos los tipos
        (input + filter + output + codec), lo que inflaba el contexto a
        ~4 KB con plugins irrelevantes y empujaba al modelo al timeout.
        """
        if not self._loaded:
            self.load()

        results = self.search(query, plugin_type="filter", top_k=5)

        parts = []
        for plugin_info in (r[0] for r in results):
            part = f"[FILTER] {plugin_info.name}:\n{plugin_info.description}"
            if plugin_info.example:
                part += f"\n\nEjemplo:\n{plugin_info.example}"
            parts.append(part)

        if not parts:
            return ""
        return "\n\n---\n\n".join(parts)


_rag: PluginRAG | None = None


def get_plugin_rag() -> PluginRAG:
    """Singleton del RAG de plugins."""
    global _rag
    if _rag is None:
        _rag = PluginRAG()
        _rag.load()
    return _rag


@cache
def _load_context_file() -> str:
    """Carga y cachea el archivo de contexto."""
    context_file = Path(__file__).parent / "docs" / "logstash-llm-context.md"
    if context_file.exists():
        try:
            return context_file.read_text(encoding="utf-8")
        except Exception:
            pass
    return ""


def get_plugin_context(query: str) -> str:
    """Obtiene contexto de plugins para una query."""
    rag_context = get_plugin_rag().get_context_for_llm(query)
    file_context = _load_context_file()
    
    if file_context and rag_context:
        return f"{file_context}\n\n---\n\n{rag_context}"
    return file_context or rag_context
