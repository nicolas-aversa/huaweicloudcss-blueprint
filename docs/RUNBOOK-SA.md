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
2. En **Entorno desplegado**, completá la "puesta en marcha": *Aplicar index template + dashboards*
   → *Iniciar ingesta* → *Provisionar capabilities*.
3. Mostrá: **Dashboards** (abrí el link), el **Asistente de datos** (preguntá en lenguaje natural →
   responde con el dato real + gráfico), y los **forecasts**.
4. Al terminar la demo, **Destruir entorno** para no dejar clusters corriendo (cuestan).

## 4. Crear un dataset nuevo con tus propios logs
Si tenés un log (tuyo o que te pasó un cliente), podés convertirlo en un dataset propio — con su
card, sus dashboards y su chatbot — sin tocar código:

1. **Crear pipeline** → toggle **"Dataset nuevo"**.
2. Elegí **de dónde salen los datos**:
   - **Tengo el archivo** — subís el `.log` (un evento por línea, hasta 50 MB). Queda guardado y lo
     desplegás las veces que quieras.
   - **Ya está en un bucket** — indicás bucket y prefijo; Logstash lee de ahí, no se copia nada.
   - **Llegan en vivo** — Kafka, Beats o una base ya corriendo: pegás 2-3 líneas de muestra.
3. **Siguiente** → el LLM arma el `filter{}` y detecta los campos. Revisalos en el paso 2.
4. Paso 3: confirmás el destino. Paso 4: **Guardar y desplegar** → nombre, icono y grupo, y el
   entorno arranca. (Si solo querés dejarlo listo, **Solo guardar**.)

Notas:
- Los datos se suben solos a **tu** bucket al guardar. Los demás usuarios de esta VM ven la card,
  pero tienen que tocar **Preparar bucket** una vez para tenerlos en el suyo.
- Los datasets creados son **compartidos en esta instancia**; los borra quien los creó (o un admin)
  con la ✕ de la card.
- Los que vienen de fábrica no se pueden borrar ni pisar.

## 5. Ver qué se ejecutó (⏱ Actividad)
Cada deploy, puesta en marcha, provisión de capabilities y teardown queda registrado. Entrá a
**Actividad** en el menú y abrí cualquier fila:

- **Sub-pasos con ✓/✗**: si el `terraform apply` corrió, si se aplicó el index template, si se
  importaron los dashboards (uno por caso), si se subieron los logs a OBS, qué capability quedó.
  Cuando algo falla, la fila muestra **el motivo**.
- **Salida cruda** de Terraform, desplegable y copiable — útil para pegarla en un ticket.
- Sobrevive refrescar el browser y reiniciar la VM. Se guardan las últimas 50 ejecuciones (30 días).

Es el primer lugar donde mirar cuando un deploy "salió bien" pero no ves datos o dashboards.

## 6. Sumar usuarios y admins (🛡 Panel de control)
El ítem **Panel de control** del menú aparece **solo si sos administrador**. Hay una sola lista de
**Usuarios**: cada email es una fila con sus etiquetas y sus acciones.

- **Invitar**: cargás un email y esa persona queda autorizada a registrarse. Aparece como
  `invitado` hasta que entra por primera vez.
- **Hacer admin / Quitar admin**: sobre cualquier cuenta que ya haya entrado. Siempre queda al
  menos un administrador: no te podés dejar afuera.
- **Contraseña**: es el "olvidé mi contraseña". El usuario avisa, vos la reseteás y él fija una
  nueva en su próximo ingreso. No pierde su rol.
- **Eliminar**: borra la credencial, el acceso y el rol. **No** borra sus datos de trabajo — el
  estado de Terraform se conserva, porque es lo único capaz de destruir un entorno ya desplegado.
  Si la cuenta tiene un entorno activo, la fila lo marca y la confirmación te avisa: pedile que lo
  **destruya antes**, o esos clusters van a seguir facturando sin que nadie pueda darlos de baja
  desde la app.

> **El registro abierto es el default.** Con la lista de invitados vacía, cualquiera que tenga el
> link puede crearse una cuenta — y entonces eliminar a alguien no sirve de nada, porque vuelve a
> entrar. El panel te lo avisa en rojo. **Invitá al menos un email** para cerrarlo: a partir de ahí
> solo entran los de la lista (los que ya tenían cuenta no se ven afectados).

Al lado de **Usuarios** está **Actividad**: el audit log de quién hizo qué. Ambos tienen su botón
de refrescar.

### ¿Quién es admin?
1. Los emails de la variable `SA_ADMINS`, si está seteada.
2. Más los promovidos desde el Panel de control.
3. Si no hay ninguno de los dos: **el primer usuario que se registró** en esa VM. Por eso una
   instancia nueva funciona sin configurar nada — el que la estrena queda como admin.

**Si no ves el Panel de control** es porque alguien se registró antes que vos. Pedile que te promueva
desde ahí. Si no hay a quién pedirle, desde la VM:
```bash
cd ~/huaweicloudcss-blueprint
docker compose -f docker-compose.hosted.yml exec app cat /app/data/users.json   # el `created` más chico es el admin
echo 'SA_ADMINS=tu.email@huawei.com' >> .env.hosted
./hosted-up.sh
```

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
