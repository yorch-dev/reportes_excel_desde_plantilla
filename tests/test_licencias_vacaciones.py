"""LicenciasVacaciones conserva el historial, IDs, NULL y múltiples tramos."""
import sys
import unittest
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from unittest.mock import patch

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analytics.licencias_vacaciones import (
    COLUMNAS_FUENTE, FECHAS, CONFIG_LICENCIAS_VACACIONES, QUERY_LICENCIAS_VACACIONES,
    IDS_NUMERICOS, TIPOS_FUENTE_SQL,
    cargar_licencias_vacaciones, preparar_licencias_vacaciones, formatos_licencias_vacaciones,
)
from analytics.datos_reportes import _tipos


SCHEMA = TIPOS_FUENTE_SQL
CATALOGO_SCHEMA = {"campana_id": pl.Int64, "campana": pl.String,
                   "cliente": pl.String, "jefe_negocio": pl.String}


def fila(**cambios):
    valores = dict(campana="A", person_id_pw="000001", rut="00.123.456-k",
        nombre_ejecutivo="Fixture", id_ausencia_buk="000002", id_empleado_buk="000003",
        tipo="licencia", estado_fuente="approved", tramo_jornada="40",
        fecha_inicio_en_unidad=date(2026, 10, 8), fecha_termino_en_unidad=date(2026, 10, 10),
        dias_calendario_tramo_unidad=3, fecha_inicio_ausencia=date(2026, 10, 1),
        fecha_termino_ausencia=date(2026, 10, 20), dias_calendario_total_ausencia_buk=20.0)
    valores.update(cambios)
    return valores


def fuente(filas):
    return pl.DataFrame(filas, schema=SCHEMA)


def catalogo(filas=None):
    return pl.DataFrame(filas if filas is not None else [(1, "A", "Cliente A", "Jefe A"),
        (2, "B", "Cliente B", "Jefe B")], schema=CATALOGO_SCHEMA, orient="row")


class LicenciasVacacionesTests(unittest.TestCase):
    def preparar(self, df, cat=None):
        return preparar_licencias_vacaciones(df, catalogo() if cat is None else cat)

    def test_ids_enteros_y_rut_conserva_ceros_y_guion(self):
        original = fuente([fila()])
        r = self.preparar(original)
        self.assertEqual(r.shape, (1, 18))
        self.assertEqual(r['rut'][0], '00123456-K')
        for c, esperado in zip(IDS_NUMERICOS, (1, 2, 3)):
            self.assertEqual(r[c][0], esperado)
            self.assertEqual(r.schema[c], pl.Int64)
        self.assertEqual(r['campana_id'][0], 1)
        otras = [c for c in COLUMNAS_FUENTE if c not in (*IDS_NUMERICOS, 'rut')]
        self.assertTrue(r.select(otras).equals(original.select(otras)))

    def test_mismo_id_en_dos_tramos_y_campanas_no_se_deduplica(self):
        original = fuente([fila(), fila(campana='B', fecha_inicio_en_unidad=date(2026, 10, 11),
            fecha_termino_en_unidad=date(2026, 10, 12), dias_calendario_tramo_unidad=2)])
        r = self.preparar(original)
        self.assertEqual(r.height, 2)
        self.assertEqual(r['id_ausencia_buk'].n_unique(), 1)
        self.assertEqual(r['id_ausencia_buk'].to_list(), [2, 2])
        self.assertEqual(r['campana_id'].to_list(), [1, 2])
        self.assertEqual(r['dias_calendario_total_ausencia_buk'].to_list(), [20, 20])

    def test_catalogo_ambiguo_sin_id_y_sin_match_conserva_fuente(self):
        casos = [catalogo([(1, 'A', 'C1', 'J1'), (2, 'A', 'C2', 'J2')]),
                 catalogo([]), catalogo([(None, 'A', 'Cliente', 'Jefe')]),
                 catalogo([(1, 'a', 'Cliente', 'Jefe')])]
        for cat in casos:
            with self.subTest(catalogo=cat.to_dicts()):
                r = self.preparar(fuente([fila()]), cat)
                self.assertEqual(r.height, 1)
                self.assertEqual(r['campana'][0], 'A')
                for c in ('campana_id', 'cliente', 'jefe_negocio'):
                    self.assertIsNone(r[c][0])

    def test_nulls_no_se_rellenan_ni_filtran(self):
        cambios = {c: None for c in FECHAS}
        cambios.update(campana=None, rut=None, person_id_pw=None, id_ausencia_buk=None,
                       id_empleado_buk=None, dias_calendario_tramo_unidad=None,
                       dias_calendario_total_ausencia_buk=None, estado_fuente=None)
        original = fuente([fila(**cambios)])
        r = self.preparar(original)
        self.assertEqual(r.height, 1)
        otras = [c for c in COLUMNAS_FUENTE if c not in IDS_NUMERICOS]
        self.assertTrue(r.select(otras).equals(original.select(otras)))
        for c in IDS_NUMERICOS:
            self.assertIsNone(r[c][0])
            self.assertEqual(r.schema[c], pl.Int64)
        self.assertIsNone(r['campana_id'][0])

    def test_historia_pasada_y_futura_estados_y_totales_fraccionales(self):
        rows = []
        for inicio, estado in ((date(2024, 4, 1), 'pending'), (date(2027, 1, 9), 'approved')):
            rows.append(fila(fecha_inicio_en_unidad=inicio, fecha_termino_en_unidad=inicio,
                fecha_inicio_ausencia=inicio, fecha_termino_ausencia=inicio,
                dias_calendario_tramo_unidad=1, dias_calendario_total_ausencia_buk=0.5,
                estado_fuente=estado))
        r = self.preparar(fuente(rows))
        self.assertEqual(r.height, 2)
        self.assertEqual(r['estado_fuente'].to_list(), ['pending', 'approved'])
        self.assertEqual(r['dias_calendario_total_ausencia_buk'].to_list(), [0.5, 0.5])
        self.assertNotIn('WHERE', QUERY_LICENCIAS_VACACIONES.upper())

    def test_validaciones_fechas_tramos_dias_y_numericos(self):
        cambios = [
            {'fecha_inicio_en_unidad': date(2026, 10, 11)},
            {'fecha_inicio_ausencia': date(2026, 10, 21)},
            {'fecha_inicio_en_unidad': date(2026, 9, 30)},
            {'fecha_termino_en_unidad': date(2026, 10, 21)},
            {'dias_calendario_tramo_unidad': 4},
            {'dias_calendario_tramo_unidad': -1},
            {'dias_calendario_total_ausencia_buk': -0.5},
            {'dias_calendario_total_ausencia_buk': float('inf')},
            {'dias_calendario_total_ausencia_buk': float('nan')},
            {'rut': 'INVALIDO'},
        ]
        for cambio in cambios:
            with self.subTest(cambio=cambio), self.assertRaises(ValueError):
                self.preparar(fuente([fila(**cambio)]))

    def test_dias_tramo_null_no_se_infiere_y_duplicado15_se_rechaza(self):
        r = self.preparar(fuente([fila(dias_calendario_tramo_unidad=None)]))
        self.assertIsNone(r['dias_calendario_tramo_unidad'][0])
        with self.assertRaisesRegex(ValueError, 'duplicadas'):
            self.preparar(fuente([fila(), fila()]))

    def test_vacio_configuracion_y_formatos(self):
        r = self.preparar(fuente([]), catalogo([]))
        self.assertEqual(r.shape, (0, 18))
        self.assertEqual(r.schema['rut'], pl.String)
        self.assertEqual(r.schema['fecha_inicio_en_unidad'], pl.Date)
        self.assertEqual(r.schema['dias_calendario_total_ausencia_buk'], pl.Float64)
        self.assertEqual(set(formatos_licencias_vacaciones()), set(r.columns))
        self.assertEqual(formatos_licencias_vacaciones()['person_id_pw'], '0')
        for c in IDS_NUMERICOS:
            self.assertEqual(r.schema[c], pl.Int64)

    def test_cargador_lee_vista_completa_y_catalogo_con_misma_conn(self):
        class Rows:
            def mappings(self):
                for c in COLUMNAS_FUENTE:
                    tipo = ('character varying' if c in COLUMNAS_FUENTE[:9] else 'date' if c in FECHAS
                            else 'integer' if c == 'dias_calendario_tramo_unidad' else 'numeric')
                    yield {'column_name': c, 'data_type': tipo}

        class Conn:
            def execute(self, query):
                self.query = str(query)
                return Rows()

        conn = Conn()
        queries = []
        def leer(query, recibido, **kwargs):
            self.assertIs(recibido, conn)
            queries.append(str(query))
            if 'reporting.licencias_vacaciones' in str(query):
                for c in IDS_NUMERICOS:
                    self.assertEqual(kwargs['schema_overrides'][c], pl.String)
            return fuente([fila()]) if 'reporting.licencias_vacaciones' in str(query) else catalogo()
        with patch('analytics.licencias_vacaciones.pl.read_database', side_effect=leer):
            r = cargar_licencias_vacaciones(conn)
        self.assertEqual(r.shape, (1, 18))
        self.assertEqual(len(queries), 2)
        self.assertNotIn('WHERE', queries[0].upper())
        self.assertIn('LEFT JOIN analytics_base.dim_cliente', queries[1])

    def test_ids_enteros_ya_tipados_se_aceptan(self):
        original = fuente([fila()]).with_columns(pl.col(c).cast(pl.Int64) for c in IDS_NUMERICOS)
        r = self.preparar(original)
        for c in IDS_NUMERICOS:
            self.assertEqual(r.schema[c], pl.Int64)
            self.assertEqual(r[c][0], original[c][0])

    def test_ids_invalidos_no_se_ocultan_ni_truncan(self):
        for c in IDS_NUMERICOS:
            for invalido in ('ABC', '1.5', '1e3', '', '1234567890123456'):
                with self.subTest(columna=c, invalido=invalido), self.assertRaises(ValueError):
                    self.preparar(fuente([fila(**{c: invalido})]))
            with self.subTest(columna=c, tipo='Float64'), self.assertRaises(ValueError):
                self.preparar(fuente([fila()]).with_columns(pl.lit(1.5).alias(c)))

    def test_fuentes_sql_usa_la_misma_conn_y_no_pasa_filtro_de_fechas(self):
        from analytics import fuentes_sql as sql
        class Conn:
            def begin(self): return nullcontext()
            def exec_driver_sql(self, query): pass
        class Engine:
            conn = Conn()
            def connect(self): return nullcontext(self.conn)
        db = Engine()
        frames = {k: pl.DataFrame(schema=_tipos(cfg)) for k, cfg in sql._CONFIGURACIONES.items()}
        frames['estados_agentes'] = frames['estados_agentes'].select(sql._COLUMNAS_ESTADOS)
        def leer(query, conn, **kwargs):
            self.assertIs(conn, db.conn)
            if 'datos_colas_diario' in str(query): key = 'colas_diario'
            elif 'datos_ejecutivos' in str(query): key = 'ejecutivos'
            elif 'datos_estados_agentes' in str(query): key = 'estados_agentes'
            else: key = 'colas'
            return frames[key]
        licencia = self.preparar(fuente([]))
        with patch.object(sql, '_validar_esquemas', return_value={k: d.columns for k, d in frames.items()}), \
             patch.object(sql.pl, 'read_database', side_effect=leer), \
             patch.object(sql, 'cargar_asignaciones_turnos', return_value={'asignaciones': pl.DataFrame(), 'turnos': pl.DataFrame()}), \
             patch.object(sql, 'cargar_licencias_vacaciones', return_value=licencia) as cargar:
            r = sql.cargar_fuentes_sql(date(2026, 10, 8), date(2026, 10, 10), date(2026, 10, 8), engine=db)
        cargar.assert_called_once_with(db.conn)
        self.assertIs(r['licencias_vacaciones'], licencia)
        self.assertEqual(len(r), 7)


if __name__ == '__main__':
    unittest.main()
