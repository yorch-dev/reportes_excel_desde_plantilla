"""Cortes de seis fuentes sin consultas SQL/API ni Excel."""
from datetime import date
import unittest
from unittest.mock import patch

import polars as pl

from src.analytics.cortes_reportes import (
    FUENTES, UNIDAD_EJECUTIVOS, UNIDAD_ESTADOS, preparar_cortes, preparar_plan_cortes,
)


def _fixture():
    mapa = pl.DataFrame([
        (1, "UA", "CA", "Cliente A", "Jefe A"),
        (2, "UB", "CB", "Cliente B", "Jefe B"),
        (3, "Compartida", "CC", "Cliente A", "Jefe A"),
        (3, "Compartida", "CD", "Cliente B", "Jefe B"),
        (4, "Nombre ambiguo", "CE", "Cliente A", "Jefe A"),
        (5, "Nombre ambiguo", "CF", "Cliente A", "Jefe A"),
        (6, "Sin propietario", "CG", None, None),
        (7, "Sin campana", None, "Cliente A", "Jefe A"),
        (8, "Discrepancia", "CH", "Cliente A", "Jefe A"),
    ], schema={"planning_unit_id": pl.Int64, "unidad_planificacion": pl.String,
               "nombre_campana": pl.String, "cliente": pl.String,
               "jefe_negocio": pl.String}, orient="row")

    def colas(rows):
        return pl.DataFrame(rows, schema={"fila_id": pl.Int64, "nombre_campana": pl.String,
                                         "cliente": pl.String, "jefe_negocio": pl.String}, orient="row")

    def workloads(rows):
        return pl.DataFrame(rows, schema={"fila_id": pl.Int64, "planning_unit_id": pl.Int64,
                                         "unidad_planificacion": pl.String}, orient="row")

    agentes = [
        (0, "UA", "Jefe A"), (1, "UB", "Jefe B"), (2, "UA", "Otro jefe"),
        (3, "Nombre ambiguo", "Jefe A"), (4, "Compartida", "Jefe A"),
        (5, "Sin propietario", None), (6, "Desconocida", "Jefe A"),
        (7, "Discrepancia", "Jefe A"), (8, "Sin campana", "Jefe A"),
        (9, None, "Jefe A"),
    ]

    def personal(columna, prefijo):
        return pl.DataFrame([(prefijo + i, unidad, jefe) for i, unidad, jefe in agentes],
                            schema={"fila_id": pl.Int64, columna: pl.String,
                                    "jefe_negocio": pl.String}, orient="row")

    datos = {
        "colas_diario": colas([(100, "CA", "Cliente A", "Jefe A"),
                                (101, "CB", "Cliente B", "Jefe B"),
                                (102, "CH", "Otro cliente", "Otro jefe"),
                                (103, "Sin mapa", "Cliente A", "Jefe A"),
                                (104, None, None, None)]),
        "colas": colas([(200, "CA", "Cliente A", "Jefe A"),
                         (201, "CB", "Cliente B", "Jefe B"),
                         (202, "CH", "Cliente A", "Jefe A")]),
        "workloads_diario": workloads([(300, 1, "UA"), (301, 2, "UB"),
                                        (302, 3, "Compartida"), (303, 4, "Nombre ambiguo"),
                                        (304, 6, "Sin propietario"), (305, 7, "Sin campana"),
                                        (306, 8, "Discrepancia"), (307, None, None),
                                        (308, 999, "Desconocida")]),
        "workloads": workloads([(400, 1, "UA"), (401, 2, "UB"),
                                 (402, 5, "Nombre ambiguo"), (403, 8, "Discrepancia"),
                                 (404, None, None)]),
        "ejecutivos": personal(UNIDAD_EJECUTIVOS, 500),
        "estados_agentes": personal(UNIDAD_ESTADOS, 600),
    }
    # Columnas ajenas al corte, incluidos NULL y texto sensible a coerción.
    datos = {nombre: df.with_columns(pl.lit(date(2026, 10, 10)).alias("fecha"),
                                    pl.lit(None, dtype=pl.Float64).alias("dato_nulo"),
                                    pl.lit("001-K").alias("rut")) for nombre, df in datos.items()}
    return datos, mapa


class CortesReportesTest(unittest.TestCase):
    def test_plan_sin_copias_y_seleccion_parcial(self):
        datos, mapa = _fixture()
        for criterio, nombre in (("cliente", "Cliente B"), ("jefe_negocio", "Jefe B")):
            with self.subTest(criterio=criterio):
                with patch.object(pl.DataFrame, "filter", side_effect=AssertionError("Filtro prematuro")):
                    plan = preparar_plan_cortes(datos, mapa, criterio)
                    self.assertIn(nombre, plan.nombres)
                    self.assertEqual(plan.filas(nombre), {fuente: 1 for fuente in FUENTES})
                    self.assertTrue(all(plan._datos[f] is datos[f] for f in FUENTES))
                    with self.assertRaises(ValueError):
                        plan.obtener("Nombre desconocido")
                filtrar = pl.DataFrame.filter
                with patch.object(pl.DataFrame, "filter", autospec=True, side_effect=filtrar) as filtro:
                    corte = plan.obtener(nombre)
                    self.assertEqual(filtro.call_count, len(FUENTES))
                self.assertEqual({f: df.height for f, df in corte.datos.items()}, plan.filas(nombre))
                anteriores, diagnosticos = preparar_cortes(datos, mapa, criterio)
                equivalente = next(c for c in anteriores if c.nombre == nombre)
                self.assertEqual(corte.nombre_archivo, equivalente.nombre_archivo)
                self.assertTrue(all(corte.datos[f].equals(equivalente.datos[f]) for f in FUENTES))
                self.assertTrue(plan.diagnosticos.equals(diagnosticos))
                for fuente in FUENTES:
                    asignadas = sum(plan.filas(n)[fuente] for n in plan.nombres)
                    excluidas = plan.diagnosticos.filter(
                        (pl.col("fuente") == fuente) & pl.col("excluidas"))["filas"].sum()
                    self.assertEqual(asignadas + excluidas, datos[fuente].height)

    def test_seis_fuentes_ambos_criterios_conservan_filas_y_tipos(self):
        datos, mapa = _fixture()
        for criterio in ("cliente", "jefe_negocio"):
            with self.subTest(criterio=criterio):
                cortes, diagnosticos = preparar_cortes(datos, mapa, criterio)
                esperados = {"colas_diario": {100, 101, 102, 103}, "colas": {200, 201, 202},
                             "workloads_diario": {300, 301, 303}, "workloads": {400, 401, 402},
                             "ejecutivos": {500, 501}, "estados_agentes": {600, 601}}
                if criterio == "cliente":
                    esperados["ejecutivos"].add(502)
                    esperados["estados_agentes"].add(602)
                for fuente in FUENTES:
                    ids = []
                    for corte in cortes:
                        self.assertEqual(set(corte.datos), set(FUENTES))
                        df = corte.datos[fuente]
                        self.assertEqual(df.schema, datos[fuente].schema)
                        ids.extend(df["fila_id"].to_list())
                        self.assertTrue(df.equals(datos[fuente].filter(
                            pl.col("fila_id").is_in(df["fila_id"].implode()))))
                    self.assertEqual(set(ids), esperados[fuente])
                    self.assertEqual(len(ids), len(set(ids)))
                    excluidas = diagnosticos.filter(
                        (pl.col("fuente") == fuente) & pl.col("excluidas"))["filas"].sum()
                    self.assertEqual(len(ids) + excluidas, datos[fuente].height)
                self.assertTrue(diagnosticos.filter(
                    (pl.col("fuente") == "colas_diario") &
                    (pl.col("motivo") == "dimension_distinta_del_mapa")).height)
                if criterio == "jefe_negocio":
                    self.assertTrue(diagnosticos.filter(
                        (pl.col("fuente") == "estados_agentes") &
                        (pl.col("motivo") == "jefe_estado_distinto_del_mapa")).height)

    def test_discrepancia_en_colas_intervalo_bloquea_ambos_workloads(self):
        datos, mapa = _fixture()
        datos["colas"] = datos["colas"].with_columns(
            pl.when(pl.col("nombre_campana") == "CA").then(pl.lit("Otro cliente"))
            .otherwise(pl.col("cliente")).alias("cliente"))
        cortes, _ = preparar_cortes(datos, mapa, "cliente")
        for fuente, id_bloqueado in (("workloads_diario", 300), ("workloads", 400),
                                    ("ejecutivos", 500), ("estados_agentes", 600)):
            self.assertFalse(any(id_bloqueado in corte.datos[fuente]["fila_id"] for corte in cortes))

    def test_mapa_duplicado_no_multiplica_hechos(self):
        datos, mapa = _fixture()
        for criterio in ("cliente", "jefe_negocio"):
            original, _ = preparar_cortes(datos, mapa, criterio)
            duplicado, _ = preparar_cortes(datos, mapa.vstack(mapa.head(1)), criterio)
            self.assertEqual([c.nombre_archivo for c in original], [c.nombre_archivo for c in duplicado])
            for antes, despues in zip(original, duplicado):
                self.assertTrue(all(antes.datos[f].equals(despues.datos[f]) for f in FUENTES))

    def test_compatibilidad_tres_fuentes_y_vacias(self):
        datos, mapa = _fixture()
        anteriores = {f: datos[f] for f in ("colas", "ejecutivos", "workloads")}
        for criterio in ("cliente", "jefe_negocio"):
            cortes, _ = preparar_cortes(anteriores, mapa, criterio)
            self.assertTrue(cortes)
            self.assertTrue(all(set(c.datos) == set(anteriores) for c in cortes))
            vacios, _ = preparar_cortes({f: df.head(0) for f, df in datos.items()}, mapa, criterio)
            self.assertEqual(vacios, [])
        with self.assertRaises(ValueError):
            preparar_cortes({"colas": datos["colas"]}, mapa, "cliente")


if __name__ == "__main__":
    unittest.main()
