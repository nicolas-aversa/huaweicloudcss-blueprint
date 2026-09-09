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
**⚙ Configuración** y completá las dos cards (el botón **Guardar** de cada una valida que
estén todos los campos):

1. **Credenciales de la cuenta** — Access Key ID / Secret Access Key de OBS (My Credentials),
   tu **MaaS API key** (ModelArts MaaS; sin ella no andan el análisis con LLM ni el chatbot), y el
   **Bucket de demos**. Tocá **Preparar bucket** para subir los datasets de demo a ese bucket
   (necesario antes de la primera demo). Guardar.
2. **Infraestructura General** — Project ID (32 hex), Región, Availability Zone, y VPC / Subnet /
   Security Group (existentes en esa región). Guardar.

Con eso estás listo para desplegar.

## 3. Presentar una demo
1. **Crear pipeline** → elegí uno o varios casos (SIEM, e-commerce, streaming, salud, ALyC,
   billetera, pozos, FortiAnalyzer…) → **Desplegar**. El primer deploy tarda ~20 min (crea los
   clusters CSS); podés cerrar/refrescar el browser, el deploy sigue y se reengancha.
2. En **Mi Infraestructura**, completá la "puesta en marcha": *Aplicar index template + dashboards*
   → *Iniciar ingesta* → *Provisionar capabilities*.
3. Mostrá: **Dashboards** (abrí el link), el **Asistente de datos** (preguntá en lenguaje natural →
   responde con el dato real + gráfico), y los **forecasts**.
4. Al terminar la demo, **Destruir entorno** para no dejar clusters corriendo (cuestan).

## 4. Crear un caso de demo con el log de un cliente
Si el cliente te pasa sus logs, podés convertirlos en un caso propio — con su card, sus dashboards
y su chatbot — sin tocar código:

1. **Crear pipeline** → toggle **"Tu log específico"**.
2. **Subí el archivo `.log`** (un evento por línea, hasta 50 MB). Se detecta el formato solo.
   Si en vez de un archivo los datos van a llegar en vivo (Kafka/Beats/JDBC del cliente), pegá
   2-3 líneas de muestra.
3. **Siguiente** → el LLM arma el `filter{}` y detecta los campos. Revisalos en el paso 2.
4. Paso 3: elegí la **fuente**. Paso 4: **Guardar como caso** → nombre, icono y grupo.
5. La card aparece en el grid del paso 1. Desplegala como cualquier otra demo.

Notas:
- El dataset se sube solo a **tu** bucket al guardar. Los demás usuarios de esta VM ven la card,
  pero tienen que tocar **Preparar bucket** una vez para tener los datos en el suyo.
- Los casos son **compartidos en esta instancia**; los borra quien los creó (o un admin) con la
  ✕ de la card.
- Los casos que vienen de fábrica no se pueden borrar ni pisar.

## 5. Ver qué se ejecutó (⏱ Actividad)
Cada deploy, puesta en marcha, provisión de capabilities y teardown queda registrado. Entrá a
**Actividad** en el menú y abrí cualquier fila:

- **Sub-pasos con ✓/✗**: si el `terraform apply` corrió, si se aplicó el index template, si se
  importaron los dashboards (uno por caso), si se subieron los logs a OBS, qué capability quedó.
  Cuando algo falla, la fila muestra **el motivo**.
- **Salida cruda** de Terraform, desplegable y copiable — útil para pegarla en un ticket.
- Sobrevive refrescar el browser y reiniciar la VM. Se guardan las últimas 50 ejecuciones (30 días).

Es el primer lugar donde mirar cuando un deploy "salió bien" pero no ves datos o dashboards.

## 6. Sumar más usuarios (solo admin)
⚙ Configuración → **Administración**:
- **Usuarios autorizados**: agregá/quitá emails (allowlist) sin tocar archivos.
- **Resetear contraseña** de un usuario (es el "olvidé mi contraseña": el usuario avisa, vos lo
  reseteás y él fija una nueva en su próximo ingreso).
- **Actividad**: audit log de quién hizo qué.

## 7. Notas
- **Costos**: cada deploy son clusters CSS reales en TU cuenta → destruí los entornos de demo al
  terminar. (Opcional: `ENV_TTL_HOURS` en `.env` auto-destruye demos viejas.)
- **Aislamiento**: cada usuario tiene su propia config y su propio estado de Terraform.
- **Backup**: el estado vive en el volumen Docker `appdata`; respaldalo si te importa conservarlo.
- **Troubleshooting**: empezá siempre por **⏱ Actividad** — te dice qué paso falló y por qué. Los
  clásicos: chatbot/análisis fallan → revisá la MaaS API key; deploy falla por VPC/subnet → deben ser
  de la MISMA región; "faltan datasets" → tocá *Preparar bucket* en ⚙.

Más detalle de hosting/operación en `HOSTING.md`; guía de uso completa en la vista **Cómo usar** de
la app.
