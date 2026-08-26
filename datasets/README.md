# Datasets de los tipos de log predefinidos

Esta carpeta contiene el **log entero real** de cada tipo predefinido del wizard. Los tipos
predefinidos ahora se **leen de OBS** (`read_existing_bucket`): el operador **pre-carga** cada
archivo una vez en el bucket, y el deploy **NO los sube** (el input es read-only one-shot —
`delete=false`, `watch_for_new_files=false`). Esto evita re-subir en cada corrida y el timeout de
`put_object` con datasets grandes (ej. fintech, ~98 MB).

La generación sintética quedó fuera del flujo predefinido; el tipo `custom` ("Traé tu propio log")
sube el archivo que importa el usuario (`log_file_content`).

## Pre-carga en OBS (una sola vez)

**Camino recomendado**: en la plataforma, **⚙ Configuración → Preparar bucket de demos**. Sube
todos los datasets que falten a **tu bucket de demos** (el de "Cuenta Huawei Cloud"), bajo el
prefijo de cada tipo, y crea el bucket si no existe. El mapa slug→archivos sale del registro
declarativo `verticals/` (campo `dataset_files` de cada vertical).

Alternativa manual (consola web de OBS u `obsutil cp`), al MISMO esquema de prefijos:

| Tipo                   | Prefijo en tu bucket        | Archivo fuente                      |
|------------------------|-----------------------------|-------------------------------------|
| `siem`                 | `siem-logs/`                | `datasets/siem-{fortigate,cloudaudit,auth,waf}.log` (los 4) |
| `fortianalyzer`        | `fortianalyzer-logs/`       | `datasets/fortianalyzer.log`        |
| `fintech-transactions` | `transacciones-billetera-logs/`          | `datasets/transacciones-billetera.log`            |
| `alyc`                 | `alyc-logs/`                | `datasets/transacciones-alyc.log`                 |
| `media-streaming`      | `streaming-ott-logs/`           | `datasets/streaming-ott.log`            |
| `oil-gas`              | `oil-gas-logs/`             | `datasets/produccion-pozos.log`              |
| `media-retail-ecommerce` | `ventas-ecommerce-logs/`          | `datasets/ventas-ecommerce.log`            |
| `health`               | `encuentros-clinicos-logs/`              | `datasets/encuentros-clinicos.log`               |
| `cts`                  | `CloudTraces/`              | (traces reales de la cuenta, si existen) |

```
obsutil cp datasets/transacciones-alyc.log obs://<tu-bucket-de-demos>/alyc-logs/
```

Como el input es read-only, el archivo **queda** en OBS para futuros deploys; cada ingesta limpia
el índice antes (`_clear_case_indices`), así que re-desplegar no duplica. `custom` no se pre-carga
(sube su archivo importado en el deploy). El deploy demo **verifica** los prefijos de los tipos
elegidos antes de correr Terraform y avisa si falta alguno.

El dataset se lee por el prefijo del tipo. Formato esperado (un evento por línea):

| Tipo               | Formato |
|--------------------|---------|
| `siem`             | SIEM unificado: **4 archivos NATIVOS** bajo `siem-logs/` — `siem-fortigate.log` (FortiGate kv multi-type), `siem-cloudaudit.log` (Huawei CTS JSON), `siem-auth.log` (syslog SSH/sudo, timestamp ISO), `siem-waf.log` (Huawei WAF JSON). Un solo input lee el prefijo; el filtro **sniffea** el formato de cada línea (`event.dataset`), parsea la fecha nativa de cada rama y normaliza a ECS + geoip + threat-intel. |
| `app-monitoring`   | JSON por línea |
| `ecommerce-search` | JSON por línea (Olist order_reviews: review_score, comentario, fechas) |
| `fintech-transactions` | `<raw_ts> - <thread> <kv>` por línea; kv `|`/`=` (target `transaction`) con `steps` JSON y `detail` `~`/`=` (device). El filtro deriva `transaction.funnel.*` y `transaction.geo_location`. |
| `oil-gas`          | kv espacio/`=` por línea (telemetría SCADA por pozo, Volve/Equinor). `downtime` solo en lecturas de pozo caído. |
| `media-retail-ecommerce` | JSON por línea (órdenes del sample e-commerce de OpenSearch, con `status`/`cancel_reason` agregados). |
| `health`           | CSV por línea (Synthea encounters): `ts,class,code,desc,patient,city,cost,claim,covered,reason,triage`. `triage` solo en emergencia/urgencia. |
| `alyc`             | `<ts yyyyMMdd-HH:mm:ss.SSS> <mercado> <kv espacio/=>` por línea (órdenes/ejecuciones/liquidación ALyC, BYMA/MAE). `reject_reason` solo en ORDER_REJECT; `fail_reason` solo en SETTLEMENT_FAIL. Solo días hábiles AR, rueda 11-17h. |
| `fortianalyzer`    | kv espacio/`=` FortiGate **nativo** (mismo formato crudo que `siem-fortigate.log`, pero el filtro NO normaliza a ECS: conserva srcip/dstip/app/sentbyte…). `attack` solo en subtype ips. |
| `media-streaming`  | JSON por línea (sesiones de reproducción OTT): PLAY_START/PLAY_END/REBUFFER/BITRATE_SWITCH/PLAYBACK_ERROR. `watch_seconds` solo en PLAY_END, `buffering_ms` solo en REBUFFER, `error_code` solo en PLAYBACK_ERROR. |

## Regenerar los datasets de los verticales nuevos

`oil-gas`, `media-retail-ecommerce` y `health` (~100k líneas cada uno,
re-fechados a **2025-07-01 → 2026-07-01**, la misma ventana que fintech) se generan con:

```
py build_vertical_datasets.py --sources <dir con volve_production.xlsx, ecommerce.ndjson y synthea/csv/>
```

Fuentes: Volve production data (Equinor, xlsx), sample e-commerce de OpenSearch
Dashboards (ndjson del repo GitHub) y Synthea 1k-patients CSV (sintético, sin PHI).
El generador es determinístico (seed fija); ver el docstring del script para el
detalle de cada transformación (re-fechado, upsampling SCADA, rachas plantadas).

El **SIEM** (~100k, repartidos en `siem-fortigate.log` / `siem-cloudaudit.log` /
`siem-auth.log` / `siem-waf.log`) se genera aparte con:

```
py build_siem_dataset.py   # reutiliza datasets/firewall.log (FortiGate multi-type) + sintetiza cloudaudit/auth/waf
```

Los 4 van al MISMO prefijo `siem-logs/` de tu bucket (un input los lee todos).

**`alyc`, `media-streaming` y `fortianalyzer`** se generan con el mismo script, sin `--sources`
(alyc y media-streaming son 100% sintéticos; fortianalyzer copia `datasets/firewall.log`, que ya
está en ventana):

```
py build_vertical_datasets.py
```

Mezcla fortigate (real, re-fechado) + cloudaudit (CTS-style) + auth (SSH) + waf, con
un set de IPs conocidas-malas de threat-intel salpicadas y picos de seguridad
realistas (scans, brute-force, ataques web) — sin kill chain correlacionada.

## Contrato de formato

1. **Un evento por línea.** Logstash consume el archivo línea por línea.
2. **Mismo formato que el `sample`** de ese tipo en `EXAMPLE_DATA` (index.html), para que
   el `filter_code` correspondiente lo parsee sin cambios.
3. **Timestamps recientes.** Para que los eventos caigan dentro de la ventana visible en
   Kibana, usar timestamps dentro de las últimas ~72h del momento del deploy.
4. **Líneas que empiezan con `#` se ignoran** (comentarios). Si el archivo queda sin
   ninguna línea de datos, el backend cae al comportamiento anterior (sample único / sintético).

## Estado actual

Cada `.log` trae 2 líneas de ejemplo (tomadas del `sample` original) marcadas con un
comentario `# REEMPLAZAR ...`. **Reemplazá esas líneas con el dataset real** del tipo.
El flujo funciona aunque todavía no las hayas reemplazado — solo que sube los ejemplos.
