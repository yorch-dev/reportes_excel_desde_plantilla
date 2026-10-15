"""Actualización completa antes de guardar, sin cambiar filtros ni conexiones.

RefreshAll respeta WorkbookConnection.RefreshWithRefreshAll. BackgroundQuery
se desactiva temporalmente sólo para las consultas incluidas. No se invoca
Model.Refresh adicional, que volvería a extraer todas sus fuentes.

Referencias Microsoft: Workbook.RefreshAll, WorkbookConnection.RefreshWithRefreshAll,
Application.CalculateUntilAsyncQueriesDone, QueryTable.AfterRefresh y
PivotTable.RefreshTable (learn.microsoft.com/en-us/office/vba/api/excel.*).
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic, sleep
from typing import Callable


_PAUSA = 0.2
_INTERVALO_AVISO = 30.0


def _coleccion(coleccion):
    return [coleccion.Item(i) for i in range(1, coleccion.Count + 1)]


def _bombear_mensajes() -> None:
    # Importación diferida: importar este módulo no requiere Windows ni COM.
    import pythoncom
    pythoncom.PumpWaitingMessages()


class _Avisos:
    """El heartbeat no llama COM; sólo publica el progreso de una fase lenta."""
    def __init__(self, progreso):
        self.progreso = progreso
        self.fase = "Preparando actualización de Excel"
        self.inicio = monotonic()
        self.detener = Event()
        self.hilo = None
        self.error = None

    def __enter__(self):
        if self.progreso is not None:
            self.hilo = Thread(target=self._informar, daemon=True)
            self.hilo.start()
        return self

    def etapa(self, mensaje):
        self.fase = mensaje
        self.inicio = monotonic()
        if self.progreso is not None:
            self.progreso(mensaje + "...")

    def _informar(self):
        while not self.detener.wait(_INTERVALO_AVISO):
            try:
                segundos = int(monotonic() - self.inicio)
                self.progreso(f"Continúa: {self.fase.lower()} ({segundos} s).")
            except BaseException as error:
                self.error = error
                return

    def __exit__(self, tipo, error, tb):
        self.detener.set()
        if self.hilo is not None:
            self.hilo.join()
        if self.error is not None:
            if error is not None:
                raise BaseExceptionGroup("Fallaron la actualización y el aviso de progreso", [error, self.error])
            raise self.error


@dataclass
class _ResultadoConsulta:
    nombre: str
    exito: bool | None = None


def _observar_consulta(query, resultado):
    from win32com.client import WithEvents

    class Eventos:
        def OnAfterRefresh(self, Success):
            # False no se transforma en una excepción dentro del callback COM.
            # Se comprueba en el hilo principal antes de refrescar los pivots.
            if not Success or self._resultado.exito is None:
                self._resultado.exito = bool(Success)

    objeto = getattr(query, "_inner", query)
    observador = WithEvents(objeto, Eventos)
    observador.__dict__["_resultado"] = resultado
    return observador


def _consultas_hoja(hoja):
    consultas = {}
    for query in _coleccion(hoja.QueryTables):
        consultas[query.Name.casefold()] = query
    # QueryTables de ListObjects deben descubrirse también por esta propiedad.
    for tabla in _coleccion(hoja.ListObjects):
        if tabla.SourceType in (0, 3, 4):  # external, query, model; no xlSrcRange.
            query = tabla.QueryTable
            if query is not None:
                consultas.setdefault(query.Name.casefold(), query)
    return list(consultas.values())


def _esperar(app, monitores, *, consultas: bool, calculo: bool) -> None:
    """Esperar trabajo real sin fecha límite y sin confundir xlPending con busy."""
    if consultas:
        app.CalculateUntilAsyncQueriesDone()
    while True:
        _bombear_mensajes()
        actualizando = any(bool(objeto.Refreshing) for objeto in monitores)
        estado = app.CalculationState
        if not actualizando and calculo and estado == 2:  # xlPending, también en manual.
            app.Calculate()
            continue
        ocupado = estado != 0 if calculo else estado == 1  # xlCalculating.
        if not actualizando and not ocupado:
            return
        sleep(_PAUSA)


def refrescar_libro(libro, *, progreso: Callable[[str], None] | None = None) -> None:
    """Calcular fuentes, actualizar consultas/modelo, esperar y actualizar análisis.

    No hay timeout: las actualizaciones lentas conservan su ejecución. Los errores
    COM, AfterRefresh=False y RefreshTable=False impiden devolver éxito. RefreshAll
    no ofrece un resultado global para consultas cargadas sólo al modelo; sus
    errores COM se propagan y se esperan las barreras OLEDB/OLAP y sus estados.
    ``progreso`` puede ser llamado desde un hilo auxiliar mientras Excel bloquea.
    La función no guarda ni cierra el libro. No cambia filtros ni segmentaciones.
    """
    api, app = libro.api, libro.app.api
    hojas = _coleccion(api.Worksheets)
    conexiones = _coleccion(api.Connections)
    activas = {conexion.Name.casefold(): bool(conexion.RefreshWithRefreshAll)
               for conexion in conexiones}
    monitores, sincronas, queries = [], [], []
    for conexion in conexiones:
        if conexion.Type not in (1, 2):  # OLEDB, ODBC; el modelo no expone Refreshing.
            continue
        proveedor = conexion.OLEDBConnection if conexion.Type == 1 else conexion.ODBCConnection
        monitores.append(proveedor)
        if activas[conexion.Name.casefold()]:
            sincronas.append(proveedor)
    for hoja in hojas:
        for query in _consultas_hoja(hoja):
            monitores.append(query)
            conexion = query.WorkbookConnection
            if conexion is None or conexion.RefreshWithRefreshAll:
                sincronas.append(query)
                queries.append(query)
    pivots = [pivot for hoja in hojas for pivot in _coleccion(hoja.PivotTables())]
    cambios, observadores, resultados = [], [], []
    error_principal = None
    with _Avisos(progreso) as avisos:
        try:
            for objeto in sincronas:
                anterior = objeto.BackgroundQuery
                if anterior:
                    cambios.append((objeto, anterior))
                    objeto.BackgroundQuery = False
            if queries and not app.EnableEvents:
                raise ValueError("Las QueryTables requieren eventos de Excel activos para verificar AfterRefresh.")
            for query in queries:
                resultado = _ResultadoConsulta(query.Name)
                observadores.append(_observar_consulta(query, resultado))
                resultados.append(resultado)
            if conexiones or queries or pivots:
                avisos.etapa("Calculando las fórmulas fuente")
                app.Calculate()
                _esperar(app, monitores, consultas=bool(conexiones or queries), calculo=True)
            if conexiones or queries:
                avisos.etapa("Actualizando conexiones y modelo")
                # Una actualización al abrir puede terminar durante el cálculo
                # previo; su evento no confirma este nuevo RefreshAll.
                for resultado in resultados:
                    resultado.exito = None
                respuesta = api.RefreshAll()
                if respuesta is False:
                    raise RuntimeError("Excel canceló RefreshAll.")
                _esperar(app, monitores, consultas=True, calculo=False)
                for resultado in resultados:
                    if resultado.exito is False:
                        raise RuntimeError(f"La consulta {resultado.nombre} no se actualizó correctamente.")
                    if resultado.exito is None:
                        raise RuntimeError(f"Excel no confirmó la actualización de {resultado.nombre}.")
                # Las consultas a hoja pueden dejar fórmulas fuente pendientes.
                app.Calculate()
                _esperar(app, monitores, consultas=True, calculo=True)
            if pivots:
                avisos.etapa("Actualizando tablas dinámicas")
                for pivot in pivots:
                    if not pivot.RefreshTable():
                        raise RuntimeError(f"La tabla dinámica {pivot.Name} no se actualizó correctamente.")
                _esperar(app, monitores, consultas=bool(conexiones or queries), calculo=False)
            avisos.etapa("Calculando los resultados finales")
            app.CalculateFull()
            _esperar(app, monitores, consultas=bool(conexiones or queries), calculo=True)
        except BaseException as error:
            error_principal = error
            raise
        finally:
            errores = []
            for observador in reversed(observadores):
                try:
                    observador.close()
                except BaseException as error:
                    errores.append(error)
            for objeto, anterior in reversed(cambios):
                try:
                    objeto.BackgroundQuery = anterior
                except BaseException as error:
                    errores.append(error)
            if errores:
                if error_principal is not None:
                    errores.insert(0, error_principal)
                raise BaseExceptionGroup("Falló la actualización o la restauración de sus opciones", errores)


__all__ = ["refrescar_libro"]
