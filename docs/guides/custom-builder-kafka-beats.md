# Builder: Kafka (DMS) y Beats (VM) paso a paso

Cómo probar el **Builder** (traé tu propio log) con dos fuentes típicas de Huawei Cloud:
**DMS for Kafka** y **Filebeat en una ECS/VM**. La idea: conseguir 3 líneas de muestra de cada
fuente, pegarlas en el paso 1 del Builder, y ver el pipeline que arma.

> Para **detectar el schema** el Builder solo necesita **3 líneas de log**. Todo lo demás
> (instalar Filebeat, consumers) es para el envío real / end-to-end.
>
> Si en vez de conectar la fuente en vivo querés un caso de demo **repetible**, subí el archivo
> `.log` completo en el paso 1: se guarda con el caso y se sube a tu bucket OBS, así lo desplegás
> las veces que quieras sin depender de que el Kafka/Filebeat del cliente esté levantado.

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
Arrancá con `filebeat -e -c filebeat.yml`.

**Red — lo hace el deploy.** Cuando un caso usa el input `beats`, Terraform abre el camino solo:
crea la regla de security group para ese puerto y un **DNAT** sobre el NAT/EIP apuntando al NIC del
Logstash. Antes había que hacerlo a mano y no estaba documentado que faltaba.

- El `<IP-DEL-LOGSTASH>` del `filebeat.yml` es la **EIP del NAT** (la misma por la que entrás a
  Dashboards), no la IP privada del cluster.
- Por defecto se acepta desde `0.0.0.0/0`. Si sabés la IP pública del cliente, acotala con la
  variable `beats_source_cidr` de Terraform.
- Si ningún caso usa `beats`, **no se abre ningún puerto**.

---

## Correr el Builder

**Crear pipeline** → toggle **"Tu log específico"**. Una corrida por fuente:

1. **Paso 1** — subí el `.log`, o pegá las 3 líneas si los datos van a llegar en vivo desde la
   fuente → **Siguiente** (el LLM arma el `filter{}` y detecta campos).
2. **Paso 2** — revisá el mapping.
3. **Paso 3** — elegí la fuente (Kafka / Beats) y sus datos de conexión; output OpenSearch.
4. **Paso 4** — **Guardar como caso**: nombre, icono y grupo. Queda como una card del grid del
   paso 1 y se despliega desde ahí, igual que un caso de fábrica.

### Dos tipos de caso
Lo que decide el tipo es **si subiste un archivo**:

| | **Con `.log`** (repetible) | **Sin `.log`** (en vivo) |
|---|---|---|
| De dónde salen los datos | del dataset guardado, que se sube a tu bucket OBS | de la fuente del cliente (Kafka/Beats/JDBC/OBS), que **tiene que estar levantada** |
| Se puede redesplegar | sí, las veces que quieras | solo mientras la fuente exista y sea alcanzable |
| Para qué sirve | demo repetible del log de un cliente | PoC sobre datos productivos |

Las credenciales de la fuente (SASL de Kafka, password de JDBC) se guardan **cifradas** con el
caso y se enmascaran en la consola de CSS.

### Qué resuelve el deploy y qué no

| Fuente | Estado |
|---|---|
| **OBS** | Anda. Es el camino de todos los casos con dataset. |
| **Kafka** | El Logstash sale por el SNAT y el deploy le agrega las *Cluster Routes* hacia las IPs del broker. Requiere que el broker sea **alcanzable** desde la VPC; uno privado en la red del cliente necesita peering/VPN, que este stack no crea. |
| **Beats** | El deploy abre el puerto (SG + DNAT). El Filebeat del cliente apunta a la EIP del NAT. |
| **JDBC** | ⚠️ **No funciona todavía.** El input necesita el `.jar` del driver en el nodo, y la Logstash de CSS no da acceso al filesystem ni tiene salida a internet para instalarlo. La UI y el `.conf` se generan bien, pero el pipeline va a fallar al arrancar salvo que el driver ya esté en la ruta indicada. Mismo problema con el truststore `.jks` de Kafka SASL_SSL. |
