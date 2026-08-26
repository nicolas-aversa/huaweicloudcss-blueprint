# Testing Guide

## Quick Testing with Cached Example

The UI includes a pre-cached financial log example that skips the LLM call, making testing much faster.

### Option 1: Click "Cargar ejemplo (cacheado)"

1. Click the button in Step 1
2. The log + filter + mapping are loaded instantly from cache
3. No API call to GLM-5.1

### Option 2: Auto-load with URL parameter

Add `?sample=1` or `?dev=1` to the URL:

```
http://localhost:8000/?sample=1
http://localhost:8000/?dev=1
```

The example will be loaded automatically on page load.

## Cached Example Details

### Log

```
2026-05-19T10:15:30.123Z host=pay-gw-03 level=INFO src_ip=10.20.30.40 dst_ip=10.20.30.5 user=jdoe txn_id=TXN-2026-0098123 amount=1530.75 currency=USD card_bin=453201 mcc=5411 auth_code=A1B2C3 resp=200 latency_ms=87 msg="payment authorized"
```

### Fields (15 fields)

| Campo | ECS Path | Tipo | Label |
|-------|----------|------|-------|
| timestamp | @timestamp | date | Fecha y hora |
| host | host.name | string | Servidor origen |
| level | log.level | string | Nivel de log |
| src_ip | source.ip | ip | IP origen |
| dst_ip | destination.ip | ip | IP destino |
| user | user.name | string | Usuario |
| txn_id | transaction.id | string | ID transacción |
| amount | transaction.amount | float | Monto |
| currency | transaction.currency | string | Moneda |
| card_bin | transaction.card_bin | string | BIN tarjeta |
| mcc | transaction.mcc | integer | Código MCC |
| auth_code | transaction.auth_code | string | Código autorización |
| resp | http.response.status_code | integer | Código respuesta HTTP |
| latency_ms | event.duration | integer | Latencia |
| msg | message | string | Mensaje |

### Filter Code

```ruby
filter {
  dissect {
    mapping => {
      "message" => "%{timestamp} host=%{host} level=%{log_level} src_ip=%{src_ip} dst_ip=%{dst_ip} user=%{user} txn_id=%{txn_id} amount=%{amount} currency=%{currency} card_bin=%{card_bin} mcc=%{mcc} auth_code=%{auth_code} resp=%{resp} latency_ms=%{latency_ms} msg=%{msg}"
    }
  }

  date {
    match => ["timestamp", "ISO8601"]
    target => "@timestamp"
    remove_field => ["timestamp"]
  }

  mutate {
    rename => {
      "host" => "[host][name]"
      "log_level" => "[log][level]"
      "src_ip" => "[source][ip]"
      "dst_ip" => "[destination][ip]"
      "user" => "[user][name]"
      "txn_id" => "[transaction][id]"
      "amount" => "[transaction][amount]"
      "currency" => "[transaction][currency]"
      "card_bin" => "[transaction][card_bin]"
      "mcc" => "[transaction][mcc]"
      "auth_code" => "[transaction][auth_code]"
      "resp" => "[http][response][status_code]"
      "latency_ms" => "[event][duration]"
    }
    convert => {
      "[transaction][amount]" => "float"
      "[transaction][mcc]" => "integer"
      "[http][response][status_code]" => "integer"
      "[event][duration]" => "integer"
    }
  }

  # ECS: duration en nanosegundos
  ruby {
    code => "event.set('[event][duration]', event.get('[event][duration]').to_i * 1_000_000)"
  }
}
```

## Development Workflow

1. **Start the server**:
   ```bash
   cd elk-solution-blueprint
   python main.py
   ```

2. **Open with auto-load**:
   ```bash
   open http://localhost:8000/?sample=1
   ```

3. **Navigate through steps**:
   - Step 1: Log already loaded ✓
   - Step 2: Mapping already loaded ✓ (instant, no LLM call)
   - Step 3: Select input plugin (OBS)
   - Step 4: Configure output (**leave Hosts EMPTY**)
   - Step 5: Generate pipeline → Download

4. **Deploy with Terraform**:
   ```bash
   cd terraform
   ./deploy.sh production
   ```

## Terraform Deployment

### Key Points

1. **Hosts field**: Leave EMPTY in Step 4. Terraform injects the OpenSearch endpoint automatically.

2. **Generated pipeline** has `hosts => []`:
   ```ruby
   output {
     elasticsearch {
       hosts => []  # Terraform fills this
       index => "logs-%{+YYYY.MM.dd}"
       ...
     }
   }
   ```

3. **Terraform** replaces with real endpoint:
   ```ruby
   output {
     elasticsearch {
       hosts => ["10.0.1.100:9200"]  # Real OpenSearch IP
       index => "logs-%{+YYYY.MM.dd}"
       ...
     }
   }
   ```

### Deployment Flow

```
UI (pipeline.conf) → Terraform → TF-CSS (Huawei Cloud)
                           │
                           ├─ Create OpenSearch → get endpoint
                           ├─ Inject endpoint in pipeline
                           └─ Deploy Logstash with pipeline
```

## Performance

| Scenario | Time |
|----------|------|
| With cache | ~0ms (instant) |
| Without cache (LLM call) | ~30-60s |
| Terraform deployment | ~5-10 min |

The cache is especially useful for:
- Rapid UI iteration
- Testing Terraform integration
- Demo scenarios
- CI/CD pipelines
