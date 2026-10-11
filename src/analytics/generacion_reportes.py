"""Generación con datos nuevos y una plantilla de tres tablas."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

import polars as pl
from dotenv import load_dotenv

from .cortes_reportes import cargar_mapa_campanas, preparar_cortes
from .datos_reportes import (
    cargar_colas, cargar_ejecutivos, cargar_workloads,
    formatos_colas, formatos_ejecutivos, formatos_workloads,
)
from .excel_reportes import TablaDatos, generar_libro


def tablas_del_corte(datos: dict[str, pl.DataFrame]) -> list[TablaDatos]:
    return [
        TablaDatos("t_datos", "Colas", datos["colas"], formatos_colas()),
        TablaDatos("t_workloads", "Workloads", datos["workloads"], formatos_workloads(datos["workloads"])),
        TablaDatos("t_ejecutivos", "Ejecutivos", datos["ejecutivos"], formatos_ejecutivos()),
    ]


def _guardar_json(ruta: Path, contenido: dict) -> None:
    with NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", dir=ruta.parent, delete=False) as f:
        temporal = Path(f.name)
        json.dump(contenido, f, ensure_ascii=False, indent=2)
        f.write("\n")
    try:
        temporal.replace(ruta)
    finally:
        temporal.unlink(missing_ok=True)


def ejecutar(criterio: str, argumentos: list[str] | None = None, base_dir: Path | None = None) -> int:
    """Cliente o jefe_negocio; las tres fuentes comparten el rango inclusive."""
    if criterio not in {"cliente", "jefe_negocio"}:
        raise ValueError("Criterio no admitido.")
    base = Path(base_dir).resolve() if base_dir else Path(__file__).resolve().parents[2]
    etiqueta = "cliente" if criterio == "cliente" else "jefe de campaña (jefe_negocio)"
    carpeta = "por_cliente" if criterio == "cliente" else "por_jefe_campana"
    selector = "--cliente" if criterio == "cliente" else "--jefe"
    parser = argparse.ArgumentParser(description=f"Actualiza las tres tablas con datos nuevos y genera un Excel por {etiqueta}.")
    parser.add_argument("--desde", type=date.fromisoformat, default=date(2026, 9, 28), metavar="AAAA-MM-DD")
    parser.add_argument("--hasta", type=date.fromisoformat, default=date(2026, 10, 25), metavar="AAAA-MM-DD")
    parser.add_argument("--plantilla", type=Path, default=base / "archivados" / "excel_anteriores" / "plantilla_reportes.xlsx",
                        help="Plantilla principal o una copia con tus cálculos y presentación.")
    parser.add_argument("--salida", type=Path, default=base / "archivos" / carpeta)
    parser.add_argument(selector, dest="grupos", action="append", metavar="NOMBRE",
                        help="Nombre exacto; repetir para generar varios. Si se omite, genera todos.")
    parser.add_argument("--listar", action="store_true", help="Consulta y muestra los cortes disponibles sin generar archivos.")
    args = parser.parse_args(argumentos)
    if args.desde > args.hasta:
        parser.error("--desde debe ser anterior o igual a --hasta.")
    args.plantilla = args.plantilla.resolve()
    args.salida = args.salida.resolve()
    if not args.listar and not args.plantilla.is_file():
        parser.error(f"No existe la plantilla: {args.plantilla}")

    print(f"Período: {args.desde} a {args.hasta}, inclusive.", flush=True)
    print("Consultando Colas...", flush=True)
    colas = cargar_colas(args.desde, args.hasta, base)
    print("Consultando Ejecutivos...", flush=True)
    ejecutivos = cargar_ejecutivos(args.desde, args.hasta, base)
    print("Consultando Workloads...", flush=True)
    workloads = cargar_workloads(args.desde, args.hasta, base)
    datos = {"colas": colas, "ejecutivos": ejecutivos, "workloads": workloads.df}
    load_dotenv(base / ".env")
    from db_conn import conn_db_wf
    engine = conn_db_wf()[0]
    try:
        mapa = cargar_mapa_campanas(engine)
    finally:
        engine.dispose()
    cortes, diagnosticos = preparar_cortes(datos, mapa, criterio)
    disponibles = {c.nombre for c in cortes}
    if args.grupos:
        desconocidos = set(args.grupos) - disponibles
        if desconocidos:
            parser.error("No hay datos atribuibles a: " + "; ".join(sorted(desconocidos)) + ". Usar --listar para ver los nombres.")
        cortes = [c for c in cortes if c.nombre in set(args.grupos)]
    for corte in cortes:
        conteos = "; ".join(f"{k}: {v.height:,}" for k, v in corte.datos.items())
        print(f"{corte.nombre}: {conteos}", flush=True)
    if diagnosticos.height:
        avisos = diagnosticos.group_by("fuente", "motivo", "excluidas").agg(pl.col("filas").sum()).sort("fuente", "motivo")
        for aviso in avisos.iter_rows(named=True):
            print(f"Aviso {aviso['fuente']}: {aviso['motivo']}; {aviso['filas']:,} filas; excluidas={aviso['excluidas']}.", flush=True)
    print(f"Alertas de Workloads: {workloads.alertas.height}.", flush=True)
    if args.listar:
        return 0

    args.salida.mkdir(parents=True, exist_ok=True)
    resumen = {
        "criterio": criterio, "desde": args.desde.isoformat(), "hasta": args.hasta.isoformat(),
        "plantilla": str(args.plantilla), "fuentes": {k: v.height for k, v in datos.items()},
        "cortes": [], "diagnosticos_cortes": diagnosticos.to_dicts(),
        "alertas_workloads": workloads.alertas.to_dicts(),
        "estado": "en_progreso", "corte_en_progreso": None,
    }
    ruta_resumen = args.salida / "resumen_generacion.json"
    _guardar_json(ruta_resumen, resumen)
    try:
        if cortes:
            import xlwings as xw
            with xw.App(visible=False, add_book=False) as app:
                for corte in cortes:
                    destino = args.salida / corte.nombre_archivo
                    if destino.resolve() == args.plantilla:
                        raise ValueError("La salida no puede reemplazar la plantilla.")
                    resumen["corte_en_progreso"] = corte.nombre
                    _guardar_json(ruta_resumen, resumen)
                    print(f"Generando {destino.name}...", flush=True)
                    generar_libro(tablas_del_corte(corte.datos), destino, plantilla=args.plantilla, app=app)
                    resumen["cortes"].append({"nombre": corte.nombre, "archivo": destino.name,
                                             "filas": {k: v.height for k, v in corte.datos.items()}})
                    resumen["corte_en_progreso"] = None
                    _guardar_json(ruta_resumen, resumen)
                    print(f"Validado: {destino.name}", flush=True)
        resumen["estado"] = "completo"
        _guardar_json(ruta_resumen, resumen)
    except Exception as error:
        resumen["estado"] = "error"
        resumen["tipo_error"] = type(error).__name__
        _guardar_json(ruta_resumen, resumen)
        raise
    print(f"Generados {len(cortes)} reportes en {args.salida}", flush=True)
    return 0


__all__ = ["ejecutar", "tablas_del_corte"]
