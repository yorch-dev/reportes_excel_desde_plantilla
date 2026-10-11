"""Fuentes de los tres reportes: valores SQL originales y Workloads a 30 minutos.

Importar este modulo no conecta a PostgreSQL ni a Peopleware. Las consultas
ocurren al llamar los cargadores. Las duraciones permanecen en horas decimales;
los TMO y los otros tiempos en segundos conservan su unidad original.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo
import math
import re
import threading
import unicodedata

import polars as pl
import requests
from dotenv import dotenv_values, load_dotenv
from requests.adapters import HTTPAdapter
from sqlalchemy import text
from urllib3.util.retry import Retry

RASTER_SALIDA = 1800
VARIANTES_BASE_EXPORTAR = ('rawAuto', 'auto', 'import', 'adjustments', 'operational')

@dataclass(frozen=True)
class ResultadoWorkloads:
    df: pl.DataFrame
    asociaciones: pl.DataFrame
    cobertura: pl.DataFrame
    cobertura_diaria: pl.DataFrame
    cobertura_fuentes: pl.DataFrame
    alertas: pl.DataFrame
    consultas_sin_datos: pl.DataFrame
    perfil: pl.DataFrame
    df_diario: pl.DataFrame | None = None


def _base_dir(base_dir=None):
    return Path(base_dir).resolve() if base_dir is not None else Path(__file__).resolve().parents[2]


def _validar_rango(desde, hasta):
    for valor in (desde, hasta):
        if valor is not None and (not isinstance(valor, date) or isinstance(valor, datetime)):
            raise TypeError("Las fechas deben ser datetime.date o None.")
    if desde is not None and hasta is not None and desde > hasta:
        raise ValueError("DESDE debe ser anterior o igual a HASTA.")


def _conexion_db(base_dir=None):
    base = _base_dir(base_dir)
    load_dotenv(base / ".env")
    from db_conn import conn_db_wf
    return conn_db_wf()[0]


def _tipos(config):
    return {c: grupo["tipo"] for grupo in config.values() for c in grupo["columnas"]}


def _formatos(config):
    return {c: grupo["formato"] for grupo in config.values() for c in grupo["columnas"]}


def _tipar(df, config):
    tipos = _tipos(config)
    if len(tipos) != sum(len(g["columnas"]) for g in config.values()):
        raise ValueError("Hay columnas repetidas en la configuracion.")
    if set(df.columns) != set(tipos):
        raise ValueError(f"Columnas sin configurar: {sorted(set(df.columns)-set(tipos))}; columnas ausentes: {sorted(set(tipos)-set(df.columns))}.")
    resultado = df.cast(tipos, strict=True)
    if resultado.null_count().row(0) != df.null_count().row(0):
        raise ValueError("Se alteraron valores nulos al fijar los tipos.")
    for columna, tipo in resultado.schema.items():
        if tipo == pl.Float64 and resultado.filter(pl.col(columna).is_not_null() & ~pl.col(columna).is_finite()).height:
            raise ValueError(f"Valores no finitos en {columna}.")
    return resultado

CONFIG_COLAS = {'fechas': {'columnas': ['fecha'], 'tipo': pl.Date, 'formato': 'dd-mm-yyyy'}, 'horas': {'columnas': ['intervalo'], 'tipo': pl.Time, 'formato': 'hh:mm'}, 'textos': {'columnas': ['tipo_medios', 'id_cola', 'nombre_cola', 'nombre_cola_2', 'nombre_campana', 'tipo_campana', 'cliente', 'unidad_negocio', 'jefe_negocio', 'supervisores'], 'tipo': pl.String, 'formato': '@'}, 'enteros': {'columnas': ['oferta', 'contestadas', 'abandonadas', 'saliente', 'flujo_saliente', 'manejo', 'retener', 'transferir', 'cumplen_sla', 'abandono_rapido', 'salientes_intentadas', 'tmo_objetivo', 'tiempo_maximo_espera_objetivo'], 'tipo': pl.Int64, 'formato': '#,##0'}, 'decimales': {'columnas': ['velocidad_media_respuesta_seg', 'espera_media_seg', 'manejo_medio_seg', 'conversacion_media_seg', 'retencion_media_seg', 'acw_medio_seg', 'abandono_medio_seg'], 'tipo': pl.Float64, 'formato': '#,##0.00'}, 'porcentajes': {'columnas': ['porcentaje_nivel_servicio', 'porcentaje_objetivo_nivel_servicio', 'nivel_atencion_objetivo', 'nivel_servicio_objetivo'], 'tipo': pl.Float64, 'formato': '0.00%'}, 'duraciones': {'columnas': ['tiempo_manejo_total', 'tiempo_conversacion_total', 'tiempo_retencion_total', 'tiempo_acw_total'], 'tipo': pl.Float64, 'formato': '0.0000'}}

CONFIG_EJECUTIVOS = {'fechas': {'columnas': ['fecha'], 'tipo': pl.Date, 'formato': 'dd-mm-yyyy', 'tipos_sql': {'date'}}, 'textos': {'columnas': ['unidad_planificación', 'rut', 'identificador', 'texto_turno', 'lic_vac_d_adm', 'tramo_jornada', 'fecha (mes)', 'supervisor', 'jefe_negocio'], 'tipo': pl.String, 'formato': '@', 'tipos_sql': {'text', 'character varying', 'character'}}, 'enteros': {'columnas': ['llamadas_contestadas', 'llamadas_salientes', 'tmo_seg', 'manejo', 'ausencia_de_conexion', 'fecha (índice de meses)'], 'tipo': pl.Int64, 'formato': '0', 'tipos_sql': {'integer', 'bigint', 'smallint'}}, 'decimales': {'columnas': ['ocupación_gen'], 'tipo': pl.Float64, 'formato': '0.00', 'tipos_sql': {'numeric', 'double precision', 'real'}}, 'duraciones': {'columnas': ['tiempo_turno', 'tiempo_conectado', 'tiempo_en_la_cola', 'tiempo_comida', 'tiempo_conversación_total', 'tiempo_descanso', 'tiempo_capacitación', 'tiempo_ausente', 'tiempo_inactivo', 'tiempo_ocupado', 'tiempo_disponible', 'tiempo_comida_programada', 'tiempo_reunión', 'tiempo_interactuando', 'tiempo_no_responde', 'tiempo_total_acd', 'tiempo_en_comunicacion', 'tiempo_fuera_de_la_cola', 'tiempo_manejo_total', 'Aux objetivo'], 'tipo': pl.Float64, 'formato': '0.0000', 'tipos_sql': {'numeric', 'double precision', 'real'}}}

QUERY_COLAS = "\n        SELECT fecha, intervalo, tipo_medios, id_cola, nombre_cola, nombre_cola_dimension as nombre_cola_2, oferta, contestadas, abandonadas, saliente, flujo_saliente, velocidad_media_respuesta_seg, porcentaje_nivel_servicio, porcentaje_objetivo_nivel_servicio, espera_media_seg, manejo_medio_seg, conversacion_media_seg, retencion_media_seg, acw_medio_seg, abandono_medio_seg, manejo, retener, transferir, tiempo_manejo_total, cumplen_sla, tiempo_conversacion_total, tiempo_retencion_total, tiempo_acw_total, abandono_rapido, salientes_intentadas, nombre_campana, tmo_objetivo, nivel_atencion_objetivo, nivel_servicio_objetivo, tiempo_maximo_espera_objetivo, tipo_campana, cliente, unidad_negocio, jefe_negocio, supervisores\n\tFROM reporting.datos_colas\n\tWHERE plataforma_operacion = 'Genesys'\n    ORDER BY fecha, intervalo, nombre_campana, nombre_cola\n"


def formatos_colas():
    """Horas decimales y segundos originales, sin convertir duraciones a dias."""
    return _formatos(CONFIG_COLAS)


def formatos_ejecutivos():
    return _formatos(CONFIG_EJECUTIVOS)


def formatos_workloads(df):
    formatos = {}
    for columna, tipo in df.schema.items():
        if tipo == pl.String:
            formato = "@"
        elif tipo == pl.Date:
            formato = "dd-mm-yyyy"
        elif tipo == pl.Time:
            formato = "hh:mm"
        elif tipo == pl.Float64:
            formato = "0.00"
        elif tipo in (pl.Int64, pl.Int32):
            formato = "0"
        else:
            raise ValueError(f"Tipo sin formato en Workloads: {columna}/{tipo}.")
        formatos[columna] = formato
    return formatos


def formatos_workloads_diario(df):
    formatos = formatos_workloads(df)
    if 'requerido_horas' in formatos:
        formatos['requerido_horas'] = '0.0000'
    return formatos


def cargar_colas(desde=None, hasta=None, base_dir=None):
    """La misma seleccion Genesys original; rango opcional inclusive."""
    _validar_rango(desde, hasta)
    query = QUERY_COLAS
    parametros = {}
    filtros = []
    if desde is not None:
        filtros.append("fecha >= :desde")
        parametros["desde"] = desde
    if hasta is not None:
        filtros.append("fecha <= :hasta")
        parametros["hasta"] = hasta
    if filtros:
        query = query.replace("ORDER BY", " AND " + " AND ".join(filtros) + " ORDER BY", 1)
    engine = _conexion_db(base_dir)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            df = pl.read_database(text(query), conn, infer_schema_length=None,
                batch_size=10_000, execute_options={"parameters": parametros})
    finally:
        engine.dispose()
    df = _tipar(df, CONFIG_COLAS)
    if df["fecha"].null_count():
        raise ValueError("La fuente Colas contiene fechas nulas.")
    return df


def cargar_ejecutivos(desde=date(2026, 8, 17), hasta=date(2026, 10, 4), base_dir=None):
    """Leer la vista completa y conservar filas, columnas, unidades y nulos."""
    _validar_rango(desde, hasta)
    if desde is None or hasta is None:
        raise ValueError("Ejecutivos requiere DESDE y HASTA.")
    query = text('SELECT * FROM reporting.datos_ejecutivos '
        'WHERE fecha BETWEEN :desde AND :hasta '
        'ORDER BY "unidad_planificaci\u00f3n", "rut", "fecha"')
    query_columnas = text("SELECT column_name, data_type, numeric_precision, numeric_scale "
        "FROM information_schema.columns "
        "WHERE table_schema = 'reporting' AND table_name = 'datos_ejecutivos' "
        "ORDER BY ordinal_position")
    engine = _conexion_db(base_dir)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            df_schema = pl.read_database(query_columnas, conn, infer_schema_length=None)
            esquema_sql = {x["column_name"]: x["data_type"] for x in df_schema.iter_rows(named=True)}
            tipos = _tipos(CONFIG_EJECUTIVOS)
            if set(esquema_sql) != set(tipos):
                raise ValueError(f"Columnas de la vista modificadas: sin configurar {sorted(set(esquema_sql)-set(tipos))}; ausentes {sorted(set(tipos)-set(esquema_sql))}.")
            for grupo in CONFIG_EJECUTIVOS.values():
                for columna in grupo["columnas"]:
                    if esquema_sql[columna] not in grupo["tipos_sql"]:
                        raise ValueError(f"Tipo SQL cambiado en {columna}: {esquema_sql[columna]}.")
            df = pl.read_database(query, conn, infer_schema_length=None, batch_size=10_000,
                execute_options={"parameters": {"desde": desde, "hasta": hasta}})
    finally:
        engine.dispose()
    if df.columns != df_schema["column_name"].to_list():
        raise ValueError("SELECT * no coincide con el esquema consultado.")
    df = _tipar(df, CONFIG_EJECUTIVOS)
    if df["fecha"].null_count() or df.filter(~pl.col("fecha").is_between(desde, hasta)).height:
        raise ValueError("Fechas nulas o fuera del rango solicitado.")
    return df


def numero_valido(v):
    return v is None or (isinstance(v, (int, float)) and (not isinstance(v, bool)) and math.isfinite(v))

def expandir_serie(serie, raster, validar_alineacion=True):
    if serie is None:
        return {}
    if not isinstance(serie, dict) or not isinstance(serie.get('values'), list):
        raise ValueError('Serie de forecast con formato inesperado.')
    valores = serie['values']
    if not all((numero_valido(v) for v in valores)):
        raise ValueError('Forecast con valores no numericos o no finitos.')
    try:
        inicio = datetime.fromisoformat(serie['startTime'].replace('Z', '+00:00'))
    except (KeyError, ValueError, AttributeError):
        raise ValueError('startTime invalido en forecast.') from None
    if inicio.tzinfo is None or inicio.microsecond or (validar_alineacion and int(inicio.timestamp()) % raster):
        raise ValueError('startTime sin zona o fuera de su raster.')
    return {int(inicio.timestamp()) + i * raster: v for i, v in enumerate(valores)}

def agregar_volumen_tmo(volumen, tmo, raster, grilla):
    if raster not in (900, 1800):
        return {ts: (None, None) for ts in grilla}
    resultado = {}
    for ts in grilla:
        slots = range(ts, ts + RASTER_SALIDA, raster)
        q = [volumen.get(t) for t in slots]
        a = [tmo.get(t) for t in slots]
        completo = all((v is not None for v in q))
        total = sum(q) if completo else None
        if raster == RASTER_SALIDA:
            media = a[0]
        elif completo and all((v >= 0 for v in q)) and (total > 0) and all((v == 0 or h is not None for v, h in zip(q, a))):
            media = sum((v * h for v, h in zip(q, a) if v > 0)) / total
        else:
            media = None
        resultado[ts] = (total, media)
    return resultado

def nombre_variante(valor):
    texto = re.sub('(?<=[a-z0-9])(?=[A-Z])', '_', valor).lower()
    texto = re.sub('[^a-z0-9]+', '_', texto).strip('_')
    if not texto:
        raise ValueError('Variante sin nombre utilizable como columna.')
    return texto

def cargar_workloads(desde=date(2026, 9, 28), hasta=date(2026, 10, 25), base_dir=None):
    """Interfaz original: exclusivamente intervalos de treinta minutos."""
    return _cargar_workloads(desde, hasta, base_dir=base_dir)


def cargar_workloads_reportes(desde: date, hasta: date, desde_intervalos: date, base_dir=None):
    """Historia diaria nativa y detalle reciente, con una sola lectura de catálogos."""
    _validar_rango(desde_intervalos, hasta)
    if desde_intervalos is None:
        raise ValueError("Workloads requiere DESDE_INTERVALOS.")
    return _cargar_workloads(desde, hasta, base_dir=base_dir, desde_intervalos=desde_intervalos)


def _cargar_workloads(desde, hasta, base_dir=None, desde_intervalos=None):
    """Workloads asociados por UUID; forecast, historico y requerido a 30 minutos.

    La API nueva aporta asociacion y variantes. Legacy aporta historia y requerido
    de la actividad asociada. No se rellenan ausencias con ceros. Se conserva el
    desplazamiento UTC para distinguir las horas repetidas al cambiar de horario.
    """
    _validar_rango(desde, hasta)
    if desde is None or hasta is None:
        raise ValueError("Workloads requiere DESDE y HASTA.")
    BASE_DIR = _base_dir(base_dir)
    DESDE, HASTA = desde, hasta
    sesiones_abiertas = []
    try:
        if not (BASE_DIR / '.env').is_file():
            raise FileNotFoundError('Ejecutar el notebook desde la carpeta del proyecto.')
        BASE_NUEVA = 'https://api.peopleware.com/'
        BASE_LEGACY = 'https://legacy-api.peopleware.com/v1/'
        DIAS_POR_CONSULTA = 14
        MAX_WORKERS = 4
        INTERVALO_MINUTOS = 30
        RASTER_SALIDA = INTERVALO_MINUTOS * 60
        UTC = timezone.utc
        ZONAS_LEGACY = {17: 'America/Santiago'}
        METRICAS_HISTORICAS = {125: 'llamadas_historicas', 104: 'contestadas_historicas', 126: 'tmo_historico'}
        VARIANTES_ORDEN = ['rawAuto', 'auto', 'import', 'strategic', 'adjustments', 'operational']
        RASTERS_API = {'PT15M': 900, 'PT30M': 1800, 'PT1H': 3600, 'P1D': 86400}
        if DESDE > HASTA or DIAS_POR_CONSULTA < 1 or MAX_WORKERS < 1:
            raise ValueError('Revisar rango de fechas y parametros de extraccion.')
        credenciales = dotenv_values(BASE_DIR / '.env')
        TOKEN_PW = next((v for k, v in credenciales.items() if k.lower() == 'token_pw'), None)
        del credenciales
        if not TOKEN_PW:
            raise ValueError('Falta TOKEN_PW en .env. No imprimir su valor.')
        sesiones = threading.local()

        def sesion_api():
            if not hasattr(sesiones, 'session'):
                session = requests.Session()
                session.headers.update({'Authorization': TOKEN_PW, 'Accept': 'application/json'})
                retry = Retry(total=3, backoff_factor=1, status_forcelist=[408, 429, 500, 502, 503, 504], allowed_methods={'GET'}, respect_retry_after_header=True)
                for base in (BASE_NUEVA, BASE_LEGACY):
                    session.mount(base, HTTPAdapter(max_retries=retry))
                sesiones.session = session
                sesiones_abiertas.append(session)
            return sesiones.session

        def consultar_json(url, params=None, permitir_404=False):
            destino = urlsplit(url)
            if destino.scheme != 'https' or destino.netloc not in {'api.peopleware.com', 'legacy-api.peopleware.com'} or destino.username or destino.password:
                raise ValueError('Destino no autorizado para la credencial de Peopleware.')
            try:
                respuesta = sesion_api().get(url, params=params, timeout=(10, 60), allow_redirects=False)
            except requests.RequestException as exc:
                raise RuntimeError(f'Fallo de conexion en {destino.path}: {type(exc).__name__}') from None
            if permitir_404 and respuesta.status_code == 404:
                return (None, 404)
            if respuesta.status_code != 200:
                raise RuntimeError(f'HTTP {respuesta.status_code} en {destino.path}')
            try:
                payload = respuesta.json()
            except ValueError:
                raise ValueError(f'Respuesta no JSON en {destino.path}') from None
            if not isinstance(payload, dict):
                raise ValueError(f'Estructura inesperada en {destino.path}')
            return (payload, 200)

        def consultar_legacy(endpoint, clave, params=None, permitir_404=False):
            payload, estado = consultar_json(BASE_LEGACY + endpoint, params, permitir_404)
            if estado == 404:
                return ([], estado)
            filas = payload.get(clave)
            if not isinstance(filas, list) or not all((isinstance(x, dict) for x in filas)):
                raise ValueError(f'Estructura inesperada en {endpoint}; se esperaba {clave}.')
            return (filas, estado)

        def catalogo_workloads():
            url, vistos, filas, total = (BASE_NUEVA + 'workloads', set(), [], None)
            while url:
                destino = urlsplit(url)
                if destino.netloc != 'api.peopleware.com' or destino.path.rstrip('/') != '/workloads':
                    raise ValueError('Paginacion inesperada del catalogo de Workloads.')
                if url in vistos:
                    raise ValueError('Paginacion repetida del catalogo de Workloads.')
                vistos.add(url)
                payload, _ = consultar_json(url)
                pagina = payload.get('data')
                if not isinstance(pagina, list) or not all((isinstance(x, dict) for x in pagina)):
                    raise ValueError('Catalogo de Workloads con formato inesperado.')
                if not isinstance(payload.get('total'), int) or isinstance(payload['total'], bool):
                    raise ValueError('Total de Workloads invalido.')
                if total is not None and payload['total'] != total:
                    raise ValueError('El catalogo cambio durante la paginacion; volver a ejecutar.')
                total = payload['total']
                filas.extend(pagina)
                siguiente = payload.get('next')
                if siguiente is not None and (not isinstance(siguiente, str)):
                    raise ValueError('Enlace de paginacion invalido.')
                url = urljoin(BASE_NUEVA, siguiente) if siguiente else None
            if len(filas) != total or len({x.get('id') for x in filas}) != len(filas):
                raise ValueError('Catalogo incompleto o IDs de Workload duplicados.')
            return filas
        workloads_api = catalogo_workloads()
        catalogos = {}
        for nombre in ('planning_units', 'activities', 'curves', 'curve_value_types', 'curve_versions'):
            catalogos[nombre], _ = consultar_legacy(nombre, nombre)
        unidades = {x['planning_unit_id']: x for x in catalogos['planning_units']}
        actividades = {x['activity_id']: x for x in catalogos['activities']}
        curvas = {x['curve_id']: x for x in catalogos['curves'] if not x.get('deleted', False) and x.get('is_active', True)}
        tipos_valor = {x['curve_value_type_id']: x for x in catalogos['curve_value_types']}
        versiones = {x['curve_version_id']: x for x in catalogos['curve_versions']}
        if 101 not in versiones or versiones[101].get('deleted'):
            raise ValueError('Version historica 101 no disponible.')
        for identificador in METRICAS_HISTORICAS:
            if identificador not in tipos_valor or tipos_valor[identificador].get('deleted'):
                raise ValueError(f'Metrica historica no disponible: {identificador}.')
        for identificador, zona in ZONAS_LEGACY.items():
            payload, _ = consultar_json(BASE_LEGACY + f'time_zones/{identificador}')
            registro = payload.get('time_zone')
            if not isinstance(registro, dict) or registro.get('time_zone_id') != identificador or zona.rsplit('/', 1)[-1].casefold() not in str(registro.get('description', '')).casefold() or (int(datetime.combine(DESDE, time(12), ZoneInfo(zona)).utcoffset().total_seconds()) not in {registro.get('std_utc_offset'), registro.get('dst_utc_offset')}):
                raise ValueError(f'No se pudo contrastar la zona legacy {identificador} con {zona}.')
        alertas = []

        def alertar(workload_id, motivo, detalle=''):
            alertas.append({'workload_id': workload_id, 'motivo': motivo, 'detalle': str(detalle)})

        def entero_id(valor):
            return isinstance(valor, int) and (not isinstance(valor, bool)) and (valor > 0)

        def normalizar_nombre(nombre):
            texto = unicodedata.normalize('NFKD', str(nombre).strip().lstrip('*').strip())
            return ' '.join(''.join((c for c in texto if not unicodedata.combining(c))).casefold().split())

        def limites_utc(zona, desde=DESDE, hasta=HASTA):
            tz = ZoneInfo(zona)
            return (int(datetime.combine(desde, time.min, tz).timestamp()), int(datetime.combine(hasta + timedelta(days=1), time.min, tz).timestamp()))

        def grilla_utc(zona, desde=DESDE, hasta=HASTA):
            inicio, fin = limites_utc(zona, desde, hasta)
            if (fin - inicio) % RASTER_SALIDA:
                raise ValueError('Periodo incompatible con intervalos completos de 30 minutos.')
            return list(range(inicio, fin, RASTER_SALIDA))
        asociaciones = {}
        for item in workloads_api:
            wid = item.get('id')
            try:
                UUID(wid)
            except (ValueError, TypeError, AttributeError):
                raise ValueError('UUID de Workload invalido.') from None
            uid, aid = (item.get('planningUnitId'), item.get('activityId'))
            if uid is None:
                alertar(wid, 'sin_unidad_asociada', item.get('name'))
                continue
            if not entero_id(uid) or (aid is not None and (not entero_id(aid))):
                raise ValueError(f'IDs de configuracion invalidos en Workload {wid}.')
            zona = item.get('timezone')
            ZoneInfo(zona)
            if item.get('intervalLength') not in (900, 1800, 3600):
                raise ValueError(f'Intervalo configurado inesperado en Workload {wid}.')
            unidad, actividad = (unidades.get(uid), actividades.get(aid))
            estado_req = 'actividad_asociada'
            if unidad is None or unidad.get('deleted'):
                estado_req = 'unidad_no_disponible_en_legacy'
            elif ZONAS_LEGACY.get(unidad.get('time_zone_id')) != zona:
                estado_req = 'zona_legacy_no_contrastada'
            elif aid is None:
                estado_req = 'sin_actividad_asociada'
            elif actividad is None or actividad.get('deleted'):
                estado_req = 'actividad_no_disponible_en_legacy'
            if estado_req != 'actividad_asociada':
                alertar(wid, estado_req, f'UP={uid}; actividad={aid}')
            asociaciones[wid] = {'workload_id': wid, 'workload': item['name'], 'canal': item['channel'], 'zona_horaria': zona, 'planning_unit_id': uid, 'unidad_planificacion': unidad['name'] if unidad else f'UP {uid}', 'activity_id': aid, 'actividad': actividad['name'] if actividad else None, 'raster_configurado': item['intervalLength'], 'curve_id': None, 'estado_historico': 'sin_curva_historica_identificada', 'estado_requerido': estado_req}
        if not asociaciones:
            raise ValueError('No hay Workloads con unidad asociada. El Excel anterior se conserva.')
        curvas_exportadas = [x for x in curvas.values() if 'automatic history export' in str(x.get('description', '')).casefold()]
        nombres_workloads = Counter((normalizar_nombre(x['workload']) for x in asociaciones.values()))
        for wid, info in asociaciones.items():
            etiqueta = wid.rsplit('-', 1)[-1]
            etiquetadas = [c for c in curvas_exportadas if etiqueta in str(c.get('name_short', '')).casefold().split()]
            por_nombre = [c for c in curvas_exportadas if normalizar_nombre(c['name']) == normalizar_nombre(info['workload'])]
            if len(etiquetadas) == 1:
                candidatos, metodo = (etiquetadas, 'uuid_en_nombre_corto')
            elif not etiquetadas and len(por_nombre) == 1 and (nombres_workloads[normalizar_nombre(info['workload'])] == 1):
                candidatos, metodo = (por_nombre, 'exportacion_automatica_nombre_unico')
            else:
                candidatos, metodo = ([], 'curva_historica_ambigua' if etiquetadas or por_nombre else 'sin_curva_historica_identificada')
            if candidatos:
                curva = candidatos[0]
                if ZONAS_LEGACY.get(curva.get('time_zone_id')) == info['zona_horaria']:
                    info.update(curve_id=curva['curve_id'], estado_historico=metodo)
                else:
                    info['estado_historico'] = 'zona_curva_no_contrastada'
            else:
                info['estado_historico'] = metodo
            if info['curve_id'] is None:
                alertar(wid, info['estado_historico'], info['workload'])
        uso_curvas = Counter((x['curve_id'] for x in asociaciones.values() if x['curve_id'] is not None))
        for wid, info in asociaciones.items():
            if info['curve_id'] is not None and uso_curvas[info['curve_id']] > 1:
                alertar(wid, 'curva_historica_compartida', info['curve_id'])
                info.update(curve_id=None, estado_historico='curva_historica_compartida')

        def descendientes(aid, ruta=()):
            if aid in ruta:
                raise ValueError('Ciclo en la jerarquia de actividades.')
            resultado = set()
            for hijo in actividades.get(aid, {}).get('child_activity_ids', []):
                resultado.add(hijo)
                resultado.update(descendientes(hijo, (*ruta, aid)))
            return resultado
        pares = Counter(((x['planning_unit_id'], x['activity_id']) for x in asociaciones.values() if x['activity_id'] is not None))
        pares_compartidos = {par for par, cantidad in pares.items() if cantidad > 1}
        jerarquias = []
        for wid, info in asociaciones.items():
            par = (info['planning_unit_id'], info['activity_id'])
            ancestros = [otro for otro in asociaciones.values() if otro['planning_unit_id'] == info['planning_unit_id'] and info['activity_id'] in descendientes(otro['activity_id'])]
            if par in pares_compartidos:
                info['estado_requerido'] = 'actividad_compartida_entre_workloads'
                alertar(wid, info['estado_requerido'], par)
            elif ancestros:
                info['estado_requerido'] = 'actividad_incluida_en_otro_workload'
                for padre in ancestros:
                    jerarquias.append((padre['workload_id'], wid))
                alertar(wid, info['estado_requerido'], [x['activity_id'] for x in ancestros])
        unidades_seleccionadas = sorted({x['planning_unit_id'] for x in asociaciones.values() if x['planning_unit_id'] in unidades and (not unidades[x['planning_unit_id']].get('deleted'))})
        membresias = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futuros = {executor.submit(consultar_legacy, f'planning_units/{uid}/activities/{DESDE.isoformat()}', 'planning_unit_activities', {'end_date': HASTA.isoformat()}, True): uid for uid in unidades_seleccionadas}
            for future in as_completed(futuros):
                uid = futuros[future]
                registros, estado = future.result()
                if any((x.get('planning_unit_id') != uid for x in registros)):
                    raise ValueError('Membresia con unidad inesperada.')
                membresias.extend(registros)
                if estado == 404:
                    alertar(None, 'membresias_no_recibidas', uid)

        def actividad_vigente(uid, aid, fecha):
            return aid is not None and any((x['planning_unit_id'] == uid and x['activity_id'] == aid and (date.fromisoformat(str(x['start_date'])[:10]) <= fecha <= date.fromisoformat(str(x['end_date'])[:10])) for x in membresias))
        SCHEMA_ASOCIACIONES = {'workload_id': pl.String, 'workload': pl.String, 'canal': pl.String, 'zona_horaria': pl.String, 'planning_unit_id': pl.Int64, 'unidad_planificacion': pl.String, 'activity_id': pl.Int64, 'actividad': pl.String, 'raster_configurado': pl.Int64, 'curve_id': pl.Int64, 'estado_historico': pl.String, 'estado_requerido': pl.String}
        df_asociaciones = pl.DataFrame(list(asociaciones.values()), schema=SCHEMA_ASOCIACIONES)

        alias_globales = {}

        def procesar_bloque(DESDE, HASTA):
            DESDE_DETALLE = DESDE if desde_intervalos is None else max(DESDE, desde_intervalos)
            def rangos_fechas():
                inicio = DESDE
                while inicio <= HASTA:
                    fin = min(inicio + timedelta(days=DIAS_POR_CONSULTA - 1), HASTA)
                    yield (inicio, fin)
                    inicio = fin + timedelta(days=1)

            def descargar(tarea):
                tipo, identificador, inicio, fin = tarea
                if tipo == 'forecast':
                    info = asociaciones[identificador]
                    comienzo, termino = limites_utc(info['zona_horaria'], inicio, fin)
                    params = {'startTime': datetime.fromtimestamp(comienzo, UTC).isoformat(), 'endTime': datetime.fromtimestamp(termino, UTC).isoformat(), 'aggregateByDay': 'false'}
                    payload, estado = consultar_json(BASE_NUEVA + f'workloads/{identificador}/forecasts', params, permitir_404=True)
                    if estado == 404:
                        return (tarea, None, estado)
                    datos = payload.get('data')
                    if not isinstance(datos, dict) or datos.get('workloadId') != identificador or datos.get('intervalDuration') not in RASTERS_API or (not isinstance(datos.get('forecasts'), dict)):
                        raise ValueError(f'Forecast con contrato inesperado: {identificador}.')
                    return (tarea, datos, estado)
                params = {'end_date': fin.isoformat(), 'zeros': 'false'}
                if tipo == 'historico':
                    endpoint, clave = (f'curves/{identificador}/values/{inicio.isoformat()}', 'curve_values')
                    params.update(version_ids='101', apply_curve_time_zone='true')
                else:
                    endpoint, clave = (f'planning_units/{identificador}/requirements/{inicio.isoformat()}', 'requirements')
                    params['apply_planning_unit_time_zone'] = 'true'
                registros, estado = consultar_legacy(endpoint, clave, params, permitir_404=True)
                for item in registros:
                    campo = 'curve_id' if tipo == 'historico' else 'planning_unit_id'
                    fecha = date.fromisoformat(str(item['date'])[:10])
                    if item.get(campo) != identificador or not inicio <= fecha <= fin:
                        raise ValueError(f'ID o fecha inesperada en {tipo} {identificador}.')
                return (tarea, registros, estado)
            curvas_seleccionadas = sorted({x['curve_id'] for x in asociaciones.values() if x['curve_id'] is not None})
            tareas = [('forecast', wid, DESDE, HASTA) for wid in asociaciones]
            tareas += [(tipo, identificador, inicio, fin) for tipo, ids in (('historico', curvas_seleccionadas), ('requerido', unidades_seleccionadas)) for identificador in ids for inicio, fin in rangos_fechas()]
            respuestas = {'forecast': {}, 'historico': [], 'requerido': []}
            consultas_sin_datos = []
            from contextlib import nullcontext
            with (ThreadPoolExecutor(max_workers=MAX_WORKERS) if desde_intervalos is None else nullcontext(executor_reportes)) as executor:
                futuros = [executor.submit(descargar, tarea) for tarea in tareas]
                for future in as_completed(futuros):
                    tarea, datos, estado = future.result()
                    tipo, identificador, inicio, fin = tarea
                    if tipo == 'forecast':
                        respuestas[tipo][identificador] = datos
                    else:
                        respuestas[tipo].extend(datos)
                    if estado == 404 or not datos:
                        consultas_sin_datos.append({'fuente': tipo, 'id': str(identificador), 'desde': inicio, 'hasta': fin, 'http': estado})
            variantes = set(VARIANTES_BASE_EXPORTAR)
            for datos in respuestas['forecast'].values():
                if datos is not None:
                    variantes.update(datos['forecasts'])
            variantes = [v for v in VARIANTES_ORDEN if v in variantes] + sorted(variantes - set(VARIANTES_ORDEN))
            alias_variantes = {v: nombre_variante(v) for v in variantes}
            if len(set(alias_variantes.values())) != len(alias_variantes):
                raise ValueError('Variantes distintas producen la misma columna; revisar sus nombres.')
            for variante, alias in alias_variantes.items():
                if alias in alias_globales and alias_globales[alias] != variante:
                    raise ValueError('Variantes de bloques distintos producen la misma columna.')
                alias_globales[alias] = variante
            COLUMNAS_FORECAST = [c for v in variantes for c in (f'forecast_{alias_variantes[v]}', f'tmo_{alias_variantes[v]}')]
            COLUMNAS_SERIES = [*METRICAS_HISTORICAS.values(), *COLUMNAS_FORECAST]
            if len(set(COLUMNAS_SERIES)) != len(COLUMNAS_SERIES):
                raise ValueError('Una variante colisiona con una columna historica; revisar sus nombres.')
            grillas = {wid: grilla_utc(x['zona_horaria'], DESDE_DETALLE, HASTA) if DESDE_DETALLE <= HASTA else [] for wid, x in asociaciones.items()}
            forecasts_agrupados, cobertura_fuentes, forecasts_nativos = ({}, [], {})
            auditoria_volumen = []
            for wid, info in asociaciones.items():
                datos = respuestas['forecast'][wid]
                forecasts_agrupados[wid] = {}
                forecasts_nativos[wid] = {}
                if datos is None:
                    alertar(wid, 'forecast_no_recibido')
                    continue
                raster = RASTERS_API[datos['intervalDuration']]
                if raster != info['raster_configurado']:
                    alertar(wid, 'raster_respuesta_distinto_configuracion', f"{raster} vs {info['raster_configurado']}")
                if raster not in (900, 1800):
                    alertar(wid, 'forecast_no_desagregable_a_30_minutos', datos['intervalDuration'])
                for variante, contenido in datos['forecasts'].items():
                    if not isinstance(contenido, dict):
                        raise ValueError('Contenido de variante inesperado.')
                    if set(contenido) - {'offered', 'averageHandlingTime'}:
                        raise ValueError('Metrica de forecast no contemplada; revisar el contrato.')
                    q = expandir_serie(contenido.get('offered'), raster, raster in (900, 1800))
                    a = expandir_serie(contenido.get('averageHandlingTime'), raster, raster in (900, 1800))
                    if any((v is not None and v < 0 for v in a.values())):
                        raise ValueError('TMO negativo en forecast.')
                    forecasts_nativos[wid][variante] = (raster, q, a)
                    agregado = agregar_volumen_tmo(q, a, raster, grillas[wid])
                    forecasts_agrupados[wid][variante] = agregado
                    parciales = [ts for ts in grillas[wid] if raster in (900, 1800) and agregado[ts][0] is None and any((q.get(t) is not None for t in range(ts, ts + RASTER_SALIDA, raster)))]
                    volumen_parcial = sum((q[t] for ts in parciales for t in range(ts, ts + RASTER_SALIDA, raster) if q.get(t) is not None))
                    if parciales:
                        alertar(wid, 'bloques_forecast_incompletos_a_30_minutos', f'{variante}: {len(parciales)} bloques; volumen conocido no exportado={volumen_parcial}')
                    for metrica, serie in contenido.items():
                        if serie is None:
                            alertar(wid, 'serie_forecast_nula', f'{variante}.{metrica}')
                            continue
                        if 'outdated' in serie and (not isinstance(serie['outdated'], bool)):
                            raise ValueError('Indicador outdated inesperado.')
                        cobertura_fuentes.append({'workload_id': wid, 'workload': info['workload'], 'variante': variante, 'metrica': metrica, 'raster': raster, 'startTime': serie['startTime'], 'valores': len(serie['values']), 'nulls': sum((v is None for v in serie['values'])), 'outdated': serie.get('outdated'), 'bloques_parciales': len(parciales) if metrica == 'offered' else None, 'volumen_parcial_no_exportado': volumen_parcial if metrica == 'offered' else None})
                    if raster in (900, 1800):
                        slots_completos = [t for ts, (valor, _) in agregado.items() if valor is not None for t in range(ts, ts + RASTER_SALIDA, raster)]
                        antes = sum((q[t] for t in slots_completos))
                        despues = sum((v for v, _ in agregado.values() if v is not None))
                        auditoria_volumen.append((wid, f'forecast_{alias_variantes[variante]}', antes, despues))

            def expandir_diaria(item, zona, etiqueta):
                raster, valores = (item.get('raster'), item.get('values'))
                if not isinstance(raster, int) or isinstance(raster, bool) or raster <= 0:
                    raise ValueError('Raster legacy invalido.')
                if not isinstance(valores, list) or (desde_intervalos is None and len(valores) * raster != 86400):
                    raise ValueError('Serie diaria legacy incompleta o de duracion inesperada.')
                if not all((numero_valido(v) for v in valores)):
                    raise ValueError('Serie legacy con valores no numericos o no finitos.')
                fecha = date.fromisoformat(str(item['date'])[:10])
                inicio, fin = limites_utc(zona, fecha, fecha)
                if desde_intervalos is None:
                    invalido = raster not in (900, 1800) or fin - inicio != 86400
                else:
                    invalido = raster not in (900, 1800, 3600, 86400) or len(valores) * raster != fin - inicio
                if invalido:
                    alertar(None, 'serie_legacy_omitida_por_raster_o_cambio_horario', f'{etiqueta}; {fecha}; {raster}')
                    return None
                return (raster, {inicio + i * raster: v for i, v in enumerate(valores)})
            historicos_nativos, requeridos_nativos, claves = ({}, {}, set())
            curva_workload = {x['curve_id']: wid for wid, x in asociaciones.items() if x['curve_id'] is not None}
            for item in respuestas['historico']:
                tipo, version = (item.get('curve_value_type_id'), item.get('curve_version_id'))
                if tipo not in METRICAS_HISTORICAS or version != 101:
                    continue
                cid, fecha = (item['curve_id'], date.fromisoformat(str(item['date'])[:10]))
                clave = ('hist', cid, fecha, tipo)
                if clave in claves:
                    raise ValueError(f'Serie historica duplicada: {clave}.')
                claves.add(clave)
                wid = curva_workload[cid]
                serie = expandir_diaria(item, asociaciones[wid]['zona_horaria'], clave)
                if serie is not None:
                    historicos_nativos[wid, fecha, METRICAS_HISTORICAS[tipo]] = serie
            pares_seleccionados = set(pares)
            for item in respuestas['requerido']:
                uid, aid = (item['planning_unit_id'], item['activity_id'])
                if (uid, aid) not in pares_seleccionados:
                    continue
                fecha = date.fromisoformat(str(item['date'])[:10])
                clave = ('req', uid, aid, fecha)
                if clave in claves:
                    raise ValueError(f'Serie de requerido duplicada: {clave}.')
                claves.add(clave)
                zona = ZONAS_LEGACY.get(unidades[uid].get('time_zone_id'))
                if zona is None:
                    continue
                if not actividad_vigente(uid, aid, fecha):
                    alertar(None, 'requerido_fuera_vigencia_omitido', clave)
                    continue
                serie = expandir_diaria(item, zona, clave)
                if serie is not None:
                    if any((v is not None and v < 0 for v in serie[1].values())):
                        raise ValueError('Requerido negativo en la actividad seleccionada.')
                    requeridos_nativos[uid, aid, fecha] = serie
            df_diario, cobertura_nativa = None, []
            if desde_intervalos is not None:
                from .workloads_diario import agregar_volumen_tmo_dia, agregar_requerido_dia
                filas_diarias = []
                for wid, info in asociaciones.items():
                    zona = info['zona_horaria']
                    for offset in range((HASTA - DESDE).days + 1):
                        fecha = DESDE + timedelta(days=offset)
                        fila = {k: v for k, v in info.items() if k != 'raster_configurado'}
                        fila['fecha'] = fecha
                        def registrar(serie, cov):
                            cobertura_nativa.append(dict(workload_id=wid, workload=info['workload'], fecha=fecha, serie=serie, **cov))
                            if cov['estado'] in {'parcial', 'ponderado_parcial', 'tmo_faltante_peso_positivo', 'raster_tmo_distinto_peso', 'raster_no_representa_dia_local', 'serie_diaria_no_alineada_al_dia_local', 'pesos_negativos'}:
                                alertar(wid, 'cobertura_diaria_' + cov['estado'], f'{fecha}; {serie}')
                        historia = {c: historicos_nativos.get((wid, fecha, c)) for c in METRICAS_HISTORICAS.values()}
                        for columna in ('llamadas_historicas', 'contestadas_historicas'):
                            raster, q = historia[columna] or (info['raster_configurado'], {})
                            total, _, cv, _ = agregar_volumen_tmo_dia(q, {}, raster, fecha, zona)
                            fila[columna] = total
                            registrar(columna, cv)
                        peso = historia['contestadas_historicas']
                        tiempos = historia['tmo_historico']
                        raster, q = peso or (info['raster_configurado'], {})
                        _, media, _, ct = agregar_volumen_tmo_dia(q, tiempos[1] if tiempos else {}, raster, fecha, zona, tiempos[0] if tiempos else None)
                        fila['tmo_historico'] = media
                        registrar('tmo_historico', ct)
                        for variante in variantes:
                            raster, q, a = forecasts_nativos[wid].get(variante, (info['raster_configurado'], {}, {}))
                            total, media, cv, ct = agregar_volumen_tmo_dia(q, a, raster, fecha, zona)
                            alias = alias_variantes[variante]
                            fila[f'forecast_{alias}'], fila[f'tmo_{alias}'] = total, media
                            registrar(f'forecast_{alias}', cv)
                            registrar(f'tmo_{alias}', ct)
                        estado = info['estado_requerido']
                        if estado == 'actividad_asociada' and not actividad_vigente(info['planning_unit_id'], info['activity_id'], fecha):
                            estado = 'actividad_fuera_de_vigencia'
                        serie = requeridos_nativos.get((info['planning_unit_id'], info['activity_id'], fecha)) if estado == 'actividad_asociada' else None
                        raster, q = serie or (info['raster_configurado'], {})
                        horas, cr = agregar_requerido_dia(q, raster, fecha, zona)
                        if estado == 'actividad_asociada' and horas is None:
                            estado = 'sin_dato_requerido'
                        elif estado == 'actividad_asociada' and cr['estado'] == 'parcial':
                            estado = 'requerido_parcial'
                        fila['requerido_horas'], fila['estado_requerido'] = horas, estado
                        registrar('requerido_horas', cr)
                        filas_diarias.append(fila)
                esquema_diario = {k: v for k, v in SCHEMA_ASOCIACIONES.items() if k != 'raster_configurado'}
                esquema_diario.update(fecha=pl.Date, requerido_horas=pl.Float64)
                esquema_diario.update({c: pl.Float64 for c in COLUMNAS_SERIES})
                columnas_diarias = ['fecha', 'planning_unit_id', 'unidad_planificacion', 'workload_id', 'workload', 'canal', 'zona_horaria', 'activity_id', 'actividad', 'curve_id', 'estado_historico', 'estado_requerido', *COLUMNAS_SERIES, 'requerido_horas']
                df_diario = pl.DataFrame(filas_diarias, schema=esquema_diario).select(columnas_diarias)
                if df_diario.select('workload_id', 'fecha').is_duplicated().any():
                    raise ValueError('Claves de Workload diario duplicadas.')
                if df_diario.filter(pl.col('requerido_horas').is_not_null()).select('planning_unit_id', 'activity_id', 'fecha').is_duplicated().any():
                    raise ValueError('Requerido diario repetido entre cargas de trabajo.')

            historicos_agrupados, requeridos_agrupados, auditoria_requerido = ({}, {}, [])
            for wid, info in asociaciones.items():
                for offset in range((HASTA - DESDE_DETALLE).days + 1):
                    fecha = DESDE_DETALLE + timedelta(days=offset)
                    grilla = grilla_utc(info['zona_horaria'], fecha, fecha)
                    series = {c: historicos_nativos.get((wid, fecha, c)) for c in METRICAS_HISTORICAS.values()}
                    for columna in ('llamadas_historicas', 'contestadas_historicas'):
                        serie = series[columna]
                        if serie is None:
                            continue
                        raster, valores = serie
                        agg = agregar_volumen_tmo(valores, {}, raster, grilla)
                        for ts, (valor, _) in agg.items():
                            historicos_agrupados.setdefault((wid, ts), {})[columna] = valor
                        slots = [t for ts, (v, _) in agg.items() if v is not None for t in range(ts, ts + RASTER_SALIDA, raster)]
                        auditoria_volumen.append((wid, columna, sum((valores[t] for t in slots)), sum((v for v, _ in agg.values() if v is not None))))
                    tmo, peso = (series['tmo_historico'], series['contestadas_historicas'])
                    if tmo is not None:
                        raster, valores = tmo
                        if peso is not None and peso[0] != raster and (raster != RASTER_SALIDA):
                            alertar(wid, 'raster_tmo_historico_distinto_contestadas', fecha)
                            continue
                        for ts, (_, media) in agregar_volumen_tmo(peso[1] if peso else {}, valores, raster, grilla).items():
                            historicos_agrupados.setdefault((wid, ts), {})['tmo_historico'] = media
            for (uid, aid, fecha), (raster, valores) in requeridos_nativos.items():
                if fecha < DESDE_DETALLE:
                    continue
                if desde_intervalos is not None and raster not in (900, 1800):
                    alertar(None, 'requerido_no_desagregable_a_30_minutos', f'{uid}/{aid}/{fecha}: {raster}')
                    continue
                zona = ZONAS_LEGACY[unidades[uid]['time_zone_id']]
                horas_antes, horas_despues = (0.0, 0.0)
                for ts in grilla_utc(zona, fecha, fecha):
                    slots = list(range(ts, ts + RASTER_SALIDA, raster))
                    completo = all((valores.get(t) is not None for t in slots))
                    valor = sum((valores[t] * raster for t in slots)) / RASTER_SALIDA if completo else None
                    requeridos_agrupados[uid, aid, ts] = valor
                    if completo:
                        horas_antes += sum((valores[t] * raster / 3600 for t in slots))
                        horas_despues += valor * RASTER_SALIDA / 3600
                auditoria_requerido.append((uid, aid, fecha, horas_antes, horas_despues))
            for padre_id, hijo_id in jerarquias:
                padre, hijo = (asociaciones[padre_id], asociaciones[hijo_id])
                inconsistentes = sum((requeridos_agrupados.get((padre['planning_unit_id'], padre['activity_id'], ts)) == 0 and (requeridos_agrupados.get((hijo['planning_unit_id'], hijo['activity_id'], ts)) or 0) > 0 for ts in grillas[hijo_id]))
                if inconsistentes:
                    alertar(hijo_id, 'requerido_hijo_positivo_con_padre_cero', inconsistentes)
                padre_ausente = sum((requeridos_agrupados.get((padre['planning_unit_id'], padre['activity_id'], ts)) is None and (requeridos_agrupados.get((hijo['planning_unit_id'], hijo['activity_id'], ts)) or 0) > 0 for ts in grillas[hijo_id]))
                if padre_ausente:
                    alertar(hijo_id, 'requerido_hijo_positivo_con_padre_sin_dato', padre_ausente)
            filas = []
            for wid, info in asociaciones.items():
                tz = ZoneInfo(info['zona_horaria'])
                for ts in grillas[wid]:
                    local = datetime.fromtimestamp(ts, tz)
                    estado_req = info['estado_requerido']
                    if estado_req == 'actividad_asociada' and (not actividad_vigente(info['planning_unit_id'], info['activity_id'], local.date())):
                        estado_req = 'actividad_fuera_de_vigencia'
                    requerido = requeridos_agrupados.get((info['planning_unit_id'], info['activity_id'], ts))
                    if estado_req != 'actividad_asociada':
                        requerido = None
                    elif requerido is None:
                        estado_req = 'sin_dato_requerido'
                    fila = {k: v for k, v in info.items() if k != 'raster_configurado'}
                    fila.update(fecha=local.date(), intervalo=local.time().replace(tzinfo=None), intervalo_minutos=INTERVALO_MINUTOS, utc_offset_minutos=int(local.utcoffset().total_seconds() / 60), timestamp_utc=ts, requerido=requerido, estado_requerido=estado_req)
                    fila.update({c: historicos_agrupados.get((wid, ts), {}).get(c) for c in METRICAS_HISTORICAS.values()})
                    for variante in variantes:
                        q, a = forecasts_agrupados[wid].get(variante, {}).get(ts, (None, None))
                        alias = alias_variantes[variante]
                        fila.update({f'forecast_{alias}': q, f'tmo_{alias}': a})
                    filas.append(fila)
            schema = {k: v for k, v in SCHEMA_ASOCIACIONES.items() if k != 'raster_configurado'}
            schema.update(fecha=pl.Date, intervalo=pl.Time, intervalo_minutos=pl.Int64, utc_offset_minutos=pl.Int64, timestamp_utc=pl.Int64, requerido=pl.Float64)
            schema.update({c: pl.Float64 for c in COLUMNAS_SERIES})
            df_base = pl.DataFrame(filas, schema=schema)
            df_workloads = df_base.select('fecha', 'intervalo', 'intervalo_minutos', 'utc_offset_minutos', 'planning_unit_id', 'unidad_planificacion', 'workload_id', 'workload', 'canal', 'zona_horaria', 'activity_id', 'actividad', 'curve_id', 'estado_historico', 'estado_requerido', *COLUMNAS_SERIES, 'requerido').sort(['unidad_planificacion', 'workload', 'fecha', 'intervalo', 'utc_offset_minutos'])
            del filas
            if df_workloads.select('workload_id', 'fecha', 'intervalo', 'utc_offset_minutos').is_duplicated().any():
                raise ValueError('Claves de Workload duplicadas.')
            if df_workloads['planning_unit_id'].null_count() or (df_workloads.height and set(df_workloads['workload_id'].unique()) != set(asociaciones)):
                raise ValueError('Se perdieron Workloads asociados o se incluyeron unidades desconocidas.')
            if df_workloads.height != sum((len(g) for g in grillas.values())):
                raise ValueError('Grilla incompleta.')
            if df_workloads.filter(~pl.col('fecha').is_between(DESDE, HASTA)).height:
                raise ValueError('Fechas fuera del rango.')
            if df_workloads.filter((pl.col('intervalo_minutos') != INTERVALO_MINUTOS) | (pl.col('intervalo').cast(pl.Int64) % (RASTER_SALIDA * 1000000000) != 0)).height:
                raise ValueError('Intervalos fuera de la grilla de 30 minutos.')
            if df_base.filter(pl.col('requerido').is_not_null()).select('planning_unit_id', 'activity_id', 'timestamp_utc').is_duplicated().any():
                raise ValueError('Requerido repetido entre cargas de trabajo.')
            if {'contestadas_auto', 'contestadas_prevision', 'actividad_ids'} & set(df_workloads.columns):
                raise ValueError('Columnas descartadas volvieron a la salida.')
            for wid, columna, antes, despues in auditoria_volumen:
                if not math.isclose(antes, despues, rel_tol=1e-12, abs_tol=1e-07):
                    raise ValueError(f'No se conserva volumen de {columna}: {wid}.')
            for uid, aid, fecha, antes, despues in auditoria_requerido:
                if not math.isclose(antes, despues, rel_tol=1e-12, abs_tol=1e-07):
                    raise ValueError(f'No se conservan horas de requerido: {uid}/{aid}/{fecha}.')
            for wid in asociaciones:
                tabla = df_base.filter(pl.col('workload_id') == wid)
                for columna in COLUMNAS_SERIES:
                    antes = sum((x[3] for x in auditoria_volumen if x[0] == wid and x[1] == columna))
                    if columna.startswith('tmo_'):
                        continue
                    if not math.isclose(antes, tabla[columna].sum() or 0, rel_tol=1e-12, abs_tol=1e-07):
                        raise ValueError(f'El enlace altero {columna}: {wid}.')
                info = asociaciones[wid]
                antes = sum((requeridos_agrupados.get((info['planning_unit_id'], info['activity_id'], ts)) or 0 for ts in grillas[wid] if info['estado_requerido'] == 'actividad_asociada' and actividad_vigente(info['planning_unit_id'], info['activity_id'], datetime.fromtimestamp(ts, ZoneInfo(info['zona_horaria'])).date())))
                if not math.isclose(antes, tabla['requerido'].sum() or 0, rel_tol=1e-12, abs_tol=1e-07):
                    raise ValueError(f'El enlace altero requerido: {wid}.')
            df_cobertura = pl.DataFrame([{'columna': c, 'valores': df_workloads.height - df_workloads[c].null_count(), 'vacios': df_workloads[c].null_count(), 'ceros': int((df_workloads[c] == 0).sum()), 'no_cero': int((df_workloads[c] != 0).sum())} for c in [*COLUMNAS_SERIES, 'requerido']])
            df_cobertura_diaria = pl.concat([df_workloads.group_by('workload_id', 'workload', 'fecha').agg(pl.col(c).is_not_null().sum().alias('valores'), (pl.col(c) == 0).sum().alias('ceros'), pl.col(c).is_null().sum().alias('vacios')).with_columns(pl.lit(c).alias('serie')) for c in [*COLUMNAS_SERIES, 'requerido']]).sort(['workload', 'fecha', 'serie'])
            if desde_intervalos is not None:
                df_cobertura_diaria = pl.DataFrame(cobertura_nativa, infer_schema_length=None)
            df_alertas = pl.DataFrame(alertas if desde_intervalos is None else [], schema={'workload_id': pl.String, 'motivo': pl.String, 'detalle': pl.String})
            df_cobertura_fuentes = pl.DataFrame(cobertura_fuentes, infer_schema_length=None) if cobertura_fuentes else pl.DataFrame()

            df_workloads = df_workloads.drop("intervalo_minutos")
            perfil = pl.DataFrame({"columna": list(df_workloads.schema),
                                  "tipo": [str(t) for t in df_workloads.schema.values()]})
            consultas_df = pl.DataFrame(consultas_sin_datos,
                schema={"fuente": pl.String, "id": pl.String, "desde": pl.Date, "hasta": pl.Date, "http": pl.Int64})
            return ResultadoWorkloads(df=df_workloads, asociaciones=df_asociaciones,
                cobertura=df_cobertura, cobertura_diaria=df_cobertura_diaria,
                cobertura_fuentes=df_cobertura_fuentes, alertas=df_alertas,
                consultas_sin_datos=consultas_df, perfil=perfil, df_diario=df_diario)

        if desde_intervalos is None:
            return procesar_bloque(DESDE, HASTA)
        resultados = []
        inicio = DESDE
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor_reportes:
            while inicio <= HASTA:
                fin = min(inicio + timedelta(days=DIAS_POR_CONSULTA - 1), HASTA)
                resultados.append(procesar_bloque(inicio, fin))
                inicio = fin + timedelta(days=1)
        def unir(nombre):
            frames = [getattr(r, nombre) for r in resultados if getattr(r, nombre) is not None]
            return pl.concat(frames, how='diagonal_relaxed') if frames else pl.DataFrame()
        intervalos, diarios = unir('df'), unir('df_diario')
        orden_variantes = [v for v in VARIANTES_ORDEN if v in alias_globales.values()] + sorted(set(alias_globales.values()) - set(VARIANTES_ORDEN))
        series = ['tmo_historico', *[c for v in orden_variantes for c in (f'forecast_{nombre_variante(v)}', f'tmo_{nombre_variante(v)}')]]
        fijas = ['fecha', 'intervalo', 'utc_offset_minutos', 'planning_unit_id', 'unidad_planificacion', 'workload_id', 'workload', 'canal', 'zona_horaria', 'activity_id', 'actividad', 'curve_id', 'estado_historico', 'estado_requerido', 'llamadas_historicas', 'contestadas_historicas']
        intervalos = intervalos.select(*fijas, *series, 'requerido').sort(['unidad_planificacion', 'workload', 'fecha', 'intervalo', 'utc_offset_minutos'])
        diarios = diarios.select(*[c for c in fijas if c not in {'intervalo', 'utc_offset_minutos'}], *series, 'requerido_horas').sort(['unidad_planificacion', 'workload', 'fecha'])
        if diarios.height != len(asociaciones) * ((HASTA - DESDE).days + 1):
            raise ValueError('Historia diaria incompleta.')
        if intervalos.select('workload_id', 'fecha', 'intervalo', 'utc_offset_minutos').is_duplicated().any() or diarios.select('workload_id', 'fecha').is_duplicated().any():
            raise ValueError('Se duplicaron claves al combinar bloques.')
        metricas = ['llamadas_historicas', 'contestadas_historicas', *series, 'requerido']
        cobertura = pl.DataFrame([{'columna': c, 'valores': intervalos.height - intervalos[c].null_count(), 'vacios': intervalos[c].null_count(), 'ceros': int((intervalos[c] == 0).sum()), 'no_cero': int((intervalos[c] != 0).sum())} for c in metricas])
        return ResultadoWorkloads(df=intervalos, df_diario=diarios, asociaciones=df_asociaciones,
            cobertura=cobertura, cobertura_diaria=unir('cobertura_diaria'),
            cobertura_fuentes=unir('cobertura_fuentes'),
            alertas=pl.DataFrame(alertas, schema={'workload_id': pl.String, 'motivo': pl.String, 'detalle': pl.String}),
            consultas_sin_datos=unir('consultas_sin_datos'),
            perfil=pl.DataFrame({'columna': list(intervalos.schema), 'tipo': [str(t) for t in intervalos.schema.values()]}))
    finally:
        for session in sesiones_abiertas:
            session.close()
