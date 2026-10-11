"""Cortes por dimensiones usando el mapeo explícito campaña–Peopleware."""

from collections import Counter, defaultdict
from dataclasses import dataclass
import re
import unicodedata
from typing import Mapping

import polars as pl
from sqlalchemy import text
from sqlalchemy.engine import Engine


CRITERIOS = frozenset({"cliente", "jefe_negocio"})
FUENTES = ("colas_diario", "colas", "workloads_diario", "workloads", "ejecutivos", "estados_agentes")
_FUENTES_ANTERIORES = ("colas", "ejecutivos", "workloads")
_FUENTES_COLAS = ("colas_diario", "colas")
_FUENTES_WORKLOADS = ("workloads_diario", "workloads")
UNIDAD_EJECUTIVOS = "unidad_planificación"
UNIDAD_ESTADOS = "unidad_planificacion"
QUERY_MAPA = text("""
    SELECT m.id_pw, m.nombre_fuente AS unidad_planificacion,
           m.campana_id, c.nombre AS nombre_campana,
           cl.nombre AS cliente, j.nombre AS jefe_negocio
    FROM analytics_base.map_campana_pw AS m
    LEFT JOIN analytics_base.dim_campana AS c ON c.id = m.campana_id
    LEFT JOIN analytics_base.dim_cliente AS cl ON cl.id = c.cliente_id
    LEFT JOIN analytics_base.dim_jefe_negocio AS j ON j.id = c.jefe_negocio_id
    ORDER BY m.id_pw, m.id
""")
SCHEMA_DIAGNOSTICOS = {
    "fuente": pl.String,
    "motivo": pl.String,
    "unidad_planificacion": pl.String,
    "planning_unit_id": pl.Int64,
    "criterio": pl.String,
    "valor": pl.String,
    "filas": pl.Int64,
    "excluidas": pl.Boolean,
    "detalle": pl.String,
}


@dataclass(frozen=True)
class Corte:
    nombre: str
    datos: dict[str, pl.DataFrame]
    nombre_archivo: str


@dataclass(frozen=True)
class PlanCortes:
    """Propietarios calculados una vez; los hechos se filtran bajo demanda."""

    nombres: list[str]
    diagnosticos: pl.DataFrame
    _datos: dict[str, pl.DataFrame]
    _etiquetas: dict[str, pl.Series]
    _archivos: dict[str, str]

    def _validar_nombre(self, nombre: str) -> None:
        if nombre not in self._archivos:
            raise ValueError(f"Corte no disponible: {nombre}.")

    def filas(self, nombre: str) -> dict[str, int]:
        """Cuenta filas atribuibles sin materializar DataFrames filtrados."""
        self._validar_nombre(nombre)
        return {fuente: int((etiquetas == nombre).sum())
                for fuente, etiquetas in self._etiquetas.items()}

    def obtener(self, nombre: str) -> Corte:
        """Materializa únicamente las fuentes del grupo solicitado."""
        self._validar_nombre(nombre)
        datos = {fuente: df.filter(self._etiquetas[fuente] == nombre)
                 for fuente, df in self._datos.items()}
        for fuente, df in datos.items():
            if df.schema != self._datos[fuente].schema:
                raise ValueError(f"El corte cambió tipos o columnas de {fuente}.")
        return Corte(nombre, datos, self._archivos[nombre])


def cargar_mapa_campanas(engine: Engine) -> pl.DataFrame:
    """Lee el mapa existente; el llamador conserva la propiedad del engine."""
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        df = pl.read_database(QUERY_MAPA, conn, infer_schema_length=None)
    return df.select(
        pl.col("id_pw").cast(pl.Int64, strict=True).alias("planning_unit_id"),
        pl.col("unidad_planificacion").cast(pl.String, strict=True),
        pl.col("campana_id").cast(pl.Int64, strict=True),
        pl.col("nombre_campana").cast(pl.String, strict=True),
        pl.col("cliente").cast(pl.String, strict=True),
        pl.col("jefe_negocio").cast(pl.String, strict=True),
    )


def nombre_archivo_corte(criterio: str, nombre: str) -> str:
    """Normaliza sólo el archivo; el valor original de la dimensión se conserva."""
    if criterio not in CRITERIOS:
        raise ValueError(f"Criterio no admitido: {criterio}.")
    texto = unicodedata.normalize("NFKD", nombre)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    slug = "_".join(re.findall(r"[a-z0-9]+", texto.casefold()))
    if not slug:
        raise ValueError("Una dimensión no produce un nombre de archivo válido.")
    prefijo = "reporte_por_cliente" if criterio == "cliente" else "reporte_por_jefe_campana"
    archivo = f"{prefijo}_{slug}.xlsx"
    if len(archivo) > 255:
        raise ValueError("Un nombre de archivo de corte supera los 255 caracteres.")
    return archivo


def _columnas(df: pl.DataFrame, columnas: set[str], fuente: str) -> None:
    faltantes = columnas - set(df.columns)
    if faltantes:
        raise ValueError(f"Faltan columnas en {fuente}: {sorted(faltantes)}.")


def _etiqueta(valor: object) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ValueError("Las etiquetas de dimensiones y unidades deben ser texto.")
    return valor if valor.strip() else None


def _id_valido(valor: object) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool) and 0 < valor < 2**63


def preparar_plan_cortes(
    datos: Mapping[str, pl.DataFrame], mapa: pl.DataFrame, criterio: str
) -> PlanCortes:
    """Atribuye filas sin copiar, unir ni duplicar los hechos.

    Las dos tablas de Colas usan su propia dimensión y las dos de Workloads
    usan planning_unit_id. Ejecutivos y Estados necesitan un nombre exacto
    que identifique un solo ID del mapa. La propiedad de una unidad se evalúa
    contra todo el mapa, incluso campañas sin Colas en el periodo. Unidades
    ambiguas o sin dimensión se excluyen con diagnóstico. También se admite
    la entrada anterior de tres fuentes.
    """
    if criterio not in CRITERIOS:
        raise ValueError(f"Criterio no admitido: {criterio}.")
    if set(datos) == set(FUENTES):
        fuentes = FUENTES
    elif set(datos) == set(_FUENTES_ANTERIORES):
        fuentes = _FUENTES_ANTERIORES
    else:
        raise ValueError("Se requieren las seis fuentes, o los DataFrames anteriores colas, ejecutivos y workloads.")
    if not all(isinstance(x, pl.DataFrame) for x in datos.values()):
        raise ValueError("Todas las fuentes deben ser DataFrames.")
    _columnas(mapa, {"planning_unit_id", "unidad_planificacion", "nombre_campana", *CRITERIOS}, "mapa")
    for fuente in _FUENTES_COLAS:
        if fuente in datos:
            _columnas(datos[fuente], {"nombre_campana", criterio}, fuente)
    for fuente in _FUENTES_WORKLOADS:
        if fuente in datos:
            _columnas(datos[fuente], {"planning_unit_id", "unidad_planificacion"}, fuente)
    unidades_por_fuente = {"ejecutivos": UNIDAD_EJECUTIVOS}
    if "estados_agentes" in datos:
        unidades_por_fuente["estados_agentes"] = UNIDAD_ESTADOS
    for fuente, columna_unidad in unidades_por_fuente.items():
        _columnas(datos[fuente], {columna_unidad} | ({criterio} if criterio == "jefe_negocio" else set()), fuente)

    diagnosticos: Counter = Counter()

    def avisar(fuente, motivo, unidad=None, uid=None, valor=None, detalle="", excluidas=False):
        diagnosticos[(fuente, motivo, unidad, uid, valor, excluidas, detalle)] += 1

    por_id: dict[int, list[dict]] = defaultdict(list)
    por_nombre: dict[str, set[int | None]] = defaultdict(set)
    por_campana: dict[str, set[str | None]] = defaultdict(set)
    filas_mapa = list(mapa.iter_rows(named=True))
    for fila in filas_mapa:
        uid = fila["planning_unit_id"]
        unidad = _etiqueta(fila["unidad_planificacion"])
        campana = _etiqueta(fila["nombre_campana"])
        valor = _etiqueta(fila[criterio])
        if unidad is not None:
            por_nombre[unidad].add(uid if _id_valido(uid) else None)
        if campana is not None:
            por_campana[campana].add(valor)
        if not _id_valido(uid):
            avisar("mapa", "id_unidad_invalido", unidad=unidad, valor=valor)
            continue
        por_id[uid].append({"unidad": unidad, "campana": campana, "valor": valor})

    # Un desacuerdo entre las dimensiones de Colas y las del mapa no cambia
    # la atribución explícita de Colas, pero bloquea las unidades de esa campaña.
    campanas_inconsistentes = set()
    dueños: dict[str, list[str | None]] = {fuente: [] for fuente in fuentes}
    for fuente in _FUENTES_COLAS:
        if fuente not in datos:
            continue
        for campana_raw, valor_raw in datos[fuente].select("nombre_campana", criterio).iter_rows():
            campana, valor = _etiqueta(campana_raw), _etiqueta(valor_raw)
            dueños[fuente].append(valor)
            if valor is None:
                avisar(fuente, "dimension_sin_valor", detalle=campana or "", excluidas=True)
            if campana not in por_campana:
                avisar(fuente, "campana_sin_mapeo_peopleware", valor=valor, detalle=campana or "")
            elif valor not in por_campana[campana]:
                campanas_inconsistentes.add(campana)
                avisar(fuente, "dimension_distinta_del_mapa", valor=valor, detalle=campana)

    propietarios: dict[int, tuple[str | None, str]] = {}
    for uid, filas in por_id.items():
        valores = {x["valor"] for x in filas if x["valor"] is not None}
        incompleto = any(x["valor"] is None or x["campana"] is None for x in filas)
        inconsistente = any(x["campana"] in campanas_inconsistentes for x in filas)
        if len(valores) > 1:
            propietario, motivo = None, "unidad_con_varios_propietarios"
        elif inconsistente:
            propietario, motivo = None, "campana_con_dimension_inconsistente"
        elif not valores:
            propietario, motivo = None, "unidad_sin_dimension"
        elif incompleto:
            propietario, motivo = None, "unidad_con_dimension_incompleta"
        else:
            propietario, motivo = next(iter(valores)), ""
        propietarios[uid] = propietario, motivo
        if motivo:
            avisar("mapa", motivo, uid=uid, detalle="; ".join(sorted(valores)))
    for unidad, ids in por_nombre.items():
        if len(ids) > 1:
            avisar("mapa", "nombre_unidad_con_varios_ids", unidad=unidad)

    for fuente in _FUENTES_WORKLOADS:
        if fuente not in datos:
            continue
        for uid, unidad_raw in datos[fuente].select("planning_unit_id", "unidad_planificacion").iter_rows():
            unidad = _etiqueta(unidad_raw)
            if not _id_valido(uid):
                propietario, motivo = None, "id_unidad_invalido"
                uid_diag = None
            else:
                propietario, motivo = propietarios.get(uid, (None, "unidad_sin_mapeo"))
                uid_diag = uid
            dueños[fuente].append(propietario)
            if motivo:
                avisar(fuente, motivo, unidad, uid_diag, excluidas=True)

    for fuente, columna_unidad in unidades_por_fuente.items():
        columnas = [columna_unidad] + ([criterio] if criterio == "jefe_negocio" else [])
        for fila in datos[fuente].select(columnas).iter_rows():
            unidad = _etiqueta(fila[0])
            ids = por_nombre.get(unidad, set())
            uid = None
            if not ids:
                propietario, motivo = None, "unidad_sin_mapeo"
            elif len(ids) != 1 or None in ids:
                propietario, motivo = None, "nombre_unidad_con_varios_ids"
            else:
                uid = next(iter(ids))
                propietario, motivo = propietarios.get(uid, (None, "unidad_sin_mapeo"))
            jefe_fuente = _etiqueta(fila[1]) if criterio == "jefe_negocio" else None
            if propietario is not None and criterio == "jefe_negocio" and jefe_fuente != propietario:
                motivo = ("jefe_ejecutivo_distinto_del_mapa" if fuente == "ejecutivos"
                          else "jefe_estado_distinto_del_mapa")
                propietario = None
            dueños[fuente].append(propietario)
            if motivo:
                avisar(fuente, motivo, unidad, uid, jefe_fuente, excluidas=True)

    nombres = sorted({nombre for propietarios_fuente in dueños.values() for nombre in propietarios_fuente if nombre is not None})
    archivos = {nombre: nombre_archivo_corte(criterio, nombre) for nombre in nombres}
    if len({archivo.casefold() for archivo in archivos.values()}) != len(archivos):
        raise ValueError("Dimensiones distintas producen el mismo nombre de archivo de corte.")
    etiquetas = {fuente: pl.Series("dimension_corte", valores, dtype=pl.String) for fuente, valores in dueños.items()}
    for fuente in fuentes:
        atribuibles = len(dueños[fuente]) - dueños[fuente].count(None)
        if len(etiquetas[fuente]) != datos[fuente].height:
            raise ValueError(f"Cantidad de propietarios incorrecta en {fuente}.")
        if sum(int((etiquetas[fuente] == nombre).sum()) for nombre in nombres) != atribuibles:
            raise ValueError(f"El corte perdió o duplicó filas atribuibles de {fuente}.")
    filas_diagnostico = [
        {"fuente": fuente, "motivo": motivo, "unidad_planificacion": unidad,
         "planning_unit_id": uid, "criterio": criterio, "valor": valor,
         "filas": cantidad, "excluidas": excluidas, "detalle": detalle}
        for (fuente, motivo, unidad, uid, valor, excluidas, detalle), cantidad in diagnosticos.items()
    ]
    df_diagnosticos = pl.DataFrame(filas_diagnostico, schema=SCHEMA_DIAGNOSTICOS)
    return PlanCortes(nombres, df_diagnosticos,
                      {fuente: datos[fuente] for fuente in fuentes}, etiquetas, archivos)


def preparar_cortes(
    datos: Mapping[str, pl.DataFrame], mapa: pl.DataFrame, criterio: str
) -> tuple[list[Corte], pl.DataFrame]:
    """Wrapper compatible que materializa todos los cortes del plan."""
    plan = preparar_plan_cortes(datos, mapa, criterio)
    return [plan.obtener(nombre) for nombre in plan.nombres], plan.diagnosticos
