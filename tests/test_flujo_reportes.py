"""Ventanas temporales y extracción compartida, sin conexiones ni Excel."""
from datetime import date
from types import SimpleNamespace
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import polars as pl

from analytics.flujo_reportes import cargar_datos, ejecutar


class FlujoReportesTest(unittest.TestCase):
    def test_forecast_futuro_no_amplia_el_rango_sql(self):
        engine = Mock()
        sql = {k: pl.DataFrame({"fecha": [date(2026, 10, 8)]})
               for k in ("colas_diario", "colas", "ejecutivos", "estados_agentes")}
        workload = SimpleNamespace(df=sql["colas"], df_diario=sql["colas_diario"], alertas=pl.DataFrame())
        with (patch("analytics.flujo_reportes.crear_engine", return_value=engine),
              patch("analytics.flujo_reportes.cargar_fuentes_sql", return_value=sql) as leer_sql,
              patch("analytics.flujo_reportes.cargar_mapa_campanas", return_value=pl.DataFrame()),
              patch("analytics.datos_reportes.cargar_workloads_reportes", return_value=workload) as api):
            resultado = cargar_datos(date(2021, 10, 10), date(2026, 10, 10),
                                     desde_intervalos=date(2026, 8, 17),
                                     desde_workloads=date(2024, 1, 1),
                                     hasta_workloads=date(2026, 11, 15), progreso=None)
        self.assertEqual(leer_sql.call_args.args[:3], (date(2021, 10, 10), date(2026, 10, 10), date(2026, 8, 17)))
        self.assertEqual(api.call_args.args[:3], (date(2024, 1, 1), date(2026, 11, 15), date(2026, 8, 17)))
        self.assertEqual(resultado.hasta, date(2026, 10, 10))
        self.assertEqual(resultado.hasta_workloads, date(2026, 11, 15))
        leer_sql.assert_called_once()
        api.assert_called_once()

    def test_historia_larga_con_detalle_56_dias_y_workloads_propio(self):
        engine = Mock()
        sql = {k: pl.DataFrame({"fecha": [date(2026, 10, 8)]})
               for k in ("colas_diario", "colas", "ejecutivos", "estados_agentes")}
        workload = SimpleNamespace(df=sql["colas"], df_diario=sql["colas_diario"], alertas=pl.DataFrame())
        with (patch("analytics.flujo_reportes.crear_engine", return_value=engine),
              patch("analytics.flujo_reportes.cargar_fuentes_sql", return_value=sql) as leer_sql,
              patch("analytics.flujo_reportes.cargar_mapa_campanas", return_value=pl.DataFrame()) as mapa,
              patch("analytics.datos_reportes.cargar_workloads_reportes", return_value=workload) as api):
            resultado = cargar_datos(date(2024, 1, 1), date(2026, 10, 25),
                                     desde_workloads=date(2026, 9, 1), progreso=None)
        self.assertEqual(resultado.desde_intervalos, date(2026, 8, 31))
        self.assertEqual(leer_sql.call_args.args[:3], (date(2024, 1, 1), date(2026, 10, 25), date(2026, 8, 31)))
        self.assertEqual(api.call_args.args[:3], (date(2026, 9, 1), date(2026, 10, 25), date(2026, 9, 1)))
        self.assertEqual(leer_sql.call_count, 1)
        self.assertEqual(api.call_count, 1)
        mapa.assert_called_once_with(engine)
        engine.dispose.assert_called_once()
        self.assertEqual(len(resultado.datos), 6)

    def test_cerrar_engine_si_falla_sql_y_no_consultar_api(self):
        engine = Mock()
        with (patch("analytics.flujo_reportes.crear_engine", return_value=engine),
              patch("analytics.flujo_reportes.cargar_fuentes_sql", side_effect=ValueError("clave duplicada")),
              patch("analytics.datos_reportes.cargar_workloads_reportes") as api):
            with self.assertRaisesRegex(ValueError, "clave duplicada"):
                cargar_datos(date(2026, 10, 8), date(2026, 10, 12), progreso=None)
        engine.dispose.assert_called_once()
        api.assert_not_called()

    def test_rechazar_rangos_invalidos_antes_de_conectar(self):
        with patch("analytics.flujo_reportes.crear_engine") as conectar:
            for argumentos in (
                (date(2026, 10, 12), date(2026, 10, 8), {}),
                (date(2026, 10, 8), date(2026, 10, 12), {"semanas_intervalos": 0}),
                (date(2026, 10, 8), date(2026, 10, 12), {"desde_workloads": date(2026, 10, 7)}),
                (date(2026, 10, 8), date(2026, 10, 12), {"desde_workloads": date(2026, 10, 13)}),
                (date(2026, 10, 8), date(2026, 10, 12), {"hasta_workloads": date(2026, 10, 11)}),
                (date(2026, 10, 8), date(2026, 10, 12), {"desde_intervalos": date(2026, 10, 7)}),
                (date(2026, 10, 8), date(2026, 10, 12), {"desde_intervalos": date(2026, 10, 13)}),
            ):
                with self.subTest(argumentos=argumentos), self.assertRaises(ValueError):
                    cargar_datos(argumentos[0], argumentos[1], **argumentos[2])
            conectar.assert_not_called()

    def test_cli_listar_no_necesita_plantilla_ni_genera_archivos(self):
        with (patch("analytics.flujo_reportes.cargar_datos", return_value=Mock()) as cargar,
              patch("analytics.flujo_reportes.listar_cortes") as listar,
              patch("analytics.flujo_reportes.generar_reportes") as escribir):
            resultado = ejecutar(["--desde", "2026-10-08", "--hasta", "2026-10-12",
                                  "--plantilla", "plantilla_inexistente.xlsx", "--listar"])
        self.assertEqual(resultado, 0)
        cargar.assert_called_once()
        listar.assert_called_once()
        escribir.assert_not_called()

    def test_cli_rechaza_plantilla_antigua_antes_de_consultar(self):
        with TemporaryDirectory() as carpeta:
            archivo = Path(carpeta) / "plantilla_antigua.xlsx"
            archivo.touch()
            with (patch("analytics.flujo_reportes._revisar_plantilla", side_effect=ValueError("Falta t_colas_diario")),
                  patch("analytics.flujo_reportes.cargar_datos") as cargar,
                  redirect_stderr(StringIO())):
                with self.assertRaises(SystemExit) as error:
                    ejecutar(["--desde", "2026-10-08", "--plantilla", str(archivo)])
                self.assertEqual(error.exception.code, 2)
                cargar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
