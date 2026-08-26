# TF-CSS Terraform Deployment

Deploy de OpenSearch + Logstash usando Huawei Cloud CSS (Cloud Search Service).

**El deploy lo corre el backend** (`POST /api/v1/terraform/deploy-stream`): arma el
`deploy.auto.tfvars.json` con lo del wizard + ⚙ Configuración y ejecuta
`terraform init/apply` en esta carpeta. No hay pasos manuales en el flujo normal.

## Flujo

```
┌──────────────────────────────────────────────────────────────────┐
│                         Plataforma UI                            │
│  Paso 1: Origen → 2: Estructura → 3: Fuente/destino → 4: Deploy  │
└──────────────────────────────────────────────────────────────────┘
                              │  (backend)
                              ▼
              deploy.auto.tfvars.json  (creds + pipelines +
              infra de ⚙ Configuración; se borra al final)
                              │
                              ▼
        terraform apply → CSS OpenSearch + CSS Logstash + NAT/DNAT
```

## Variables y de dónde salen

| Variable | Fuente | Notas |
|----------|--------|-------|
| `hwc_access_key` / `hwc_secret_key` | AK/SK de ⚙ Configuración (viajan con el deploy) | provider Huawei |
| `obs_access_key` / `obs_secret_key` | ídem | input s3 de Logstash |
| `opensearch_password` | paso 4 del wizard | |
| `vpc_id`, `subnet_id`, `security_group_id`, `availability_zone`, `region` | **⚙ Configuración → Cuenta Huawei Cloud** (server-side) | si no están configurados, fallback al `terraform.tfvars` de esta carpeta |
| `huawei_project_id` | ⚙ Configuración > `.env` | solo para la URL de consola de Dashboards |
| `pipelines` | generadas por el wizard | mapa slug → conf |

## Fallback: terraform.tfvars

Si no configuraste tu cuenta en la UI, el deploy usa `terraform.tfvars` de esta carpeta:

```bash
cp terraform.tfvars.example terraform.tfvars
# completar vpc_id / subnet_id / security_group_id / availability_zone
```

Ese archivo está gitignoreado (trae IDs de TU cuenta). Lo recomendado es configurar todo por UI
y no tocarlo.

## Deploy manual (debug)

```bash
terraform init
terraform plan     # con un deploy.auto.tfvars.json presente, o -var=...
terraform output   # opensearch_endpoint / logstash_endpoint / dashboards_url
```

## Archivos

```
terraform/
├── main.tf                    # Recursos CSS + NAT/DNAT + variables
├── terraform.tfvars.example   # Template del fallback
├── terraform.tfvars           # (gitignoreado) fallback de infra
├── deploy.auto.tfvars.json    # (efímero, gitignoreado) lo escribe el backend
└── .pipelines.json            # (gitignoreado) registro de pipelines activas
```
