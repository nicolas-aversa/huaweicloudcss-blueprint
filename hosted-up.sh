#!/bin/sh
# Levanta la app hosteada con HTTPS automático, SIN dominio propio: detecta la EIP
# de esta VM y usa <eip>.sslip.io (que resuelve a esa IP), así Caddy saca un cert
# real de Let's Encrypt. Si no puede detectar la EIP (o ya fijaste APP_DOMAIN),
# respeta lo que haya; y si no hay nada, cae a HTTP :80.
#
# Uso:  ./hosted-up.sh
# Requisitos: puertos 80 y 443 abiertos en el security group.
set -e
COMPOSE="docker compose -f docker-compose.hosted.yml"

if [ -z "$APP_DOMAIN" ]; then
  # 1) Metadata de la ECS Huawei (la EIP bindeada). 2) fallback: echo de IP pública.
  EIP="$(curl -s --max-time 4 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)"
  [ -z "$EIP" ] && EIP="$(curl -s --max-time 4 https://api.ipify.org 2>/dev/null || true)"
  if echo "$EIP" | grep -qE '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
    export APP_DOMAIN="${EIP}.sslip.io"
    echo "[hosted-up] EIP detectada: $EIP → HTTPS en https://${APP_DOMAIN}"
  else
    echo "[hosted-up] No pude detectar la EIP → HTTP en :80. Para HTTPS: export APP_DOMAIN=<tu-EIP>.sslip.io"
  fi
else
  echo "[hosted-up] Usando APP_DOMAIN=$APP_DOMAIN"
fi

exec $COMPOSE up --build -d
