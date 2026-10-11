# Reportes desde una plantilla

El flujo vigente tiene tres archivos. Usar el entorno `.venv` desde la carpeta del proyecto. PostgreSQL y Peopleware usan las credenciales del `.env` existente; no se incluyen en los reportes.

| Archivo | Función |
|---|---|
| `rev_plantilla_reportes.ipynb` | Respaldo para crear una base nueva con seis tablas, consultando directamente las fuentes. |
| `generar_reportes.py` | Consulta una vez, rellena la plantilla y genera el libro general y los cortes por cliente y jefe de campaña (`jefe_negocio`). |
| `rev_pruebas_reportes.ipynb` | Pruebas con rangos cortos, revisión de tipos y cobertura, y generación de salidas seleccionadas. Comparte las funciones del script. |

## Tablas y períodos

| Hoja | Tabla | Fuente | Período |
|---|---|---|---|
| Colas_diario | `t_colas_diario` | `REPORTING.DATOS_COLAS_DIARIO`, sólo campañas mapeadas | Histórico disponible hasta hoy, con tope de cinco años. |
| Colas | `t_datos` | `REPORTING.DATOS_COLAS`, filtro Genesys original | Siete semanas cerradas más la actual hasta hoy. |
| Workloads_diario | `t_workloads_diario` | Series nativas de Peopleware agregadas por día | Histórico disponible desde el 29/09/2025, con tope de cinco años, más el horizonte de forecast. |
| Workloads | `t_workloads` | Peopleware, salida de 30 minutos | Siete semanas cerradas y seis semanas desde el lunes actual. |
| Ejecutivos | `t_ejecutivos` | `REPORTING.DATOS_EJECUTIVOS` | Histórico disponible hasta hoy, con tope de cinco años; conserva sus 37 columnas. |
| Estados_agentes | `t_estados_agentes` | `REPORTING.DATOS_ESTADOS_AGENTES` | Siete semanas cerradas más la actual hasta hoy; sólo observaciones reales. |

Estos períodos son la configuración automática de `rev_plantilla_reportes.ipynb`, usando la fecha de Santiago. Las siete semanas cerradas terminan el último domingo; la actual es la octava y se va completando. Las seis semanas de forecast incluyen la actual. Los extremos de fechas son inclusivos. Los diarios incluyen también los días recientes: no sumar entre sí las tablas diarias y las de intervalos.

Las duraciones son **horas decimales**, sin dividir por 24. Los TMO y otros valores en segundos conservan segundos. Estados convierte sus doce duraciones SQL a horas y adapta sólo su RUT al formato con guion de Ejecutivos; no crea una grilla ni rellena ausencias. Fecha e intervalo son tipos reales de Excel, los identificadores de texto siguen siendo texto y los nulos quedan vacíos.

Estados conserva también las duraciones superiores a los 30 minutos del intervalo para revisarlas mediante alertas en Excel. El ETL puede reprocesar datos que Genesys corrija posteriormente; una nueva ejecución del reporte tomará esa versión de la vista. Las claves duplicadas y los cambios de esquema siguen deteniendo la extracción para proteger el grano y los totales.

Workloads diario suma cada volumen por separado. El TMO histórico se pondera por contestadas; el TMO de cada forecast, por su propio volumen. Sin peso positivo o con volumen positivo sin TMO, el TMO diario queda nulo. `requerido_horas` suma dotación por duración de los intervalos conocidos. Las variantes son alternativas: no sumar sus forecasts entre sí. El diario se calcula desde las series originales, antes de agrupar a 30 minutos. Se conservan las alertas de cobertura y de cambios de hora. `utc_offset_minutos` permanece en la tabla por intervalo para distinguir horas repetidas.

En las dos tablas Workloads, las parejas `forecast_<variante>`/`tmo_<variante>` se reservan para series de Peopleware. Si la plantilla conserva una pareja y la API no devuelve esa variante en el rango consultado, el generador mantiene ambas columnas y las deja vacías. Esto incluye variantes como `strategic`, `version3` o `version4`; no conserva los datos antiguos ni los sustituye por cero. Las parejas con fórmulas de columna siguen siendo calculadas; si hay fórmulas individuales sin ese metadato, el proceso se detiene antes de borrarlas. Una variante nueva que no tenga columnas en la plantilla requiere actualizar su estructura.

## Crear y personalizar la base

Ejecutar `rev_plantilla_reportes.ipynb`: crea **`archivos/plantilla_reportes_base.xlsx`** con seis hojas y seis tablas. Actualiza las fechas al ejecutar; `MAX_ANOS_HISTORIA`, `SEMANAS_CERRADAS`, `SEMANAS_WORKLOADS_DESDE_LUNES` e `INICIO_PEOPLEWARE` permiten ajustar sus alcances. No requiere los tres Excel anteriores. El notebook de pruebas conserva un rango pequeño para revisiones controladas.

Con fecha de ejecución 10/10/2026, solicita diarios SQL del 10/10/2021 al 10/10/2026, intervalos reales del 17/08/2026 al 10/10/2026, Workloads diario del 29/09/2025 al 15/11/2026 y Workloads por intervalo del 17/08/2026 al 15/11/2026. Sólo se exporta el histórico disponible. Guardar los cierres mensuales como libros separados para conservar el corte de cada ejecución.

Crear copias de la base para personalizar el reporte general, el de cliente y el de jefe. Conservar los nombres de tablas, hojas y columnas de origen; se pueden añadir hojas de análisis y columnas calculadas con fórmula uniforme, incluso intercaladas. Dejar espacio libre debajo de las tablas para su crecimiento. Los datos manuales adicionales deben ir fuera de las tablas de origen.

Cada salida se escribe sobre una copia y se comprueban sus valores, tipos y rangos antes de reemplazar el destino. Las tablas pueden crecer, reducirse o quedar con una fila vacía. Se conservan las fórmulas y los formatos de la plantilla; al reducir, se limpian las filas retiradas del antiguo cuerpo. Sólo se refrescan pivots locales cuya fuente sea el nombre exacto de una tabla; las conexiones externas y el modelo de PowerPivot requieren la actualización que configure el usuario.

Guardar y cerrar el destino en Excel antes de regenerarlo. La lectura compartida de Windows admite los handles habituales de OneDrive y comprueba cambios concurrentes.

## Generar los reportes

```powershell
.\.venv\Scripts\python.exe .\generar_reportes.py --desde 2021-10-10 --hasta 2026-10-11 --desde-intervalos 2026-08-17 --desde-workloads 2026-08-17 --hasta-workloads 2026-11-15
```

`--desde` es obligatorio para elegir conscientemente el histórico. `--hasta` por defecto es la fecha actual de Santiago. La plantilla por defecto es `archivos/plantilla_reportes_base.xlsx`. Las salidas son:

- `archivos/reporte_general.xlsx`.
- `archivos/archivos_por_cliente/`: un libro por cliente.
- `archivos/archivos_por_jefe/`: un libro por `jefe_negocio`.
- `archivos/resumen_generacion.json`: períodos, conteos, cobertura, exclusiones y estado de los archivos generados.

El script permite elegir los períodos explícitamente. `--desde-intervalos` fija el lunes de inicio del detalle. Si se omite, usa las últimas ocho semanas terminando en `--hasta`; `--semanas-intervalos` ajusta esa ventana. `--desde-workloads` evita consultar Peopleware antes de su inicio. `--hasta-workloads` extiende sólo el forecast, sin ampliar el período real de las vistas SQL; por defecto coincide con `--hasta`. El tope de cinco años lo configura el notebook; el script respeta el histórico solicitado.

Consultar años de Workloads puede requerir muchas llamadas; no se promete que la API conserve todas las versiones históricas de forecast. `resumen_generacion.json` es un diagnóstico de la última ejecución, no una entrada: puede borrarse y se vuelve a crear al generar reportes.

```powershell
.\.venv\Scripts\python.exe .\generar_reportes.py --desde 2026-09-28 --hasta 2026-10-10 --hasta-workloads 2026-11-15 --listar
.\.venv\Scripts\python.exe .\generar_reportes.py --desde 2026-09-28 --hasta 2026-10-10 --hasta-workloads 2026-11-15 --cliente "Mutual" --jefe "Nombre exacto"
.\.venv\Scripts\python.exe .\generar_reportes.py --desde 2026-09-28 --hasta-workloads 2026-11-15 --plantilla .\archivos\plantilla_general.xlsx --plantilla-cliente .\archivos\plantilla_cliente.xlsx --plantilla-jefe .\archivos\plantilla_jefe.xlsx
```

`--listar` consulta y muestra los cortes sin crear Excel. `--cliente` y `--jefe` pueden repetirse; al omitirlos se generan todos los cortes disponibles. `--sin-general`, `--sin-clientes` y `--sin-jefes` omiten esas salidas. Los tres argumentos `--salida-*` permiten cambiar los destinos. Los selectores afectan a los archivos separados; el general conserva las fuentes completas.

Los cortes particionan cada tabla independientemente, sin unir hechos. Colas usa sus propias dimensiones; Workloads usa `planning_unit_id` de `analytics_base.map_campana_pw`. Ejecutivos y Estados utilizan el nombre exacto de unidad y comprueban el jefe. Las asociaciones ambiguas o incompletas se excluyen sólo del corte correspondiente y se registran en el resumen; permanecen en el libro general para su revisión. No se infiere el cliente por el nombre de un Workload.

## Respaldo

`archivados/bases_plantillas/` conserva los generadores independientes de Colas, Workloads y Ejecutivos. Sus tres Excel están en `archivados/bases_plantillas/archivos/`, y los notebooks escriben allí al ejecutarse. `archivados/excel_anteriores/` conserva los dos ejemplos, la plantilla anterior y los antiguos cortes por campaña. `archivados/` conserva los scripts anteriores de corte, las pruebas iniciales y `rev_forecast.ipynb`. Los módulos de extracción y escritura son compartidos; no hace falta ejecutar estos respaldos para el flujo vigente.

El temporal de Ejecutivos y `archivos/validacion/` se eliminaron. El contrato y las evidencias compactas de validación permanecen en `docs/PLAN_REPORTES.md`, `docs/VALIDACION_FUENTES.json` y `docs/VALIDACION_IMPLEMENTACION.json`; esos JSON documentan las comprobaciones anteriores y no son entradas del generador.


.\.venv\Scripts\python.exe .\generar_reportes.py --desde 2021-10-10 --hasta 2026-10-11 --desde-intervalos 2026-08-17 --desde-workloads 2026-08-17 --hasta-workloads 2026-11-15 --sin-clientes