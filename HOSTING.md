# Hostear la app para otros SAs (multi-usuario)

Por defecto la app corre **single-user** (un operador, su cuenta) sin login. Este
modo **hosteado** la pone detrás de una URL con **HTTPS** y **login por SA**, y le
da a cada SA su **workspace aislado**: su propia configuración de cuenta Huawei /
MaaS y su propio estado de Terraform. Dos SAs pueden desplegar a la vez, cada uno
en **su** cuenta (o en la del owner), sin pisarse.

> El login se activa **solo** si está la env var `APP_SECRET_KEY`. Sin ella, todo
> sigue funcionando single-user como siempre (dev local, tests).

## Cómo funciona el aislamiento

- **Auth**: email + password por SA (`auth.py`). Los emails permitidos salen de
  `SA_ALLOWLIST`; el primer login de cada uno crea su cuenta (password hasheado
  con pbkdf2). La sesión es una cookie firmada (HMAC), `Secure`+`HttpOnly`.
- **Workspace por usuario**: bajo `APP_DATA_DIR/users/<id>/` viven su
  `platform_settings.json` (cuenta Huawei + MaaS key) y su `terraform/` (config +
  **estado** separado). El middleware liga ese contexto en cada request.
- **Lock por usuario**: un mismo SA no puede lanzar dos deploys/destroys a la vez
  (protege su state); SAs distintos corren en paralelo.

## Pasos

1. **Una VM** (ECS de tu cuenta Huawei, u otra) con Docker + Docker Compose,
   **puertos 80 y 443 abiertos**, y un **dominio** cuyo DNS apunte a su IP pública.

2. **Levantá — zero-config** (no hace falta cargar ningún `.env`):
   ```bash
   docker compose -f docker-compose.hosted.yml up --build -d
   ```
   - **Auth queda activa**: el contenedor **autogenera** `APP_SECRET_KEY` y la
     persiste en el volumen.
   - **Sin dominio → sirve por HTTP en `:80`** (entrás por `http://<IP>/`).
   - **El primer usuario que se registra queda como admin** y gestiona el resto
     (allowlist, resets) desde ⚙ Configuración → **Administración**.

   Opcional, para fijar cosas (HTTPS, allowlist inicial, admins): `cp
   .env.hosted.example .env.hosted`, editá, y agregá `--env-file .env.hosted` al
   comando. Con `APP_DOMAIN=tudominio` Caddy saca el **cert TLS** solo (necesita
   DNS→IP y 80/443 abiertos).

3. **Entrá** a `http://<IP>/` (o `https://tudominio` si pusiste `APP_DOMAIN`). Cada SA:
   - hace login con su email + una contraseña (el primer login crea su cuenta),
   - va a **⚙ Configuración** y carga **su** cuenta Huawei (VPC/subnet/SG/AZ,
     project id, región, bucket) y **su** MaaS API key — o los datos del owner si
     van a desplegar en la cuenta del owner,
   - usa la plataforma normalmente; sus deploys van a **su** state aislado.

## Operación

- **Agregar/quitar SAs (desde la app)**: los **admins** (env `SA_ADMINS`) manejan
  la allowlist desde ⚙ Configuración → **Administración** → "Usuarios autorizados":
  agregan/quitan emails **sin tocar `.env` ni recrear el contenedor**. `SA_ALLOWLIST`
  (env) sigue siendo la base no-removible; lo agregado por UI se guarda en el volumen.
- **Admins**: los emails en `SA_ADMINS` (subset de la allowlist) ven la card
  **Administración**: allowlist, lista de usuarios con **"Resetear contraseña"**
  (borra la credencial → el usuario fija una nueva en su próximo ingreso) y el
  **audit log**. Es el "olvidé mi contraseña": el usuario avisa, un admin lo resetea.
- **Login**: es un overlay sobre la app (la app se ve blureada detrás). El primer
  login de cada SA crea su cuenta con la contraseña que elija.
- **Backups**: todo el estado por-usuario vive en el volumen `appdata`
  (`/app/data`). Respaldalo (incluye states de Terraform y credenciales).
- **Costos**: si los SAs despliegan en **su** cuenta, el costo es de ellos. Si usan
  la del **owner**, cada deploy crea clusters CSS (caros) — coordiná y destruí los
  entornos de demo cuando no se usen.
- **Guardrails de costo** (env, opcionales):
  - `MAX_PIPELINES_PER_USER` (default **0 = sin límite**): tope duro opcional. Sin
    tope, la plataforma **escala el flavor** del cluster y **reparte los workers** de
    Logstash entre las pipelines (más pipelines → menos workers c/u, mín 1) para no
    sobre-suscribir el nodo — ver `_capacity_for`.
  - `ENV_TTL_HOURS` (default 0 = off): auto-destruye entornos demo más viejos que N
    horas (usa las creds guardadas del deploy). Útil para demos efímeras; **destruye
    infra sola**, así que activalo con criterio. Chequeo cada `ENV_TTL_CHECK_SECONDS`.
  - Cada deploy/destroy queda en el **audit log** (panel Administración).

## Seguridad de las credenciales

- El `platform_settings.json` por-usuario (MaaS key + OBS AK/SK) se guarda **cifrado
  en reposo** con Fernet (AES-CBC + HMAC), clave derivada de `APP_SECRET_KEY`. Los
  archivos planos previos se migran a cifrado en el próximo guardado. Si perdés
  `APP_SECRET_KEY`, esos settings dejan de ser legibles (guardá el secreto).
- Aun así, para el modo "cuenta del owner" conviene una **AK/SK dedicada y
  revocable** en vez de tu key principal.

## Fase 2 — estado

- ✅ **Cola de jobs**: el deploy corre en background y sobrevive un refresh del browser.
- ✅ **Guardrails de costo**: cap de pipelines configurable + reaper TTL opcional.
- ✅ **Audit log**: quién hizo qué, en el panel de Administración.
- ⏳ **Estado de Terraform remoto en OBS**: pendiente (hoy el state vive en el volumen
  persistente `appdata`; para durabilidad extra, respaldá ese volumen).
