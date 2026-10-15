"""Hojas de trabajo visibles y datos ocultos sin perder contenido, sin COM."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import polars as pl

from src.analytics.excel_reportes import (
    TablaDatos, configurar_hojas_trabajo, verificar_libro,
)


_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _fuente(nombre="t_colas_diario"):
    return TablaDatos(nombre, nombre, pl.DataFrame({"id": [7]}))


class _ApiHoja:
    def __init__(self, hoja):
        self.hoja = hoja

    @property
    def Name(self):
        return self.hoja.name

    @property
    def Visible(self):
        return self.hoja.visible

    @Visible.setter
    def Visible(self, valor):
        hoja = self.hoja
        if valor != -1 and not any(h.visible == -1 for h in hoja.libro.sheets if h is not hoja):
            raise RuntimeError("Excel no permite ocultar la última hoja visible.")
        hoja.libro.eventos.append(("visible", hoja.name, valor))
        hoja.visible = valor


class _Hoja:
    def __init__(self, libro, nombre, visible=-1, contenido=None):
        self.libro, self.name, self.visible = libro, nombre, visible
        self.contenido = {} if contenido is None else contenido.copy()
        self.api = _ApiHoja(self)

    def activate(self):
        if self.visible != -1:
            raise RuntimeError("No se puede activar una hoja oculta.")
        self.libro.eventos.append(("activar", self.name))
        self.libro.activa = self.name


class _Hojas(list):
    def __init__(self, libro):
        super().__init__()
        self.libro = libro

    def add(self, nombre, after=None):
        hoja = _Hoja(self.libro, nombre)
        posicion = len(self) if after is None else self.index(after) + 1
        self.insert(posicion, hoja)
        self.libro.eventos.append(("crear", nombre))
        return hoja


class _Libro:
    def __init__(self, hojas):
        self.eventos, self.activa = [], None
        self.sheets = _Hojas(self)
        self.graficos = []
        self.api = _ApiLibro(self)
        for nombre, visible, contenido in hojas:
            self.sheets.append(_Hoja(self, nombre, visible, contenido))


class _ApiLibro:
    def __init__(self, libro):
        self.libro = libro

    @property
    def Sheets(self):
        return [hoja.api for hoja in [*self.libro.sheets, *self.libro.graficos]]


def _libro_xml(ruta, estados):
    workbook = ET.Element(f"{{{_NS}}}workbook")
    hojas = ET.SubElement(workbook, f"{{{_NS}}}sheets")
    relaciones = ET.Element(f"{{{_REL}}}Relationships")
    partes = {}
    for indice, (nombre, estado) in enumerate(estados, 1):
        atributos = {"name": nombre, "sheetId": str(indice), f"{{{_R}}}id": f"rId{indice}"}
        if estado is not None:
            atributos["state"] = estado
        ET.SubElement(hojas, f"{{{_NS}}}sheet", atributos)
        ET.SubElement(relaciones, f"{{{_REL}}}Relationship", {
            "Id": f"rId{indice}", "Type": f"{_R}/worksheet", "Target": f"worksheets/sheet{indice}.xml"})
        xml_hoja = f'<worksheet xmlns="{_NS}"><sheetData/></worksheet>'
        if nombre == "t_colas_diario":
            xml_hoja = f'<worksheet xmlns="{_NS}"><sheetData>'
            xml_hoja += '<row r="1"><c r="A1" t="inlineStr"><is><t>id</t></is></c></row>'
            xml_hoja += '<row r="2"><c r="A2"><v>7</v></c></row></sheetData></worksheet>'
            partes[f"xl/worksheets/_rels/sheet{indice}.xml.rels"] = (
                f'<Relationships xmlns="{_REL}"><Relationship Id="rId1" Type="{_R}/table" '
                'Target="../tables/table1.xml"/></Relationships>')
        partes[f"xl/worksheets/sheet{indice}.xml"] = xml_hoja
    ET.SubElement(workbook, f"{{{_NS}}}calcPr", {"calcMode": "auto"})
    partes.update({
        "xl/workbook.xml": ET.tostring(workbook),
        "xl/_rels/workbook.xml.rels": ET.tostring(relaciones),
        "xl/tables/table1.xml": f'<table xmlns="{_NS}" id="1" name="t_colas_diario" displayName="t_colas_diario" ref="A1:A2">'
            '<tableColumns count="1"><tableColumn id="1" name="id"/></tableColumns></table>',
    })
    with ZipFile(ruta, "w") as z:
        for nombre, contenido in partes.items():
            z.writestr(nombre, contenido)


class HojasOcultasTests(unittest.TestCase):
    def test_nueve_fuentes_y_analisis_adicionales_conservan_contenido(self):
        nombres = ("t_colas_diario", "t_colas_intervalo", "t_workloads_diario",
                   "t_workloads_intervalo", "t_ejecutivos_diario", "t_estados_agentes_intervalo",
                   "t_asignaciones_historial", "t_turnos_diario", "t_licencias_vacaciones")
        iniciales = [(nombre, -1, {"A2": 7}) for nombre in nombres]
        iniciales += [("ejemplo_de_uso", 2, {"B4": "=SUM(t_colas_diario[id])"}),
                      ("Mi análisis", 0, {"A1": "Manual", "C7": "=1+2"})]
        libro = _Libro(iniciales)
        grafico = _Hoja(libro, "Gráfico aparte", 2, {"serie": "t_colas_diario[id]"})
        libro.graficos.append(grafico)
        originales = [(hoja.name, hoja.contenido.copy()) for hoja in libro.sheets]
        configurar_hojas_trabajo(libro, [_fuente(nombre) for nombre in nombres])
        self.assertEqual([(h.name, h.contenido) for h in libro.sheets], originales)
        self.assertEqual([h.visible for h in libro.sheets[:9]], [0] * 9)
        self.assertEqual([h.visible for h in libro.sheets[9:]], [-1, -1])
        self.assertEqual(grafico.visible, -1)
        self.assertEqual(grafico.contenido, {"serie": "t_colas_diario[id]"})
        self.assertEqual(libro.activa, "ejemplo_de_uso")
        primer_ocultamiento = next(i for i, e in enumerate(libro.eventos) if e[0] == "visible" and e[2] == 0)
        self.assertLess(libro.eventos.index(("activar", "ejemplo_de_uso")), primer_ocultamiento)

    def test_crea_trabajo_antes_de_ocultar_la_unica_hoja_visible(self):
        libro = _Libro([("t_colas_diario", -1, {"A2": 7})])
        configurar_hojas_trabajo(libro, [_fuente()])
        self.assertEqual([h.name for h in libro.sheets], ["t_colas_diario", "ejemplo_de_uso"])
        self.assertEqual(libro.sheets[0].visible, 0)
        self.assertEqual(libro.sheets[1].visible, -1)
        self.assertEqual(libro.sheets[1].contenido, {})
        self.assertEqual(libro.eventos[0], ("crear", "ejemplo_de_uso"))
        self.assertEqual(libro.activa, "ejemplo_de_uso")

    def test_hojas_fuente_ausentes_o_trabajo_fuente_fallan_antes_de_mutar(self):
        libro = _Libro([("t_colas_diario", -1, {})])
        for fuente in (_fuente("otra_fuente"), _fuente("ejemplo_de_uso")):
            with self.subTest(nombre=fuente.hoja):
                with self.assertRaises(ValueError):
                    configurar_hojas_trabajo(libro, [fuente])
                self.assertEqual(libro.eventos, [])

    def test_verificador_exige_datos_hidden_y_todos_los_analisis_visibles(self):
        correcto = [("t_colas_diario", "hidden"), ("ejemplo_de_uso", None), ("Análisis", "visible")]
        with TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "reporte.xlsx"
            _libro_xml(ruta, correcto)
            self.assertEqual(verificar_libro(ruta, [_fuente()], ocultar_hojas_datos=True), {"t_colas_diario": 1})
            for indice, estado in ((0, "visible"), (0, "veryHidden"), (1, "hidden"), (2, "veryHidden")):
                with self.subTest(hoja=indice, estado=estado):
                    incorrecto = correcto.copy()
                    incorrecto[indice] = (incorrecto[indice][0], estado)
                    _libro_xml(ruta, incorrecto)
                    with self.assertRaisesRegex(ValueError, "Visibilidad incorrecta"):
                        verificar_libro(ruta, [_fuente()], ocultar_hojas_datos=True)
            _libro_xml(ruta, [correcto[0], correcto[2]])
            with self.assertRaisesRegex(ValueError, "ejemplo_de_uso"):
                verificar_libro(ruta, [_fuente()], ocultar_hojas_datos=True)

    def test_opcion_desactivada_preserva_validacion_legacy(self):
        with TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "reporte.xlsx"
            _libro_xml(ruta, [("t_colas_diario", "visible")])
            self.assertEqual(verificar_libro(ruta, [_fuente()]), {"t_colas_diario": 1})
            self.assertEqual(verificar_libro(ruta, [_fuente()], ocultar_hojas_datos=False), {"t_colas_diario": 1})
            with self.assertRaises(ValueError):
                verificar_libro(ruta, [_fuente()], ocultar_hojas_datos=True)


if __name__ == "__main__":
    unittest.main()
