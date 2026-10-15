"""Licencias, vacaciones y días administrativos: historial y tramos originales."""
from __future__ import annotations

from collections import defaultdict

import polars as pl
from sqlalchemy import text

from .asignaciones_turnos import QUERY_CATALOGO, _tipar_fuente
from .datos_reportes import _formatos, _tipos
from .numeros_exportacion import convertir_numeros


COLUMNAS_FUENTE = (
    "campana", "person_id_pw", "rut", "nombre_ejecutivo", "id_ausencia_buk",
    "id_empleado_buk", "tipo", "estado_fuente", "tramo_jornada",
    "fecha_inicio_en_unidad", "fecha_termino_en_unidad", "dias_calendario_tramo_unidad",
    "fecha_inicio_ausencia", "fecha_termino_ausencia", "dias_calendario_total_ausencia_buk",
)
FECHAS = (
    "fecha_inicio_en_unidad", "fecha_termino_en_unidad",
    "fecha_inicio_ausencia", "fecha_termino_ausencia",
)
IDS_NUMERICOS = ("person_id_pw", "id_ausencia_buk", "id_empleado_buk")
CONFIG_LICENCIAS_VACACIONES = {
    "textos": {"columnas": [*[c for c in COLUMNAS_FUENTE[:9] if c not in IDS_NUMERICOS],
                             "cliente", "jefe_negocio"],
               "tipo": pl.String, "formato": "@"},
    "fechas": {"columnas": list(FECHAS), "tipo": pl.Date, "formato": "dd-mm-yyyy"},
    "enteros": {"columnas": [*IDS_NUMERICOS, "dias_calendario_tramo_unidad", "campana_id"],
                "tipo": pl.Int64, "formato": "0"},
    "decimales": {"columnas": ["dias_calendario_total_ausencia_buk"],
                  "tipo": pl.Float64, "formato": "0.00"},
}
# Los tres IDs siguen siendo varchar en SQL. Leerlos como texto permite validar
# el contenido antes de convertirlo; el contrato de exportación es Int64.
TIPOS_FUENTE_SQL = {c: pl.String if c in IDS_NUMERICOS else _tipos(CONFIG_LICENCIAS_VACACIONES)[c]
                    for c in COLUMNAS_FUENTE}
QUERY_LICENCIAS_VACACIONES = (
    f"SELECT {', '.join(COLUMNAS_FUENTE)} FROM reporting.licencias_vacaciones "
    "ORDER BY campana, nombre_ejecutivo, fecha_inicio_en_unidad, fecha_termino_en_unidad, "
    "id_ausencia_buk, id_empleado_buk, person_id_pw, rut"
)


def formatos_licencias_vacaciones() -> dict[str, str]:
    return _formatos(CONFIG_LICENCIAS_VACACIONES)


def _validar_fuente(df: pl.DataFrame) -> pl.DataFrame:
    if df.columns != list(COLUMNAS_FUENTE):
        raise ValueError("Cambio de columnas u orden en LicenciasVacaciones.")
    df = convertir_numeros(df, IDS_NUMERICOS, pl.Int64, "LicenciasVacaciones")
    tipos = {c: _tipos(CONFIG_LICENCIAS_VACACIONES)[c] for c in COLUMNAS_FUENTE}
    for c, esperado in tipos.items():
        actual = df.schema[c]
        if actual == pl.Null:
            continue
        if esperado == pl.Int64:
            valido = actual.is_integer()
        elif esperado == pl.Float64:
            valido = actual.is_numeric()
        else:
            valido = actual == esperado
        if not valido:
            raise ValueError(f"Tipo incompatible en LicenciasVacaciones.{c}.")
    df = df.cast(tipos, strict=True)
    # NULL permanece NULL; sólo se normaliza la presentación de RUT conocidos.
    rut = pl.col("rut").str.strip_chars().str.to_uppercase().str.replace_all(r"[.\s-]", "")
    if df.filter(rut.is_not_null() & ~rut.str.contains(r"^[0-9]{1,8}[0-9K]$")).height:
        raise ValueError("RUT conocidos sin formato válido en LicenciasVacaciones.")
    df = df.with_columns(rut.str.replace(r"^([0-9]{1,8})([0-9K])$", "${1}-${2}").alias("rut"))
    if df.select(COLUMNAS_FUENTE).is_duplicated().any():
        raise ValueError("Filas duplicadas por las quince columnas de LicenciasVacaciones.")
    tramo_inicio, tramo_fin = pl.col(FECHAS[0]), pl.col(FECHAS[1])
    ausencia_inicio, ausencia_fin = pl.col(FECHAS[2]), pl.col(FECHAS[3])
    if df.filter((tramo_inicio > tramo_fin) | (ausencia_inicio > ausencia_fin)).height:
        raise ValueError("Fechas invertidas en LicenciasVacaciones.")
    if df.filter(pl.any_horizontal([
        tramo_inicio < ausencia_inicio, tramo_inicio > ausencia_fin,
        tramo_fin < ausencia_inicio, tramo_fin > ausencia_fin,
    ])).height:
        raise ValueError("Tramos fuera de la ausencia en LicenciasVacaciones.")
    dias_tramo = pl.col("dias_calendario_tramo_unidad")
    dias_total = pl.col("dias_calendario_total_ausencia_buk")
    if df.filter((dias_tramo < 0) | (dias_total < 0) |
                 (dias_total.is_not_null() & ~dias_total.is_finite())).height:
        raise ValueError("Días negativos o no finitos en LicenciasVacaciones.")
    completo = tramo_inicio.is_not_null() & tramo_fin.is_not_null() & dias_tramo.is_not_null()
    if df.filter(completo & (dias_tramo != (tramo_fin - tramo_inicio).dt.total_days() + 1)).height:
        raise ValueError("Días del tramo no coinciden con las fechas inclusivas en LicenciasVacaciones.")
    return df


def preparar_licencias_vacaciones(df: pl.DataFrame, catalogo: pl.DataFrame) -> pl.DataFrame:
    """Añadir tres dimensiones exactas, conservando todos los tramos y las filas."""
    df = _validar_fuente(df)
    tipos_catalogo = {"campana_id": pl.Int64, "campana": pl.String,
                      "cliente": pl.String, "jefe_negocio": pl.String}
    catalogo = _tipar_fuente(catalogo, tuple(tipos_catalogo), tipos_catalogo, "Catálogo de campañas")
    if catalogo.filter(pl.col("campana_id").is_not_null() & (pl.col("campana_id") <= 0)).height:
        raise ValueError("Catálogo contiene ID de campaña no positivos.")
    por_campana = defaultdict(list)
    for registro in catalogo.iter_rows(named=True):
        if registro["campana"] is not None:
            por_campana[registro["campana"]].append(registro)
    esquema_dims = {"campana_id": pl.Int64, "cliente": pl.String, "jefe_negocio": pl.String}
    mapa = {}
    for campana in df["campana"].unique().to_list():
        candidatos = por_campana.get(campana, [])
        valido = len(candidatos) == 1 and candidatos[0]["campana_id"] is not None
        mapa[campana] = {c: candidatos[0][c] if valido else None for c in esquema_dims}
    dimensiones = [mapa[campana] for campana in df["campana"].to_list()]
    resultado = df.hstack(pl.DataFrame(dimensiones, schema=esquema_dims))
    if resultado.height != df.height or resultado.columns[:15] != list(COLUMNAS_FUENTE):
        raise ValueError("El catálogo alteró la conservación de LicenciasVacaciones.")
    return resultado


def _validar_contrato_sql(conn) -> None:
    filas = conn.execute(text(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'reporting' AND table_name = 'licencias_vacaciones' "
        "ORDER BY ordinal_position"
    )).mappings()
    esquema = {f["column_name"]: f["data_type"] for f in filas}
    if set(esquema) != set(COLUMNAS_FUENTE):
        raise ValueError("Cambio de columnas en reporting.licencias_vacaciones.")
    strings = {"text", "character varying", "character"}
    for columna in COLUMNAS_FUENTE:
        if columna in COLUMNAS_FUENTE[:9]:
            aceptados = strings
        elif columna in FECHAS:
            aceptados = {"date"}
        elif columna == "dias_calendario_tramo_unidad":
            aceptados = {"smallint", "integer", "bigint"}
        else:
            aceptados = {"numeric", "double precision", "real"}
        if esquema[columna] not in aceptados:
            raise ValueError(f"Tipo SQL incompatible en licencias_vacaciones.{columna}.")


def cargar_licencias_vacaciones(conn) -> pl.DataFrame:
    """Leer la vista completa y el catálogo en el snapshot de la conexión recibida."""
    _validar_contrato_sql(conn)
    df = pl.read_database(text(QUERY_LICENCIAS_VACACIONES), conn,
        infer_schema_length=None, batch_size=10_000, schema_overrides=TIPOS_FUENTE_SQL)
    catalogo = pl.read_database(text(QUERY_CATALOGO), conn, infer_schema_length=None,
        schema_overrides={"campana_id": pl.Int64, "campana": pl.String,
                          "cliente": pl.String, "jefe_negocio": pl.String})
    return preparar_licencias_vacaciones(df, catalogo)


__all__ = ["CONFIG_LICENCIAS_VACACIONES", "formatos_licencias_vacaciones",
           "cargar_licencias_vacaciones", "preparar_licencias_vacaciones"]
