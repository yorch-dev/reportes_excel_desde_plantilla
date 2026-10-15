"""Vigencias y conservación de turnos: sólo fixtures, sin DB, API ni Excel."""
import sys
import unittest
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analytics.asignaciones_turnos import (
    COLUMNAS_ASIGNACIONES_FUENTE, COLUMNAS_TURNOS_FUENTE, TIMESTAMPS_TURNOS,
    CONFIG_ASIGNACIONES, CONFIG_TURNOS, TIPOS_ASIGNACIONES_FUENTE,
    formatos_asignaciones, formatos_turnos,
    preparar_asignaciones_turnos,
)


DIA = date(2026, 10, 8)
RUT = "12345678-K"
SCHEMA_ASIGNACIONES = {
    **{c: pl.String for c in COLUMNAS_ASIGNACIONES_FUENTE[:4]},
    "fecha_inicio": pl.Date, "fecha_termino": pl.Date,
}
SCHEMA_TURNOS = {"id": pl.Int64, "rut": pl.String, "fecha": pl.Date,
                 **{c: pl.Datetime("us") for c in TIMESTAMPS_TURNOS}}
SCHEMA_CATALOGO = {"campana_id": pl.Int64, "campana": pl.String,
                   "cliente": pl.String, "jefe_negocio": pl.String}


def asignaciones(rows):
    return pl.DataFrame(rows, schema=SCHEMA_ASIGNACIONES, orient="row")


def turnos(rows):
    return pl.DataFrame(rows, schema=SCHEMA_TURNOS)


def turno(identificador=1, rut=RUT, dia=DIA):
    inicio = datetime.combine(dia, datetime.min.time()) + timedelta(hours=22, microseconds=123456)
    return {"id": identificador, "rut": rut, "fecha": dia,
            "inicio_turno": inicio, "fin_turno": inicio + timedelta(hours=9),
            "inicio_b1": None, "fin_b1": None,
            "inicio_almuerzo": inicio + timedelta(hours=3),
            "fin_almuerzo": inicio + timedelta(hours=3, minutes=30),
            "inicio_b2": None, "fin_b2": None}


def catalogo(rows=None):
    return pl.DataFrame(rows if rows is not None else [(1, "A", "Cliente A", "Jefe A"),
        (2, "B", "Cliente B", "Jefe B")], schema=SCHEMA_CATALOGO, orient="row")


class AsignacionesTurnosTests(unittest.TestCase):
    def preparar(self, a, t, c=None):
        return preparar_asignaciones_turnos(a, t, catalogo() if c is None else c,
                                            date(2026, 10, 1), date(2026, 10, 31))

    def test_vigencia_inclusiva_extremos_abiertos_normalizacion_y_medianoche(self):
        a = asignaciones([
            ("Agente A", "12.345.678-k", "A", "40", None, DIA),
            ("Agente A", "12345678K", "B", "30", DIA + timedelta(days=1), None),
        ])
        t = turnos([turno(1), turno(2, dia=DIA + timedelta(days=1)), turno(3, "10000000-1")])
        r = self.preparar(a, t)
        self.assertEqual(r["asignaciones"].shape, (2, 9))
        self.assertEqual(r["turnos"].shape, (3, 18))
        self.assertEqual(r["asignaciones"]["rut"].to_list(), [RUT, RUT])
        self.assertEqual(r["turnos"]["campana"].to_list(), ["A", "B", None])
        self.assertEqual(r["asignaciones"].schema["contrato"], pl.Int64)
        self.assertEqual(r["turnos"].schema["contrato"], pl.Int64)
        self.assertEqual(r["turnos"]["contrato"].to_list(), [40, 30, None])
        self.assertEqual(r["turnos"]["estado_asignacion"].to_list(),
                         ["asignacion_vigente", "asignacion_vigente", "sin_asignacion_vigente"])
        self.assertTrue(r["turnos"].select(COLUMNAS_TURNOS_FUENTE).equals(t))
        self.assertGreater(r["turnos"]["fin_turno"][0].date(), DIA)
        self.assertEqual(r["turnos"]["inicio_turno"][0].microsecond, 123456)
        self.assertEqual(r["turnos"].schema["inicio_turno"], pl.Datetime("us"))

    def test_dos_asignaciones_no_multiplican_turno_y_no_eligen_metadatos(self):
        a = asignaciones([("A", RUT, "A", "40", None, None),
                          ("B", RUT, "B", "30", DIA, DIA)])
        r = self.preparar(a, turnos([turno()]))
        self.assertEqual(r["turnos"].height, 1)
        self.assertEqual(r["turnos"]["estado_asignacion"][0], "asignacion_ambigua")
        for c in ("nombre", "campana", "contrato", "campana_id", "cliente", "jefe_negocio"):
            self.assertIsNone(r["turnos"][c][0])
        self.assertEqual(r["turnos"].schema["contrato"], pl.Int64)

    def test_catalogo_ambiguo_no_identificado_y_sin_id(self):
        a = asignaciones([("Agente", RUT, "A", "40", None, None)])
        casos = [
            (catalogo([(1, "A", "C1", "J1"), (2, "A", "C2", "J2")]), "campana_ambigua_en_catalogo"),
            (catalogo([]), "campana_sin_catalogo"),
            (catalogo([(None, "A", "Cliente", "Jefe")]), "campana_sin_id"),
            (catalogo([(1, "a", "Cliente", "Jefe")]), "campana_sin_catalogo"),
        ]
        for c, estado in casos:
            with self.subTest(estado=estado):
                r = self.preparar(a, turnos([turno()]), c)
                self.assertEqual(r["turnos"]["estado_asignacion"][0], estado)
                self.assertEqual(r["turnos"]["nombre"][0], "Agente")
                for campo in ("campana_id", "cliente", "jefe_negocio"):
                    self.assertIsNone(r["asignaciones"][campo][0])
                    self.assertIsNone(r["turnos"][campo][0])

    def test_fin_turno_null_y_pausas_null_se_conservan(self):
        fila = turno()
        fila.update(fin_turno=None, inicio_almuerzo=None, fin_almuerzo=None)
        a = asignaciones([("Agente", RUT, "A", None, None, None)])
        r = self.preparar(a, turnos([fila]))
        self.assertIsNone(r["turnos"]["fin_turno"][0])
        self.assertIsNone(r["turnos"]["contrato"][0])
        self.assertEqual(r["turnos"]["estado_asignacion"][0], "asignacion_vigente")

    def test_duplicados_fuente_y_normalizados_se_rechazan(self):
        row = ("A", RUT, "A", "40", None, None)
        for a in (asignaciones([row, row]), asignaciones([row, ("A", "12.345.678-k", "A", "40", None, None)])):
            with self.assertRaisesRegex(ValueError, "duplicadas"):
                self.preparar(a, turnos([turno()]))
        a = asignaciones([row])
        for t in (turnos([turno(1), turno(1, "10000000-1")]),
                  turnos([turno(1), turno(2, "12345678k")])):
            with self.assertRaisesRegex(ValueError, "duplicados"):
                self.preparar(a, t)

    def test_fecha_pares_y_limites_temporales_invalidos(self):
        a = asignaciones([("A", RUT, "A", "40", None, None)])
        cambios = [
            {"fecha": DIA + timedelta(days=1)},
            {"inicio_turno": None},
            {"fin_turno": datetime(2026, 10, 8, 21)},
            {"inicio_b1": datetime(2026, 10, 8, 23)},
            {"inicio_b1": datetime(2026, 10, 8, 23), "fin_b1": datetime(2026, 10, 8, 22, 59)},
            {"inicio_b1": datetime(2026, 10, 8, 21), "fin_b1": datetime(2026, 10, 8, 21, 10)},
        ]
        for cambio in cambios:
            with self.subTest(cambio=cambio), self.assertRaises(ValueError):
                fila = turno(); fila.update(cambio)
                self.preparar(a, turnos([fila]))
        with self.assertRaisesRegex(ValueError, "vigencias"):
            self.preparar(asignaciones([("A", RUT, "A", "40", DIA + timedelta(days=1), DIA)]), turnos([turno()]))

    def test_rut_texto_ceros_iniciales_y_id_positivo(self):
        a = asignaciones([("A", "00.123.456-k", "A", "40", None, None)])
        t = turnos([turno(rut="00123456K")])
        r = self.preparar(a, t)
        self.assertEqual(r["turnos"]["rut"][0], "00123456-K")
        for fila in (turno(0), turno(rut="INVALIDO")):
            with self.assertRaises(ValueError):
                self.preparar(a, turnos([fila]))

    def test_vacios_tienen_tipos_y_formatos_completos(self):
        r = self.preparar(asignaciones([]), turnos([]), catalogo([]))
        self.assertEqual(r["asignaciones"].shape, (0, 9))
        self.assertEqual(r["turnos"].shape, (0, 18))
        self.assertEqual(r["turnos"].schema["inicio_turno"], pl.Datetime("us"))
        self.assertEqual(r["asignaciones"].schema["contrato"], pl.Int64)
        self.assertEqual(r["turnos"].schema["contrato"], pl.Int64)
        self.assertEqual(formatos_turnos()["inicio_turno"], "dd-mm-yyyy hh:mm:ss")
        self.assertEqual(formatos_asignaciones()["contrato"], "0")
        self.assertEqual(formatos_turnos()["contrato"], "0")
        self.assertEqual(sum(len(g["columnas"]) for g in CONFIG_ASIGNACIONES.values()), 9)
        self.assertEqual(sum(len(g["columnas"]) for g in CONFIG_TURNOS.values()), 18)

    def test_timestamp_con_zona_se_rechaza(self):
        a = asignaciones([("A", RUT, "A", "40", None, None)])
        t = turnos([turno()]).with_columns(pl.col("inicio_turno").dt.replace_time_zone("UTC"))
        with self.assertRaisesRegex(ValueError, "Timestamp"):
            self.preparar(a, t)

    def test_contratos_numericos_conservan_null_filas_y_fuentes_originales(self):
        a = asignaciones([
            ("Agente 1", RUT, "A", " 41 ", None, None),
            ("Agente 2", "10000000-1", "A", "024", None, None),
            ("Agente 3", "20000000-2", "B", None, None, None),
        ])
        t = turnos([turno(1), turno(2, "10000000-1"), turno(3, "20000000-2"),
                    turno(4, "30000000-3")])
        originales_a, originales_t = a.clone(), t.clone()
        r = self.preparar(a, t)
        self.assertEqual(r["asignaciones"]["contrato"].to_list(), [41, 24, None])
        self.assertEqual(r["turnos"]["contrato"].to_list(), [41, 24, None, None])
        self.assertEqual(r["asignaciones"].height, a.height)
        self.assertEqual(r["turnos"].height, t.height)
        self.assertTrue(r["asignaciones"].select([c for c in COLUMNAS_ASIGNACIONES_FUENTE if c != "contrato"]).equals(
            a.select([c for c in COLUMNAS_ASIGNACIONES_FUENTE if c != "contrato"])))
        self.assertTrue(r["turnos"].select(COLUMNAS_TURNOS_FUENTE).equals(t))
        self.assertTrue(a.equals(originales_a))
        self.assertTrue(t.equals(originales_t))
        self.assertEqual(a.schema["contrato"], pl.String)

    def test_contratos_invalidos_no_se_convierten_en_null_ni_se_truncan(self):
        for valor in ("40,5", "40.5", "40 horas", "", "   ", "NaN", "1000000000000000"):
            with self.subTest(valor=valor):
                a = asignaciones([("Agente", RUT, "A", valor, None, None)])
                with self.assertRaises(ValueError):
                    self.preparar(a, turnos([turno()]))

    def test_conversion_no_colapsa_asignaciones_con_textos_equivalentes(self):
        a = asignaciones([("Agente", RUT, "A", "040", None, None),
                          ("Agente", RUT, "A", "40", None, None)])
        r = self.preparar(a, turnos([turno()]))
        self.assertEqual(r["asignaciones"].height, 2)
        self.assertEqual(r["asignaciones"]["contrato"].to_list(), [40, 40])
        self.assertEqual(r["turnos"].height, 1)
        self.assertEqual(r["turnos"]["estado_asignacion"][0], "asignacion_ambigua")
        self.assertIsNone(r["turnos"]["contrato"][0])

    def test_loader_lee_contrato_varchar_y_exporta_int64(self):
        from analytics import asignaciones_turnos as modulo

        conn = object()
        a = asignaciones([("Agente", RUT, "A", "42", None, None)])
        t, c = turnos([turno()]), catalogo()
        vistas = []

        def leer(query, conexion, **kwargs):
            self.assertIs(conexion, conn)
            vistas.append(str(query))
            if "historial_asignaciones" in str(query):
                self.assertEqual(kwargs["schema_overrides"], TIPOS_ASIGNACIONES_FUENTE)
                self.assertEqual(kwargs["schema_overrides"]["contrato"], pl.String)
                return a
            if "fact_turnos" in str(query):
                self.assertEqual(kwargs["execute_options"]["parameters"], {"desde": DIA, "hasta": DIA})
                return t
            return c

        with patch.object(modulo, "_validar_contratos_sql") as validar, \
             patch.object(modulo.pl, "read_database", side_effect=leer):
            r = modulo.cargar_asignaciones_turnos(conn, DIA, DIA)
        validar.assert_called_once_with(conn)
        self.assertEqual(len(vistas), 3)
        for fuente in ("asignaciones", "turnos"):
            self.assertEqual(r[fuente].schema["contrato"], pl.Int64)
            self.assertEqual(r[fuente]["contrato"].to_list(), [42])

    def test_loader_comparte_snapshot_y_no_extiende_las_cuatro_vistas(self):
        from analytics import fuentes_sql as sql
        from analytics.datos_reportes import _tipos

        class Conn:
            def __init__(self): self.statements = []
            def begin(self): return nullcontext()
            def exec_driver_sql(self, statement): self.statements.append(statement)

        class Engine:
            def __init__(self): self.conn = Conn()
            def connect(self): return nullcontext(self.conn)

        db = Engine()
        frames = {key: pl.DataFrame(schema=_tipos(cfg)) for key, cfg in sql._CONFIGURACIONES.items()}
        frames["estados_agentes"] = frames["estados_agentes"].select(sql._COLUMNAS_ESTADOS)
        consultas = []

        def leer(query, conn, **kwargs):
            self.assertIs(conn, db.conn)
            parametros = kwargs['execute_options']['parameters']
            self.assertEqual(parametros['hasta'], date(2026, 10, 10))
            consultas.append(str(query))
            if "datos_colas_diario" in str(query): key = "colas_diario"
            elif "datos_ejecutivos" in str(query): key = "ejecutivos"
            elif "datos_estados_agentes" in str(query): key = "estados_agentes"
            else: key = "colas"
            return frames[key]

        nuevo = self.preparar(asignaciones([]), turnos([]))
        with patch.object(sql, '_validar_esquemas', return_value={k: f.columns for k, f in frames.items()}), \
             patch.object(sql.pl, 'read_database', side_effect=leer), \
             patch.object(sql, 'cargar_asignaciones_turnos', return_value=nuevo) as adicionales, \
             patch.object(sql, 'cargar_licencias_vacaciones', return_value=pl.DataFrame()) as licencias:
            r = sql.cargar_fuentes_sql(date(2026, 10, 1), date(2026, 10, 10), date(2026, 10, 8),
                                      engine=db, hasta_turnos=date(2026, 10, 31))
        self.assertEqual(len(consultas), 4)
        adicionales.assert_called_once_with(db.conn, date(2026, 10, 1), date(2026, 10, 31))
        licencias.assert_called_once_with(db.conn)
        self.assertEqual(set(r), {'colas_diario', 'colas', 'ejecutivos', 'estados_agentes', 'asignaciones', 'turnos', 'licencias_vacaciones'})
        self.assertEqual(db.conn.statements[0], 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')


if __name__ == "__main__":
    unittest.main()
