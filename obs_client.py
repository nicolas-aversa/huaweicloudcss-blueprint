"""
obs_client.py
=============

Cliente para subir objetos a OBS (Huawei Cloud Object Storage Service)
usado por el endpoint ``/api/v1/terraform/deploy``.

Decisiones de diseño:

- **Creds desde el constructor, no desde el env.** El operador de preventa
  llena AK/SK en el step 3 del wizard (Input - OBS). Esos valores son los
  que viajan al backend en el request del deploy. El backend instancia
  ``OBSClient(access_key_id=..., secret_access_key=..., endpoint=..., bucket=...)``
  con esos datos del form. No leemos de ``.env`` ni de ``terraform.tfvars`` —
  cada deploy puede ser contra cuentas distintas.

- **SDK oficial** (``esdk-obs-python``). Lazy import: si la lib no está
  instalada, falla con mensaje accionable solo cuando se intenta usar.

- **Cliente per-instancia**, no singleton. Cada deploy puede ser contra una
  cuenta distinta (vendedor demando demo a un cliente nuevo). Singleton no
  serviría.
"""

from __future__ import annotations

from typing import Any


class OBSConfigError(RuntimeError):
    """El SDK no está instalado o faltan parámetros obligatorios."""


class OBSUploadError(RuntimeError):
    """OBS rechazó el upload (errores 4xx/5xx, problemas de red)."""


class OBSClient:
    """Cliente OBS con credenciales del form, no del env.

    Parameters
    ----------
    access_key_id, secret_access_key:
        AK/SK de la cuenta Huawei Cloud del operador.
    endpoint:
        URL del servicio OBS regional (ej. ``https://obs.la-south-2.myhuaweicloud.com``).
    bucket:
        Bucket destino donde se suben los objetos. El bucket tiene que
        existir y la cuenta tiene que tener permiso de PutObject.

    Raises
    ------
    OBSConfigError
        Si falta algún parámetro o si ``esdk-obs-python`` no está instalada.
    """

    def __init__(
        self,
        access_key_id: str,
        secret_access_key: str,
        endpoint: str,
        bucket: str,
    ) -> None:
        if not access_key_id or not secret_access_key:
            raise OBSConfigError(
                "Faltan access_key_id / secret_access_key. Asegurate de que "
                "el operador haya completado las credenciales OBS en el "
                "step 3 (Input) del wizard."
            )
        if not endpoint:
            raise OBSConfigError("Falta `endpoint` de OBS.")
        if not bucket:
            raise OBSConfigError("Falta `bucket` de OBS.")

        try:
            from obs import ObsClient as _SdkClient  # type: ignore[import-not-found]
        except ImportError as exc:
            raise OBSConfigError(
                "El SDK de Huawei OBS no está instalado. Ejecutá "
                "`pip install -r requirements.txt` (instala esdk-obs-python)."
            ) from exc

        # Normalizamos el endpoint para que el SDK lo acepte. El SDK quiere
        # el host SIN protocolo en algunos casos; aceptamos ambas variantes
        # de input y dejamos pasar tal como vino.
        self._endpoint = endpoint
        self._bucket = bucket
        self._client: Any = _SdkClient(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            server=endpoint,
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    def put_object(self, key: str, data: bytes | str) -> None:
        """Sube un objeto al bucket. ``data`` puede ser bytes o str.

        Raises
        ------
        OBSUploadError
            Si OBS rechaza el upload (2xx vs error). El mensaje incluye el
            status y el detalle del SDK para que el frontend lo muestre.
        """
        if isinstance(data, bytes):
            try:
                data = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OBSUploadError(
                    f"El contenido para `{key}` no es UTF-8: {exc}"
                ) from exc

        try:
            resp = self._client.putContent(self._bucket, key, content=data)
        except Exception as exc:
            # El SDK puede tirar errores de red/auth — los envolvemos.
            raise OBSUploadError(
                f"Fallo al subir `{key}` a OBS: {exc}"
            ) from exc

        status_code = getattr(resp, "status", None)
        if status_code is None or status_code >= 300:
            err_msg = (
                getattr(resp, "errorMessage", "")
                or getattr(resp, "reason", "")
                or "(sin detalle)"
            )
            raise OBSUploadError(
                f"OBS rechazó el upload de `{key}` con status {status_code}: {err_msg}"
            )

    def put_file(self, key: str, file_path: str) -> None:
        """Sube un archivo local a OBS streameando de disco (``putFile``).

        Para archivos grandes (los datasets de demo llegan a ~100 MB):
        ``putContent`` carga todo en memoria y sufre timeouts; ``putFile``
        streamea y es la vía recomendada por el SDK.
        """
        try:
            resp = self._client.putFile(self._bucket, key, file_path)
        except Exception as exc:
            raise OBSUploadError(f"Fallo al subir `{key}` a OBS: {exc}") from exc
        status_code = getattr(resp, "status", None)
        if status_code is None or status_code >= 300:
            err_msg = (
                getattr(resp, "errorMessage", "")
                or getattr(resp, "reason", "")
                or "(sin detalle)"
            )
            raise OBSUploadError(
                f"OBS rechazó el upload de `{key}` con status {status_code}: {err_msg}"
            )

    def object_exists(self, key: str) -> bool:
        """True si el objeto existe en el bucket (metadata HEAD, sin descargar)."""
        try:
            resp = self._client.getObjectMetadata(self._bucket, key)
        except Exception as exc:
            raise OBSUploadError(f"Fallo consultando `{key}` en OBS: {exc}") from exc
        status_code = getattr(resp, "status", None)
        return status_code is not None and status_code < 300

    def ensure_bucket(self, region: str = "") -> bool:
        """Crea el bucket si no existe (para el bucket de demos del SA).

        Devuelve True si lo creó, False si ya existía. ``region`` es la
        location de OBS (ej. la-south-2) — obligatoria al crear en regiones
        distintas de la default del endpoint.
        """
        try:
            head = self._client.headBucket(self._bucket)
        except Exception as exc:
            raise OBSConfigError(f"Fallo consultando el bucket: {exc}") from exc
        if getattr(head, "status", 500) < 300:
            return False
        try:
            resp = self._client.createBucket(self._bucket, location=region or None)
        except Exception as exc:
            raise OBSConfigError(f"Fallo creando el bucket `{self._bucket}`: {exc}") from exc
        status_code = getattr(resp, "status", None)
        if status_code is None or status_code >= 300:
            err_msg = getattr(resp, "errorMessage", "") or getattr(resp, "reason", "") or "(sin detalle)"
            raise OBSConfigError(
                f"OBS rechazó la creación del bucket `{self._bucket}` "
                f"con status {status_code}: {err_msg}"
            )
        return True

    def prefix_has_objects(self, prefix: str) -> bool:
        """True si hay al menos un objeto REAL bajo ``prefix`` (ignora los
        marcadores de carpeta: keys terminadas en ``/`` o de 0 bytes)."""
        try:
            resp = self._client.listObjects(self._bucket, prefix=prefix, max_keys=10)
        except Exception as exc:
            raise OBSUploadError(f"Fallo listando `{prefix}` en OBS: {exc}") from exc
        if getattr(resp, "status", 500) >= 300:
            err_msg = getattr(resp, "errorMessage", "") or getattr(resp, "reason", "") or "(sin detalle)"
            raise OBSUploadError(f"OBS rechazó el listado de `{prefix}`: {err_msg}")
        contents = list(getattr(getattr(resp, "body", None), "contents", None) or [])
        return any(
            getattr(o, "key", "") and not str(o.key).endswith("/")
            and int(getattr(o, "size", 0) or 0) > 0
            for o in contents
        )

    def delete_prefix(self, prefix: str) -> int:
        """Borra todos los objetos bajo ``prefix`` (paginado). Devuelve cuántos
        borró. Best-effort: loguea y sigue si algo falla (no rompe el deploy).

        Se usa para limpiar un prefijo OBS antes de subir el dataset fresco, así
        cada deploy arranca limpio por prefijo (sin acumulación ni archivos
        cruzados de corridas anteriores).
        """
        deleted = 0
        try:
            marker = None
            while True:
                resp = self._client.listObjects(
                    self._bucket, prefix=prefix, marker=marker, max_keys=1000
                )
                if getattr(resp, "status", 500) >= 300:
                    break
                body = getattr(resp, "body", None)
                contents = list(getattr(body, "contents", None) or [])
                for obj in contents:
                    key = getattr(obj, "key", None)
                    if not key:
                        continue
                    try:
                        self._client.deleteObject(self._bucket, key)
                        deleted += 1
                    except Exception:  # noqa: BLE001 — best-effort por objeto
                        pass
                if not getattr(body, "is_truncated", False):
                    break
                marker = getattr(body, "next_marker", None) or (
                    contents[-1].key if contents else None
                )
                if not marker:
                    break
        except Exception as exc:  # noqa: BLE001 — best-effort
            print(f"[obs] delete_prefix({prefix!r}) falló: {exc!r}")
        return deleted

    # No muestrear objetos gigantes en memoria (el sample solo necesita la 1ra línea).
    _MAX_SAMPLE_OBJECT_BYTES = 64 * 1024 * 1024
    # Cuántos bytes pedimos en el range read (sin descargar el archivo entero).
    # 512 KB alcanza para varias líneas de cualquier log, incluso JSON multiline.
    _SAMPLE_RANGE_BYTES = 512 * 1024

    def read_sample(self, prefix: str = "") -> tuple[str, int, str]:
        """Lee el primer objeto REAL bajo ``prefix`` y devuelve su primera línea no vacía.

        Saltea los marcadores de carpeta (keys terminadas en ``/`` o de 0 bytes) y
        descomprime ``.gz`` (los datos productivos — ej. traces de CTS — suelen venir
        gzipeados). El download usa ``loadStreamInMemory=True``: sin eso el SDK devuelve
        un ``ObjectStream`` (no bytes) cuyos atributos desconocidos son ``None`` — llamar
        ``.splitlines`` daba ``'NoneType' object is not callable``.

        Returns
        -------
        (sample_line, total_objects, object_key)

        Raises
        ------
        OBSUploadError
            Si no hay objetos o si falla la lectura.
        """
        try:
            resp = self._client.listObjects(
                self._bucket, prefix=prefix or None, max_keys=1000
            )
            if getattr(resp, "status", 500) >= 300:
                raise OBSUploadError(
                    f"OBS listObjects falló: status {getattr(resp, 'status', '?')}"
                )
            body = getattr(resp, "body", None)
            contents = list(getattr(body, "contents", None) or [])
            # Objetos REALES: fuera los marcadores de carpeta (key con '/' final o 0 bytes).
            real = [
                o for o in contents
                if getattr(o, "key", "") and not getattr(o, "key", "").endswith("/")
                and (getattr(o, "size", 0) or 0) > 0
            ]
            if not real:
                raise OBSUploadError(
                    f"No se encontraron objetos bajo '{prefix}' en el bucket '{self._bucket}'."
                )
            obj = real[0]
            key = getattr(obj, "key", "")
            size = getattr(obj, "size", 0) or 0
            total = len(real)   # con is_truncated es "1000+", alcanza para el preview

            is_gz = key.endswith(".gz")

            if is_gz and size > self._MAX_SAMPLE_OBJECT_BYTES:
                # .gz: no podemos range-read (los datos están comprimidos).
                # El size reportado es el comprimido — si pasa de 64 MB comprimido
                # es un archivo enorme. Pedir uno más chico.
                raise OBSUploadError(
                    f"El objeto '{key}' pesa {size / 1048576:.0f} MB comprimido — demasiado grande. "
                    "Dejá un archivo más chico bajo el prefijo o ajustá el prefijo."
                )

            if not is_gz and size > self._SAMPLE_RANGE_BYTES:
                # Range read: solo los primeros 512 KB. Sin descargar el archivo
                # entero (puede pesar cientos de MB). Suficiente para varias líneas.
                from obs import GetObjectHeader
                content_resp = self._client.getObject(
                    self._bucket, key, loadStreamInMemory=True,
                    headers=GetObjectHeader(range=f"0-{self._SAMPLE_RANGE_BYTES - 1}"),
                )
            else:
                # Archivo chico o .gz: download completo.
                content_resp = self._client.getObject(
                    self._bucket, key, loadStreamInMemory=True
                )
            if getattr(content_resp, "status", 500) >= 300:
                raise OBSUploadError(
                    f"OBS getObject falló para '{key}': status {getattr(content_resp, 'status', '?')}"
                )
            buf = getattr(getattr(content_resp, "body", None), "buffer", None)
            if not buf:
                raise OBSUploadError(f"Objeto '{key}' está vacío.")
            if key.endswith(".gz"):
                import gzip
                try:
                    buf = gzip.decompress(buf)
                except OSError as exc:
                    raise OBSUploadError(
                        f"No se pudo descomprimir '{key}' (.gz corrupto o formato no soportado)."
                    ) from exc
            text = buf.decode("utf-8", errors="replace") if isinstance(buf, bytes) else str(buf)
            # Tomar hasta 3 líneas no vacías. Con range read la última línea puede
            # estar truncada — la descartamos si el archivo era más grande que el
            # range (no tenemos el final del archivo para confirmar que está completa).
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            # Si hicimos range read y hay líneas, descartar la última (potencialmente truncada).
            if not is_gz and size > self._SAMPLE_RANGE_BYTES and len(lines) > 1:
                lines = lines[:-1]
            if lines:
                return "\n".join(lines[:3]), total, key
            raise OBSUploadError(f"Objeto '{key}' no tiene líneas no vacías.")
        except OBSUploadError:
            raise
        except Exception as exc:
            raise OBSUploadError(f"Error leyendo sample de OBS: {exc}") from exc

    def close(self) -> None:
        """Libera el socket del SDK. Llamar al final del request si querés ser prolijo."""
        try:
            self._client.close()
        except Exception:
            pass
