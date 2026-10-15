"""RefreshAll completo y fallos observables, con COM simulado y sin Excel."""
from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import patch

from src.analytics import actualizacion_excel as modulo


class _Coleccion:
    def __init__(self, valores=()):
        self.valores = list(valores)
        self.Count = len(self.valores)

    def Item(self, indice):
        return self.valores[indice - 1]


class _Proveedor:
    def __init__(self, libro, nombre, background=True):
        self.libro, self.Name = libro, nombre
        self.background = background
        self.cambios = []
        self.error_restaurar = None

    @property
    def BackgroundQuery(self):
        return self.background

    @BackgroundQuery.setter
    def BackgroundQuery(self, valor):
        if valor and self.error_restaurar is not None:
            raise self.error_restaurar
        self.cambios.append(valor)
        self.background = valor

    @property
    def Refreshing(self):
        return self.libro.pendiente


class _App:
    def __init__(self, libro):
        self.libro, self.estado = libro, 2
        self.EnableEvents = True

    @property
    def CalculationState(self):
        return self.estado

    def Calculate(self):
        self.libro.eventos.append("calcular")
        self.estado = 0

    def CalculateFull(self):
        self.libro.eventos.append("calcular_final")
        self.estado = 0

    def CalculateUntilAsyncQueriesDone(self):
        self.libro.eventos.append("barrera_async")


class _Pivot:
    Name = "Resumen"

    def __init__(self, libro):
        self.libro = libro
        self.resultado = True
        self.slicer = ["A", "B"]

    def RefreshTable(self):
        if self.libro.pendiente:
            raise AssertionError("El pivot recibió conexiones incompletas.")
        self.libro.eventos.append("pivot")
        return self.resultado


class _Libro:
    def __init__(self, *, query=False, pivot=True, conexiones=True, activo=True, modelo=False):
        self.eventos, self.pendiente, self.ticks = [], False, 0
        self.resultado_query = True
        self.confirmar_query = True
        self.observadores = []
        self.error_refresh = None
        self.al_refrescar = None
        self.proveedor = _Proveedor(self, "PQ")
        self.conexion = SimpleNamespace(Name="PQ", Type=1, RefreshWithRefreshAll=activo,
                                        OLEDBConnection=self.proveedor)
        self.query = _Proveedor(self, "Consulta")
        self.query.WorkbookConnection = self.conexion
        lista_conexiones = [self.conexion] if conexiones else []
        if modelo:
            lista_conexiones.append(SimpleNamespace(Name="ThisWorkbookDataModel", Type=7,
                                                    RefreshWithRefreshAll=True))
        self.pivot = _Pivot(self)
        # La query aparece en ambas colecciones: se debe observar una sola vez.
        hoja = SimpleNamespace(Name="Trabajo", QueryTables=_Coleccion([self.query] if query else []),
            ListObjects=_Coleccion([SimpleNamespace(SourceType=3, QueryTable=self.query)] if query else []),
            PivotTables=lambda: _Coleccion([self.pivot] if pivot else []))
        self.app_api = _App(self)
        self.api = SimpleNamespace(Worksheets=_Coleccion([hoja]),
            Connections=_Coleccion(lista_conexiones), RefreshAll=self.RefreshAll)
        self.app = SimpleNamespace(api=self.app_api)

    def RefreshAll(self):
        self.eventos.append("refresh_all")
        if self.al_refrescar is not None:
            self.al_refrescar()
        if self.error_refresh is not None:
            raise self.error_refresh
        self.pendiente = True
        self.ticks = 3

    def bombear(self):
        if self.pendiente:
            self.ticks -= 1
            if self.ticks == 0:
                self.pendiente = False
                self.eventos.append("consultas_completas")
                if self.confirmar_query:
                    for resultado in self.observadores:
                        resultado.exito = self.resultado_query


class ActualizacionExcelTests(unittest.TestCase):
    def refrescar(self, libro, **kwargs):
        def observar(query, resultado):
            libro.observadores.append(resultado)
            return SimpleNamespace(close=lambda: libro.eventos.append("cerrar_observador"))

        with patch.object(modulo, "_bombear_mensajes", side_effect=libro.bombear), \
             patch.object(modulo, "_observar_consulta", side_effect=observar), \
             patch.object(modulo, "sleep"):
            return modulo.refrescar_libro(libro, **kwargs)

    def test_modelo_consultas_y_pivots_completan_en_orden_sin_reextraccion(self):
        libro = _Libro(query=True, modelo=True)
        self.refrescar(libro)
        self.assertEqual(libro.eventos.count("refresh_all"), 1)
        self.assertLess(libro.eventos.index("calcular"), libro.eventos.index("refresh_all"))
        self.assertLess(libro.eventos.index("consultas_completas"), libro.eventos.index("pivot"))
        self.assertLess(libro.eventos.index("pivot"), libro.eventos.index("calcular_final"))
        self.assertEqual(len(libro.observadores), 1)
        self.assertFalse(libro.pendiente)
        self.assertEqual(libro.app_api.CalculationState, 0)
        self.assertEqual(libro.proveedor.cambios, [False, True])
        self.assertEqual(libro.query.cambios, [False, True])
        self.assertEqual(libro.pivot.slicer, ["A", "B"])

    def test_conexion_excluida_no_cambia_background_ni_se_observa(self):
        libro = _Libro(query=True, activo=False)
        self.refrescar(libro)
        self.assertEqual(libro.proveedor.cambios, [])
        self.assertEqual(libro.query.cambios, [])
        self.assertEqual(libro.observadores, [])
        self.assertFalse(libro.conexion.RefreshWithRefreshAll)

    def test_sin_consultas_ni_pivots_solo_calcula_sin_importar_com(self):
        libro = _Libro(query=False, pivot=False, conexiones=False)
        self.refrescar(libro, progreso=None)
        self.assertEqual(libro.eventos, ["calcular_final"])
        self.assertEqual(libro.app_api.CalculationState, 0)

    def test_error_com_original_se_propaga_y_restauran_opciones(self):
        libro = _Libro(query=True)
        error = RuntimeError("Error COM al consultar")
        libro.error_refresh = error
        with self.assertRaises(RuntimeError) as capturado:
            self.refrescar(libro)
        self.assertIs(capturado.exception, error)
        self.assertTrue(libro.proveedor.BackgroundQuery)
        self.assertTrue(libro.query.BackgroundQuery)
        self.assertNotIn("pivot", libro.eventos)
        self.assertIn("cerrar_observador", libro.eventos)

    def test_afterrefresh_false_impide_calcular_y_publicar_exito(self):
        libro = _Libro(query=True)
        libro.resultado_query = False
        with self.assertRaisesRegex(RuntimeError, "consulta .*no se actualizó"):
            self.refrescar(libro)
        self.assertNotIn("pivot", libro.eventos)
        self.assertNotIn("calcular_final", libro.eventos)
        self.assertTrue(libro.proveedor.BackgroundQuery)
        self.assertTrue(libro.query.BackgroundQuery)

    def test_refresh_table_false_falla_sin_alterar_slicer(self):
        libro = _Libro()
        libro.pivot.resultado = False
        with self.assertRaisesRegex(RuntimeError, "tabla dinámica"):
            self.refrescar(libro)
        self.assertEqual(libro.pivot.slicer, ["A", "B"])
        self.assertTrue(libro.proveedor.BackgroundQuery)
        self.assertNotIn("calcular_final", libro.eventos)

    def test_evento_anterior_no_confirma_un_refreshall_sin_evento_nuevo(self):
        libro = _Libro(query=True)
        libro.pendiente, libro.ticks = True, 1  # Actualización iniciada al abrir.
        libro.al_refrescar = lambda: setattr(libro, "confirmar_query", False)
        with self.assertRaisesRegex(RuntimeError, "no confirmó"):
            self.refrescar(libro)
        self.assertNotIn("pivot", libro.eventos)
        self.assertTrue(libro.proveedor.BackgroundQuery)

    def test_xlpending_en_manual_se_calcula_y_no_espera_para_siempre(self):
        libro = _Libro()
        original_full = libro.app_api.CalculateFull

        def pendiente():
            original_full()
            libro.app_api.estado = 2

        libro.app_api.CalculateFull = pendiente
        self.refrescar(libro)
        self.assertEqual(libro.app_api.CalculationState, 0)
        self.assertEqual(libro.eventos[-1], "calcular")

    def test_fallo_al_restaurar_no_oculta_el_error_original(self):
        libro = _Libro(query=True)
        error = RuntimeError("Error de consulta")
        restauracion = RuntimeError("Error al restaurar")
        libro.error_refresh = error
        libro.proveedor.error_restaurar = restauracion
        with self.assertRaises(ExceptionGroup) as capturado:
            self.refrescar(libro)
        self.assertIn(error, capturado.exception.exceptions)
        self.assertIn(restauracion, capturado.exception.exceptions)
        self.assertTrue(libro.query.BackgroundQuery)

    def test_heartbeat_informa_incluso_si_refreshall_bloquea(self):
        libro = _Libro()
        aviso = Event()
        mensajes = []

        def progreso(mensaje):
            mensajes.append(mensaje)
            if mensaje.startswith("Continúa: actualizando conexiones"):
                aviso.set()

        def bloqueo_com():
            self.assertTrue(aviso.wait(2), "No hubo progreso durante la llamada bloqueante.")

        libro.al_refrescar = bloqueo_com
        with patch.object(modulo, "_INTERVALO_AVISO", 0.01):
            self.refrescar(libro, progreso=progreso)
        self.assertTrue(any("Continúa:" in m for m in mensajes))
        self.assertFalse(libro.pendiente)


if __name__ == "__main__":
    unittest.main()
