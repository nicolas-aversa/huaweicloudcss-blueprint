# Runbook de entrega — CSS Accelerator (para el SA que la recibe)

Esta app te deja mostrar, sobre **Huawei Cloud CSS (OpenSearch + Logstash)**, el camino de un
log crudo → pipeline → dashboards → chatbot NL→PPL → forecasts. Corre en esta VM (Docker) y
despliega en **tu** cuenta Huawei. Todo se configura desde la UI; no toques archivos.

---

## 1. Arrancar (HTTPS automático, zero-config)
En la VM (con puertos **80 y 443** abiertos en el security group):
```bash
cd ~/huaweicloudcss-blueprint
./hosted-up.sh
```
- Detecta la **EIP** de la VM y sirve por **HTTPS** en `https://<EIP>.sslip.io/` (cert real de
  Let's Encrypt, sin dominio propio). El script te imprime la URL.
- La auth se activa sola (clave de sesión autogenerada). **El primer usuario que se registra
  queda como ADMIN.**
- ¿Tenés dominio propio? `export APP_DOMAIN=tudominio` (DNS → la IP) y corré `./hosted-up.sh`.
- Fallback: si no se detecta la EIP, arranca por HTTP en `:80` (`http://<EIP>/`).

## 2. Entrar y configurar (⚙ Configuración)
Registrate con tu email + una contraseña (tu primer ingreso crea la cuenta). Andá a
**⚙ Configuración** — arriba tenés un **checklist** de lo que falta. Completá **en orden**:
1. **Credenciales OBS** — tu Access Key ID / Secret Access Key de Huawei (My Credentials).
2. **Cuenta Huawei Cloud** — Project ID (32 hex), Región, Availability Zone, y VPC / Subnet /
   Security Group (los tres juntos; tienen que existir en esa región). Guardar.
3. **MaaS API Key** — tu key de ModelArts MaaS (sin ella no andan el análisis con LLM ni el chatbot).
4. **Bucket de demos** + botón **Preparar** — escribí el nombre del bucket y tocá *Preparar*: sube
   los datasets de demo a tu bucket OBS (necesario antes de la primera demo).

Cuando el checklist esté en ✓, estás listo para desplegar.

## 3. Presentar una demo
1. **Crear pipeline** → elegí uno o varios casos (SIEM, e-commerce, streaming, salud, ALyC,
   billetera, pozos, FortiAnalyzer…) → **Desplegar**. El primer deploy tarda ~20 min (crea los
   clusters CSS); podés cerrar/refrescar el browser, el deploy sigue y se reengancha.
2. En **Mi Infraestructura**, completá la "puesta en marcha": *Aplicar index template + dashboards*
   → *Iniciar ingesta* → *Provisionar capabilities*.
3. Mostrá: **Dashboards** (abrí el link), el **Asistente de datos** (preguntá en lenguaje natural →
   responde con el dato real + gráfico), y los **forecasts**.
4. Al terminar la demo, **Destruir entorno** para no dejar clusters corriendo (cuestan).

## 4. Sumar más usuarios (solo admin)
⚙ Configuración → **Administración**:
- **Usuarios autorizados**: agregá/quitá emails (allowlist) sin tocar archivos.
- **Resetear contraseña** de un usuario (es el "olvidé mi contraseña": el usuario avisa, vos lo
  reseteás y él fija una nueva en su próximo ingreso).
- **Actividad**: audit log de quién hizo qué.

## 5. Notas
- **Costos**: cada deploy son clusters CSS reales en TU cuenta → destruí los entornos de demo al
  terminar. (Opcional: `ENV_TTL_HOURS` en `.env` auto-destruye demos viejas.)
- **Aislamiento**: cada usuario tiene su propia config y su propio estado de Terraform.
- **Backup**: el estado vive en el volumen Docker `appdata`; respaldalo si te importa conservarlo.
- **Troubleshooting**: chatbot/análisis fallan → revisá la MaaS API key; deploy falla por VPC/subnet
  → deben ser de la MISMA región; "faltan datasets" → tocá *Preparar* en ⚙.

Más detalle de hosting/operación en `HOSTING.md`; guía de uso completa en la vista **Cómo usar** de
la app.
