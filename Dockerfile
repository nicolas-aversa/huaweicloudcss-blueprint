# CSS Accelerator — imagen de la app (FastAPI + SPA).
# Corre TODA la experiencia (Home, Cómo usar, Builder, Demo, Desplegar).
# Incluye Terraform para que "Desplegar" funcione montando las credenciales.
FROM python:3.11-slim

# Terraform CLI (deploy de los clusters CSS). Pin de versión.
ARG TERRAFORM_VERSION=1.9.8
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -o /tmp/tf.zip; \
    unzip /tmp/tf.zip -d /usr/local/bin; \
    rm /tmp/tf.zip; \
    apt-get purge -y --auto-remove curl unzip; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps primero para cachear la capa (cambian menos que el código).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código de la app.
COPY . .
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000
# El entrypoint baja los datasets de demo (si faltan) y luego arranca uvicorn.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
