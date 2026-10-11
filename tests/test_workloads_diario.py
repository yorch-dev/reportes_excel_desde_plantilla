"""Contratos diarios nativos; no requieren red, credenciales ni Excel."""
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from analytics.workloads_diario import (
    agregar_requerido_dia, agregar_volumen_tmo_dia, limites_dia,
)
from analytics.datos_reportes import cargar_workloads_reportes, cargar_workloads


class WorkloadsDiarioTests(unittest.TestCase):
    fecha = date(2026, 10, 8)
    zona = 'America/Santiago'

    def serie(self, raster, valor, fecha=None):
        ini, fin = limites_dia(fecha or self.fecha, self.zona)
        return {ts: valor for ts in range(ini, fin, raster)}

    def test_null_no_es_cero_y_peso_cero_no_crea_tmo(self):
        total, tmo, cv, ct = agregar_volumen_tmo_dia({}, {}, 900, self.fecha, self.zona)
        self.assertIsNone(total)
        self.assertIsNone(tmo)
        self.assertEqual(cv['estado'], 'sin_datos')
        q = self.serie(900, 0)
        total, tmo, cv, ct = agregar_volumen_tmo_dia(q, {}, 900, self.fecha, self.zona)
        self.assertEqual(total, 0)
        self.assertIsNone(tmo)
        self.assertEqual(ct['estado'], 'peso_cero')

    def test_tmo_ponderado_por_pesos_propios(self):
        q = self.serie(900, 0)
        ts = list(q)
        q[ts[0]], q[ts[1]] = 1, 3
        a = {ts[0]: 100, ts[1]: 300}
        total, tmo, _, ct = agregar_volumen_tmo_dia(q, a, 900, self.fecha, self.zona)
        self.assertEqual(total, 4)
        self.assertEqual(tmo, 250)
        self.assertEqual(ct['estado'], 'completo')
        del a[ts[1]]
        self.assertIsNone(agregar_volumen_tmo_dia(q, a, 900, self.fecha, self.zona)[1])

    def test_quince_minutos_parciales_conservan_volumen_diario(self):
        q = self.serie(900, 1)
        a = self.serie(900, 200)
        del q[next(iter(q))]
        total, tmo, cv, ct = agregar_volumen_tmo_dia(q, a, 900, self.fecha, self.zona)
        self.assertEqual(total, 95)
        self.assertEqual(tmo, 200)
        self.assertEqual(cv['estado'], 'parcial')
        self.assertEqual(ct['estado'], 'ponderado_parcial')

    def test_positivo_null_y_cero_ponderan_solo_lo_observado(self):
        ini, _ = limites_dia(self.fecha, self.zona)
        q = {ini: 2, ini + 900: None, ini + 1800: 0}
        a = {ini: 123}
        total, tmo, cv, ct = agregar_volumen_tmo_dia(q, a, 900, self.fecha, self.zona)
        self.assertEqual(total, 2)
        self.assertEqual(tmo, 123)
        self.assertEqual(cv['estado'], 'parcial')
        self.assertEqual(ct['estado'], 'ponderado_parcial')

    def test_rasters_tmo_distintos_no_se_interpolan(self):
        q = self.serie(900, 1)
        a = self.serie(1800, 200)
        total, tmo, _, ct = agregar_volumen_tmo_dia(q, a, 900, self.fecha, self.zona, 1800)
        self.assertEqual(total, 96)
        self.assertIsNone(tmo)
        self.assertEqual(ct['estado'], 'raster_tmo_distinto_peso')

    def test_horario_y_diario_son_validos_sin_desagregar_intervalos(self):
        for raster, volumen in ((3600, 24), (86400, 1)):
            q = self.serie(raster, 1)
            a = self.serie(raster, 120)
            total, tmo, _, ct = agregar_volumen_tmo_dia(q, a, raster, self.fecha, self.zona)
            self.assertEqual(total, volumen)
            self.assertEqual(tmo, 120)

    def test_duracion_real_dst_en_volumen_y_requerido(self):
        for fecha, horas in ((date(2026, 9, 6), 23), (date(2026, 4, 4), 25)):
            q = self.serie(900, 1, fecha)
            a = self.serie(900, 100, fecha)
            total, tmo, cv, _ = agregar_volumen_tmo_dia(q, a, 900, fecha, self.zona)
            self.assertEqual(total, horas * 4)
            self.assertEqual(tmo, 100)
            req, cov = agregar_requerido_dia(self.serie(900, 2, fecha), 900, fecha, self.zona)
            self.assertEqual(req, horas * 2)
            self.assertEqual(cov['horas_dia'], horas)

    def test_p1d_dst_o_utc_no_alineado_es_explicito(self):
        fecha = date(2026, 9, 6)
        q = {limites_dia(fecha, self.zona)[0]: 1}
        total, tmo, cv, _ = agregar_volumen_tmo_dia(q, q, 86400, fecha, self.zona)
        self.assertIsNone(total)
        self.assertEqual(cv['estado'], 'raster_no_representa_dia_local')
        ts = int(datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp())
        total, _, cv, _ = agregar_volumen_tmo_dia({ts: 10}, {}, 86400, self.fecha, self.zona)
        self.assertIsNone(total)
        self.assertEqual(cv['estado'], 'serie_diaria_no_alineada_al_dia_local')


class ExtraccionBloquesTests(unittest.TestCase):
    def test_catalogos_unicos_bloques_y_variante_tardia(self):
        wid = '00000000-0000-0000-0000-000000000001'
        requests_seen = []

        class Response:
            status_code = 200
            def __init__(self, payload): self.payload = payload
            def json(self): return self.payload

        class Session:
            def __init__(self): self.headers = {}
            def mount(self, *args): pass
            def close(self): pass
            def get(self, url, params=None, **kwargs):
                path = urlsplit(url).path
                requests_seen.append((path, dict(params or {})))
                if path == '/workloads':
                    return Response({'data': [{'id': wid, 'name': 'Fixture', 'planningUnitId': 1,
                        'activityId': 10, 'timezone': 'America/Santiago', 'channel': 'phone',
                        'intervalLength': 900}], 'total': 1, 'next': None})
                catalogs = {'planning_units': [{'planning_unit_id': 1, 'name': 'Unit', 'time_zone_id': 17}],
                    'activities': [{'activity_id': 10, 'name': 'Activity', 'child_activity_ids': []}],
                    'curves': [{'curve_id': 20, 'name': '*Fixture', 'name_short': wid.rsplit('-', 1)[-1],
                        'description': 'automatic history export', 'time_zone_id': 17}],
                    'curve_value_types': [{'curve_value_type_id': i} for i in (125, 104, 126)],
                    'curve_versions': [{'curve_version_id': 101}]}
                if path.removeprefix('/v1/') in catalogs:
                    name = path.removeprefix('/v1/')
                    return Response({name: catalogs[name]})
                if path == '/v1/time_zones/17':
                    return Response({'time_zone': {'time_zone_id': 17, 'description': 'Santiago',
                        'std_utc_offset': -14400, 'dst_utc_offset': -10800}})
                if '/activities/' in path:
                    return Response({'planning_unit_activities': [{'planning_unit_id': 1, 'activity_id': 10,
                        'start_date': '2026-01-01', 'end_date': '2026-12-31'}]})
                if path.endswith('/forecasts'):
                    ini = datetime.fromisoformat(params['startTime'])
                    end = datetime.fromisoformat(params['endTime'])
                    n = int((end - ini).total_seconds() / 900)
                    series = {'auto': {'offered': {'startTime': params['startTime'], 'values': [1] * n},
                                      'averageHandlingTime': {'startTime': params['startTime'], 'values': [200] * n}}}
                    if ini.date() >= date(2026, 10, 15):
                        series['customLater'] = {'offered': {'startTime': params['startTime'], 'values': [2] * n}}
                    return Response({'data': {'workloadId': wid, 'intervalDuration': 'PT15M', 'forecasts': series}})
                ini = date.fromisoformat(path.rsplit('/', 1)[-1])
                end = date.fromisoformat(params['end_date'])
                days = [date.fromordinal(d) for d in range(ini.toordinal(), end.toordinal() + 1)]
                if '/values/' in path:
                    return Response({'curve_values': [{'curve_id': 20, 'curve_value_type_id': metric,
                        'curve_version_id': 101, 'date': str(d), 'raster': 900, 'values': [value] * 96}
                        for d in days for metric, value in ((125, 1), (104, 2), (126, 100))]})
                if '/requirements/' in path:
                    return Response({'requirements': [{'planning_unit_id': 1, 'activity_id': 10,
                        'date': str(d), 'raster': 900, 'values': [3] * 96} for d in days]})
                raise AssertionError(path)

        with TemporaryDirectory() as folder:
            Path(folder, '.env').write_text('TOKEN_PW=fixture\n', encoding='utf-8')
            with patch('analytics.datos_reportes.requests.Session', Session):
                r = cargar_workloads_reportes(date(2026, 10, 1), date(2026, 10, 17), date(2026, 10, 15), folder)
            self.assertEqual(r.df.height, 3 * 48)
            self.assertEqual(r.df_diario.height, 17)
            self.assertEqual(r.df_diario['requerido_horas'].to_list(), [72] * 17)
            self.assertEqual(r.df_diario['tmo_historico'].to_list(), [100] * 17)
            self.assertEqual(r.df_diario['tmo_auto'].to_list(), [200] * 17)
            self.assertEqual(r.df_diario['forecast_custom_later'].null_count(), 14)
            self.assertEqual(sum(p == '/workloads' for p, _ in requests_seen), 1)
            self.assertEqual(sum(p == '/v1/planning_units' for p, _ in requests_seen), 1)
            forecasts = [params for p, params in requests_seen if p.endswith('/forecasts')]
            self.assertEqual(len(forecasts), 2)
            self.assertTrue(all((datetime.fromisoformat(x['endTime']) - datetime.fromisoformat(x['startTime'])).days <= 14 for x in forecasts))
            requests_seen.clear()
            with patch('analytics.datos_reportes.requests.Session', Session):
                old = cargar_workloads(date(2026, 10, 1), date(2026, 10, 2), folder)
            self.assertEqual(old.df.height, 96)
            self.assertEqual(len(old.df.columns), 28)
            self.assertIsNone(old.df_diario)


if __name__ == '__main__':
    unittest.main()
