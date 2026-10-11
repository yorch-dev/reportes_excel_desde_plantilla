"""Tablas de Polars a Excel, con actualización segura de una plantilla.

Las magnitudes numéricas se escriben sin escalar: las horas siguen siendo horas
decimales y los segundos siguen siendo segundos. Sólo ``pl.Time`` se representa
como fracción de día, que es el tipo de hora de Excel.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
import math
from pathlib import Path
import posixpath
import re
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Mapping, Sequence
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from .archivos_excel import abrir_lectura, copiar_lectura

import polars as pl

if TYPE_CHECKING:
    import xlwings as xw


_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_TAG_V = f"{{{_NS['x']}}}v"
_TAG_F = f"{{{_NS['x']}}}f"
_TAG_C = f"{{{_NS['x']}}}c"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_MAX_FILAS = 1_048_576
_MAX_COLUMNAS = 16_384
_BLOQUE = 10_000
_NS_DIA = 86_400_000_000_000


@dataclass(frozen=True)
class TablaDatos:
    """Fuente de una tabla; ``formatos`` usa nombres de columnas y formatos Excel.

    En una plantilla las columnas fuente se localizan por su nombre, incluso si
    se intercalan columnas calculadas. Éstas deben contener una fórmula uniforme;
    no se reinterpretan columnas adicionales con datos manuales.
    """

    nombre: str
    hoja: str
    df: pl.DataFrame
    formatos: Mapping[str, str] = field(default_factory=dict)
    columnas_no_recibidas: frozenset[str] = field(default_factory=frozenset)


@dataclass
class _TablaXML:
    nombre: str
    hoja: str
    ruta_hoja: str
    fila: int
    columna: int
    ultima_fila: int
    ultima_columna: int
    encabezados: list[str]
    formulas: dict[str, str]
    totales: int


def _letra(numero: int) -> str:
    letras = ""
    while numero:
        numero, resto = divmod(numero - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


def _coordenada(direccion: str) -> tuple[int, int]:
    patron = re.fullmatch(r"\$?([A-Z]+)\$?(\d+)", direccion)
    if patron is None:
        raise ValueError(f"Coordenada Excel inválida: {direccion}")
    columna = 0
    for letra in patron[1]:
        columna = columna * 26 + ord(letra) - 64
    return int(patron[2]), columna


def _destino_rel(ruta: str, target: str) -> str:
    return (target.lstrip("/") if target.startswith("/") else
            posixpath.normpath(posixpath.join(posixpath.dirname(ruta), target)))


def _relaciones(z: ZipFile, ruta: str) -> dict[str, str]:
    rels = posixpath.join(posixpath.dirname(ruta), "_rels", posixpath.basename(ruta) + ".rels")
    if rels not in z.namelist():
        return {}
    return {r.get("Id"): _destino_rel(ruta, r.get("Target"))
            for r in ET.fromstring(z.read(rels)).findall(f"{{{_REL}}}Relationship")
            if r.get("TargetMode") != "External"}


def _tablas_xml(z: ZipFile) -> tuple[dict[str, _TablaXML], bool]:
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    enlaces = _relaciones(z, "xl/workbook.xml")
    tablas = {}
    for hoja in workbook.findall("x:sheets/x:sheet", _NS):
        ruta_hoja = enlaces[hoja.get(f"{{{_OFFICE_REL}}}id")]
        rels_hoja = _relaciones(z, ruta_hoja)
        # Las relaciones identifican las tablas sin leer cientos de miles de celdas.
        for ruta_tabla in rels_hoja.values():
            if not ruta_tabla.startswith("xl/tables/") or not ruta_tabla.endswith(".xml"):
                continue
            tabla = ET.fromstring(z.read(ruta_tabla))
            inicio, fin = tabla.get("ref").split(":")
            fila, columna = _coordenada(inicio)
            ultima_fila, ultima_columna = _coordenada(fin)
            columnas = tabla.findall("x:tableColumns/x:tableColumn", _NS)
            nombre = tabla.get("name")
            if nombre in tablas:
                raise ValueError(f"Tabla repetida: {nombre}")
            tablas[nombre] = _TablaXML(
                nombre, hoja.get("name"), ruta_hoja, fila, columna,
                ultima_fila, ultima_columna,
                [c.get("name") for c in columnas],
                {c.get("name"): c.find("x:calculatedColumnFormula", _NS).text or ""
                 for c in columnas if c.find("x:calculatedColumnFormula", _NS) is not None},
                int(tabla.get("totalsRowCount", "0")),
            )
    wbpr = workbook.find("x:workbookPr", _NS)
    usa_1904 = wbpr is not None and wbpr.get("date1904", "0") in ("1", "true")
    return tablas, usa_1904


def _validar_fuentes(tablas: Sequence[TablaDatos]) -> None:
    if not tablas:
        raise ValueError("Se requiere al menos una tabla.")
    nombres = [t.nombre.casefold() for t in tablas]
    if len(nombres) != len(set(nombres)):
        raise ValueError("Los nombres de tabla deben ser únicos.")
    for tabla in tablas:
        if not isinstance(tabla.df, pl.DataFrame) or not tabla.df.width:
            raise ValueError(f"{tabla.nombre}: se requiere un DataFrame con columnas.")
        if not tabla.nombre or not tabla.hoja:
            raise ValueError("Cada tabla requiere nombre y hoja.")
        if tabla.df.width > _MAX_COLUMNAS or max(1, tabla.df.height) + 1 > _MAX_FILAS:
            raise ValueError(f"{tabla.nombre}: dimensiones fuera del límite de Excel.")
        if len({c.casefold() for c in tabla.df.columns}) != tabla.df.width:
            raise ValueError(f"{tabla.nombre}: encabezados repetidos para Excel.")
        if set(tabla.formatos) - set(tabla.df.columns):
            raise ValueError(f"{tabla.nombre}: formatos de columnas inexistentes.")
        if not tabla.columnas_no_recibidas <= set(tabla.df.columns):
            raise ValueError(f"{tabla.nombre}: columnas no recibidas inexistentes.")
        if any(tabla.df[c].null_count() != tabla.df.height for c in tabla.columnas_no_recibidas):
            raise ValueError(f"{tabla.nombre}: una columna no recibida contiene datos.")
        for columna, tipo in tabla.df.schema.items():
            if not (tipo in (pl.String, pl.Date, pl.Time, pl.Boolean, pl.Null)
                    or tipo.is_integer() or tipo.is_float()):
                raise ValueError(f"{tabla.nombre}.{columna}: tipo no soportado: {tipo}")
            serie = tabla.df[columna]
            if tipo.is_float() and (serie.is_not_null() & ~serie.is_finite()).any():
                raise ValueError(f"{tabla.nombre}.{columna}: números no finitos.")
            if tipo.is_integer() and serie.len() and (
                (serie.min() is not None and serie.min() < -999_999_999_999_999)
                or (serie.max() is not None and serie.max() > 999_999_999_999_999)
            ):
                raise ValueError(f"{tabla.nombre}.{columna}: un entero excede 15 dígitos; usar String.")
            if tipo == pl.String:
                if serie.str.len_chars().max() is not None and serie.str.len_chars().max() > 32_767:
                    raise ValueError(f"{tabla.nombre}.{columna}: texto mayor a 32767 caracteres.")
                if serie.str.contains(r"[\x00-\x08\x0B\x0C\x0E-\x1F]").any():
                    raise ValueError(f"{tabla.nombre}.{columna}: texto con caracteres XML inválidos.")


def _serial_fecha(valor: date, usa_1904: bool) -> int:
    if usa_1904:
        return (valor - date(1904, 1, 1)).days
    # Excel incluye el ficticio 29/02/1900; no afecta fechas de los reportes.
    resultado = (valor - date(1899, 12, 31)).days
    return resultado + (valor >= date(1900, 3, 1))


def _para_excel(df: pl.DataFrame, usa_1904: bool) -> pl.DataFrame:
    expresiones = []
    for columna, tipo in df.schema.items():
        if tipo == pl.Time:
            expresiones.append((pl.col(columna).cast(pl.Int64) / _NS_DIA).alias(columna))
        elif tipo == pl.Date:
            # Evitar que el conversor de fechas cambie formatos de la plantilla.
            offset = _serial_fecha(date(1970, 1, 1), usa_1904)
            serial = pl.col(columna).cast(pl.Int64) + offset
            if not usa_1904:
                serial = serial - (pl.col(columna) < date(1900, 3, 1)).cast(pl.Int64)
            expresiones.append(serial.alias(columna))
        elif tipo == pl.String:
            # Excel quita este prefijo de entrada; el literal original se valida
            # en XML. Evita coerción de 00123, 1,70758, fechas y cadenas '=...'.
            expresiones.append((pl.lit("'") + pl.col(columna)).alias(columna))
    return df.with_columns(expresiones)


def _formatos(tabla: TablaDatos) -> dict[str, str]:
    formatos = {}
    for c, tipo in tabla.df.schema.items():
        formatos[c] = ("@" if tipo == pl.String else "dd-mm-yyyy" if tipo == pl.Date
                       else "hh:mm" if tipo == pl.Time else "0.00" if tipo.is_float() else "0")
    formatos.update(tabla.formatos)
    return formatos


def _matriz(valor) -> list[list]:
    if isinstance(valor, (tuple, list)):
        if valor and isinstance(valor[0], (tuple, list)):
            return [list(fila) for fila in valor]
        return [list(valor)]
    return [[valor]]


def _actualizar_tabla(libro, fuente: TablaDatos, previa: _TablaXML | None) -> tuple[int, int, int]:
    hoja = libro.sheets[fuente.hoja]
    tabla = hoja.tables[fuente.nombre]
    if tabla.header_row_range is None:
        raise ValueError(f"{fuente.nombre}: la tabla debe mostrar encabezados.")
    encabezados = _matriz(tabla.header_row_range.api.Value2)[0]
    if any(c not in encabezados for c in fuente.df.columns):
        raise ValueError(f"{fuente.nombre}: falta una columna fuente en la plantilla.")
    for c in fuente.columnas_no_recibidas:
        cuerpo = tabla.api.ListColumns(encabezados.index(c) + 1).DataBodyRange
        # HasFormula es None si mezcla fórmulas y valores. Incluso sin el
        # metadato calculatedColumnFormula, preservar esos cálculos manuales.
        if cuerpo is not None and cuerpo.HasFormula != False:
            raise ValueError(f"{fuente.nombre}.{c}: variante no recibida con fórmulas en la plantilla.")
    posiciones = {c: encabezados.index(c) for c in fuente.df.columns}
    extras = [c for c in encabezados if c not in posiciones]
    inicio = tabla.header_row_range.row
    columna = tabla.header_row_range.column
    filas_antes = 0 if tabla.data_body_range is None else tabla.data_body_range.rows.count
    filas_nuevas = max(1, fuente.df.height)
    totales = int(bool(tabla.show_totals))
    final = inicio + filas_nuevas + totales
    if final > _MAX_FILAS or columna + len(encabezados) - 1 > _MAX_COLUMNAS:
        raise ValueError(f"{fuente.nombre}: la tabla supera el límite de Excel.")

    formulas = {}
    for c in extras:
        cuerpo = tabla.api.ListColumns(encabezados.index(c) + 1).DataBodyRange
        formula = None
        if cuerpo is not None and cuerpo.HasFormula:
            valores = [v for fila in _matriz(cuerpo.FormulaR1C1) for v in fila]
            if valores and len(set(valores)) == 1 and str(valores[0]).startswith("="):
                formula = valores[0]
        if formula is None and previa is not None and c in previa.formulas:
            # Tabla vacía, o una fila placeholder que todavía no tiene fórmula.
            if cuerpo is None or libro.app.api.WorksheetFunction.CountA(cuerpo) == 0:
                celda = hoja.range((inicio + 1, columna + encabezados.index(c))).api
                formula = libro.app.api.ConvertFormula(
                    "=" + previa.formulas[c].lstrip("="), 1, -4150, 4, celda)
        if formula is None:
            raise ValueError(f"{fuente.nombre}.{c}: columna extra sin fórmula uniforme.")
        formulas[c] = formula

    ultimo_anterior = inicio + filas_antes + totales
    if final > ultimo_anterior:
        expansion = hoja.range((ultimo_anterior + 1, columna),
                               (final, columna + len(encabezados) - 1))
        if libro.app.api.WorksheetFunction.CountA(expansion.api):
            raise ValueError(f"{fuente.nombre}: crecer la tabla sobrescribiría contenido externo.")
    # Quitar sólo el filtro de esta tabla, sin tocar otras tablas de la hoja.
    if tabla.api.AutoFilter.FilterMode:
        tabla.api.AutoFilter.ShowAllData()
    formatos_plantilla = [hoja.range((inicio + 1, columna + i)).number_format
                         for i in range(len(encabezados))]
    if tabla.data_body_range is not None:
        tabla.data_body_range.clear_contents()
    tabla.resize(hoja.range((inicio, columna), (final, columna + len(encabezados) - 1)))

    # Las filas retiradas ya no pertenecen a la tabla. Sus formatos vacíos
    # mantendrían miles de filas en el archivo; limitar la limpieza al cuerpo
    # anterior preserva las celdas externas y la nueva fila de totales.
    ultimo_cuerpo_anterior = inicio + filas_antes
    if final < ultimo_cuerpo_anterior:
        retirado = hoja.range((final + 1, columna),
                             (ultimo_cuerpo_anterior, columna + len(encabezados) - 1))
        retirado.clear_formats()

    # Copiar sólo formatos a filas adicionales; conservar formatos y fórmulas
    # fuera de la tabla y las modificaciones de formato de sus filas existentes.
    if previa is not None and filas_nuevas > filas_antes and filas_antes:
        modelo = hoja.range((inicio + 1, columna), (inicio + 1, columna + len(encabezados) - 1))
        nuevo = hoja.range((inicio + max(1, filas_antes) + 1, columna),
                           (inicio + filas_nuevas, columna + len(encabezados) - 1))
        modelo.api.Copy()
        nuevo.api.PasteSpecial(Paste=-4122)  # xlPasteFormats; no datos/fórmulas.
        libro.app.api.CutCopyMode = False
    if previa is None:
        separadores = str.maketrans({".": libro.app.api.DecimalSeparator,
                                   ",": libro.app.api.ThousandsSeparator})
        for c, formato in _formatos(fuente).items():
            rango = hoja.range((inicio + 1, columna + posiciones[c]),
                               (inicio + filas_nuevas, columna + posiciones[c]))
            rango.number_format = formato.translate(separadores)
    elif not filas_antes:
        for i, formato in enumerate(formatos_plantilla):
            hoja.range((inicio + 1, columna + i), (inicio + filas_nuevas, columna + i)).number_format = formato

    df_excel = _para_excel(fuente.df, bool(libro.api.Date1904))
    # Escribir por tramos contiguos de columnas fuente: no pisa calculadas intercaladas.
    tramos = []
    for c in encabezados:
        if c not in posiciones:
            continue
        if tramos and posiciones[c] == tramos[-1][-1][1] + 1:
            tramos[-1].append((c, posiciones[c]))
        else:
            tramos.append([(c, posiciones[c])])
    for inicio_bloque in range(0, fuente.df.height, _BLOQUE):
        bloque = df_excel.slice(inicio_bloque, _BLOQUE)
        for tramo in tramos:
            datos = tuple(tuple(fila) for fila in bloque.select([c for c, _ in tramo]).iter_rows())
            rango = hoja.range((inicio + 1 + inicio_bloque, columna + tramo[0][1]),
                               (inicio + inicio_bloque + bloque.height, columna + tramo[-1][1]))
            rango.api.Value2 = datos
    for c, formula in formulas.items():
        # Excel puede devolver DataBodyRange=None al reducir a una fila vacía.
        # El rango físico conserva esa fila placeholder y permite su fórmula.
        posicion = columna + encabezados.index(c)
        hoja.range((inicio + 1, posicion),
                   (inicio + filas_nuevas, posicion)).api.FormulaR1C1 = formula
    # Una fila vacía conserva el esquema y las fórmulas para futuros crecimientos.
    return inicio, columna, ultimo_anterior - totales


def _valor_celda(celda, shared: list[str]):
    if celda is None:
        return None, None
    tipo = celda.get("t", "n")
    v = celda.find(_TAG_V)
    if tipo == "s" and v is not None:
        return tipo, shared[int(v.text)]
    if tipo == "inlineStr":
        return tipo, "".join(t.text or "" for t in celda.findall(".//x:t", _NS))
    return tipo, None if v is None else v.text


def _tipo_comparacion(tipo) -> str:
    if tipo == pl.String:
        return "texto"
    if tipo == pl.Boolean:
        return "booleano"
    if tipo == pl.Date:
        return "fecha"
    if tipo == pl.Time:
        return "hora"
    return "entero" if tipo.is_integer() else "decimal"


def _comprobar_valor(celda, esperado, tipo_df, shared, usa_1904, direccion):
    tipo, valor = _valor_celda(celda, shared)
    if celda is not None and celda.find(_TAG_F) is not None:
        raise ValueError(f"Fórmula inesperada en una columna fuente: {direccion}")
    if esperado is None:
        if valor not in (None, ""):
            raise ValueError(f"NULL alterado en {direccion}")
        return
    if tipo_df == "texto":
        if esperado == "" and valor in (None, ""):
            return  # Excel no distingue una cadena vacía de una celda vacía.
        if tipo not in ("s", "inlineStr") or valor != esperado:
            raise ValueError(f"Texto alterado o convertido en {direccion}")
        return
    if tipo_df == "booleano":
        if tipo != "b" or valor != ("1" if esperado else "0"):
            raise ValueError(f"Booleano alterado en {direccion}")
        return
    if tipo != "n" or valor is None:
        raise ValueError(f"Dato no numérico en {direccion}")
    numero = float(valor)
    if not math.isfinite(numero):
        raise ValueError(f"Dato no finito en {direccion}")
    if tipo_df == "fecha":
        numero_esperado = _serial_fecha(esperado, usa_1904)
    elif tipo_df == "hora":
        numero_esperado = (esperado.hour * 3600 + esperado.minute * 60 + esperado.second
                          + esperado.microsecond / 1_000_000) / 86_400
        if not 0 <= numero < 1:
            raise ValueError(f"Hora fuera de un día en {direccion}")
    else:
        numero_esperado = esperado
    if tipo_df == "entero":
        coincide = Decimal(valor) == Decimal(esperado)
    else:
        coincide = math.isclose(numero, float(numero_esperado), rel_tol=1e-12, abs_tol=1e-9)
    if not coincide:
        raise ValueError(f"Valor alterado en {direccion}")


def verificar_libro(ruta: str | Path, tablas: Sequence[TablaDatos], *,
                    _rangos_anteriores: Mapping[str, tuple[int, int, int]] | None = None) -> dict[str, int]:
    """Comparar todas las celdas fuente mediante ZIP/XML, sin abrir Excel.

    Permite hojas, gráficos, fórmulas y tablas adicionales del usuario. Las tablas
    indicadas deben existir en su hoja, conservar las columnas fuente y tener la
    altura exacta (una fila placeholder cuando no hay datos).
    """
    tablas = tuple(tablas)
    _validar_fuentes(tablas)
    with abrir_lectura(ruta) as archivo_excel, ZipFile(archivo_excel) as z:
        if z.testzip() is not None:
            raise ValueError("El archivo Excel está dañado.")
        metadatos, usa_1904 = _tablas_xml(z)
        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        calc = workbook.find("x:calcPr", _NS)
        if calc is not None and calc.get("calcMode", "auto") != "auto":
            raise ValueError("El libro no quedó en cálculo automático.")
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            with z.open("xl/sharedStrings.xml") as archivo:
                for _, si in ET.iterparse(archivo, events=("end",)):
                    if si.tag == f"{{{_NS['x']}}}si":
                        shared.append("".join(t.text or "" for t in si.findall(".//x:t", _NS)))
                        si.clear()
        por_hoja = {}
        for fuente in tablas:
            if fuente.nombre not in metadatos:
                raise ValueError(f"Falta la tabla {fuente.nombre}.")
            meta = metadatos[fuente.nombre]
            if meta.hoja != fuente.hoja or any(c not in meta.encabezados for c in fuente.df.columns):
                raise ValueError(f"Hoja o columnas incorrectas en {fuente.nombre}.")
            if len(meta.encabezados) != meta.ultima_columna - meta.columna + 1:
                raise ValueError(f"Ancho incorrecto en {fuente.nombre}.")
            if meta.ultima_fila != meta.fila + max(1, fuente.df.height) + meta.totales:
                raise ValueError(f"Altura incorrecta en {fuente.nombre}.")
            esquema = dict(fuente.df.schema)
            columnas = tuple((c, _letra(meta.columna + meta.encabezados.index(c)), _tipo_comparacion(esquema[c]))
                             for c in fuente.df.columns)
            calculadas = tuple((c, _letra(meta.columna + i)) for i, c in enumerate(meta.encabezados)
                               if c not in esquema)
            prefijos = tuple(_letra(meta.columna + i) for i in range(len(meta.encabezados)))
            por_hoja.setdefault(meta.ruta_hoja, []).append(
                (fuente, meta, iter(fuente.df.iter_rows()), columnas, calculadas, prefijos))
        leidas = {t.nombre: 0 for t in tablas}
        encabezados_leidos = set()
        for ruta_hoja, fuentes in por_hoja.items():
            with z.open(ruta_hoja) as archivo:
                sheet_data = None
                for evento, row in ET.iterparse(archivo, events=("start", "end")):
                    if evento == "start":
                        if row.tag == f"{{{_NS['x']}}}sheetData":
                            sheet_data = row
                        continue
                    if row.tag != f"{{{_NS['x']}}}row":
                        continue
                    fila = int(row.get("r"))
                    celdas = {c.get("r"): c for c in row.findall(_TAG_C)}
                    for fuente, meta, iterador, columnas, calculadas, prefijos in fuentes:
                        if fila == meta.fila:
                            for prefijo, nombre in zip(prefijos, meta.encabezados):
                                direccion = f"{prefijo}{fila}"
                                _, literal = _valor_celda(celdas.get(direccion), shared)
                                if literal != nombre:
                                    raise ValueError(f"Encabezado alterado en {direccion}.")
                            encabezados_leidos.add(fuente.nombre)
                        if meta.fila < fila <= meta.fila + max(1, fuente.df.height):
                            if fuente.df.height:
                                if fila != meta.fila + leidas[fuente.nombre] + 1:
                                    raise ValueError(f"Filas discontinuas en {fuente.nombre}.")
                                esperado = next(iterador)
                                leidas[fuente.nombre] += 1
                            else:
                                esperado = (None,) * fuente.df.width
                            for (_, prefijo, tipo), valor in zip(columnas, esperado):
                                direccion = f"{prefijo}{fila}"
                                _comprobar_valor(celdas.get(direccion), valor, tipo,
                                                shared, usa_1904, f"{meta.hoja}!{direccion}")
                            for nombre, prefijo in calculadas:
                                c = celdas.get(f"{prefijo}{fila}")
                                if c is None or c.find(_TAG_F) is None:
                                    raise ValueError(f"Columna calculada sin fórmula: {fuente.nombre}.{nombre}")
                        anterior = (_rangos_anteriores or {}).get(fuente.nombre)
                        if anterior and meta.ultima_fila < fila <= anterior[2]:
                            for prefijo in prefijos:
                                direccion = f"{prefijo}{fila}"
                                _comprobar_valor(celdas.get(direccion), None, "decimal", shared, usa_1904,
                                                f"{meta.hoja}!{direccion}")
                    row.clear()
                    if sheet_data is not None:
                        sheet_data.remove(row)
        if encabezados_leidos != {t.nombre for t in tablas} or any(leidas[t.nombre] != t.df.height for t in tablas):
            raise ValueError("Cantidad de filas o encabezados exportados incorrecta.")
    return leidas


def generar_libro(tablas: Sequence[TablaDatos], destino: str | Path,
                   plantilla: str | Path | None = None, app: xw.App | None = None) -> Path:
    """Crear/actualizar tablas en una copia y publicar después de verificar XML.

    La plantilla y el destino anterior permanecen intactos si hay un fallo. Una
    instancia ``app`` prestada no se cierra. Sólo se refrescan cachés de pivots
    locales cuya fuente sea exactamente una de estas tablas, sin RefreshAll.
    Al reducir una tabla, se limpian los formatos de sus filas retiradas sólo
    dentro del antiguo cuerpo, conservando los elementos externos.
    """
    # El validador ZIP/XML no necesita importar xlwings ni registrar su cleanup.
    import xlwings as xw

    tablas = tuple(tablas)
    _validar_fuentes(tablas)
    destino = Path(destino).resolve()
    if destino.suffix.lower() != ".xlsx":
        raise ValueError("El destino debe ser .xlsx.")
    plantilla = None if plantilla is None else Path(plantilla).resolve()
    previas = {}
    if plantilla is not None:
        if not plantilla.is_file():
            raise FileNotFoundError(plantilla)
        if plantilla == destino:
            raise ValueError("La plantilla y el destino deben ser archivos distintos.")
        if plantilla.suffix.lower() != ".xlsx":
            raise ValueError("La plantilla debe ser .xlsx.")
        with abrir_lectura(plantilla) as archivo_excel, ZipFile(archivo_excel) as z:
            previas, _ = _tablas_xml(z)
        for fuente in tablas:
            if fuente.nombre not in previas or previas[fuente.nombre].hoja != fuente.hoja:
                raise ValueError(f"La plantilla no contiene {fuente.hoja}/{fuente.nombre}.")
    elif len({t.hoja.casefold() for t in tablas}) != len(tablas):
        raise ValueError("Al crear un libro, cada tabla requiere su propia hoja.")
    destino.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".reporte_", dir=destino.parent) as carpeta:
        temporal = Path(carpeta) / destino.name
        if plantilla is not None:
            copiar_lectura(plantilla, temporal)
        contexto = nullcontext(app) if app is not None else xw.App(visible=False, add_book=False)
        with contexto as instancia:
            libro = (instancia.books.open(str(temporal), update_links=False, read_only=False)
                     if plantilla is not None else instancia.books.add())
            try:
                with instancia.properties(calculation="manual", screen_updating=False, display_alerts=False):
                    if plantilla is None:
                        inicial = list(libro.sheets)
                        for i, fuente in enumerate(tablas):
                            hoja = inicial[0] if i == 0 else libro.sheets.add(after=libro.sheets[-1])
                            hoja.name = fuente.hoja
                            hoja.range((1, 1), (1, fuente.df.width)).api.Value2 = (
                                tuple("'" + c for c in fuente.df.columns),)
                            rango = hoja.range((1, 1), (max(1, fuente.df.height) + 1, fuente.df.width))
                            tabla = hoja.tables.add(source=rango, name=fuente.nombre, has_headers=True)
                            tabla.api.TableStyle = ""
                            tabla.show_table_style_row_stripes = False
                            tabla.show_table_style_column_stripes = False
                            hoja.range((1, 1), (1, fuente.df.width)).font.bold = True
                        for hoja in inicial[1:]:
                            hoja.delete()
                    anteriores = {}
                    for fuente in tablas:
                        anteriores[fuente.nombre] = _actualizar_tabla(libro, fuente, previas.get(fuente.nombre))
                        if plantilla is None:
                            hoja = libro.sheets[fuente.hoja]
                            hoja.range((1, 1), (min(fuente.df.height + 1, 201), fuente.df.width)).columns.autofit()
                            for i in range(1, fuente.df.width + 1):
                                columna = hoja.range((1, i))
                                columna.column_width = min(45, max(10, columna.column_width))
                instancia.calculation = "automatic"
                nombres = {t.nombre.casefold() for t in tablas}
                caches = libro.api.PivotCaches()
                for i in range(1, caches.Count + 1):
                    cache = caches.Item(i)
                    if cache.SourceType == 1:
                        fuente_cache = cache.SourceData
                        if isinstance(fuente_cache, str) and fuente_cache.casefold() in nombres:
                            cache.MissingItemsLimit = 0  # No retener categorías del corte anterior.
                            cache.Refresh()
                for hoja in libro.sheets:
                    hoja.api.Calculate()
                if plantilla is None:
                    libro.save(str(temporal))
                else:
                    libro.save()
            finally:
                libro.close()
        verificar_libro(temporal, tablas, _rangos_anteriores=anteriores)
        temporal.replace(destino)
    return destino


__all__ = ["TablaDatos", "generar_libro", "verificar_libro"]
