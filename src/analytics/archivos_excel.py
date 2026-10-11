"""Lectura de Excel compatible con los handles de sincronización de OneDrive."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import BinaryIO


def abrir_lectura(ruta: str | Path) -> BinaryIO:
    """Abrir sólo para leer, permitiendo los handles de renombrado de Windows.

    No cambia permisos ni desbloquea otros procesos. El llamador debe comprobar
    cambios concurrentes cuando use el contenido como fuente de una copia.
    """
    if os.name != "nt":
        return open(ruta, "rb")
    import msvcrt
    import win32con
    import win32file
    handle = win32file.CreateFile(
        str(Path(ruta).resolve()), win32con.GENERIC_READ,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        None, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None,
    )
    entero = handle.Detach()
    try:
        fd = msvcrt.open_osfhandle(entero, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        win32file.CloseHandle(entero)
        raise
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def copiar_lectura(origen: str | Path, destino: str | Path) -> None:
    """Copiar desde un handle compartido; conservar los metadatos de la fuente."""
    origen, destino = Path(origen), Path(destino)
    antes = origen.stat()
    with abrir_lectura(origen) as lectura, destino.open("wb") as escritura:
        shutil.copyfileobj(lectura, escritura, length=1024 * 1024)
    despues = origen.stat()
    if (antes.st_size, antes.st_mtime_ns, antes.st_ino) != (despues.st_size, despues.st_mtime_ns, despues.st_ino):
        raise RuntimeError("La fuente cambió durante la copia; no se publica el reporte.")
    shutil.copystat(origen, destino)
