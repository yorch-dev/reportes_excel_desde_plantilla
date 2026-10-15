"""Conversión explícita de números almacenados como texto en las fuentes SQL."""
from __future__ import annotations

from collections.abc import Sequence

import polars as pl


def convertir_numeros(df: pl.DataFrame, columnas: Sequence[str], tipo: pl.DataType,
                     fuente: str) -> pl.DataFrame:
    """Convertir enteros sin truncar, rellenar nulos ni exceder precisión Excel."""
    if tipo != pl.Int64:
        raise ValueError("Esta conversión requiere Int64.")
    expresiones = []
    for columna in columnas:
        actual = df.schema[columna]
        valor = pl.col(columna)
        if actual == pl.String:
            valor = valor.str.strip_chars()
            if df.filter(valor.is_not_null() & ~valor.str.contains(r"^[+-]?[0-9]+$")).height:
                raise ValueError(f"{fuente}.{columna}: valores de texto que no son enteros.")
        elif actual != pl.Null and not actual.is_integer():
            raise ValueError(f"{fuente}.{columna}: tipo incompatible para un entero.")
        expresiones.append(valor.cast(pl.Int64, strict=True).alias(columna))
    try:
        resultado = df.with_columns(expresiones)
    except pl.exceptions.PolarsError as error:
        raise ValueError(f"{fuente}: números fuera del rango entero en {', '.join(columnas)}.") from error
    for columna in columnas:
        if resultado.filter((pl.col(columna) < -999_999_999_999_999) |
                            (pl.col(columna) > 999_999_999_999_999)).height:
            raise ValueError(f"{fuente}.{columna}: entero excede la precisión de 15 dígitos de Excel.")
    return resultado
