# Builder: Kafka (DMS) y Beats (VM) paso a paso

Cómo probar el **Builder** (traé tu propio log) con dos fuentes típicas de Huawei Cloud:
**DMS for Kafka** y **Filebeat en una ECS/VM**. La idea: conseguir 3 líneas de muestra de cada
fuente, pegarlas en el paso 1 del Builder, y ver el pipeline que arma.

> El Builder solo necesita **3 líneas de log**. Todo lo demás (instalar Filebeat, consumers) es
> para el envío real / end-to-end.

---

## Kafka — DMS for Kafka

DMS for Kafka es un servicio PaaS: podés **ver mensajes desde la consola** (Message Query), sin CLI.

### Ver mensajes existentes (consola)
1. DMS → tu instancia Kafka → **Message Query**.
2. Elegí el **Topic** y filtrá por partición + rango de tiempo (u offset).
3. Copiá el **body** de 3 mensajes → ese es tu sample.

### Generar mensajes de prueba (si el topic está vacío)
Para **producir** necesitás un cliente. Lo más cómodo: una ECS en la misma VPC que el DMS.

```bash
# Cliente Kafka (Ubuntu)
sudo apt-get update && sudo apt-get install -y default-jre
curl -L -O https://archive.apache.org/dist/kafka/2.7.0/kafka_2.13-2.7.0.tgz
tar xzf kafka_2.13-2.7.0.tgz && cd kafka_2.13-2.7.0

export DMS="IP1:9092,IP2:9092,IP3:9092"   # connection address del DMS (consola)

# Crear el topic (RF 1 siempre funciona; si el auto-create está ON, saltealo)
bin/kafka-topics.sh --create --topic pagos-eventos --bootstrap-server $DMS \
  --partitions 3 --replication-factor 1

# Producir (estas líneas también sirven de sample para el Builder)
cat <<'EOF' | bin/kafka-console-producer.sh --topic pagos-eventos --bootstrap-server $DMS
{"event_id":"evt-9f3a2b","ts":"2026-08-12T14:03:11.482Z","type":"PAYMENT_AUTHORIZED","account_id":"acc-100482","amount":15230.5,"currency":"ARS","channel":"APP","status":"OK","latency_ms":142}
{"event_id":"evt-9f3a2c","ts":"2026-08-12T14:03:12.113Z","type":"PAYMENT_DECLINED","account_id":"acc-100999","amount":8300.0,"currency":"ARS","channel":"WEB","status":"DECLINED","reason":"INSUFFICIENT_FUNDS","latency_ms":95}
{"event_id":"evt-9f3a2d","ts":"2026-08-12T14:03:12.640Z","type":"REFUND","account_id":"acc-100482","amount":15230.5,"currency":"ARS","channel":"APP","status":"OK","latency_ms":210}
EOF

# Verificar
bin/kafka-console-consumer.sh --topic pagos-eventos --bootstrap-server $DMS --from-beginning --max-messages 3
```

### En el Builder (paso 3, fuente = Kafka)
- `bootstrap_servers` = el connection address del DMS; `topic` = tu topic.
- **Codec**: con `json`, el mensaje llega ya parseado y el filtro NO debe re-parsear; con `plain`,
  el filtro abre con `json { source => "message" }`.

### DMS con autenticación (SASL_SSL, puertos 9093/9095)
En los comandos, agregá `--consumer.config client.properties` / `--producer.config`:

```properties
security.protocol=SASL_SSL
sasl.mechanism=PLAIN
sasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required username="USER" password="PASS";
ssl.truststore.location=/ruta/client.truststore.jks
ssl.truststore.password=TRUSTSTORE_PASS
```

Y en el Builder tildá **SASL_SSL (Kafka con autenticación)** y cargá mecanismo, credenciales y
truststore. El pipeline generado emite `security_protocol`, `sasl_mechanism`, `sasl_jaas_config`
(inline, sin archivo jaas aparte) y `ssl_truststore_*`.

---

## Beats — Filebeat en una VM/ECS

El input `beats` de Logstash decodifica el sobre de Filebeat y deja el log crudo en `message`, así
que lo que el Builder necesita es **la línea cruda** del archivo que Filebeat cosecha.

### Sacar el sample (sin instalar nada)
```bash
tail -n 3 /var/log/tu-app.log     # el path que pondrías en filebeat.inputs.paths — NO el registry
```

> ⚠️ El `registry` de Filebeat (`/var/lib/filebeat/registry/...`) es el estado interno de offsets,
> **no** log. No sirve como muestra.

Para ver además los campos que agrega Filebeat (`host`, `agent`, `log.file.path`):
```bash
filebeat -e -c /etc/filebeat/filebeat.yml -E 'output.logstash.enabled=false' \
  -E 'output.console.pretty=true' -once | head -40
```

### En el Builder (paso 3, fuente = Beats)
- Puerto **5044**.

### Envío real (end-to-end)
Instalá Filebeat en la VM y configurá `filebeat.yml`:
```yaml
filebeat.inputs:
  - type: log
    enabled: true
    paths:
      - /var/log/tu-app.log

output.logstash:
  hosts: ["<IP-DEL-LOGSTASH>:5044"]
```
Arrancá con `filebeat -e -c filebeat.yml`. **Red**: el security group del Logstash tiene que
permitir inbound **5044** desde la VM (idealmente misma VPC).

---

## Correr el Builder

En <http://localhost:8000> → **Crear pipeline** → toggle **"Builder (tu log → kit)"**. Una corrida
por fuente:

1. **Paso 1** — pegá las 3 líneas → **Siguiente** (el LLM arma el `filter{}` y detecta campos).
2. **Paso 2** — revisá el mapping.
3. **Paso 3** — elegí la fuente (Kafka / Beats) y sus datos de conexión; output OpenSearch.
4. **Paso 4** — **Exportar Starter Kit** (documento con todo) o Desplegar.
