"""Cortes de seis fuentes sin consultas SQL/API ni Excel."""
from datetime import date, datetime
import unittest
from unittest.mock import patch

import polars as pl

from src.analytics.cortes_reportes import (
    FUENTES as FUENTES_NUEVE, SCHEMA_DIAGNOSTICOS,
    _FUENTES_OCHO as FUENTES_OCHO,
    _FUENTES_SEIS as FUENTES,
    UNIDAD_EJECUTIVOS, UNIDAD_ESTADOS, preparar_cortes, preparar_plan_cortes,
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


def _fixture_ocho():
    datos, mapa = _fixture()
    schema = {"fila_id": pl.Int64, "campana": pl.String, "campana_id": pl.Int64,
              "cliente": pl.String, "jefe_negocio": pl.String}
    datos["asignaciones"] = pl.DataFrame([
        (800, "Solo catálogo", 101, "Cliente C", "Jefe C"),
        (801, "CA", 1, "Cliente B", "Jefe B"),
        (802, None, 101, "Cliente C", "Jefe C"),
        (803, "Sin ID", None, "Cliente C", "Jefe C"),
        (804, "ID inválido", 0, "Cliente C", "Jefe C"),
        (805, "Sin cliente", 101, None, "Jefe C"),
        (806, "Sin jefe", 101, "Cliente C", "  "),
        (807, "Campaña histórica", 202, "Cliente D", "Jefe D"),
    ], schema=schema, orient="row").with_columns(
        pl.lit("Persona 001").alias("nombre"), pl.lit("001-K").alias("rut"),
        pl.lit("0001").alias("contrato"),
        pl.lit(date(2023, 1, 1)).alias("fecha_inicio"),
        pl.when(pl.col("fila_id") == 807).then(pl.lit(date(2024, 12, 31)))
        .otherwise(pl.lit(None, dtype=pl.Date)).alias("fecha_termino"),
    )
    datos["turnos"] = pl.DataFrame([
        (900, "Solo catálogo", 101, "Cliente C", "Jefe C", "asignacion_vigente"),
        (901, "CA", 1, "Cliente B", "Jefe B", "asignacion_vigente"),
        (902, "Solo catálogo", 101, "Cliente C", "Jefe C", "sin_asignacion"),
        (903, "Solo catálogo", 101, "Cliente C", "Jefe C", "asignacion_ambigua"),
        (904, None, 101, "Cliente C", "Jefe C", "asignacion_vigente"),
        (905, "Sin ID", None, "Cliente C", "Jefe C", "asignacion_vigente"),
        (906, "Sin cliente", 101, "  ", "Jefe C", "asignacion_vigente"),
        (907, "Sin jefe", 101, "Cliente C", None, "asignacion_vigente"),
        (908, "Solo catálogo", 101, "Cliente C", "Jefe C", None),
        (909, "CA", 1, "Cliente A", "Jefe A", "asignacion_vigente"),
    ], schema={**schema, "estado_asignacion": pl.String}, orient="row").with_columns(
        pl.lit("Persona 001").alias("nombre"), pl.lit("001-K").alias("rut"),
        pl.lit(None, dtype=pl.String).alias("contrato"),
        pl.lit(date(2026, 9, 11)).alias("fecha"),
        pl.lit(datetime(2026, 9, 11, 8, 0)).alias("inicio_turno"),
        pl.lit(datetime(2026, 9, 11, 8, 30)).alias("fin_turno"),
        pl.lit(1800.25).alias("duracion_seg"),
        pl.lit(None, dtype=pl.Float64).alias("dato_nulo"),
    )
    return datos, mapa


def _fixture_nueve():
    datos, mapa = _fixture_ocho()
    datos["licencias_vacaciones"] = pl.DataFrame([
        (1000, 0, "Sólo licencias", 301, "Cliente L", "Jefe L", date(2020, 1, 1), date(2020, 1, 4)),
        (1000, 1, "Otro tramo", 302, "Cliente M", "Jefe M", date(2021, 4, 1), date(2021, 4, 2)),
        (1001, 2, "CA", 1, "Cliente M", "Jefe M", date(2026, 10, 10), date(2026, 10, 11)),
        (1002, 3, "Sólo licencias", 301, None, "Jefe L", date(2028, 11, 1), None),
        (1003, 4, "Sólo licencias", 301, "Cliente L", None, date(2025, 5, 1), date(2025, 5, 2)),
        (1004, 5, None, 301, "Cliente L", "Jefe L", date(2026, 10, 10), date(2026, 10, 10)),
        (1005, 6, "Sin ID", None, "Cliente L", "Jefe L", None, None),
        (1006, 7, "Sin dimensión", 301, "  ", "  ", date(2026, 10, 10), date(2026, 10, 10)),
    ], schema={
        "id_ausencia": pl.Int64, "tramo": pl.Int64, "campana": pl.String,
        "campana_id": pl.Int64, "cliente": pl.String, "jefe_negocio": pl.String,
        "fecha_inicio": pl.Date, "fecha_termino": pl.Date,
    }, orient="row").with_columns(
        pl.lit("99999999-K").alias("rut"), pl.lit("Empleado sólo ausencias").alias("nombre"),
        pl.lit("0000123").alias("contrato"), pl.lit(None, dtype=pl.String).alias("observacion"),
        pl.lit(None, dtype=pl.Float64).alias("horas"),
    )
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


class CortesAsignacionesTurnosTest(unittest.TestCase):
    def test_ocho_fuentes_preservan_granos_tipos_null_y_historia(self):
        datos, mapa = _fixture_ocho()
        originales = {fuente: df.clone() for fuente, df in datos.items()}
        for criterio in ("cliente", "jefe_negocio"):
            with self.subTest(criterio=criterio):
                cortes, diagnosticos = preparar_cortes(datos, mapa, criterio)
                self.assertEqual(diagnosticos.schema, SCHEMA_DIAGNOSTICOS)
                esperados = ({"asignaciones": {800, 801, 806, 807}, "turnos": {900, 901, 907, 909}}
                             if criterio == "cliente" else
                             {"asignaciones": {800, 801, 805, 807}, "turnos": {900, 901, 906, 909}})
                for fuente in ("asignaciones", "turnos"):
                    filas = []
                    for corte in cortes:
                        self.assertEqual(set(corte.datos), set(FUENTES_OCHO))
                        df = corte.datos[fuente]
                        self.assertEqual(df.schema, datos[fuente].schema)
                        filas.extend(df["fila_id"].to_list())
                        self.assertTrue(df.equals(datos[fuente].filter(
                            pl.col("fila_id").is_in(df["fila_id"].implode()))))
                    self.assertEqual(set(filas), esperados[fuente])
                    self.assertEqual(len(filas), len(set(filas)))
                    excluidas = diagnosticos.filter(
                        (pl.col("fuente") == fuente) & pl.col("excluidas"))["filas"].sum()
                    self.assertEqual(len(filas) + excluidas, datos[fuente].height)
                historico = next(c for c in cortes if c.nombre in ("Cliente D", "Jefe D"))
                self.assertEqual(historico.datos["asignaciones"]["fecha_termino"].to_list(), [date(2024, 12, 31)])
                self.assertTrue(all(df.equals(originales[fuente]) for fuente, df in datos.items()))

    def test_catalogo_sin_peopleware_y_discrepancia_nueva_no_cambian_seis_fuentes(self):
        datos, mapa = _fixture_ocho()
        anteriores = {f: datos[f] for f in FUENTES}
        for criterio in ("cliente", "jefe_negocio"):
            prefijo = "Cliente" if criterio == "cliente" else "Jefe"
            plan_seis = preparar_plan_cortes(anteriores, mapa, criterio)
            plan_ocho = preparar_plan_cortes(datos, mapa.vstack(mapa.head(1)), criterio)
            self.assertEqual(set(plan_ocho.nombres), set(plan_seis.nombres) | {f"{prefijo} C", f"{prefijo} D"})
            for nombre in plan_seis.nombres:
                antes, despues = plan_seis.obtener(nombre), plan_ocho.obtener(nombre)
                self.assertTrue(all(antes.datos[f].equals(despues.datos[f]) for f in FUENTES))
            nuevo = plan_ocho.obtener(f"{prefijo} C")
            self.assertTrue(all(nuevo.datos[f].is_empty() for f in FUENTES))
            self.assertEqual(nuevo.datos["asignaciones"].height, 2)
            self.assertEqual(nuevo.datos["turnos"].height, 2)
            # El catálogo completo es suficiente aunque el mapa PW esté vacío.
            sin_pw = preparar_plan_cortes(datos, mapa.head(0), criterio)
            self.assertTrue(sin_pw.obtener(f"{prefijo} C").datos["turnos"].equals(nuevo.datos["turnos"]))

    def test_estados_no_atribuibles_diagnostican_una_causa_y_quedan_en_general(self):
        datos, mapa = _fixture_ocho()
        plan = preparar_plan_cortes(datos, mapa, "cliente")
        alertas = plan.diagnosticos.filter(pl.col("fuente") == "turnos")
        estados = alertas.filter(pl.col("motivo") == "turno_sin_asignacion_vigente")
        self.assertEqual(estados["filas"].sum(), 3)
        self.assertTrue(estados["excluidas"].all())
        self.assertTrue(estados["planning_unit_id"].is_null().all())
        self.assertTrue(any("asignacion_ambigua" in detalle for detalle in estados["detalle"]))
        self.assertEqual(set(alertas["motivo"]), {
            "turno_sin_asignacion_vigente", "campana_sin_valor", "id_campana_invalido", "dimension_sin_valor",
        })
        self.assertEqual(plan._datos["turnos"].height, 10)
        self.assertIs(plan._datos["turnos"], datos["turnos"])

    def test_plan_ocho_es_perezoso_y_fuentes_nuevas_vacias_conservan_schema(self):
        datos, mapa = _fixture_ocho()
        with patch.object(pl.DataFrame, "filter", side_effect=AssertionError("Filtro prematuro")):
            plan = preparar_plan_cortes(datos, mapa, "cliente")
            self.assertEqual(plan.filas("Cliente C"), {
                **{fuente: 0 for fuente in FUENTES}, "asignaciones": 2, "turnos": 2,
            })
            self.assertTrue(all(plan._datos[f] is datos[f] for f in FUENTES_OCHO))
        vacios = {**datos, "asignaciones": datos["asignaciones"].head(0), "turnos": datos["turnos"].head(0)}
        corte = preparar_plan_cortes(vacios, mapa, "cliente").obtener("Cliente B")
        for fuente in ("asignaciones", "turnos"):
            self.assertEqual(corte.datos[fuente].height, 0)
            self.assertEqual(corte.datos[fuente].schema, datos[fuente].schema)

    def test_nuevas_fuentes_exigen_campos_y_el_contrato_completo(self):
        datos, mapa = _fixture_ocho()
        for fuente, columna in (("asignaciones", "campana_id"), ("turnos", "estado_asignacion")):
            with self.subTest(fuente=fuente), self.assertRaisesRegex(ValueError, fuente):
                preparar_plan_cortes({**datos, fuente: datos[fuente].drop(columna)}, mapa, "cliente")
        with self.assertRaisesRegex(ValueError, "nueve fuentes"):
            preparar_plan_cortes({f: df for f, df in datos.items() if f != "turnos"}, mapa, "cliente")


class CortesLicenciasVacacionesTest(unittest.TestCase):
    def test_id_ausencia_repetido_no_colapsa_campanas_ni_periodos(self):
        datos, mapa = _fixture_nueve()
        fuente = datos["licencias_vacaciones"]
        original = fuente.clone()
        self.assertFalse(any("99999999-K" in datos[f]["rut"] for f in FUENTES_OCHO))
        for criterio in ("cliente", "jefe_negocio"):
            with self.subTest(criterio=criterio):
                plan = preparar_plan_cortes(datos, mapa.head(0), criterio)
                esperados = {0, 1, 2, 4} if criterio == "cliente" else {0, 1, 2, 3}
                tramos, ids = [], []
                for nombre in plan.nombres:
                    corte = plan.obtener(nombre)
                    self.assertEqual(set(corte.datos), set(FUENTES_NUEVE))
                    licencias = corte.datos["licencias_vacaciones"]
                    self.assertEqual(licencias.schema, fuente.schema)
                    self.assertTrue(licencias.equals(fuente.filter(
                        pl.col("tramo").is_in(licencias["tramo"].implode()))))
                    tramos.extend(licencias["tramo"].to_list())
                    ids.extend(licencias["id_ausencia"].to_list())
                self.assertEqual(set(tramos), esperados)
                self.assertEqual(len(tramos), len(esperados))
                self.assertEqual(ids.count(1000), 2)
                self.assertIs(plan._datos["licencias_vacaciones"], fuente)
                self.assertTrue(fuente.equals(original))
                # Se conservan tramos históricos y futuros sin desplegar días.
                propietario = "Cliente L" if criterio == "cliente" else "Jefe L"
                periodos = plan.obtener(propietario).datos["licencias_vacaciones"]
                self.assertIn(date(2020, 1, 1), periodos["fecha_inicio"])
                if criterio == "jefe_negocio":
                    self.assertIn(date(2028, 11, 1), periodos["fecha_inicio"])
                    self.assertEqual(periodos["fecha_termino"].null_count(), 1)

    def test_dimensiones_propias_no_cambian_las_ocho_fuentes_anteriores(self):
        datos, mapa = _fixture_nueve()
        anteriores = {f: datos[f] for f in FUENTES_OCHO}
        for criterio in ("cliente", "jefe_negocio"):
            prefijo = "Cliente" if criterio == "cliente" else "Jefe"
            plan_ocho = preparar_plan_cortes(anteriores, mapa, criterio)
            plan_nueve = preparar_plan_cortes(datos, mapa, criterio)
            self.assertEqual(set(plan_nueve.nombres), set(plan_ocho.nombres) | {f"{prefijo} L", f"{prefijo} M"})
            for nombre in plan_ocho.nombres:
                antes, despues = plan_ocho.obtener(nombre), plan_nueve.obtener(nombre)
                self.assertTrue(all(antes.datos[f].equals(despues.datos[f]) for f in FUENTES_OCHO))
            nuevo = plan_nueve.obtener(f"{prefijo} M")
            self.assertTrue(all(nuevo.datos[f].is_empty() for f in FUENTES_OCHO))
            self.assertEqual(nuevo.datos["licencias_vacaciones"].height, 2)

    def test_excluidas_tienen_una_causa_y_conservan_todos_los_registros_generales(self):
        datos, mapa = _fixture_nueve()
        for criterio in ("cliente", "jefe_negocio"):
            plan = preparar_plan_cortes(datos, mapa, criterio)
            self.assertEqual(plan.diagnosticos.schema, SCHEMA_DIAGNOSTICOS)
            diagnosticos = plan.diagnosticos.filter(pl.col("fuente") == "licencias_vacaciones")
            self.assertEqual(set(diagnosticos["motivo"]), {
                "campana_sin_valor", "id_campana_invalido", "dimension_sin_valor",
            })
            self.assertTrue(diagnosticos["excluidas"].all())
            self.assertTrue(diagnosticos["planning_unit_id"].is_null().all())
            self.assertEqual(diagnosticos["filas"].sum(), 4)
            filas = sum(plan.filas(nombre)["licencias_vacaciones"] for nombre in plan.nombres)
            self.assertEqual(filas + diagnosticos["filas"].sum(), datos["licencias_vacaciones"].height)
            self.assertEqual(plan._datos["licencias_vacaciones"].height, 8)

    def test_licencias_vacias_y_campos_obligatorios(self):
        datos, mapa = _fixture_nueve()
        vacios = {**datos, "licencias_vacaciones": datos["licencias_vacaciones"].head(0)}
        corte = preparar_plan_cortes(vacios, mapa, "cliente").obtener("Cliente A")
        self.assertTrue(corte.datos["licencias_vacaciones"].is_empty())
        self.assertEqual(corte.datos["licencias_vacaciones"].schema, datos["licencias_vacaciones"].schema)
        with self.assertRaisesRegex(ValueError, "licencias_vacaciones"):
            preparar_plan_cortes({**datos, "licencias_vacaciones": datos["licencias_vacaciones"].drop("cliente")},
                                mapa, "cliente")


if __name__ == "__main__":
    unittest.main()
