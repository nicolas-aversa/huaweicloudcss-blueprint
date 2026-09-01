#!/bin/sh
# Entrypoint del contenedor: si no hay datasets de demo (clone fresco), los baja
# del Release y los extrae en /app/datasets. Si ya están (el host los montó, o se
# bajaron en un arranque previo), no hace nada. Después arranca la app.
set -e

export DATASETS_URL="${DATASETS_URL:-https://github.com/nicolas-aversa/huaweicloudcss-blueprint/releases/download/datasets/datasets.tar.gz}"
export DATASETS_DIR="${DATASETS_DIR:-/app/datasets}"

if [ -z "$(ls "$DATASETS_DIR"/*.log 2>/dev/null)" ]; then
  echo "[entrypoint] Sin datasets en $DATASETS_DIR — bajando de $DATASETS_URL ..."
  python - <<'PY' || true
import urllib.request, tarfile, io, os
url = os.environ["DATASETS_URL"]; d = os.environ["DATASETS_DIR"]
try:
    data = urllib.request.urlopen(url, timeout=180).read()
    tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").extractall(d)
    n = len([f for f in os.listdir(d) if f.endswith(".log")])
    print("[entrypoint] datasets listos: %d archivos (%d MB)" % (n, len(data) // (1024 * 1024)))
except Exception as e:
    print("[entrypoint] descarga de datasets falló: %r" % e)
    print("[entrypoint] la app arranca igual; las demos no tendrán datos hasta cargarlos.")
PY
fi

# Multi-usuario hosteado (opcional): asegura el dir de datos por-usuario y el
# cache de providers de Terraform compartido, si están configurados por env.
# En single-user estas vars no están y no pasa nada.
[ -n "$APP_DATA_DIR" ] && mkdir -p "$APP_DATA_DIR"
[ -n "$TF_PLUGIN_CACHE_DIR" ] && mkdir -p "$TF_PLUGIN_CACHE_DIR"

exec uvicorn main:app --host 0.0.0.0 --port 8000
