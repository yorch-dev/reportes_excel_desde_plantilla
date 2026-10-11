"""Compatibilidad de variantes Workloads ausentes sin consultas ni Excel."""
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import polars as pl

from analytics.excel_reportes import TablaDatos, _actualizar_tabla, _validar_fuentes
from analytics.flujo_reportes import (
    CONTRATO_TABLAS, FUENTES, DatosReportes,
    _completar_variantes_plantilla, generar_reportes,
)


class VariantesPlantillaTest(unittest.TestCase):
    def fuente(self, nombre="t_workloads", *, vacia=False):
        df = pl.DataFrame({
            "fecha": [date(2026, 10, 11)],
            "forecast_operational": [123.0],
            "tmo_operational": [456.25],
            "requerido": [7.5],
        })
        if vacia:
            df = df.head(0)
        return TablaDatos(nombre, "Workloads", df, {"requerido": "0.0000"})

    def metadata(self, fuente, extras, formulas=None):
        return {fuente.nombre: SimpleNamespace(
            encabezados=[*fuente.df.columns, *extras], formulas=formulas or {},
        )}

    def test_variantes_ausentes_diarias_e_intervalos_son_nulos_sin_cambiar_datos(self):
        for nombre, alias in (("t_workloads_diario", "version_3"),
                              ("t_workloads", "version_4")):
            with self.subTest(tabla=nombre):
                fuente = self.fuente(nombre)
                original = fuente.df.clone()
                adicionales = ["forecast_strategic", "tmo_strategic",
                               f"forecast_{alias}", f"tmo_{alias}"]
                resultado = _completar_variantes_plantilla(
                    [fuente], self.metadata(fuente, adicionales),
                )[0]
                self.assertEqual(resultado.nombre, fuente.nombre)
                self.assertEqual(resultado.hoja, fuente.hoja)
                self.assertEqual(resultado.df.height, original.height)
                self.assertTrue(resultado.df.select(original.columns).equals(original))
                self.assertEqual(set(resultado.df.columns) - set(original.columns), set(adicionales))
                self.assertEqual(resultado.columnas_no_recibidas, frozenset(adicionales))
                for columna in adicionales:
                    self.assertEqual(resultado.df.schema[columna], pl.Float64)
                    self.assertEqual(resultado.df[columna].null_count(), original.height)
                self.assertTrue(fuente.df.equals(original))
                self.assertEqual(fuente.formatos, {"requerido": "0.0000"})

    def test_cero_filas_conserva_vacio_y_tipos_de_variantes(self):
        fuente = self.fuente(vacia=True)
        columnas = ["forecast_version_3", "tmo_version_3"]
        resultado = _completar_variantes_plantilla(
            [fuente], self.metadata(fuente, columnas),
        )[0]
        self.assertEqual(resultado.df.height, 0)
        self.assertEqual({c: resultado.df.schema[c] for c in columnas},
                         {c: pl.Float64 for c in columnas})
        self.assertEqual(fuente.df.columns, ["fecha", "forecast_operational", "tmo_operational", "requerido"])

    def test_variantes_existentes_no_se_reemplazan_por_nulos(self):
        fuente = self.fuente()
        resultado = _completar_variantes_plantilla(
            [fuente], self.metadata(fuente, []),
        )[0]
        self.assertTrue(resultado.df.equals(fuente.df))
        self.assertEqual(resultado.df["forecast_operational"].to_list(), [123.0])
        self.assertEqual(resultado.df["tmo_operational"].to_list(), [456.25])

    def test_parejas_incompletas_y_columnas_manuales_no_se_inventan(self):
        fuente = self.fuente()
        extras = ["forecast_solo", "tmo_otro", "observacion", "tmo_parcial"]
        fuente = TablaDatos(fuente.nombre, fuente.hoja,
                           fuente.df.with_columns(pl.lit(17.0).alias("forecast_parcial")),
                           fuente.formatos)
        resultado = _completar_variantes_plantilla(
            [fuente], self.metadata(fuente, extras),
        )[0]
        self.assertTrue(resultado.df.equals(fuente.df))

    def test_pareja_con_formula_se_conserva_para_el_motor_de_excel(self):
        fuente = self.fuente()
        pareja = ["forecast_calculado", "tmo_calculado"]
        for columna in pareja:
            with self.subTest(formula=columna):
                resultado = _completar_variantes_plantilla(
                    [fuente], self.metadata(fuente, pareja, {columna: "1+1"}),
                )[0]
                self.assertTrue(resultado.df.equals(fuente.df))

    def test_alias_invalido_no_se_confunde_con_variante_de_api(self):
        fuente = self.fuente()
        for alias in ("version__5", "VERSION_6", "version-7", "_inicio", "fin_", ""):
            with self.subTest(alias=alias):
                pareja = [f"forecast_{alias}", f"tmo_{alias}"]
                resultado = _completar_variantes_plantilla(
                    [fuente], self.metadata(fuente, pareja),
                )[0]
                self.assertTrue(resultado.df.equals(fuente.df))

    def test_otras_fuentes_no_admiten_columnas_manuales_por_esta_via(self):
        fuentes = [self.fuente("t_datos"), self.fuente("t_ejecutivos")]
        metadatos = {}
        for fuente in fuentes:
            metadatos.update(self.metadata(fuente, ["forecast_strategic", "tmo_strategic"]))
        resultado = _completar_variantes_plantilla(fuentes, metadatos)
        self.assertEqual(len(resultado), len(fuentes))
        for antes, despues in zip(fuentes, resultado):
            self.assertEqual(despues.nombre, antes.nombre)
            self.assertTrue(despues.df.equals(antes.df))

    def test_formula_no_declarada_en_xml_se_rechaza_antes_de_modificar_excel(self):
        fuente = self.fuente()
        extras = ["forecast_strategic", "tmo_strategic"]
        completada = _completar_variantes_plantilla(
            [fuente], self.metadata(fuente, extras),
        )[0]
        for tiene_formula in (True, None):
            with self.subTest(has_formula=tiene_formula):
                tabla = Mock()
                tabla.header_row_range = SimpleNamespace(
                    api=SimpleNamespace(Value2=(tuple(completada.df.columns),)),
                )
                tabla.api.ListColumns.return_value = SimpleNamespace(
                    DataBodyRange=SimpleNamespace(HasFormula=tiene_formula),
                )
                hoja = SimpleNamespace(tables={completada.nombre: tabla}, range=Mock())
                libro = SimpleNamespace(sheets={completada.hoja: hoja})
                with self.assertRaisesRegex(ValueError, "variante no recibida con f"):
                    _actualizar_tabla(libro, completada, None)
                tabla.data_body_range.clear_contents.assert_not_called()
                tabla.resize.assert_not_called()
                hoja.range.assert_not_called()

    def test_columna_marcada_no_recibida_debe_contener_solo_nulos(self):
        fuente = self.fuente()
        erronea = TablaDatos(
            fuente.nombre, fuente.hoja, fuente.df, fuente.formatos,
            columnas_no_recibidas=frozenset({"forecast_operational"}),
        )
        with self.assertRaisesRegex(ValueError, "una columna no recibida contiene datos"):
            _validar_fuentes([erronea])

    def test_general_cliente_y_jefe_completan_segun_su_propia_plantilla(self):
        datos = {clave: self.fuente().df.clone() for clave in FUENTES}
        resultado = DatosReportes(
            datos, pl.DataFrame(),
            SimpleNamespace(alertas=pl.DataFrame(), cobertura_diaria=pl.DataFrame()),
            date(2026, 10, 11), date(2026, 10, 11),
            date(2026, 10, 11), date(2026, 10, 11),
        )

        def fuentes_para(frames):
            return [TablaDatos(nombre, hoja, frames[clave])
                    for clave, (nombre, hoja) in zip(FUENTES, CONTRATO_TABLAS.items())]

        plan = SimpleNamespace(
            nombres=["Prueba"], diagnosticos=pl.DataFrame(),
            obtener=lambda nombre: SimpleNamespace(datos=datos),
        )
        with TemporaryDirectory() as directorio:
            base = Path(directorio).resolve()
            variantes = {
                base / "base.xlsx": "strategic",
                base / "cliente.xlsx": "version_3",
                base / "jefe.xlsx": "version_4",
            }
            for archivo in variantes:
                archivo.touch()
            metadatos = {}
            for archivo, alias in variantes.items():
                metadatos[archivo] = {}
                for fuente in fuentes_para(datos):
                    extras = ([f"forecast_{alias}", f"tmo_{alias}"]
                              if fuente.nombre in ("t_workloads", "t_workloads_diario") else [])
                    metadatos[archivo][fuente.nombre] = SimpleNamespace(
                        hoja=fuente.hoja, encabezados=[*fuente.df.columns, *extras], formulas={},
                    )
            with (patch("analytics.flujo_reportes.tablas_reportes", side_effect=fuentes_para),
                  patch("analytics.flujo_reportes.preparar_plan_cortes", return_value=plan),
                  patch("analytics.flujo_reportes._revisar_plantilla",
                        side_effect=lambda ruta: metadatos[Path(ruta)]),
                  patch("analytics.flujo_reportes._json"),
                  patch("analytics.flujo_reportes.generar_libro") as escribir,
                  patch("xlwings.App")):
                resumen = generar_reportes(
                    resultado, base / "base.xlsx", salida_general=base / "general.xlsx",
                    salida_clientes=base / "clientes", salida_jefes=base / "jefes",
                    plantilla_cliente=base / "cliente.xlsx", plantilla_jefe=base / "jefe.xlsx",
                    progreso=lambda mensaje: None,
                )
            self.assertEqual(resumen["estado"], "completo")
            self.assertEqual(escribir.call_count, 3)
            for llamada in escribir.call_args_list:
                alias = variantes[llamada.kwargs["plantilla"]]
                esperadas = {f"forecast_{alias}", f"tmo_{alias}"}
                for fuente in llamada.args[0]:
                    if fuente.nombre in ("t_workloads", "t_workloads_diario"):
                        self.assertEqual(set(fuente.df.columns) - set(self.fuente().df.columns), esperadas)
                        for columna in esperadas:
                            self.assertEqual(fuente.df[columna].to_list(), [None])
                    self.assertTrue(fuente.df.select(self.fuente().df.columns).equals(self.fuente().df))
            for frame in datos.values():
                self.assertTrue(frame.equals(self.fuente().df))


if __name__ == "__main__":
    unittest.main()
