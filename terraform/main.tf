# TF-CSS: Deploy OpenSearch + Logstash Pipeline
# 1 nodo OpenSearch + 1 Logstash para ingestar

terraform {
  required_providers {
    huaweicloud = {
      source  = "huaweicloud/huaweicloud"
      version = "~> 1.87.0"
    }
  }
}

provider "huaweicloud" {
  region     = var.region
  access_key = var.hwc_access_key
  secret_key = var.hwc_secret_key
}

variable "region" {
  description = "Región Huawei Cloud de los recursos (⚙ Configuración de la plataforma)"
  type        = string
  default     = "la-south-2"
}

# ── Variables de autenticación ───────────────────────────────────────────────

variable "hwc_access_key" {
  type      = string
  sensitive = true
}

variable "hwc_secret_key" {
  type      = string
  sensitive = true
}

variable "obs_access_key" {
  type      = string
  sensitive = true
}

variable "obs_secret_key" {
  type      = string
  sensitive = true
}

variable "opensearch_password" {
  type      = string
  sensitive = true
}

# ── Variables de infraestructura ─────────────────────────────────────────────

variable "vpc_id" {
  description = "VPC ID where the CSS clusters will be deployed"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID where the CSS clusters will be deployed"
  type        = string
}

variable "security_group_id" {
  description = "Security Group ID for the CSS clusters"
  type        = string
}

variable "availability_zone" {
  description = "Availability zone for the clusters (e.g., la-south-2a)"
  type        = string
}

# ── Variable: Pipelines generados por la plataforma ──────────────────────────
# Mapa aditivo de pipelines que corren EN PARALELO sobre el mismo cluster
# Logstash. Clave = slug (ej. "ej1"); valor = el .conf generado + el flag de
# arranque de DOS FASES por pipeline. Cada pipeline lee de su propio prefijo OBS
# y escribe a su propio índice (lo garantiza el backend), así no colisionan.
variable "pipelines" {
  description = "Mapa slug => { pipeline_conf, start_ingestion } de las pipelines activas en el cluster."
  type = map(object({
    pipeline_conf   = string
    start_ingestion = bool
  }))
  default = {}
}

variable "project_name" {
  description = "Nombre del proyecto (para tags)"
  type        = string
  default     = "log-analytics"
}

variable "https_enabled" {
  description = "Habilitar HTTPS para OpenSearch"
  type        = bool
  default     = true
}

variable "logstash_flavor" {
  description = "Flavor del nodo Logstash (ess.spec-4u8g para 1 pipeline, ess.spec-8u16g para múltiples)"
  type        = string
  default     = "ess.spec-4u8g"
}

variable "opensearch_flavor" {
  description = "Flavor del nodo OpenSearch (ess.spec-4u8g para 1 pipeline, ess.spec-8u16g para múltiples)"
  type        = string
  default     = "ess.spec-4u8g"
}

variable "existing_opensearch_endpoint" {
  description = "Endpoint de un cluster OpenSearch existente (ip:port). Si no está vacío, se saltea la creación del cluster OpenSearch y se usa este endpoint. Solo Logstash se crea nuevo."
  type        = string
  default     = ""
}

# ── OpenSearch Cluster (1 nodo) ──────────────────────────────────────────────

locals {
  # Disco de OpenSearch: escala con el flavor. ess.spec-4u8g admite 40 GB, pero
  # ess.spec-8u16g (2+ pipelines) exige >= 80 GB — con 40 da CSS.5078 "Invalid
  # disk size". El Logstash sí acepta 40 GB en ambos flavors.
  opensearch_volume_size = var.opensearch_flavor == "ess.spec-8u16g" ? 80 : 40
}

resource "huaweicloud_css_cluster" "opensearch_cluster" {
  count = var.existing_opensearch_endpoint == "" ? 1 : 0

  name              = "${var.project_name}-opensearch"
  engine_type       = "opensearch"
  engine_version    = "3.4.0"
  region            = var.region
  vpc_id            = var.vpc_id
  subnet_id         = var.subnet_id
  security_group_id = var.security_group_id
  availability_zone = var.availability_zone

  ess_node_config {
    flavor          = var.opensearch_flavor
    instance_number = 1
    volume {
      volume_type = "HIGH"
      size        = local.opensearch_volume_size
    }
  }

  security_mode = true
  password      = var.opensearch_password
  https_enabled = var.https_enabled

  # NOTA: sin `public_access`/`kibana_public_access`. El cluster queda PRIVADO y
  # corre HTTP (https_enabled=false). El acceso externo (laptop) entra por el NAT
  # gateway + DNAT de abajo, que reenvía por puerto a los endpoints privados — así
  # una sola EIP sirve OpenSearch (9200) y Kibana, y no aplica el CSS.5310 que
  # forzaba HTTPS.

  tags = {
    Project     = var.project_name
    Environment = "production"
    Application = "search"
  }
}

# ── NAT gateway: UNA EIP para OpenSearch + Kibana vía DNAT ───────────────────
# El backend corre fuera de la VPC. En vez de exponer cada servicio con su EIP de
# CSS (que obliga HTTPS), un NAT gateway con 2 reglas DNAT reenvía por puerto al
# cluster privado: <EIP>:9200 -> OpenSearch, <EIP>:kibana_port -> Kibana. Es a
# nivel red (transparente para CSS), así que el cluster sigue en HTTP.

variable "kibana_port" {
  description = "Puerto privado de OpenSearch Dashboards (Kibana) en el nodo CSS. Huawei usa 5601; si difiere, ajustar."
  type        = number
  default     = 5601
}

resource "huaweicloud_nat_gateway" "nat" {
  name      = "${var.project_name}-nat"
  spec      = "1"
  vpc_id    = var.vpc_id
  subnet_id = var.subnet_id
}

resource "huaweicloud_vpc_eip" "nat_eip" {
  publicip {
    type = "5_bgp"
  }
  # Facturación POR TRÁFICO (charge_mode = "traffic"): se paga por GB transferidos,
  # no por ancho de banda reservado. El bloque `bandwidth` es obligatorio en el
  # recurso; `size` acá es solo el TOPE (5 Mbit/s), no la facturación.
  bandwidth {
    name        = "${var.project_name}-nat-eip"
    size        = 5
    share_type  = "PER"
    charge_mode = "traffic"
  }
}

locals {
  # IP privada del nodo OpenSearch (primer host del endpoint "ip:9200[,ip:9200]").
  # Solo se usa cuando creamos el cluster; si reusamos uno existente, queda vacío.
  os_private_ip = var.existing_opensearch_endpoint == "" ? split(":", split(",", huaweicloud_css_cluster.opensearch_cluster[0].endpoint)[0])[0] : ""
}

# Puerto (NIC) del nodo CSS, buscado por su IP privada. Solo cuando creamos OS.
data "huaweicloud_networking_port" "os_node" {
  count      = var.existing_opensearch_endpoint == "" ? 1 : 0
  fixed_ip   = local.os_private_ip
  depends_on = [huaweicloud_css_cluster.opensearch_cluster]
}

# DNAT 9200 -> OpenSearch (index template, queries). Escenario VPC vía port_id.
resource "huaweicloud_nat_dnat_rule" "opensearch" {
  count                 = var.existing_opensearch_endpoint == "" ? 1 : 0
  nat_gateway_id        = huaweicloud_nat_gateway.nat.id
  floating_ip_id        = huaweicloud_vpc_eip.nat_eip.id
  protocol              = "tcp"
  port_id               = data.huaweicloud_networking_port.os_node[0].id
  internal_service_port = 9200
  external_service_port = 9200
}

# DNAT kibana_port -> Kibana (saved_objects / import de dashboards). Mismo NIC.
resource "huaweicloud_nat_dnat_rule" "kibana" {
  count                 = var.existing_opensearch_endpoint == "" ? 1 : 0
  nat_gateway_id        = huaweicloud_nat_gateway.nat.id
  floating_ip_id        = huaweicloud_vpc_eip.nat_eip.id
  protocol              = "tcp"
  port_id               = data.huaweicloud_networking_port.os_node[0].id
  internal_service_port = var.kibana_port
  external_service_port = var.kibana_port
}

# ── Salida del cluster a MaaS (agente conversacional ml-commons) ─────────────
# El cluster CSS llama HACIA AFUERA a MaaS (api-*.modelarts-maas.com) para el
# connector del agente. Hacen falta DOS cosas:
#   1. Cluster Routes del CSS (config DENTRO del cluster) — las agrega el backend
#      por la API del CSS (`_add_css_cluster_routes` en main.py), porque el provider
#      terraform no las expone.
#   2. SNAT sobre el NAT+EIP (ESTE recurso) — la salida REAL a internet del subnet
#      privado. Sin SNAT, aunque estén las Cluster Routes, el egress no ocurre y da
#      "Connection timed out ...:443". (Se había sacado por error; se restaura.)
resource "huaweicloud_nat_snat_rule" "maas_egress" {
  nat_gateway_id = huaweicloud_nat_gateway.nat.id
  floating_ip_id = huaweicloud_vpc_eip.nat_eip.id
  subnet_id      = var.subnet_id
}

# Ingress a 9200 y al puerto de Kibana en el SG del cluster: el DNAT preserva el
# source, así que el SG del nodo CSS debe permitir esos puertos desde internet.
resource "huaweicloud_networking_secgroup_rule" "opensearch_public_9200" {
  count             = var.existing_opensearch_endpoint == "" ? 1 : 0
  security_group_id = var.security_group_id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 9200
  port_range_max    = 9200
  remote_ip_prefix  = "0.0.0.0/0"
}

resource "huaweicloud_networking_secgroup_rule" "kibana_public" {
  count             = var.existing_opensearch_endpoint == "" ? 1 : 0
  security_group_id = var.security_group_id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = var.kibana_port
  port_range_max    = var.kibana_port
  remote_ip_prefix  = "0.0.0.0/0"
}

# ── Logstash Cluster ─────────────────────────────────────────────────────────

resource "huaweicloud_css_logstash_cluster" "logstash_cluster" {
  name              = "${var.project_name}-logstash"
  engine_version    = "7.10.0"
  region            = var.region
  vpc_id            = var.vpc_id
  subnet_id         = var.subnet_id
  security_group_id = var.security_group_id
  availability_zone = var.availability_zone

  node_config {
    flavor          = var.logstash_flavor
    instance_number = 1
    volume {
      volume_type = "HIGH"
      size        = 40
    }
  }

  tags = {
    Project     = var.project_name
    Environment = "production"
    Application = "log-processing"
  }
}

# ── Preparar hosts de OpenSearch ────────────────────────────────────────────

locals {
  # Protocolo según configuración
  protocol = var.https_enabled ? "https" : "http"

  # Endpoint efectivo: el del cluster creado, o el existente si se proveyó.
  opensearch_endpoint = var.existing_opensearch_endpoint != "" ? var.existing_opensearch_endpoint : huaweicloud_css_cluster.opensearch_cluster[0].endpoint

  # Convertir endpoint del OpenSearch a formato protocol://host
  opensearch_hosts = [
    for h in split(",", local.opensearch_endpoint) : "${local.protocol}://${h}"
  ]

  # String de hosts ya formateado para inyectar en cada pipeline.
  hosts_literal = "hosts => [\"${join("\", \"", local.opensearch_hosts)}\"]"

  # Nombres de las configuraciones cuya fase 2 está prendida (a activar).
  active_pipeline_names = [
    for k, v in var.pipelines :
    huaweicloud_css_logstash_configuration.pipeline[k].name if v.start_ingestion
  ]
}

# ── Logstash Configuration ───────────────────────────────────────────────────

# Una configuración por pipeline del mapa. Corren en paralelo sobre el mismo
# cluster Logstash; cada una con su propio prefijo OBS + índice (lo asegura el
# backend), por lo que no se pisan. `workers`/`batch_size` bajados porque el
# nodo es 1× ess.spec-4u8g (4 vCPU) y pueden convivir hasta ~4 pipelines.
resource "huaweicloud_css_logstash_configuration" "pipeline" {
  for_each = var.pipelines

  cluster_id = huaweicloud_css_logstash_cluster.logstash_cluster.id
  # Nombre = `pipeline-<slug>` (sin el prefijo del proyecto). Huawei CSS exige que
  # el nombre de la configuración tenga entre 4 y 32 caracteres ("conf name is
  # invalid" fuera de ese rango): el prefijo `pipeline-` asegura el mínimo de 4
  # incluso para slugs cortos como `cts`; y `pipeline-fintech-transactions` = 29
  # entra en 32. Se acota a 32 por las dudas.
  name = substr("pipeline-${each.key}", 0, 32)

  # Inyectar los hosts de OpenSearch en el `.conf` de esta entrada.
  conf_content = each.value.pipeline_conf != "" ? replace(
    each.value.pipeline_conf,
    "hosts => []",
    local.hosts_literal
  ) : ""

  setting {
    queue_type = "memory"
    batch_size = 1000
    workers    = 2
  }

  sensitive_words = [
    var.obs_access_key,
    var.obs_secret_key,
    var.opensearch_password
  ]
}

# ── Logstash Pipeline (activar configuraciones) ──────────────────────────────
# Deploy en dos fases POR pipeline: una configuración con start_ingestion=false
# queda creada pero NO se activa acá (para aplicar su index template antes de
# que Logstash cree el índice con el primer documento). La fase 2 prende su flag
# y entra a la lista `names`. Agregar/activar una pipeline reasienta esta lista
# sin recrear las configuraciones existentes.

resource "huaweicloud_css_logstash_pipeline" "pipeline" {
  count      = length(local.active_pipeline_names) > 0 ? 1 : 0
  cluster_id = huaweicloud_css_logstash_cluster.logstash_cluster.id
  names      = local.active_pipeline_names
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "opensearch_endpoint" {
  description = "OpenSearch cluster endpoint"
  value       = local.opensearch_endpoint
}

output "opensearch_cluster_id" {
  description = "ID del cluster CSS. Lo usa el backend para agregar las Cluster Routes a MaaS por la API del CSS."
  value       = var.existing_opensearch_endpoint != "" ? "" : huaweicloud_css_cluster.opensearch_cluster[0].id
}

output "opensearch_hosts" {
  description = "OpenSearch hosts para Logstash"
  value       = local.opensearch_hosts
}

output "opensearch_public_endpoint" {
  description = "Endpoint público del OpenSearch. Si es cluster existente, es el mismo endpoint; si es nuevo, vía NAT/DNAT (EIP:9200)."
  value       = var.existing_opensearch_endpoint != "" ? var.existing_opensearch_endpoint : "${huaweicloud_vpc_eip.nat_eip.address}:9200"
}

output "dashboards_public_endpoint" {
  description = "Endpoint público de OpenSearch Dashboards/Kibana."
  value       = var.existing_opensearch_endpoint != "" ? "${split(":", var.existing_opensearch_endpoint)[0]}:${var.kibana_port}" : "${huaweicloud_vpc_eip.nat_eip.address}:${var.kibana_port}"
}

output "logstash_endpoint" {
  description = "LogStash cluster endpoint"
  value       = huaweicloud_css_logstash_cluster.logstash_cluster.endpoint
}

output "opensearch_status" {
  description = "OpenSearch cluster status"
  value       = var.existing_opensearch_endpoint != "" ? "active" : huaweicloud_css_cluster.opensearch_cluster[0].status
}

output "logstash_status" {
  description = "LogStash cluster status"
  value       = huaweicloud_css_logstash_cluster.logstash_cluster.status
}

output "pipeline_names" {
  description = "Configuraciones Logstash creadas (slug => nombre)."
  value       = { for k, c in huaweicloud_css_logstash_configuration.pipeline : k => c.name }
}

output "active_pipeline_names" {
  description = "Configuraciones cuya ingesta está activa (fase 2)."
  value       = local.active_pipeline_names
}

output "project_name" {
  description = "Nombre del proyecto del último apply (para reconstruir UX post-F5 del wizard)"
  value       = var.project_name
}

# Project ID de la cuenta Huawei Cloud — necesario para construir la URL
# de la consola Kibana. El operador lo provee via .env (HUAWEI_PROJECT_ID)
# y el backend lo pasa por tfvars. NO se hardcodea en el HCL — varía por
# cuenta. Si no se provee, el output queda vacío y el frontend cae al
# link interno (que solo sirve desde VPC).
variable "huawei_project_id" {
  description = "IAM project_id (32 hex chars). Lo obtenés del header X-Subject-Token o de console > My Credentials."
  type        = string
  default     = ""
}

output "dashboards_url" {
  description = "OpenSearch Dashboards URL (consola Huawei Cloud, accesible desde internet)"
  value = var.existing_opensearch_endpoint != "" ? "${local.protocol}://${var.existing_opensearch_endpoint}/_dashboards" : (
    var.huawei_project_id != "" ? format(
      "https://%s-console.huaweicloud.com/elasticsearch/kibana/%s/%s/%s/app/login",
      var.region,
      var.region,
      var.huawei_project_id,
      huaweicloud_css_cluster.opensearch_cluster[0].id,
    ) : "${local.protocol}://${huaweicloud_css_cluster.opensearch_cluster[0].endpoint}/_dashboards"
  )
}

output "dashboards_url_internal" {
  description = "Dashboards URL interna (VPC-only) — útil si conectás desde otra VM de la misma VPC"
  value       = "${local.protocol}://${local.opensearch_endpoint}/_dashboards"
}
