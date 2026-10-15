"""Fechas con hora reales de Excel, sin COM ni fuentes externas."""
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import polars as pl

from src.analytics.excel_reportes import (
    TablaDatos, _comprobar_valor, _formatos, _para_excel, _tipo_comparacion,
    _validar_fuentes, verificar_libro,
)


_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _celda_numero(valor):
    celda = ET.Element(f"{{{_NS}}}c", {"r": "A2"})
    ET.SubElement(celda, f"{{{_NS}}}v").text = format(valor, ".15g")
    return celda


def _libro_xml(ruta, seriales, usa_1904):
    """Paquete mínimo para recorrer el validador completo de tablas y celdas."""
    hoja = ET.Element(f"{{{_NS}}}worksheet")
    datos = ET.SubElement(hoja, f"{{{_NS}}}sheetData")
    fila = ET.SubElement(datos, f"{{{_NS}}}row", {"r": "1"})
    celda = ET.SubElement(fila, f"{{{_NS}}}c", {"r": "A1", "t": "inlineStr"})
    ET.SubElement(ET.SubElement(celda, f"{{{_NS}}}is"), f"{{{_NS}}}t").text = "turno"
    for numero_fila, serial in enumerate(seriales, 2):
        fila = ET.SubElement(datos, f"{{{_NS}}}row", {"r": str(numero_fila)})
        if serial is not None:
            celda = _celda_numero(serial)
            celda.set("r", f"A{numero_fila}")
            fila.append(celda)
    partes = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<workbookPr date1904="{int(usa_1904)}"/><sheets><sheet name="t_turnos_diario" sheetId="1" r:id="rId1"/></sheets><calcPr calcMode="auto"/></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": ET.tostring(hoja, encoding="utf-8"),
        "xl/worksheets/_rels/sheet1.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table1.xml"/></Relationships>',
        "xl/tables/table1.xml": f'<table xmlns="{_NS}" id="1" name="t_turnos_diario" displayName="t_turnos_diario" ref="A1:A{len(seriales) + 1}" totalsRowCount="0">'
            '<tableColumns count="1"><tableColumn id="1" name="turno"/></tableColumns></table>',
    }
    with ZipFile(ruta, "w") as libro:
        for nombre, contenido in partes.items():
            libro.writestr(nombre, contenido)


class ExcelDatetimeTests(unittest.TestCase):
    def test_turno_cruza_medianoche_con_nulos_y_tres_unidades(self):
        valores = [datetime(2026, 10, 13, 23, 59, 59, 654321),
                   datetime(2026, 10, 14, 0, 0, 0, 123456), None]
        for unidad in ("ms", "us", "ns"):
            with self.subTest(unidad=unidad):
                df = pl.DataFrame({"turno": pl.Series(valores, dtype=pl.Datetime(unidad))})
                tabla = TablaDatos("t_turnos_diario", "t_turnos_diario", df)
                _validar_fuentes([tabla])
                self.assertEqual(_formatos(tabla)["turno"], "dd-mm-yyyy hh:mm:ss")
                self.assertEqual(_tipo_comparacion(df.schema["turno"]), "fecha_hora")
                seriales = _para_excel(df, False)["turno"].to_list()
                self.assertEqual(int(seriales[1]) - int(seriales[0]), 1)
                self.assertIsNone(seriales[2])
                for original, serial in zip(df["turno"].to_list()[:2], seriales[:2]):
                    recuperado = datetime(1899, 12, 30) + timedelta(days=serial)
                    self.assertLessEqual(abs((recuperado - original).total_seconds()), 1e-6)
                    _comprobar_valor(_celda_numero(serial), original, "fecha_hora", [], False, "A2")

    def test_epoca_1904_y_salto_ficticio_de_1900(self):
        casos = [(True, [datetime(1904, 1, 1, 12), datetime(1904, 1, 2, 6), None], [0.5, 1.25, None]),
                 (False, [datetime(1900, 2, 28, 12), datetime(1900, 3, 1, 12), None], [59.5, 61.5, None])]
        for usa_1904, valores, esperados in casos:
            with self.subTest(usa_1904=usa_1904):
                df = pl.DataFrame({"turno": pl.Series(valores, dtype=pl.Datetime("us"))})
                self.assertEqual(_para_excel(df, usa_1904)["turno"].to_list(), esperados)
                for valor, serial in zip(valores, esperados):
                    _comprobar_valor(None if serial is None else _celda_numero(serial),
                                     valor, "fecha_hora", [], usa_1904, "A2")

    def test_zona_horaria_rechazada_incluso_sin_valores(self):
        for valores in ([datetime(2026, 10, 13, tzinfo=timezone.utc)], [None], []):
            with self.subTest(filas=len(valores)):
                df = pl.DataFrame({"turno": pl.Series(valores, dtype=pl.Datetime("us", "UTC"))})
                with self.assertRaisesRegex(ValueError, "zona horaria"):
                    _validar_fuentes([TablaDatos("turnos", "turnos", df)])
                with self.assertRaisesRegex(ValueError, "zona horaria"):
                    _para_excel(df, False)

    def test_verificacion_xml_completa_y_alteraciones(self):
        valores = [datetime(2026, 10, 13, 23, 59, 59, 123456),
                   datetime(2026, 10, 14, 0, 0, 0, 654321), None]
        df = pl.DataFrame({"turno": pl.Series(valores, dtype=pl.Datetime("us"))})
        tabla = TablaDatos("t_turnos_diario", "t_turnos_diario", df)
        with TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "turnos.xlsx"
            for usa_1904 in (False, True):
                with self.subTest(usa_1904=usa_1904):
                    seriales = _para_excel(df, usa_1904)["turno"].to_list()
                    _libro_xml(ruta, seriales, usa_1904)
                    self.assertEqual(verificar_libro(ruta, [tabla]), {"t_turnos_diario": 3})
                    for desplazamiento in (20e-6 / 86_400, 0.001 / 86_400, 1):
                        alterados = [seriales[0] + desplazamiento, *seriales[1:]]
                        _libro_xml(ruta, alterados, usa_1904)
                        with self.assertRaisesRegex(ValueError, "Valor alterado"):
                            verificar_libro(ruta, [tabla])
                    _libro_xml(ruta, [*seriales[:2], 0.0], usa_1904)
                    with self.assertRaisesRegex(ValueError, "NULL alterado"):
                        verificar_libro(ruta, [tabla])

    def test_ns_y_fecha_hora_no_admiten_coercion_a_texto(self):
        instante_ns = 1_792_022_400_123_456_789  # 2026-10-15 00:00:00.123456789.
        df = pl.DataFrame({"turno": pl.Series([instante_ns]).cast(pl.Datetime("ns"))})
        serial = _para_excel(df, False)["turno"][0]
        esperado = df["turno"][0]  # Python conserva microsegundos, no nanosegundos.
        _comprobar_valor(_celda_numero(serial), esperado, "fecha_hora", [], False, "A2")
        texto = ET.fromstring(f'<c xmlns="{_NS}" r="A2" t="inlineStr"><is><t>2026-10-15 00:00:00</t></is></c>')
        with self.assertRaisesRegex(ValueError, "Dato no numérico"):
            _comprobar_valor(texto, esperado, "fecha_hora", [], False, "A2")

    def test_fecha_hora_simple_y_horas_decimales_conservan_contrato(self):
        df = pl.DataFrame({"fecha": [date(2026, 10, 13)], "intervalo": [time(8, 30)],
                           "horas": [7.5], "segundos": [27000.0]})
        resultado = _para_excel(df, False).row(0)
        self.assertEqual(resultado[0], (date(2026, 10, 13) - date(1899, 12, 30)).days)
        self.assertEqual(resultado[1], 8.5 / 24)
        self.assertEqual(resultado[2:], (7.5, 27000.0))
        tabla = TablaDatos("datos", "datos", df, {"horas": "0.0000"})
        formatos = _formatos(tabla)
        self.assertEqual(formatos["fecha"], "dd-mm-yyyy")
        self.assertEqual(formatos["intervalo"], "hh:mm")
        self.assertEqual(formatos["horas"], "0.0000")


if __name__ == "__main__":
    unittest.main()
