# Panel de la ECS

Webapp mínima para **prender y apagar desde el celular** la ECS que hostea la plataforma, sin entrar
a la consola de Huawei Cloud. Al encender **abre los puertos 80 y 443** en el security group; al
apagar **los cierra**.

## El estado en vivo

El panel poletea cada 5 s mientras hay una transición en curso, y para cuando el estado se estabiliza.

Lo que lo hace menos obvio de lo que parece: **la API de ECS no cambia `status` durante la
transición**. Un `os-start` deja la instancia reportando `SHUTOFF` con
`OS-EXT-STS:task_state: "powering-on"` hasta que termina (y un `os-stop`, `ACTIVE` con
`powering-off`). Leyendo solo `status`, el panel veía el estado viejo 1,5 s después de apretar el
botón, lo daba por definitivo y **cortaba el polling** — se quedaba mostrando "Apagada" para
siempre, mientras la máquina arrancaba.

Hay dos redes, a propósito:

1. **`ecs_status` combina `status` con `task_state`** y devuelve `TRANSICION` si hay una operación
   en curso.
2. **El frente recuerda qué acción pediste**: un `SHUTOFF` que llega justo después de un `start` se
   trata como transición aunque el backend todavía no lo sepa.

La segunda no es redundante: **el Worker y la función se despliegan por separado**, así que el panel
tiene que aguantar hablando con una función vieja que todavía miente. Hay un test de eso.

Las acciones además **reconcilian el security group**: apretar Apagar sobre una máquina ya apagada
cierra los puertos igual. Sin eso, si el arranque fallaba después de abrirlos quedabas en
"apagada + puertos abiertos" sin forma de salir desde el panel.

## Por qué son dos piezas

FunctionGraph **no ofrece ninguna puerta HTTP gratis**. Las HTTP functions solo aceptan triggers de
APIG (dedicado, facturado por hora) o APIC (solo AP-Singapore), y el
[APIG compartido está dado de baja](https://support.huaweicloud.com/intl/en-us/usermanual-apig/apig-ug-180307020.html)
para cuentas nuevas. En LA-Santiago no queda ninguna opción sin costo.

Así que el frente vive afuera y la lógica adentro:

```
celular → Cloudflare Worker  ──(API de invocación)──→  FunctionGraph  ──→  ECS + VPC
          password + cookie                            agency con
          (worker.js)                                  permisos reales (index.py)
```

Partirlo así tiene una ventaja que no es accidental: **la credencial que guarda Cloudflare se
scopea a "invocar esta función" y nada más**. Si se filtra, lo máximo que consigue alguien es
prenderte y apagarte la máquina — no tocar ECS ni VPC. Los permisos anchos se quedan del lado de
Huawei, en la agency.

## Qué es cada archivo

| | |
|---|---|
| `index.py` | La función de FunctionGraph: ECS y reglas de security group. ~295 líneas, sin dependencias fuera de la stdlib. Reemplaza a la función de start/stop que ya tenías. |
| `worker.js` | El frente en Cloudflare: sirve la página, valida la password, invoca la función. |
| `wrangler.toml` | Config del Worker. **Los secretos no van acá.** |
| `test_panel.py` | 46 tests de la función. `py -m pytest ecs-panel/ -q` desde la raíz del repo. |
| `worker.test.mjs` | 41 checks: el Worker con Web Crypto real y `fetch` interceptado, **más el JS del panel** corrido en un sandbox con timers controlados y una ECS que tarda en arrancar. Ese último bloque es el que hacía falta: el script del panel vive dentro de un template string y hasta ahora no lo ejecutaba ningún test. `node ecs-panel/worker.test.mjs` (desde esta carpeta). |

## Despliegue

### 1. La función (Huawei)

**Agency de IAM** delegada en FunctionGraph, con permisos de **ECS** (consultar, arrancar, detener)
y **VPC** (crear, listar y borrar reglas de security group). Si ya tenías una para el encendido
programado, seguramente solo tiene ECS: **agregale VPC**, o el panel encenderá bien pero fallará al
abrir los puertos.

Crear una función Python, pegar `index.py`, handler `index.handler`, timeout 60 s.

**Red: public access, no VPC access.** La función sólo habla con las APIs de control de Huawei, que
son públicas. Adentro de una VPC [queda aislada de
internet](https://support.huaweicloud.com/intl/en-us/functiongraph_faq/functiongraph_03_0834.html) y
necesitaría un NAT gateway sólo para volver al punto de partida; el síntoma sería un timeout en la
primera llamada, sin ninguna pista de que el problema es la red.

Configuración en Environment/User Data:

| Clave | Obligatoria | Qué es |
|---|---|---|
| `project_id` | sí | Project ID de la región. |
| `region` | sí | Ej. `la-south-2`. |
| `ecs_id` | sí | La instancia a manejar. |
| `sg_id` | sí | El security group donde se abren y cierran los puertos. |
| `app_url` | no | URL de la plataforma (ej. `https://<EIP>.sslip.io`), para el link "Abrir la plataforma". No se puede derivar sola: el frente vive en el dominio del Worker. |
| `ports` | no | Default `80,443`. |
| `source_cidr` | no | Default `0.0.0.0/0`. |
| `rule_marker` | no | Default `ecs-panel:auto`. Ver "Cómo decide qué borrar". |

**No hace falta ningún trigger HTTP.** El Worker la invoca por la API.

### 2. El usuario IAM para el Worker

Creá un usuario IAM **dedicado** cuyo único permiso sea invocar esa función
(`functiongraph:function:invoke`). No reutilices tu usuario ni uno con permisos anchos: la gracia de
esta arquitectura es justamente que esa credencial no pueda hacer nada más.

### 3. El Worker (Cloudflare)

En `wrangler.toml` completá `HW_REGION`, `HW_PROJECT_ID` y `FUNCTION_URN` (lo muestra la consola de
FunctionGraph). Después, los secretos — **nunca en el archivo**:

```bash
wrangler secret put PANEL_PASSWORD   # larga y aleatoria, ver "Seguridad"
wrangler secret put PANEL_SECRET     # otro string largo al azar, para firmar la cookie
wrangler secret put HW_USER          # el usuario IAM del paso 2
wrangler secret put HW_DOMAIN        # la cuenta (domain name) de ese usuario
wrangler secret put HW_PASSWORD
wrangler deploy
```

La URL que imprime es la que guardás como bookmark en el celular.

### 4. El encendido programado (opcional)

Un trigger **Timer** sobre la función, con `user_event` = `<ecs_id>,startup` o
`<ecs_id>,shutdown`. Es el mismo contrato que la función anterior, así que un timer ya configurado
sigue funcionando apuntado a esta — y ahora además maneja los puertos.

## Seguridad

La URL del Worker es pública: cualquiera que la adivine llega al **formulario de password**. No a
las acciones, que exigen una cookie firmada con HMAC-SHA256; sin `PANEL_SECRET` no se puede
fabricar. La cookie es `HttpOnly; Secure; SameSite=Strict` y dura 7 días, así que desde el celular
se loguea una vez y queda.

**`PANEL_PASSWORD` tiene que ser larga y aleatoria.** No hay rate limiting: sin estado compartido
entre invocaciones no hay dónde contar intentos. La comparación sí es de tiempo constante (se
comparan los SHA-256, no los strings, para que el tiempo tampoco dependa del largo).

## Cómo decide qué borrar

Ese security group tiene la regla de SSH y las que haya puesto Terraform. El panel **solo borra
reglas que él mismo creó**, y las reconoce porque el campo `description` es exactamente
`rule_marker`. Nunca borra por puerto ni por posición: un match por puerto se llevaría puesta una
regla agregada a mano, y uno por posición es una bomba de tiempo.

Por eso, si alguien abrió el 80/443 a mano, el panel igual crea la suya: si no, al apagar no
tendría qué borrar y el puerto quedaría abierto.

## Detalles que no son arbitrarios

- **Encender abre los puertos ANTES de arrancar.** Caddy pide y renueva el certificado de Let's
  Encrypt al bootear, y necesita el 80/443 alcanzable; con los puertos cerrados el challenge ACME
  falla y la plataforma queda sin HTTPS.
- **Apagar detiene ANTES de cerrar los puertos.** Al revés, si el stop falla queda una máquina
  encendida e inalcanzable.
- **El apagado es `SOFT`, no `HARD`** (la versión anterior usaba HARD). Un `HARD` es desenchufar la
  máquina. Aunque el estado de Terraform ahora viva en OBS, adentro siguen estando el `users.json`,
  los settings cifrados de cada SA, los casos creados y el registro de pipelines.
- **La función no sirve ninguna página ni valida contraseñas.** Eso lo hace el Worker. Lo único que
  la protege es la credencial IAM que hace falta para invocarla — por eso esa credencial se scopea a
  invocar esta función y nada más.

## Antes de confiar en el panel

**La app tiene que volver sola al encender.** Los dos servicios ya tienen `restart: unless-stopped`,
así que alcanza con que el daemon de Docker arranque en el boot:

```bash
sudo systemctl enable docker
sudo systemctl is-enabled docker    # debe decir "enabled"
sudo reboot
```

Esperá ~60 s y abrí la URL de la plataforma **sin entrar por SSH**. Si carga, el panel sirve.

La EIP no cambia al detener la ECS (sigue bindeada, y facturándose igual que el disco), así que la
URL `<EIP>.sslip.io` se mantiene y el apagado es transparente para la app.

## Verificación la primera vez

Mirá el security group en la consola después de cada paso:

1. Abrir la URL del Worker sin sesión → pide password. Con una incorrecta → rechaza.
2. **Encender** → aparece una regla 80/443 con el `description` del marcador, y la ECS pasa a
   `ACTIVE` en ~40-60 s.
3. Abrir la plataforma → carga con candado verde (si el cert no renovó, revisá el orden de los
   puertos).
4. **Apagar** → la ECS pasa a `SHUTOFF` y la regla marcada desaparece.
5. **La regla de SSH sigue ahí.** Este es el chequeo que importa.

## Limitación conocida

El panel **no chequea si hay un deploy en curso** antes de apagar. Un `terraform apply` dura 5-25
minutos y cortar la VM en el medio deja recursos a medio crear en Huawei. Por ahora: mirá Actividad
en la plataforma antes de apagar. El paso siguiente natural es que la función consulte
`/api/v1/terraform/jobs` y se niegue si hay uno corriendo.
