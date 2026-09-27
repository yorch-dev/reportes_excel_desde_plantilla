"""Genera un libro por campaña a partir de la plantilla reporte_colas.xlsx."""

from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
import unicodedata

import polars as pl
import xlwings as xw

from db_conn import conn_db_wf


BASE_DIR = Path(__file__).resolve().parent
PLANTILLA = BASE_DIR / "archivos" / "reporte_colas.xlsx"
CARPETA_SALIDA = BASE_DIR / "archivos" / "archivos_por_campana"
TAMANO_BLOQUE = 10_000
MAX_FILAS_EXCEL = 1_048_576

QUERY = """
    SELECT fecha, intervalo, tipo_medios, id_cola, nombre_cola,
        nombre_cola_dimension AS nombre_cola_2, oferta, contestadas, abandonadas,
        saliente, flujo_saliente, velocidad_media_respuesta_seg,
        porcentaje_nivel_servicio, porcentaje_objetivo_nivel_servicio,
        espera_media_seg, manejo_medio_seg, conversacion_media_seg,
        retencion_media_seg, acw_medio_seg, abandono_medio_seg, manejo, retener,
        transferir, tiempo_manejo_total, cumplen_sla, tiempo_conversacion_total,
        tiempo_retencion_total, tiempo_acw_total, abandono_rapido,
        salientes_intentadas, nombre_campana, tmo_objetivo,
        nivel_atencion_objetivo, nivel_servicio_objetivo,
        tiempo_maximo_espera_objetivo, tipo_campana, cliente, unidad_negocio,
        jefe_negocio, supervisores
    FROM reporting.datos_colas
    WHERE plataforma_operacion = 'Genesys'
    ORDER BY fecha, intervalo, nombre_campana, nombre_cola
"""

config_columnas = {
    "fechas": {
        "columnas": ["fecha"],
        "tipo": pl.Date,
        "formato": "dd-mm-yyyy",
    },
    "horas": {
        "columnas": ["intervalo"],
        "tipo": pl.Time,
        "formato": "hh:mm",
    },
    "textos": {
        "columnas": [
            "tipo_medios", "id_cola", "nombre_cola", "nombre_cola_2",
            "nombre_campana", "tipo_campana", "cliente", "unidad_negocio",
            "jefe_negocio", "supervisores",
        ],
        "tipo": pl.String,
        "formato": "@",
    },
    "enteros": {
        "columnas": [
            "oferta", "contestadas", "abandonadas", "saliente", "flujo_saliente",
            "manejo", "retener", "transferir", "cumplen_sla", "abandono_rapido",
            "salientes_intentadas", "tmo_objetivo", "tiempo_maximo_espera_objetivo",
        ],
        "tipo": pl.Int64,
        "formato": "#,##0",
    },
    "decimales": {
        "columnas": [
            "velocidad_media_respuesta_seg", "espera_media_seg", "manejo_medio_seg",
            "conversacion_media_seg", "retencion_media_seg", "acw_medio_seg",
            "abandono_medio_seg",
        ],
        "tipo": pl.Float64,
        "formato": "#,##0.00",
    },
    "porcentajes": {
        "columnas": [
            "porcentaje_nivel_servicio", "porcentaje_objetivo_nivel_servicio",
            "nivel_atencion_objetivo", "nivel_servicio_objetivo",
        ],
        "tipo": pl.Float64,
        "formato": "0.00%",
    },
    "duraciones": {
        "columnas": [
            "tiempo_manejo_total", "tiempo_conversacion_total",
            "tiempo_retencion_total", "tiempo_acw_total",
        ],
        "tipo": pl.Float64,
        "formato": "[h]:mm:ss",
    },
}


def cargar_datos() -> pl.DataFrame:
    """Consulta una sola vez y cierra la conexión antes de trabajar con Excel."""
    engine, _ = conn_db_wf()
    try:
        with engine.connect() as conn:
            return pl.read_database(
                QUERY, conn, infer_schema_length=None, batch_size=TAMANO_BLOQUE
            )
    finally:
        engine.dispose()


def preparar_para_excel(df: pl.DataFrame) -> pl.DataFrame:
    columnas = [
        columna
        for grupo in config_columnas.values()
        for columna in grupo["columnas"]
    ]
    if len(columnas) != len(set(columnas)):
        raise ValueError("Hay columnas repetidas en config_columnas.")

    sin_configurar = sorted(set(df.columns) - set(columnas))
    fuera_de_query = sorted(set(columnas) - set(df.columns))
    if sin_configurar or fuera_de_query:
        raise ValueError(
            f"Columnas sin configurar: {sin_configurar}; "
            f"columnas fuera de la query: {fuera_de_query}"
        )

    tipos_esperados = {
        columna: grupo["tipo"]
        for grupo in config_columnas.values()
        for columna in grupo["columnas"]
    }
    df = df.cast(tipos_esperados)

    # La vista SQL entrega horas decimales; Excel necesita fracciones de día.
    # No redondear: el formato de las celdas controla la presentación.
    return df.with_columns(
        [
            pl.col(col).dt.strftime("%H:%M:%S").alias(col)
            for col, dtype in df.schema.items()
            if dtype == pl.Time
        ]
        + [
            (pl.col(col) / 24).alias(col)
            for col in config_columnas["duraciones"]["columnas"]
        ]
    )


def nombre_archivo(campana: str | None) -> str:
    """Normaliza únicamente el archivo; los nombres en los datos no cambian."""
    if campana is None or not campana.strip():
        nombre = "SinCampana"
    else:
        sin_acentos = "".join(
            letra
            for letra in unicodedata.normalize("NFKD", campana)
            if not unicodedata.combining(letra)
        )
        palabras = re.findall(r"[A-Za-z0-9]+", sin_acentos)
        if not palabras:
            raise ValueError(f"La campaña {campana!r} no produce un nombre válido.")
        nombre = "".join(palabra.capitalize() for palabra in palabras)

    archivo = f"reporte_colas_{nombre}.xlsx"
    if len(archivo) > 255:
        raise ValueError(f"El nombre de archivo de {campana!r} es demasiado largo.")
    return archivo


def actualizar_tabla(libro: xw.Book, df_excel: pl.DataFrame) -> None:
    """Reutiliza t_datos, limpiando todo el cuerpo anterior antes de reducirlo."""
    hoja = libro.sheets["Datos"]
    tabla = hoja.tables["t_datos"]
    if tabla.header_row_range is None:
        raise ValueError("La plantilla debe mostrar los encabezados de t_datos.")
    encabezados = tabla.header_row_range.value
    if encabezados != df_excel.columns:
        raise ValueError(
            "Los encabezados y su orden en t_datos deben coincidir con la query."
        )
    if tabla.show_totals:
        raise ValueError("t_datos debe ser una tabla de datos sin fila de totales.")
    if df_excel.is_empty():
        raise ValueError("No hay datos para generar este reporte.")

    primera_fila = tabla.header_row_range.row
    primera_columna = tabla.header_row_range.column
    ultima_fila = primera_fila + df_excel.height
    ultima_columna = primera_columna + df_excel.width - 1
    if ultima_fila > MAX_FILAS_EXCEL:
        raise ValueError("La campaña supera el límite de filas de Excel.")

    # También se limpian las filas que quedarán fuera de la tabla redimensionada.
    if tabla.data_body_range is not None:
        tabla.data_body_range.clear_contents()
    if hoja.api.FilterMode:
        hoja.api.ShowAllData()
    tabla.resize(
        hoja.range((primera_fila, primera_columna), (ultima_fila, ultima_columna))
    )

    separadores = str.maketrans({
        ".": libro.app.api.DecimalSeparator,
        ",": libro.app.api.ThousandsSeparator,
    })
    for grupo in config_columnas.values():
        for columna in grupo["columnas"]:
            indice = primera_columna + df_excel.columns.index(columna)
            hoja.range(
                (primera_fila + 1, indice), (ultima_fila, indice)
            ).number_format = grupo["formato"].translate(separadores)

    for inicio in range(0, df_excel.height, TAMANO_BLOQUE):
        bloque = df_excel.slice(inicio, TAMANO_BLOQUE)
        hoja.range((primera_fila + 1 + inicio, primera_columna)).value = bloque.rows()


def generar_reporte(
    app: xw.App, df_excel: pl.DataFrame, destino: Path, plantilla: Path
) -> None:
    # La copia temporal está en el mismo disco que el destino. Un fallo deja
    # intactos tanto la plantilla como el reporte anterior de esta campaña.
    with TemporaryDirectory(prefix=".reporte_", dir=destino.parent) as carpeta:
        temporal = Path(carpeta) / destino.name
        shutil.copy2(plantilla, temporal)
        libro = app.books.open(str(temporal), update_links=False)
        try:
            app.calculation = "automatic"
            with app.properties(calculation="manual"):
                actualizar_tabla(libro, df_excel)

            # Guardar después de restaurar automático, con las fórmulas calculadas.
            app.calculate()
            libro.save()
        finally:
            libro.close()

        temporal.replace(destino)


def main() -> None:
    if not PLANTILLA.is_file():
        raise FileNotFoundError(f"No existe la plantilla: {PLANTILLA}")

    print("Consultando datos de Genesys...", flush=True)
    df_excel = preparar_para_excel(cargar_datos())
    if df_excel.is_empty():
        print("La consulta no devolvió filas; no se generaron archivos.")
        return

    # Nulos y textos vacíos comparten archivo, conservando sus valores originales.
    campanas = sorted(
        {
            nombre if nombre is not None and nombre.strip() else None
            for nombre in df_excel["nombre_campana"].unique().to_list()
        },
        key=lambda nombre: (nombre is None, nombre or ""),
    )
    destinos = {}
    usados = {}
    for campana in campanas:
        archivo = nombre_archivo(campana)
        clave = archivo.casefold()
        if clave in usados:
            raise ValueError(
                f"Las campañas {usados[clave]!r} y {campana!r} generan "
                f"el mismo archivo: {archivo}"
            )
        usados[clave] = campana
        destinos[campana] = CARPETA_SALIDA / archivo

    CARPETA_SALIDA.mkdir(parents=True, exist_ok=True)
    print(f"{df_excel.height:,} filas; {len(campanas)} campañas.", flush=True)
    with xw.App(visible=False, add_book=False) as app:
        with app.properties(screen_updating=False, display_alerts=False):
            for numero, campana in enumerate(campanas, 1):
                columna = pl.col("nombre_campana")
                condicion = (
                    columna.is_null() | (columna.str.strip_chars() == "")
                    if campana is None
                    else columna == campana
                )
                datos = df_excel.filter(condicion)
                destino = destinos[campana]
                print(
                    f"[{numero}/{len(campanas)}] {destino.name}: {datos.height:,} filas",
                    flush=True,
                )
                try:
                    generar_reporte(app, datos, destino, PLANTILLA)
                except Exception as error:
                    raise RuntimeError(
                        f"No se pudo generar el reporte de {campana!r}. "
                        "El archivo anterior, si existía, no se reemplazó."
                    ) from error

    print(f"Reportes guardados en {CARPETA_SALIDA}", flush=True)


if __name__ == "__main__":
    main()
