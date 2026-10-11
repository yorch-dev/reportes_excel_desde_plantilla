"""Combinar los tres Excel generados sin volver a consultar sus fuentes.

Combina las partes XML de los libros planos. La validación recorre por filas y
compara valores, tipos, encabezados y formatos numéricos antes de reemplazar
el archivo principal. No materializa las tablas en un DataFrame.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import posixpath
import re
from tempfile import TemporaryDirectory
from typing import Mapping
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from .archivos_excel import abrir_lectura

_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_MAIN = _NS["x"]
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_FORMATO_BUILTIN = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00",
    9: "0%", 10: "0.00%", 11: "0.00E+00", 12: "# ?/?", 13: "# ??/??",
    14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss",
    22: "m/d/yy h:mm", 37: "#,##0 ;(#,##0)", 38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;[Red](#,##0.00)",
    45: "mm:ss", 46: "[h]:mm:ss", 47: "mmss.0", 48: "##0.0E+0", 49: "@",
}


@dataclass(frozen=True)
class _Fuente:
    archivo: str
    hoja_origen: str
    hoja_destino: str
    tabla: str


_FUENTES = (
    _Fuente("reporte_colas.xlsx", "Datos", "Colas", "t_datos"),
    _Fuente("reporte_workloads.xlsx", "Workloads", "Workloads", "t_workloads"),
    _Fuente("reporte_ejecutivos.xlsx", "Ejecutivos", "Ejecutivos", "t_ejecutivos"),
)


@dataclass(frozen=True)
class _Tabla:
    nombre: str
    hoja: str
    ruta_xml: str
    referencia: str
    fila: int
    columna: int
    ultima_fila: int
    ultima_columna: int
    encabezados: tuple[str, ...]
    atributos: tuple[tuple[str, str], ...]
    estilo: tuple[tuple[str, str], ...]
    totales: int


@dataclass(frozen=True)
class _Libro:
    hojas: tuple[str, ...]
    tablas: Mapping[str, _Tabla]
    fecha_1904: bool


@dataclass(frozen=True)
class _Firma:
    referencia: str
    encabezados: tuple[str, ...]
    filas: int
    atributos: tuple[tuple[str, str], ...]
    estilo: tuple[tuple[str, str], ...]
    formatos_encabezados: tuple[str, ...]
    formatos_datos: tuple[str, ...]
    celdas_con_valor: int
    digest: str


def _estado_archivo(ruta: Path) -> tuple[int, int] | None:
    if not ruta.exists():
        return None
    estado = ruta.stat()
    return estado.st_size, estado.st_mtime_ns


def _coordenada(direccion: str) -> tuple[int, int]:
    patron = re.fullmatch(r"\$?([A-Z]+)\$?(\d+)", direccion)
    if patron is None:
        raise ValueError(f"Coordenada Excel inválida: {direccion}")
    columna = 0
    for letra in patron[1]:
        columna = columna * 26 + ord(letra) - 64
    return int(patron[2]), columna


def _relaciones(z: ZipFile, ruta: str) -> dict[str, str]:
    relaciones = posixpath.join(
        posixpath.dirname(ruta), "_rels", posixpath.basename(ruta) + ".rels"
    )
    if relaciones not in z.namelist():
        return {}
    resultado = {}
    for elemento in ET.fromstring(z.read(relaciones)).findall(f"{{{_REL}}}Relationship"):
        if elemento.get("TargetMode") == "External":
            continue
        destino = elemento.get("Target", "")
        destino = (destino.lstrip("/") if destino.startswith("/") else
                   posixpath.normpath(posixpath.join(posixpath.dirname(ruta), destino)))
        if destino.startswith("../"):
            raise ValueError("Relación XML fuera del libro.")
        resultado[elemento.get("Id")] = destino
    return resultado


def _definiciones_dxf(z: ZipFile) -> list[str]:
    """Resolver estilos diferenciales; sus IDs son locales a cada libro."""
    if "xl/styles.xml" not in z.namelist():
        return []
    estilos = ET.fromstring(z.read("xl/styles.xml"))

    def semantica(elemento: ET.Element) -> tuple:
        # numFmtId también puede cambiar al integrar formatos de otro libro;
        # formatCode expresa el mismo formato de manera independiente del ID.
        atributos = tuple(sorted((k, v) for k, v in elemento.attrib.items()
                                 if not (elemento.tag == f"{{{_MAIN}}}numFmt"
                                         and k == "numFmtId" and elemento.get("formatCode") is not None)))
        return elemento.tag, atributos, elemento.text or "", tuple(semantica(c) for c in elemento)

    return [json.dumps(semantica(dxf), ensure_ascii=True, separators=(",", ":"))
            for dxf in estilos.findall("x:dxfs/x:dxf", _NS)]


def _leer_libro(z: ZipFile) -> _Libro:
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    relaciones = _relaciones(z, "xl/workbook.xml")
    nombres_hojas = []
    tablas = {}
    estilos_dxf = _definiciones_dxf(z)
    for hoja in workbook.findall("x:sheets/x:sheet", _NS):
        nombre = hoja.get("name")
        nombres_hojas.append(nombre)
        ruta_hoja = relaciones[hoja.get(f"{{{_OFFICE_REL}}}id")]
        for ruta in _relaciones(z, ruta_hoja).values():
            if not ruta.startswith("xl/tables/") or not ruta.endswith(".xml"):
                continue
            elemento = ET.fromstring(z.read(ruta))
            referencia = elemento.get("ref", "")
            limites = referencia.split(":")
            fila, columna = _coordenada(limites[0])
            ultima_fila, ultima_columna = _coordenada(limites[-1])
            encabezados = tuple(c.get("name") for c in elemento.findall("x:tableColumns/x:tableColumn", _NS))
            if (ultima_fila < fila or ultima_columna < columna
                    or len(encabezados) != ultima_columna - columna + 1
                    or len(set(encabezados)) != len(encabezados)):
                raise ValueError(f"Dimensiones o encabezados inválidos en {referencia}.")
            nombre_tabla = elemento.get("name")
            if nombre_tabla in tablas:
                raise ValueError(f"Tabla repetida: {nombre_tabla}")
            estilo = elemento.find("x:tableStyleInfo", _NS)
            atributos = []
            for clave, valor in elemento.attrib.items():
                if clave == "id" or clave.startswith("{"):
                    continue
                if clave.endswith("DxfId"):
                    indice = int(valor)
                    if not 0 <= indice < len(estilos_dxf):
                        raise ValueError(f"Índice de estilo diferencial inválido en {nombre_tabla}.")
                    valor = estilos_dxf[indice]
                atributos.append((clave, valor))
            tablas[nombre_tabla] = _Tabla(
                nombre_tabla, nombre, ruta_hoja, referencia,
                fila, columna, ultima_fila, ultima_columna, encabezados,
                # Se comparan estilos por su contenido, no por el ID que Excel
                # reasigna al integrar tablas provenientes de varios libros.
                tuple(sorted(atributos)),
                tuple(sorted(estilo.attrib.items())) if estilo is not None else (),
                int(elemento.get("totalsRowCount", "0")),
            )
    propiedades = workbook.find("x:workbookPr", _NS)
    fecha_1904 = propiedades is not None and propiedades.get("date1904", "0") in ("1", "true")
    return _Libro(tuple(nombres_hojas), tablas, fecha_1904)


def _texto(elemento: ET.Element | None) -> str:
    if elemento is None:
        return ""
    partes = []
    for hijo in elemento:
        if hijo.tag == f"{{{_MAIN}}}t":
            partes.append(hijo.text or "")
        elif hijo.tag == f"{{{_MAIN}}}r":
            texto = hijo.find("x:t", _NS)
            partes.append(texto.text or "" if texto is not None else "")
    texto = "".join(partes)
    # Una pasada respeta el escape de un literal como _x005F_x000D_.
    return re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m[1], 16)), texto)


def _textos_compartidos(z: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    textos = []
    with z.open("xl/sharedStrings.xml") as archivo:
        eventos = ET.iterparse(archivo, events=("start", "end"))
        _, raiz = next(eventos)
        for evento, elemento in eventos:
            if evento == "end" and elemento.tag == f"{{{_MAIN}}}si":
                textos.append(_texto(elemento))
                elemento.clear()
                raiz.clear()
    return textos


def _formatos_numero(z: ZipFile) -> list[str]:
    if "xl/styles.xml" not in z.namelist():
        return ["General"]
    estilos = ET.fromstring(z.read("xl/styles.xml"))
    formatos = dict(_FORMATO_BUILTIN)
    formatos.update({int(f.get("numFmtId")): f.get("formatCode")
                     for f in estilos.findall("x:numFmts/x:numFmt", _NS)})
    resultado = []
    for estilo in estilos.findall("x:cellXfs/x:xf", _NS):
        identificador = int(estilo.get("numFmtId", "0"))
        resultado.append(formatos.get(identificador, f"builtin:{identificador}"))
    return resultado or ["General"]


def _valor(celda: ET.Element, textos: list[str]) -> tuple[str, str] | None:
    valor = celda.find("x:v", _NS)
    contenido = valor.text if valor is not None else None
    tipo = celda.get("t", "n")
    if tipo == "inlineStr":
        return "s", _texto(celda.find("x:is", _NS))
    if contenido is None:
        return None
    if tipo == "s":
        indice = int(contenido)
        if not 0 <= indice < len(textos):
            raise ValueError("Índice de texto compartido inválido.")
        return "s", textos[indice]
    if tipo == "str":
        return "s", contenido
    if tipo == "n":
        try:
            numero = Decimal(contenido)
        except InvalidOperation:
            raise ValueError("Celda numérica inválida.") from None
        if not numero.is_finite():
            raise ValueError("Celda numérica no finita.")
        return "n", str(numero.normalize()) if numero else "0"
    if tipo == "b":
        if contenido not in ("0", "1"):
            raise ValueError("Celda booleana inválida.")
        return "b", contenido
    if tipo in ("e", "d"):
        return tipo, contenido
    raise ValueError(f"Tipo de celda no contemplado: {tipo}")


def _firma_tabla(z: ZipFile, tabla: _Tabla) -> _Firma:
    textos = _textos_compartidos(z)
    formatos = _formatos_numero(z)
    formatos_columnas = {}
    formatos_filas = {}
    digest = sha256()
    celdas_con_valor = 0
    encabezados_xml = {}
    fila_anterior = 0
    datos_hoja = None
    with z.open(tabla.ruta_xml) as archivo:
        eventos = ET.iterparse(archivo, events=("start", "end"))
        _, raiz = next(eventos)
        for evento, elemento in eventos:
            if evento != "end":
                if elemento.tag == f"{{{_MAIN}}}sheetData":
                    datos_hoja = elemento
                continue
            if elemento.tag == f"{{{_MAIN}}}col":
                estilo = int(elemento.get("style", "0"))
                for columna in range(max(tabla.columna, int(elemento.get("min"))),
                                     min(tabla.ultima_columna, int(elemento.get("max"))) + 1):
                    formatos_columnas[columna] = estilo
                elemento.clear()
            elif elemento.tag == f"{{{_MAIN}}}row":
                fila = int(elemento.get("r"))
                if fila <= fila_anterior:
                    raise ValueError(f"Filas fuera de orden en {tabla.nombre}.")
                fila_anterior = fila
                if tabla.fila <= fila <= tabla.ultima_fila:
                    celdas = []
                    estilo_fila = (int(elemento.get("s", "0"))
                                   if elemento.get("customFormat") in ("1", "true") else None)
                    estilos_primera_fila = {}
                    columna_anterior = 0
                    for celda in elemento.findall("x:c", _NS):
                        fila_celda, columna = _coordenada(celda.get("r", ""))
                        if fila_celda != fila or columna <= columna_anterior:
                            raise ValueError(f"Celdas fuera de orden en {tabla.nombre}.")
                        columna_anterior = columna
                        if not tabla.columna <= columna <= tabla.ultima_columna:
                            continue
                        estilo = int(celda.get("s", str(estilo_fila if estilo_fila is not None
                                                        else formatos_columnas.get(columna, 0))))
                        if not 0 <= estilo < len(formatos):
                            raise ValueError("Índice de estilo inválido.")
                        if fila in (tabla.fila, tabla.fila + 1):
                            estilos_primera_fila[columna] = estilo
                        valor = _valor(celda, textos)
                        formula = celda.find("x:f", _NS)
                        formula_texto = formula.text or "" if formula is not None else None
                        if fila == tabla.fila:
                            if valor is None or valor[0] != "s":
                                raise ValueError(f"Encabezado no textual en {tabla.nombre}.")
                            encabezados_xml[columna] = valor[1]
                        if valor is not None or formula is not None:
                            celdas.append((columna - tabla.columna, valor, formula_texto))
                            celdas_con_valor += 1
                    if fila in (tabla.fila, tabla.fila + 1):
                        formatos_filas[fila] = tuple(
                            formatos[estilos_primera_fila.get(c, estilo_fila if estilo_fila is not None
                                                            else formatos_columnas.get(c, 0))]
                            for c in range(tabla.columna, tabla.ultima_columna + 1)
                        )
                    if celdas:
                        registro = json.dumps((fila - tabla.fila, celdas), ensure_ascii=False,
                                              separators=(",", ":")).encode("utf-8", "surrogatepass")
                        digest.update(registro)
                        digest.update(b"\n")
                elemento.clear()
                # sheetData es el padre directo de las filas; limpiarlo impide
                # acumular cientos de miles de elementos row ya procesados.
                if datos_hoja is not None:
                    datos_hoja.clear()
                raiz.clear()
    if tuple(encabezados_xml.get(c) for c in range(tabla.columna, tabla.ultima_columna + 1)) != tabla.encabezados:
        raise ValueError(f"Los encabezados de la hoja no coinciden con {tabla.nombre}.")
    formatos_predeterminados = tuple(formatos[formatos_columnas.get(c, 0)]
                                     for c in range(tabla.columna, tabla.ultima_columna + 1))
    return _Firma(
        tabla.referencia, tabla.encabezados,
        tabla.ultima_fila - tabla.fila - tabla.totales,
        tabla.atributos, tabla.estilo,
        formatos_filas.get(tabla.fila, formatos_predeterminados),
        formatos_filas.get(tabla.fila + 1, formatos_predeterminados),
        celdas_con_valor, digest.hexdigest(),
    )


def crear_plantilla(base_dir: Path | str | None = None,
                   destino: Path | str | None = None) -> Path:
    """Copiar los tres Excel independientes y publicar el combinado validado.

    Conserva los nombres t_datos, t_workloads y t_ejecutivos. La hoja Datos
    pasa a llamarse Colas. Las fuentes se abren en sólo lectura; el destino
    anterior se conserva ante errores o cambios concurrentes de archivos.
    """
    from .combinacion_excel import combinar_excel

    base = (Path(base_dir).resolve() if base_dir is not None
            else Path(__file__).resolve().parents[2])
    salida = (Path(destino) if destino is not None
              else base / "archivos" / "plantilla_reportes.xlsx")
    if not salida.is_absolute():
        salida = base / salida
    salida = salida.resolve()
    if not salida.is_relative_to(base) or salida.suffix.lower() != ".xlsx":
        raise ValueError("El destino debe ser un .xlsx dentro de la carpeta del proyecto.")
    fuentes = [(f, (base / "archivos" / f.archivo).resolve()) for f in _FUENTES]
    if salida in {ruta for _, ruta in fuentes}:
        raise ValueError("El destino no puede sustituir uno de los Excel independientes.")
    firmas = {}
    estados = {}
    fecha_1904 = None
    for fuente, ruta in fuentes:
        if not ruta.is_file():
            raise FileNotFoundError(f"Falta el Excel independiente: {ruta.name}")
        estados[ruta] = _estado_archivo(ruta)
        with abrir_lectura(ruta) as archivo, ZipFile(archivo) as z:
            libro = _leer_libro(z)
            tablas_hoja = {nombre for nombre, tabla in libro.tablas.items()
                           if tabla.hoja == fuente.hoja_origen}
            if fuente.hoja_origen not in libro.hojas or tablas_hoja != {fuente.tabla}:
                raise ValueError(f"{ruta.name}: se esperaba la hoja {fuente.hoja_origen} con una tabla {fuente.tabla}.")
            if fecha_1904 is not None and fecha_1904 != libro.fecha_1904:
                raise ValueError("Los tres Excel deben usar el mismo sistema de fechas 1900/1904.")
            fecha_1904 = libro.fecha_1904
            firmas[fuente.tabla] = _firma_tabla(z, libro.tablas[fuente.tabla])
        if _estado_archivo(ruta) != estados[ruta]:
            raise RuntimeError(f"{ruta.name} cambió durante la validación; volver a generar la plantilla.")
    estado_destino = _estado_archivo(salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".tmp_plantilla_", dir=salida.parent) as directorio:
        temporal = Path(directorio) / "plantilla_reportes.xlsx"
        combinar_excel([(ruta, fuente.hoja_origen, fuente.hoja_destino, fuente.tabla)
                        for fuente, ruta in fuentes], temporal)
        with abrir_lectura(temporal) as archivo, ZipFile(archivo) as z:
            libro = _leer_libro(z)
            if libro.hojas != tuple(f.hoja_destino for f in _FUENTES):
                raise ValueError("El combinado debe contener Colas, Workloads y Ejecutivos, en ese orden.")
            if set(libro.tablas) != set(firmas) or libro.fecha_1904 != fecha_1904:
                raise ValueError("Se alteraron las tablas o el sistema de fechas del combinado.")
            for fuente, _ in fuentes:
                tabla = libro.tablas[fuente.tabla]
                if tabla.hoja != fuente.hoja_destino:
                    raise ValueError(f"La copia cambió la hoja de {fuente.tabla}.")
                firma = _firma_tabla(z, tabla)
                diferencias = [campo.name for campo in fields(_Firma)
                               if getattr(firma, campo.name) != getattr(firmas[fuente.tabla], campo.name)]
                if diferencias:
                    raise ValueError(f"La copia alteró {fuente.tabla}; campos distintos: {', '.join(diferencias)}.")
        for _, ruta in fuentes:
            if _estado_archivo(ruta) != estados[ruta]:
                raise RuntimeError(f"{ruta.name} cambió durante la copia; se conserva el destino anterior.")
        if _estado_archivo(salida) != estado_destino:
            raise RuntimeError("El destino cambió durante la copia; se conserva esa versión.")
        temporal.replace(salida)
    return salida
