"""Conservar anomalías de duración sin perder las guardas estructurales."""
from datetime import date, time
import unittest

import polars as pl

from analytics.fuentes_sql import ESTADOS, HORAS_COLAS_DIARIO, _validar_frame


class FuentesSqlTest(unittest.TestCase):
    def estados(self):
        fila = {columna: None for columna in ESTADOS}
        fila.update(id=560024, fecha=date(2026, 9, 11), intervalo=time(18, 30),
                    id_agente="agente-1", rut="12345678-5",
                    conectado=3598.313 / 3600, en_la_cola=3596.626 / 3600,
                    fuera_de_la_cola=1.687 / 3600, interactuando=0.5)
        schema = {"id": pl.Int64, "fecha": pl.Date, "intervalo": pl.Time,
                  "id_agente": pl.String, "rut": pl.String,
                  **{columna: pl.Float64 for columna in ESTADOS}}
        return pl.DataFrame([fila], schema=schema)

    def validar_estados(self, df):
        _validar_frame("estados_agentes", df, date(2026, 9, 11), date(2026, 9, 11))

    def test_exceso_de_conexion_se_conserva_para_excel(self):
        df = self.estados()
        original = df.clone()
        self.validar_estados(df)
        self.assertTrue(df.equals(original))
        self.assertEqual(df["conectado"][0], 3598.313 / 3600)
        self.assertEqual(df["comida"].null_count(), 1)

    def test_duracion_negativa_sigue_rechazada(self):
        df = self.estados().with_columns(pl.lit(-0.01).alias("descanso"))
        with self.assertRaisesRegex(ValueError, "Duraciones negativas"):
            self.validar_estados(df)

    def test_intervalo_de_15_minutos_sigue_rechazado(self):
        df = self.estados().with_columns(pl.lit(time(18, 15)).alias("intervalo"))
        with self.assertRaisesRegex(ValueError, "raster de 30 minutos"):
            self.validar_estados(df)

    def test_registros_distintos_del_mismo_intervalo_siguen_rechazados(self):
        df = self.estados()
        duplicado = df.with_columns(pl.lit(560025, dtype=pl.Int64).alias("id"))
        with self.assertRaisesRegex(ValueError, "fecha,id_agente,intervalo"):
            self.validar_estados(pl.concat([df, duplicado]))

    def test_colas_diario_con_nombres_distintos_siguen_rechazadas(self):
        fecha = date(2025, 10, 30)
        filas = []
        for identificador, nombre in [(116170, "isalud_inhabil"), (492628, "ISALUD_INHABIL")]:
            fila = {columna: None for columna in HORAS_COLAS_DIARIO}
            fila.update(rendimiento_id=identificador, fecha=fecha, id_cola="cola-1",
                        tipo_medios="voz", id_division="division-1", campana_id=1,
                        nombre_division=nombre)
            filas.append(fila)
        schema = {"rendimiento_id": pl.Int64, "fecha": pl.Date, "id_cola": pl.String,
                  "tipo_medios": pl.String, "id_division": pl.String, "campana_id": pl.Int64,
                  "nombre_division": pl.String,
                  **{columna: pl.Float64 for columna in HORAS_COLAS_DIARIO}}
        df = pl.DataFrame(filas, schema=schema)
        with self.assertRaisesRegex(ValueError, "fecha,id_cola,tipo_medios,id_division"):
            _validar_frame("colas_diario", df, fecha, fecha)


if __name__ == "__main__":
    unittest.main()
