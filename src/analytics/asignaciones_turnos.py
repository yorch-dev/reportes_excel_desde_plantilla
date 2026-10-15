"""Historial de asignaciones y turnos originales, sin expansión de filas."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

import polars as pl
from sqlalchemy import text

from .datos_reportes import _formatos, _tipos
from .numeros_exportacion import convertir_numeros


COLUMNAS_ASIGNACIONES_FUENTE = (
    "nombre", "rut", "campana", "contrato", "fecha_inicio", "fecha_termino",
)
# La vista entrega contrato varchar; sólo la copia exportada pasa a Int64.
TIPOS_ASIGNACIONES_FUENTE = {
    **{c: pl.String for c in COLUMNAS_ASIGNACIONES_FUENTE[:4]},
    "fecha_inicio": pl.Date, "fecha_termino": pl.Date,
}
TIMESTAMPS_TURNOS = (
    "inicio_turno", "fin_turno", "inicio_b1", "fin_b1", "inicio_almuerzo",
    "fin_almuerzo", "inicio_b2", "fin_b2",
)
COLUMNAS_TURNOS_FUENTE = ("id", "rut", "fecha", *TIMESTAMPS_TURNOS)
CONFIG_ASIGNACIONES = {
    "textos": {"columnas": ["nombre", "rut", "campana", "cliente", "jefe_negocio"],
               "tipo": pl.String, "formato": "@"},
    "numeros": {"columnas": ["contrato"], "tipo": pl.Int64, "formato": "0"},
    "fechas": {"columnas": ["fecha_inicio", "fecha_termino"], "tipo": pl.Date,
               "formato": "dd-mm-yyyy"},
    "ids": {"columnas": ["campana_id"], "tipo": pl.Int64, "formato": "0"},
}
CONFIG_TURNOS = {
    "ids": {"columnas": ["id", "campana_id"], "tipo": pl.Int64, "formato": "0"},
    "textos": {"columnas": ["rut", "nombre", "campana", "cliente",
                             "jefe_negocio", "estado_asignacion"],
               "tipo": pl.String, "formato": "@"},
    "numeros": {"columnas": ["contrato"], "tipo": pl.Int64, "formato": "0"},
    "fechas": {"columnas": ["fecha"], "tipo": pl.Date, "formato": "dd-mm-yyyy"},
    "timestamps": {"columnas": list(TIMESTAMPS_TURNOS), "tipo": pl.Datetime("us"),
                   "formato": "dd-mm-yyyy hh:mm:ss"},
}

QUERY_ASIGNACIONES = (
    "SELECT nombre, rut, campana, contrato, fecha_inicio, fecha_termino "
    "FROM reporting.historial_asignaciones WHERE campana IS NOT NULL "
    "ORDER BY nombre, fecha_inicio ASC NULLS FIRST, fecha_termino ASC NULLS LAST, campana, rut, contrato"
)
QUERY_TURNOS = (
    f"SELECT {', '.join(COLUMNAS_TURNOS_FUENTE)} FROM analytics_base.fact_turnos "
    "WHERE fecha BETWEEN :desde AND :hasta ORDER BY fecha, rut, id"
)
QUERY_CATALOGO = (
    "SELECT c.id AS campana_id, c.nombre AS campana, cl.nombre AS cliente, "
    "j.nombre AS jefe_negocio FROM analytics_base.dim_campana c "
    "LEFT JOIN analytics_base.dim_cliente cl ON cl.id = c.cliente_id "
    "LEFT JOIN analytics_base.dim_jefe_negocio j ON j.id = c.jefe_negocio_id "
    "ORDER BY c.nombre, c.id"
)


def formatos_asignaciones() -> dict[str, str]:
    return _formatos(CONFIG_ASIGNACIONES)


def formatos_turnos() -> dict[str, str]:
    return _formatos(CONFIG_TURNOS)


def _validar_rango(desde: date, hasta: date) -> None:
    if any(not isinstance(v, date) or isinstance(v, datetime) for v in (desde, hasta)):
        raise TypeError("Las fechas de Turnos deben ser datetime.date.")
    if desde > hasta:
        raise ValueError("Se requiere DESDE <= HASTA_TURNOS.")


def _tipar_fuente(df: pl.DataFrame, columnas: tuple, tipos: dict, fuente: str) -> pl.DataFrame:
    if df.columns != list(columnas):
        raise ValueError(f"Cambio de columnas u orden en {fuente}.")
    for c, esperado in tipos.items():
        actual = df.schema[c]
        if actual == pl.Null:
            continue
        if isinstance(esperado, pl.Datetime):
            if not isinstance(actual, pl.Datetime) or actual.time_zone is not None or actual.time_unit != "us":
                raise ValueError(f"Timestamp no naive o sin precisión de microsegundos en {fuente}.{c}.")
        elif esperado == pl.Int64:
            if not actual.is_integer():
                raise ValueError(f"Tipo no entero en {fuente}.{c}.")
        elif actual != esperado:
            raise ValueError(f"Tipo incompatible en {fuente}.{c}.")
    return df.cast(tipos, strict=True)


def _normalizar_rut(df: pl.DataFrame, fuente: str) -> pl.DataFrame:
    # Sólo cambia la presentación de la copia exportada; conserva ceros iniciales.
    rut = pl.col("rut").str.strip_chars().str.to_uppercase().str.replace_all(r"[.\s-]", "")
    if df.filter(rut.is_null() | ~rut.str.contains(r"^[0-9]{1,8}[0-9K]$")).height:
        raise ValueError(f"RUT nulos o inválidos en {fuente}.")
    return df.with_columns(rut.str.replace(r"^([0-9]{1,8})([0-9K])$", "${1}-${2}").alias("rut"))


def _validar_asignaciones(df: pl.DataFrame) -> pl.DataFrame:
    df = _tipar_fuente(df, COLUMNAS_ASIGNACIONES_FUENTE, TIPOS_ASIGNACIONES_FUENTE, "Asignaciones")
    if df["campana"].null_count():
        raise ValueError("Asignaciones contiene campañas NULL fuera del contrato de selección.")
    df = _normalizar_rut(df, "Asignaciones")
    if df.select(COLUMNAS_ASIGNACIONES_FUENTE).is_duplicated().any():
        raise ValueError("Asignaciones duplicadas por sus seis columnas.")
    if df.filter(pl.col("fecha_inicio") > pl.col("fecha_termino")).height:
        raise ValueError("Asignaciones contiene vigencias con fin anterior al inicio.")
    return convertir_numeros(df, ["contrato"], pl.Int64, "Asignaciones")


def _validar_turnos(df: pl.DataFrame, desde: date, hasta: date) -> pl.DataFrame:
    tipos = {"id": pl.Int64, "rut": pl.String, "fecha": pl.Date,
             **{c: pl.Datetime("us") for c in TIMESTAMPS_TURNOS}}
    df = _tipar_fuente(df, COLUMNAS_TURNOS_FUENTE, tipos, "Turnos")
    df = _normalizar_rut(df, "Turnos")
    if df.filter(pl.col("id").is_null() | (pl.col("id") <= 0)).height:
        raise ValueError("Turnos contiene ID nulos o no positivos.")
    if df["id"].is_duplicated().any() or df.select("rut", "fecha").is_duplicated().any():
        raise ValueError("Turnos duplicados por ID o RUT y fecha.")
    if df.filter(pl.col("fecha").is_null() | ~pl.col("fecha").is_between(desde, hasta)).height:
        raise ValueError("Turnos contiene fechas nulas o fuera del rango solicitado.")
    if df.filter(pl.col("inicio_turno").is_null() | (pl.col("fecha") != pl.col("inicio_turno").dt.date())).height:
        raise ValueError("La fecha de Turnos no coincide con la fecha de inicio_turno.")
    pares = (("inicio_turno", "fin_turno"), ("inicio_b1", "fin_b1"),
             ("inicio_almuerzo", "fin_almuerzo"), ("inicio_b2", "fin_b2"))
    for ini, fin in pares:
        if ini != "inicio_turno" and df.filter(pl.col(ini).is_null() != pl.col(fin).is_null()).height:
            raise ValueError(f"Pausa incompleta en Turnos: {ini}/{fin}.")
        if df.filter(pl.col(fin) < pl.col(ini)).height:
            raise ValueError(f"Fin anterior al inicio en Turnos: {ini}/{fin}.")
        if ini != "inicio_turno" and df.filter(
            (pl.col(ini) < pl.col("inicio_turno")) |
            (pl.col("fin_turno").is_not_null() & (pl.col(fin) > pl.col("fin_turno")))
        ).height:
            raise ValueError(f"Pausa fuera del turno en Turnos: {ini}/{fin}.")
    # fin_turno NULL se conserva: no se inventa un final ni se calcula duración.
    return df


def preparar_asignaciones_turnos(asignaciones: pl.DataFrame, turnos: pl.DataFrame,
                                 catalogo: pl.DataFrame, desde: date, hasta: date) -> dict[str, pl.DataFrame]:
    """Enriquecer sin multiplicar filas ni elegir entre coincidencias ambiguas."""
    _validar_rango(desde, hasta)
    asignaciones = _validar_asignaciones(asignaciones)
    turnos = _validar_turnos(turnos, desde, hasta)
    tipos_catalogo = {"campana_id": pl.Int64, "campana": pl.String,
                      "cliente": pl.String, "jefe_negocio": pl.String}
    catalogo = _tipar_fuente(catalogo, tuple(tipos_catalogo), tipos_catalogo, "Catálogo de campañas")
    if catalogo.filter(pl.col("campana_id").is_not_null() & (pl.col("campana_id") <= 0)).height:
        raise ValueError("Catálogo contiene ID de campaña no positivos.")
    por_campana = defaultdict(list)
    for registro in catalogo.iter_rows(named=True):
        if registro["campana"] is not None:
            por_campana[registro["campana"]].append(registro)
    mapa = {}
    for nombre in asignaciones["campana"].unique().to_list():
        candidatos = por_campana.get(nombre, [])
        if len(candidatos) > 1:
            meta, estado = {}, "campana_ambigua_en_catalogo"
        elif not candidatos:
            meta, estado = {}, "campana_sin_catalogo"
        elif candidatos[0]["campana_id"] is None:
            meta, estado = {}, "campana_sin_id"
        else:
            meta, estado = candidatos[0], "asignacion_vigente"
        mapa[nombre] = {c: meta.get(c) for c in ("campana_id", "cliente", "jefe_negocio")}, estado
    enriquecimiento = []
    por_rut = defaultdict(list)
    for registro in asignaciones.iter_rows(named=True):
        meta, estado = mapa[registro["campana"]]
        enriquecimiento.append(meta)
        por_rut[registro["rut"]].append((registro, meta, estado))
    esquema_dims = {"campana_id": pl.Int64, "cliente": pl.String, "jefe_negocio": pl.String}
    asignaciones_final = asignaciones.hstack(pl.DataFrame(enriquecimiento, schema=esquema_dims))
    metadatos_turnos = []
    for rut, fecha in turnos.select("rut", "fecha").iter_rows():
        vigentes = [a for a in por_rut.get(rut, [])
                    if (a[0]["fecha_inicio"] is None or a[0]["fecha_inicio"] <= fecha)
                    and (a[0]["fecha_termino"] is None or fecha <= a[0]["fecha_termino"])]
        if len(vigentes) == 1:
            registro, dims, estado = vigentes[0]
            meta = {c: registro[c] for c in ("nombre", "campana", "contrato")}
            meta.update(dims, estado_asignacion=estado)
        else:
            meta = {c: None for c in ("nombre", "campana", "contrato", *esquema_dims)}
            meta["estado_asignacion"] = "asignacion_ambigua" if vigentes else "sin_asignacion_vigente"
        metadatos_turnos.append(meta)
    esquema_meta = {"nombre": pl.String, "campana": pl.String, "contrato": pl.Int64,
                   **esquema_dims, "estado_asignacion": pl.String}
    turnos_final = turnos.hstack(pl.DataFrame(metadatos_turnos, schema=esquema_meta))
    if asignaciones_final.height != asignaciones.height or turnos_final.height != turnos.height:
        raise ValueError("El enriquecimiento alteró el número de filas fuente.")
    return {"asignaciones": asignaciones_final, "turnos": turnos_final}


def _validar_contratos_sql(conn) -> None:
    filas = conn.execute(text(
        "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
        "WHERE (table_schema = 'reporting' AND table_name = 'historial_asignaciones') OR "
        "(table_schema = 'analytics_base' AND table_name IN "
        "('fact_turnos', 'dim_campana', 'dim_cliente', 'dim_jefe_negocio')) "
        "ORDER BY table_schema, table_name, ordinal_position"
    )).mappings()
    esquemas = defaultdict(dict)
    for fila in filas:
        esquemas[fila["table_schema"], fila["table_name"]][fila["column_name"]] = fila["data_type"]
    strings = {"text", "character varying", "character"}
    integers = {"smallint", "integer", "bigint"}
    contratos = {
        ("reporting", "historial_asignaciones"): {
            **{c: strings for c in COLUMNAS_ASIGNACIONES_FUENTE[:4]},
            "fecha_inicio": {"date"}, "fecha_termino": {"date"}},
        ("analytics_base", "fact_turnos"): {
            "id": integers, "rut": strings, "fecha": {"date"},
            **{c: {"timestamp without time zone"} for c in TIMESTAMPS_TURNOS}},
        ("analytics_base", "dim_campana"): {"id": integers, "nombre": strings,
            "cliente_id": integers, "jefe_negocio_id": integers},
        ("analytics_base", "dim_cliente"): {"id": integers, "nombre": strings},
        ("analytics_base", "dim_jefe_negocio"): {"id": integers, "nombre": strings},
    }
    for fuente, columnas in contratos.items():
        observado = esquemas[fuente]
        if not set(columnas) <= set(observado) or (fuente[1] == "fact_turnos" and list(observado) != list(COLUMNAS_TURNOS_FUENTE)):
            raise ValueError(f"Cambio de columnas en {'.'.join(fuente)}.")
        for columna, tipos in columnas.items():
            if observado[columna] not in tipos:
                raise ValueError(f"Tipo SQL incompatible en {'.'.join(fuente)}.{columna}.")


def cargar_asignaciones_turnos(conn, desde: date, hasta: date) -> dict[str, pl.DataFrame]:
    """Leer el historial completo y los turnos acotados en la conexión del caller."""
    _validar_rango(desde, hasta)
    _validar_contratos_sql(conn)
    tipos_asig = dict(TIPOS_ASIGNACIONES_FUENTE)
    tipos_turnos = {c: _tipos(CONFIG_TURNOS)[c] for c in COLUMNAS_TURNOS_FUENTE}
    asignaciones = pl.read_database(text(QUERY_ASIGNACIONES), conn, infer_schema_length=None,
        batch_size=10_000, schema_overrides=tipos_asig)
    turnos = pl.read_database(text(QUERY_TURNOS), conn, infer_schema_length=None,
        batch_size=10_000, schema_overrides=tipos_turnos,
        execute_options={"parameters": {"desde": desde, "hasta": hasta}})
    catalogo = pl.read_database(text(QUERY_CATALOGO), conn, infer_schema_length=None,
        schema_overrides={"campana_id": pl.Int64, "campana": pl.String,
                          "cliente": pl.String, "jefe_negocio": pl.String})
    return preparar_asignaciones_turnos(asignaciones, turnos, catalogo, desde, hasta)


__all__ = ["CONFIG_ASIGNACIONES", "CONFIG_TURNOS", "TIPOS_ASIGNACIONES_FUENTE", "formatos_asignaciones", "formatos_turnos",
           "cargar_asignaciones_turnos", "preparar_asignaciones_turnos"]
