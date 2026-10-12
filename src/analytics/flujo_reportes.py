"""Una extracción compartida para la plantilla y los reportes de seis tablas."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import Callable
from zoneinfo import ZoneInfo

import polars as pl

from .cortes_reportes import cargar_mapa_campanas, preparar_plan_cortes
from .datos_reportes import formatos_colas, formatos_ejecutivos, formatos_workloads
from .excel_reportes import TablaDatos, generar_libro
from .fuentes_sql import (
    cargar_fuentes_sql, crear_engine, formatos_colas_diario, formatos_estados_agentes,
)


FUENTES = ("colas_diario", "colas", "workloads_diario", "workloads", "ejecutivos", "estados_agentes")
BASE_DIR = Path(__file__).resolve().parents[2]
CONTRATO_TABLAS = {
    "t_colas_diario": "t_colas_diario", "t_colas_intervalo": "t_colas_intervalo",
    "t_workloads_diario": "t_workloads_diario", "t_workloads_intervalo": "t_workloads_intervalo",
    "t_ejecutivos_diario": "t_ejecutivos_diario", "t_estados_agentes_intervalo": "t_estados_agentes_intervalo",
}


@dataclass(frozen=True)
class DatosReportes:
    datos: dict[str, pl.DataFrame]
    mapa: pl.DataFrame
    workloads: object
    desde: date
    hasta: date
    desde_intervalos: date
    desde_workloads: date
    hasta_workloads: date | None = None


def cargar_datos(
    desde: date, hasta: date, *, semanas_intervalos: int = 8,
    desde_intervalos: date | None = None,
    desde_workloads: date | None = None, hasta_workloads: date | None = None,
    base_dir: Path | None = None,
    progreso: Callable[[str], None] | None = print,
) -> DatosReportes:
    """Lee cuatro vistas y la API una vez; devuelve datos sin generar Excel.

    Los diarios conservan todo el rango solicitado. El detalle se acota a las
    últimas semanas de ese rango. Workloads permite un inicio histórico propio
    porque la API puede no conservar la misma historia que PostgreSQL. Su fecha
    final puede extender el forecast sin ampliar el rango real de las vistas SQL.
    """
    from .datos_reportes import _validar_rango, cargar_workloads_reportes

    _validar_rango(desde, hasta)
    if desde is None or hasta is None:
        raise ValueError("Se requieren desde y hasta.")
    if not isinstance(semanas_intervalos, int) or isinstance(semanas_intervalos, bool) or semanas_intervalos < 1:
        raise ValueError("semanas_intervalos debe ser un entero positivo.")
    inicio_workloads = desde if desde_workloads is None else desde_workloads
    _validar_rango(inicio_workloads, hasta)
    fin_workloads = hasta if hasta_workloads is None else hasta_workloads
    _validar_rango(hasta, fin_workloads)
    if inicio_workloads < desde:
        raise ValueError("El inicio de Workloads no puede preceder al histórico general.")
    if desde_intervalos is not None:
        _validar_rango(desde_intervalos, hasta)
        if desde_intervalos < desde:
            raise ValueError("El inicio por intervalos no puede preceder al histórico general.")
    inicio_detalle = desde_intervalos or max(desde, hasta - timedelta(days=semanas_intervalos * 7 - 1))
    base = BASE_DIR if base_dir is None else Path(base_dir).resolve()
    avisar = progreso or (lambda mensaje: None)
    avisar(f"Diarios: {desde} a {hasta}; intervalos SQL: {inicio_detalle} a {hasta}.")
    avisar("Consultando las cuatro vistas SQL y el mapa de campañas...")
    engine = crear_engine(base)
    try:
        datos = cargar_fuentes_sql(desde, hasta, inicio_detalle, base_dir=base, engine=engine)
        mapa = cargar_mapa_campanas(engine)
    finally:
        engine.dispose()
    avisar(f"Consultando Peopleware: {inicio_workloads} a {fin_workloads}...")
    workloads = cargar_workloads_reportes(
        inicio_workloads, fin_workloads, max(inicio_detalle, inicio_workloads), base_dir=base,
    )
    datos.update(workloads=workloads.df, workloads_diario=workloads.df_diario)
    datos = {fuente: datos[fuente] for fuente in FUENTES}
    for fuente, df in datos.items():
        avisar(f"{fuente}: {df.height:,} filas; {df.width} columnas.")
    avisar(f"Alertas de Workloads: {workloads.alertas.height}.")
    return DatosReportes(datos, mapa, workloads, desde, hasta, inicio_detalle, inicio_workloads, fin_workloads)


def tablas_reportes(datos: dict[str, pl.DataFrame]) -> list[TablaDatos]:
    """Nombres estables para las seis tablas, sin combinar sus granos."""
    from .datos_reportes import formatos_workloads_diario

    if set(datos) != set(FUENTES):
        raise ValueError("Se requieren las seis fuentes del reporte.")
    return [
        TablaDatos("t_colas_diario", "t_colas_diario", datos["colas_diario"], formatos_colas_diario()),
        TablaDatos("t_colas_intervalo", "t_colas_intervalo", datos["colas"], formatos_colas()),
        TablaDatos("t_workloads_diario", "t_workloads_diario", datos["workloads_diario"], formatos_workloads_diario(datos["workloads_diario"])),
        TablaDatos("t_workloads_intervalo", "t_workloads_intervalo", datos["workloads"], formatos_workloads(datos["workloads"])),
        TablaDatos("t_ejecutivos_diario", "t_ejecutivos_diario", datos["ejecutivos"], formatos_ejecutivos()),
        TablaDatos("t_estados_agentes_intervalo", "t_estados_agentes_intervalo", datos["estados_agentes"], formatos_estados_agentes()),
    ]


def _completar_variantes_plantilla(fuentes: list[TablaDatos], metadatos: dict) -> list[TablaDatos]:
    """Conservar variantes de Peopleware que no llegaron en esta consulta.

    Las parejas forecast_<variante>/tmo_<variante> son columnas de origen.
    Si faltan ambas en la fuente, se escriben NULL; nunca se reutilizan valores
    anteriores de la plantilla. Las columnas calculadas siguen a cargo del
    escritor y las columnas adicionales de otras tablas no se reinterpretan.
    """
    resultado = []
    for fuente in fuentes:
        if fuente.nombre not in {"t_workloads_diario", "t_workloads_intervalo"}:
            resultado.append(fuente)
            continue
        meta = metadatos[fuente.nombre]
        encabezados = set(meta.encabezados)
        existentes = set(fuente.df.columns)
        ausentes = []
        for columna in meta.encabezados:
            coincidencia = re.fullmatch(r"forecast_([a-z0-9]+(?:_[a-z0-9]+)*)", columna)
            if coincidencia is None:
                continue
            pareja = (columna, f"tmo_{coincidencia[1]}")
            if (all(c in encabezados and c not in existentes for c in pareja)
                    and not any(c in meta.formulas for c in pareja)):
                ausentes.extend(pareja)
        if ausentes:
            # Series explícitas conservan cero filas en fuentes vacías.
            df = fuente.df.with_columns([
                pl.Series(c, [None] * fuente.df.height, dtype=pl.Float64) for c in ausentes
            ])
            fuente = replace(fuente, df=df,
                             columnas_no_recibidas=fuente.columnas_no_recibidas | frozenset(ausentes))
        resultado.append(fuente)
    return resultado


def crear_plantilla(resultado: DatosReportes, destino: str | Path, *, app=None) -> Path:
    """Genera una base nueva; no depende de Excel intermedios ni de otra plantilla."""
    return generar_libro(tablas_reportes(resultado.datos), destino, app=app)


def _json(ruta: Path, contenido: dict) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", dir=ruta.parent, delete=False) as archivo:
        temporal = Path(archivo.name)
        json.dump(contenido, archivo, ensure_ascii=False, indent=2,
                  default=lambda valor: valor.isoformat() if isinstance(valor, (date, datetime)) else str(valor))
        archivo.write("\n")
    try:
        temporal.replace(ruta)
    finally:
        temporal.unlink(missing_ok=True)


def _seleccionar(disponibles: list[str], nombres: list[str] | None, criterio: str) -> list[str]:
    if nombres is None:
        return disponibles
    faltantes = set(nombres) - set(disponibles)
    if faltantes:
        raise ValueError(f"Sin datos atribuibles para {criterio}: {'; '.join(sorted(faltantes))}. Usar --listar.")
    return [nombre for nombre in disponibles if nombre in set(nombres)]


def _revisar_plantilla(ruta: str | Path):
    """Rechaza una plantilla incompatible antes de consultar las fuentes."""
    from .archivos_excel import abrir_lectura
    from .excel_reportes import _tablas_xml
    from zipfile import ZipFile

    archivo = Path(ruta).resolve()
    if archivo.suffix.lower() != ".xlsx":
        raise ValueError("La plantilla debe ser .xlsx.")
    with abrir_lectura(archivo) as stream, ZipFile(stream) as z:
        tablas, _ = _tablas_xml(z)
    for nombre, hoja in CONTRATO_TABLAS.items():
        if nombre not in tablas or tablas[nombre].hoja != hoja:
            raise ValueError(f"La plantilla {archivo.name} requiere {hoja}/{nombre}.")
    return tablas


def listar_cortes(resultado: DatosReportes, *, progreso=print) -> dict[str, list[str]]:
    disponibles = {}
    for criterio in ("cliente", "jefe_negocio"):
        plan = preparar_plan_cortes(resultado.datos, resultado.mapa, criterio)
        disponibles[criterio] = list(plan.nombres)
        for nombre in plan.nombres:
            progreso(f"{criterio}: {nombre}; " + "; ".join(f"{k}: {v:,}" for k, v in plan.filas(nombre).items()))
        progreso(f"Diagnósticos de {criterio}: {plan.diagnosticos.height}.")
    return disponibles


def generar_reportes(
    resultado: DatosReportes, plantilla: str | Path, *,
    salida_general: str | Path | None = None,
    salida_clientes: str | Path | None = None, salida_jefes: str | Path | None = None,
    plantilla_cliente: str | Path | None = None, plantilla_jefe: str | Path | None = None,
    clientes: list[str] | None = None, jefes: list[str] | None = None,
    ruta_resumen: str | Path | None = None, progreso=print,
) -> dict:
    """Rellena copias de las plantillas sin repetir las consultas.

    Un destino None omite esa clase de salida. Los filtros seleccionan archivos
    de los cortes; el libro general conserva las seis fuentes completas.
    """
    plantilla = Path(plantilla).resolve()
    clases = [
        ("cliente", salida_clientes, plantilla_cliente, clientes),
        ("jefe_negocio", salida_jefes, plantilla_jefe, jefes),
    ]
    planes = []
    for criterio, carpeta, plantilla_especifica, nombres in clases:
        if carpeta is None:
            continue
        plan = preparar_plan_cortes(resultado.datos, resultado.mapa, criterio)
        seleccionados = _seleccionar(plan.nombres, nombres, criterio)
        planes.append((criterio, Path(carpeta).resolve(),
                       Path(plantilla_especifica).resolve() if plantilla_especifica else plantilla,
                       seleccionados, plan))
    general = Path(salida_general).resolve() if salida_general is not None else None
    if general is None and not planes:
        raise ValueError("Seleccionar al menos una salida.")
    plantillas = {plan[2] for plan in planes} | ({plantilla} if general else set())
    metadatos_plantillas = {}
    for archivo in plantillas:
        if not archivo.is_file():
            raise FileNotFoundError(f"No existe la plantilla: {archivo}")
        tablas = _revisar_plantilla(archivo)
        metadatos_plantillas[archivo] = tablas
        for fuente in _completar_variantes_plantilla(tablas_reportes(resultado.datos), tablas):
            if fuente.nombre not in tablas or tablas[fuente.nombre].hoja != fuente.hoja:
                raise ValueError(f"La plantilla {archivo.name} requiere {fuente.hoja}/{fuente.nombre}.")
            faltantes = set(fuente.df.columns) - set(tablas[fuente.nombre].encabezados)
            if faltantes:
                raise ValueError(f"Columnas ausentes en {archivo.name}/{fuente.nombre}: {sorted(faltantes)}.")
    from .cortes_reportes import nombre_archivo_corte
    destinos = ([general] if general else []) + [carpeta / nombre_archivo_corte(criterio, nombre)
                for criterio, carpeta, _, nombres, _ in planes for nombre in nombres]
    if len(set(destinos)) != len(destinos) or set(destinos) & plantillas:
        raise ValueError("Los destinos deben ser distintos entre sí y de todas las plantillas.")
    if any(destino.suffix.lower() != ".xlsx" for destino in destinos):
        raise ValueError("Los destinos de libros deben ser .xlsx.")
    resumen = {
        "desde": resultado.desde.isoformat(), "hasta": resultado.hasta.isoformat(),
        "desde_intervalos": resultado.desde_intervalos.isoformat(),
        "desde_workloads": resultado.desde_workloads.isoformat(),
        "hasta_workloads": (resultado.hasta_workloads or resultado.hasta).isoformat(),
        "fuentes": {k: {"filas": v.height, "columnas": v.width} for k, v in resultado.datos.items()},
        "alertas_workloads": resultado.workloads.alertas.to_dicts(),
        "cobertura_workloads": resultado.workloads.cobertura_diaria.to_dicts(),
        "diagnosticos_cortes": {criterio: plan.diagnosticos.to_dicts() for criterio, _, _, _, plan in planes},
        "archivos": [], "estado": "en_progreso", "archivo_en_progreso": None,
    }
    ruta = Path(ruta_resumen).resolve() if ruta_resumen else (general.parent if general else planes[0][1]) / "resumen_generacion.json"
    if ruta in plantillas or ruta in destinos:
        raise ValueError("El resumen no puede reemplazar un libro.")
    _json(ruta, resumen)
    trabajos = ([('general', None, general, plantilla, None)] if general else [])
    trabajos += [(criterio, nombre, carpeta / nombre_archivo_corte(criterio, nombre), modelo, plan)
                 for criterio, carpeta, modelo, nombres, plan in planes for nombre in nombres]
    try:
        if trabajos:
            import xlwings as xw
            with xw.App(visible=False, add_book=False) as app:
                for criterio, nombre, destino, modelo, plan in trabajos:
                    datos = resultado.datos if plan is None else plan.obtener(nombre).datos
                    resumen["archivo_en_progreso"] = str(destino)
                    _json(ruta, resumen)
                    progreso(f"Generando {destino.name}...")
                    fuentes = _completar_variantes_plantilla(
                        tablas_reportes(datos), metadatos_plantillas[modelo],
                    )
                    generar_libro(fuentes, destino, plantilla=modelo, app=app)
                    resumen["archivos"].append({"criterio": criterio, "nombre": nombre, "archivo": str(destino),
                                               "filas": {k: v.height for k, v in datos.items()}})
                    resumen["archivo_en_progreso"] = None
                    _json(ruta, resumen)
                    progreso(f"Validado: {destino.name}.")
                    del datos, fuentes
        resumen["estado"] = "completo"
        _json(ruta, resumen)
    except Exception as error:
        resumen["estado"] = "error"
        resumen["tipo_error"] = type(error).__name__
        _json(ruta, resumen)
        raise
    return resumen


def ejecutar(argumentos: list[str] | None = None, base_dir: Path | None = None) -> int:
    base = BASE_DIR if base_dir is None else Path(base_dir).resolve()
    parser = argparse.ArgumentParser(description="Consulta seis tablas una vez y genera el libro general y los cortes por cliente y jefe_negocio.")
    parser.add_argument("--desde", required=True, type=date.fromisoformat, metavar="AAAA-MM-DD", help="Inicio del histórico diario SQL.")
    parser.add_argument("--hasta", type=date.fromisoformat, default=datetime.now(ZoneInfo("America/Santiago")).date(), metavar="AAAA-MM-DD")
    parser.add_argument("--desde-workloads", type=date.fromisoformat, metavar="AAAA-MM-DD", help="Inicio del diario Peopleware; por defecto --desde.")
    parser.add_argument("--hasta-workloads", type=date.fromisoformat, metavar="AAAA-MM-DD", help="Fin del forecast Peopleware; por defecto --hasta. Puede incluir semanas futuras.")
    parser.add_argument("--semanas-intervalos", type=int, default=8)
    parser.add_argument("--desde-intervalos", type=date.fromisoformat, metavar="AAAA-MM-DD", help="Inicio exacto del detalle; permite alinear las semanas al lunes.")
    parser.add_argument("--plantilla", type=Path, default=base / "archivos" / "plantilla_reportes_base.xlsx")
    parser.add_argument("--plantilla-cliente", type=Path)
    parser.add_argument("--plantilla-jefe", type=Path)
    parser.add_argument("--salida-general", type=Path, default=base / "archivos" / "reporte_general.xlsx")
    parser.add_argument("--salida-clientes", type=Path, default=base / "archivos" / "archivos_por_cliente")
    parser.add_argument("--salida-jefes", type=Path, default=base / "archivos" / "archivos_por_jefe")
    parser.add_argument("--cliente", action="append", help="Nombre exacto; repetir para varios clientes.")
    parser.add_argument("--jefe", action="append", help="Nombre exacto del jefe_negocio; repetir para varios.")
    parser.add_argument("--sin-general", action="store_true")
    parser.add_argument("--sin-clientes", action="store_true")
    parser.add_argument("--sin-jefes", action="store_true")
    parser.add_argument("--listar", action="store_true", help="Consulta y lista los cortes sin crear Excel.")
    args = parser.parse_args(argumentos)
    if args.desde > args.hasta or args.semanas_intervalos < 1:
        parser.error("Verificar el rango de fechas y las semanas positivas.")
    if args.desde_workloads and not args.desde <= args.desde_workloads <= args.hasta:
        parser.error("--desde-workloads debe estar dentro del rango general.")
    if args.hasta_workloads and args.hasta_workloads < args.hasta:
        parser.error("--hasta-workloads debe ser igual o posterior a --hasta.")
    if args.desde_intervalos and not args.desde <= args.desde_intervalos <= args.hasta:
        parser.error("--desde-intervalos debe estar dentro del rango general.")
    if args.sin_general and args.sin_clientes and args.sin_jefes and not args.listar:
        parser.error("Seleccionar al menos una salida o usar --listar.")
    if not args.listar:
        necesarias = ([] if args.sin_general else [args.plantilla])
        necesarias += ([] if args.sin_clientes else [args.plantilla_cliente or args.plantilla])
        necesarias += ([] if args.sin_jefes else [args.plantilla_jefe or args.plantilla])
        for archivo in necesarias:
            if not archivo.is_file():
                parser.error(f"No existe {archivo}; crear primero la base con rev_plantilla_reportes.ipynb.")
        try:
            for archivo in set(necesarias):
                _revisar_plantilla(archivo)
        except ValueError as error:
            parser.error(str(error))
    resultado = cargar_datos(args.desde, args.hasta, semanas_intervalos=args.semanas_intervalos,
                            desde_intervalos=args.desde_intervalos,
                            desde_workloads=args.desde_workloads, hasta_workloads=args.hasta_workloads, base_dir=base,
                            progreso=lambda mensaje: print(mensaje, flush=True))
    if args.listar:
        listar_cortes(resultado)
        return 0
    generar_reportes(resultado, args.plantilla,
                     salida_general=None if args.sin_general else args.salida_general,
                     salida_clientes=None if args.sin_clientes else args.salida_clientes,
                     salida_jefes=None if args.sin_jefes else args.salida_jefes,
                     plantilla_cliente=args.plantilla_cliente, plantilla_jefe=args.plantilla_jefe,
                     clientes=args.cliente, jefes=args.jefe,
                     ruta_resumen=base / "archivos" / "resumen_generacion.json",
                     progreso=lambda mensaje: print(mensaje, flush=True))
    return 0


__all__ = ["DatosReportes", "cargar_datos", "tablas_reportes", "crear_plantilla", "generar_reportes", "listar_cortes", "ejecutar"]
