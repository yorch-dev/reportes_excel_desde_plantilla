"""Fuentes SQL con una conexion y sus unidades de exportacion.

No conecta ni carga credenciales al importar. Conserva Colas y Ejecutivos;
Estados convierte las duraciones a horas decimales y presenta RUT con guion.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import polars as pl
from dotenv import dotenv_values
from sqlalchemy import create_engine as _create_engine, text
from sqlalchemy.engine import Engine, URL

from .datos_reportes import (
    CONFIG_COLAS,
    CONFIG_EJECUTIVOS,
    QUERY_COLAS,
    _formatos,
    _tipar,
    _tipos,
    formatos_colas,
    formatos_ejecutivos,
)
from .asignaciones_turnos import (
    CONFIG_ASIGNACIONES, CONFIG_TURNOS, cargar_asignaciones_turnos,
    formatos_asignaciones, formatos_turnos,
)
from .licencias_vacaciones import (
    CONFIG_LICENCIAS_VACACIONES, cargar_licencias_vacaciones,
    formatos_licencias_vacaciones,
)


ESTADOS = (
    "conectado", "en_la_cola", "inactivo", "no_responde", "fuera_de_la_cola",
    "disponible", "ocupado", "ausente", "descanso", "comida", "capacitacion",
    "interactuando",
)
HORAS_COLAS_DIARIO = (
    "tiempo_manejo_total", "tiempo_conversacion_total", "tiempo_retencion_total",
    "tiempo_acw_total", "tiempo_contactando_total", "tiempo_marcando_total",
    "tiempo_espera_total", "tiempo_devoluciones_llamadas_activas_total",
)

CONFIG_COLAS_DIARIO = {
    "fechas": {"columnas": ["fecha"], "tipo": pl.Date, "formato": "dd-mm-yyyy"},
    "ids": {
        "columnas": ["rendimiento_id", "cola_id", "campana_id", "cliente_id", "horario_id",
                     "plataforma_operacion_id", "jefe_negocio_id", "unidad_negocio_id",
                     "grupo_facturacion_id", "tipo_campana_id"],
        "tipo": pl.Int64, "formato": "0",
    },
    "textos": {
        "columnas": ["tipo_medios", "id_cola", "nombre_cola", "nombre_cola_dimension",
                     "id_division", "nombre_division", "nombre_campana", "tipo_campana",
                     "cliente", "plataforma_operacion", "unidad_negocio", "jefe_negocio",
                     "observaciones_campana", "supervisores"],
        "tipo": pl.String, "formato": "@",
    },
    "enteros": {
        "columnas": ["oferta", "contestadas", "abandono_rapido", "abandono_no_rapido",
                     "flujo_saliente", "cumplen_sla", "manejo", "conversacion", "retener",
                     "acw", "contactando", "marcando", "saliente", "espera",
                     "transferencia_sin_consulta", "transferencia_con_consulta", "consultar",
                     "devoluciones_llamadas_activas", "medios_entrantes", "medios_salientes",
                     "tmo_objetivo", "tiempo_maximo_espera_objetivo"],
        "tipo": pl.Int64, "formato": "#,##0",
    },
    "porcentajes": {
        "columnas": ["porcentaje_objetivo_nivel_servicio", "nivel_atencion_objetivo",
                     "nivel_servicio_objetivo"],
        "tipo": pl.Float64, "formato": "0.00%",
    },
    "segundos": {
        "columnas": ["velocidad_media_respuesta_seg", "abandono_medio_seg"],
        "tipo": pl.Float64, "formato": "#,##0.00",
    },
    "duraciones": {"columnas": list(HORAS_COLAS_DIARIO), "tipo": pl.Float64, "formato": "0.0000"},
}
CONFIG_ESTADOS_AGENTES = {
    "fechas": {"columnas": ["fecha"], "tipo": pl.Date, "formato": "dd-mm-yyyy"},
    "intervalos": {"columnas": ["intervalo"], "tipo": pl.Time, "formato": "hh:mm"},
    "ids": {"columnas": ["id"], "tipo": pl.Int64, "formato": "0"},
    "textos": {
        "columnas": ["id_agente", "nombre_agente", "rut", "supervisor", "jefe_negocio",
                     "unidad_planificacion"],
        "tipo": pl.String, "formato": "@",
    },
    "duraciones": {"columnas": list(ESTADOS), "tipo": pl.Float64, "formato": "0.0000"},
}

_COLUMNAS_ESTADOS = (
    "id", "fecha", "intervalo", "id_agente", "nombre_agente", *ESTADOS,
    "rut", "supervisor", "jefe_negocio", "unidad_planificacion",
)
_CONFIGURACIONES = {
    "colas_diario": CONFIG_COLAS_DIARIO,
    "colas": CONFIG_COLAS,
    "ejecutivos": CONFIG_EJECUTIVOS,
    "estados_agentes": CONFIG_ESTADOS_AGENTES,
}
_VISTAS = {
    "colas_diario": "datos_colas_diario",
    "colas": "datos_colas",
    "ejecutivos": "datos_ejecutivos",
    "estados_agentes": "datos_estados_agentes",
}
_TIPOS_SQL = {
    pl.String: {"text", "character varying", "character"},
    pl.Date: {"date"},
    pl.Time: {"time without time zone"},
    pl.Int64: {"smallint", "integer", "bigint"},
    pl.Float64: {"numeric", "double precision", "real"},
}


def crear_engine(base_dir: str | Path | None = None) -> Engine:
    """La misma configuracion PostgreSQL de db_conn, sin metadata.reflect.

    El proceso existente tiene prioridad sobre .env, igual que load_dotenv.
    El caller que crea el engine es responsable de disponerlo.
    """
    base = Path(base_dir).resolve() if base_dir is not None else Path(__file__).resolve().parents[2]
    configuracion = dotenv_values(base / ".env")

    def valor(clave: str):
        return os.environ.get(clave) or configuracion.get(clave)

    if not valor("POSTGRES_USER") or not valor("POSTGRES_DB"):
        raise ValueError("Falta configurar POSTGRES_USER o POSTGRES_DB.")
    url = URL.create(
        drivername="postgresql+psycopg",
        username=valor("POSTGRES_USER"),
        password=valor("POSTGRES_PASSWORD"),
        host="localhost",
        port=valor("PORT"),
        database=valor("POSTGRES_DB"),
    )
    return _create_engine(url, connect_args={"connect_timeout": 5})


def formatos_colas_diario() -> dict[str, str]:
    return _formatos(CONFIG_COLAS_DIARIO)


def formatos_estados_agentes() -> dict[str, str]:
    return _formatos(CONFIG_ESTADOS_AGENTES)


def _validar_fechas(desde: date, hasta: date, desde_intervalos: date) -> None:
    for valor in (desde, hasta, desde_intervalos):
        if not isinstance(valor, date) or isinstance(valor, datetime):
            raise TypeError("Las fechas deben ser datetime.date.")
    if not desde <= desde_intervalos <= hasta:
        raise ValueError("Se requiere DESDE <= DESDE_INTERVALOS <= HASTA.")


def _validar_esquemas(conn) -> dict[str, list[str]]:
    """Comprobar contratos de columnas/tipos sin reflejar ni leer los hechos."""
    filas = conn.execute(text(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'reporting' AND table_name IN "
        "('datos_colas_diario', 'datos_colas', 'datos_ejecutivos', 'datos_estados_agentes') "
        "ORDER BY table_name, ordinal_position"
    )).mappings()
    esquemas: dict[str, dict[str, str]] = {nombre: {} for nombre in _VISTAS.values()}
    for fila in filas:
        esquemas[fila["table_name"]][fila["column_name"]] = fila["data_type"]
    ordenes = {}
    for clave, config in _CONFIGURACIONES.items():
        esquema = esquemas[_VISTAS[clave]]
        tipos = _tipos(config)
        nombres_fuente = {
            columna: "nombre_cola_dimension" if clave == "colas" and columna == "nombre_cola_2" else columna
            for columna in tipos
        }
        requeridas = set(nombres_fuente.values())
        if not requeridas <= set(esquema) or (clave != "colas" and requeridas != set(esquema)):
            raise ValueError(f"Cambio de columnas en reporting.{_VISTAS[clave]}.")
        for columna, tipo in tipos.items():
            aceptados = {"interval"} if clave == "estados_agentes" and columna in ESTADOS else _TIPOS_SQL[tipo]
            if esquema[nombres_fuente[columna]] not in aceptados:
                raise ValueError(f"Tipo SQL incompatible en {_VISTAS[clave]}.{nombres_fuente[columna]}.")
        ordenes[clave] = list(esquema)
    if ordenes["estados_agentes"] != list(_COLUMNAS_ESTADOS):
        raise ValueError("Cambio de orden de columnas en datos_estados_agentes.")
    return ordenes


def _consultas() -> dict[str, str]:
    colas = QUERY_COLAS.replace(
        "ORDER BY", " AND fecha BETWEEN :desde_intervalos AND :hasta ORDER BY", 1
    )
    proyeccion_estados = ", ".join(
        f'EXTRACT(EPOCH FROM "{columna}") / 3600.0 AS "{columna}"'
        if columna in ESTADOS else f'"{columna}"'
        for columna in _COLUMNAS_ESTADOS
    )
    return {
        "colas_diario": (
            "SELECT * FROM reporting.datos_colas_diario "
            "WHERE fecha BETWEEN :desde AND :hasta AND campana_id IS NOT NULL "
            "ORDER BY nombre_campana, nombre_cola, fecha, tipo_medios, id_division, rendimiento_id"
        ),
        "colas": colas,
        "ejecutivos": (
            "SELECT * FROM reporting.datos_ejecutivos WHERE fecha BETWEEN :desde AND :hasta "
            'ORDER BY "unidad_planificaci\u00f3n", "rut", "fecha"'
        ),
        "estados_agentes": (
            f"SELECT {proyeccion_estados} FROM reporting.datos_estados_agentes "
            "WHERE fecha BETWEEN :desde_intervalos AND :hasta "
            "ORDER BY unidad_planificacion, rut, fecha, intervalo, id"
        ),
    }


def _normalizar_rut_estados(df: pl.DataFrame) -> pl.DataFrame:
    canonico = pl.col("rut").str.strip_chars().str.to_uppercase().str.replace_all("-", "")
    if df.filter(canonico.is_null() | ~canonico.str.contains(r"^[0-9]{1,8}[0-9K]$")).height:
        raise ValueError("Estados contiene RUT nulos o sin formato canonico valido.")
    return df.with_columns(canonico.str.replace(r"^([0-9]{1,8})([0-9K])$", "${1}-${2}").alias("rut"))


def _validar_frame(clave: str, df: pl.DataFrame, desde: date, hasta: date) -> None:
    if df["fecha"].null_count() or df.filter(~pl.col("fecha").is_between(desde, hasta)).height:
        raise ValueError(f"Fechas nulas o fuera del rango solicitado en {clave}.")
    claves = {
        "colas_diario": [("rendimiento_id",), ("fecha", "id_cola", "tipo_medios", "id_division")],
        "colas": [("fecha", "intervalo", "id_cola", "tipo_medios")],
        "ejecutivos": [("fecha", "rut")],
        "estados_agentes": [("id",), ("fecha", "id_agente", "intervalo"), ("fecha", "rut", "intervalo")],
    }
    for columnas in claves[clave]:
        duplicados = int(df.select(list(columnas)).is_duplicated().sum())
        if duplicados:
            raise ValueError(f"{duplicados} filas duplicadas por {','.join(columnas)} en {clave}.")
    if clave in ("colas", "estados_agentes"):
        intervalo = pl.col("intervalo")
        fuera = (intervalo.is_null() | ~intervalo.dt.minute().is_in([0, 30]) |
                 (intervalo.dt.second() != 0) | (intervalo.dt.nanosecond() != 0))
        if df.filter(fuera).height:
            raise ValueError(f"Intervalos fuera del raster de 30 minutos en {clave}.")
    if clave == "colas_diario":
        if df["campana_id"].null_count():
            raise ValueError("ColasDiario contiene filas sin campana.")
        duraciones = HORAS_COLAS_DIARIO
    elif clave == "estados_agentes":
        duraciones = ESTADOS
        # Conservar los excesos de duración de la fuente para alertarlos en Excel.
        # Una corrección posterior en Genesys se incorpora al reprocesar el ETL.
    else:
        duraciones = ()
    if duraciones and df.filter(pl.any_horizontal([pl.col(c) < 0 for c in duraciones])).height:
        raise ValueError(f"Duraciones negativas en {clave}.")


def cargar_fuentes_sql(
    desde: date,
    hasta: date,
    desde_intervalos: date,
    base_dir: str | Path | None = None,
    engine: Engine | None = None,
    hasta_turnos: date | None = None,
) -> dict[str, pl.DataFrame]:
    """Leer las fuentes SQL en un único snapshot de solo lectura.

    Los rangos incluyen ambos extremos. Diarios: DESDE..HASTA; detalle:
    DESDE_INTERVALOS..HASTA. No rellena intervalos ni sustituye NULL por cero.
    Si se proporciona engine, su ciclo de vida queda a cargo del caller.
    Asignaciones conserva su historial completo; Turnos termina en HASTA_TURNOS
    cuando se especifica ese horizonte, conservando todos sus timestamps.
    LicenciasVacaciones conserva su historial completo, sin filtros adicionales.
    """
    _validar_fechas(desde, hasta, desde_intervalos)
    fin_turnos = hasta if hasta_turnos is None else hasta_turnos
    if not isinstance(fin_turnos, date) or isinstance(fin_turnos, datetime):
        raise TypeError("HASTA_TURNOS debe ser datetime.date.")
    if fin_turnos < desde:
        raise ValueError("HASTA_TURNOS no puede preceder a DESDE.")
    propio = engine is None
    db = crear_engine(base_dir) if propio else engine
    parametros = {"desde": desde, "hasta": hasta, "desde_intervalos": desde_intervalos}
    resultado: dict[str, pl.DataFrame] = {}
    try:
        with db.connect() as conn, conn.begin():
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.exec_driver_sql("SET LOCAL statement_timeout = '120000ms'")
            ordenes = _validar_esquemas(conn)
            for clave, query in _consultas().items():
                config = _CONFIGURACIONES[clave]
                df = pl.read_database(
                    text(query), conn, infer_schema_length=None, batch_size=10_000,
                    schema_overrides=_tipos(config),
                    execute_options={"parameters": parametros},
                )
                if clave != "colas" and df.columns != ordenes[clave]:
                    raise ValueError(f"Cambio de orden de columnas en {clave}.")
                df = _tipar(df, config)
                if clave == "estados_agentes":
                    df = _normalizar_rut_estados(df)
                inicio = desde_intervalos if clave in ("colas", "estados_agentes") else desde
                _validar_frame(clave, df, inicio, hasta)
                resultado[clave] = df
            resultado.update(cargar_asignaciones_turnos(conn, desde, fin_turnos))
            resultado["licencias_vacaciones"] = cargar_licencias_vacaciones(conn)
    finally:
        if propio:
            db.dispose()
    return resultado
