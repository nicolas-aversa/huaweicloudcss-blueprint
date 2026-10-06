# Datasets de ejemplo

Los archivos de ejemplo de las demos: el SIEM (FortiGate, CloudAudit, auth, WAF),
FortiAnalyzer, transacciones, pozos, e-commerce, salud y streaming. No se versionan
(pesan ~1,3 GB): se generan con los scripts `build_*.py` de abajo.

No hay casos curados: una demo se crea como la de un cliente, subiendo el archivo en
**Pipeline → Dataset nuevo** (el SIEM, sus cuatro archivos juntos: quedan como una sola
tarjeta con cuatro fuentes). El descubrimiento arma el filter, los campos y el plan del
cluster; al guardarlo queda como una tarjeta del grid.

## Pre-carga en OBS (una sola vez)

**Camino recomendado**: en la plataforma, **⚙ Configuración → Preparar bucket de demos**. Sube
todos los datasets que falten a **tu bucket de demos** (el de "Cuenta Huawei Cloud"), bajo el
prefijo de cada tipo, y crea el bucket si no existe. Sube los archivos de los datasets guardados
(cada uno se crea subiendo su archivo en **Pipeline → Dataset nuevo**).

Como el input es read-only, el archivo **queda** en OBS para futuros deploys; cada ingesta limpia
el índice antes (`_clear_case_indices`), así que re-desplegar no duplica. El deploy **verifica** el
prefijo (`<dataset>-logs/`) de cada dataset elegido antes de correr Terraform y lo sube si falta.

El dataset se lee por el prefijo del tipo. Formato esperado (un evento por línea):

| Tipo               | Formato |
|--------------------|---------|
| `siem`             | SIEM unificado: **4 archivos NATIVOS** bajo `siem-logs/` — `siem-fortigate.log` (FortiGate kv multi-type), `siem-cloudaudit.log` (Huawei CTS JSON), `siem-auth.log` (syslog SSH/sudo, timestamp ISO), `siem-waf.log` (Huawei WAF JSON). Un solo input lee el prefijo; el filtro **sniffea** el formato de cada línea (`event.dataset`), parsea la fecha nativa de cada rama y normaliza a ECS + geoip + threat-intel. |
| `app-monitoring`   | JSON por línea |
| `ecommerce-search` | JSON por línea (Olist order_reviews: review_score, comentario, fechas) |
| `fintech-transactions` | `<raw_ts> - <thread> <kv>` por línea; kv `|`/`=` (target `transaction`) con `steps` JSON y `detail` `~`/`=` (device). El filtro deriva `transaction.funnel.*` y `transaction.geo_location`. |
| `oil-gas`          | kv espacio/`=` por línea (telemetría SCADA por pozo, Volve/Equinor). `downtime` solo en lecturas de pozo caído. |
| `media-retail-ecommerce` | JSON por línea (órdenes del sample e-commerce de OpenSearch, con `status`/`cancel_reason` agregados). |
| `encuentros-clinicos`           | CSV por línea (Synthea encounters): `ts,class,code,desc,patient,city,cost,claim,covered,reason,triage`. `triage` solo en emergencia/urgencia. |
| `alyc`             | `<ts yyyyMMdd-HH:mm:ss.SSS> <mercado> <kv espacio/=>` por línea (órdenes/ejecuciones/liquidación ALyC, BYMA/MAE). `reject_reason` solo en ORDER_REJECT; `fail_reason` solo en SETTLEMENT_FAIL. Solo días hábiles AR, rueda 11-17h. |
| `fortianalyzer`    | kv espacio/`=` FortiGate **nativo** (mismo formato crudo que `siem-fortigate.log`, pero el filtro NO normaliza a ECS: conserva srcip/dstip/app/sentbyte…). `attack` solo en subtype ips. |
| `media-streaming`  | JSON por línea (sesiones de reproducción OTT): PLAY_START/PLAY_END/REBUFFER/BITRATE_SWITCH/PLAYBACK_ERROR. `watch_seconds` solo en PLAY_END, `buffering_ms` solo en REBUFFER, `error_code` solo en PLAYBACK_ERROR. |

## Regenerar los datasets de los verticales nuevos

`oil-gas`, `media-retail-ecommerce` y `encuentros-clinicos` (~100k líneas cada uno,
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
