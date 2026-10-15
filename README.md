# Reportes desde una plantilla

El flujo vigente tiene tres archivos. Usar el entorno `.venv` desde la carpeta del proyecto. PostgreSQL y Peopleware usan las credenciales del `.env` existente; no se incluyen en los reportes.

| Archivo | Función |
|---|---|
| `rev_plantilla_reportes.ipynb` | Respaldo para crear una base nueva con nueve tablas: siete fuentes SQL y dos resultados de Workloads de Peopleware. |
| `generar_reportes.py` | Consulta una vez, rellena la plantilla y genera el libro general y los cortes por cliente y jefe de campaña (`jefe_negocio`). |
| `rev_pruebas_reportes.ipynb` | Pruebas con rangos cortos, revisión de tipos y cobertura, y generación de salidas seleccionadas. Comparte las funciones del script. |

## Tablas y períodos

| Hoja | Tabla | Fuente | Período |
|---|---|---|---|
| t_colas_diario | `t_colas_diario` | `REPORTING.DATOS_COLAS_DIARIO`, sólo campañas mapeadas | Histórico disponible hasta hoy, con tope de cinco años. |
| t_colas_intervalo | `t_colas_intervalo` | `REPORTING.DATOS_COLAS`, filtro Genesys original | Siete semanas cerradas más la actual hasta hoy. |
| t_workloads_diario | `t_workloads_diario` | Series nativas de Peopleware agregadas por día | Histórico disponible desde el 29/09/2025, con tope de cinco años, más el horizonte de forecast. |
| t_workloads_intervalo | `t_workloads_intervalo` | Peopleware, salida de 30 minutos | Siete semanas cerradas y seis semanas desde el lunes actual. |
| t_ejecutivos_diario | `t_ejecutivos_diario` | `REPORTING.DATOS_EJECUTIVOS` | Histórico disponible hasta hoy, con tope de cinco años; conserva sus 37 columnas. |
| t_estados_agentes_intervalo | `t_estados_agentes_intervalo` | `REPORTING.DATOS_ESTADOS_AGENTES` | Siete semanas cerradas más la actual hasta hoy; sólo observaciones reales. |
| t_asignaciones_historial | `t_asignaciones_historial` | `REPORTING.HISTORIAL_ASIGNACIONES` y catálogo de campañas | Historial disponible con campaña; una fila por asignación, conservando los extremos abiertos. |
| t_turnos_diario | `t_turnos_diario` | `ANALYTICS_BASE.FACT_TURNOS` y asignación vigente | Desde `--desde` hasta `--hasta-workloads`; una fila por RUT y fecha, incluyendo programación futura disponible. |
| t_licencias_vacaciones | `t_licencias_vacaciones` | `REPORTING.LICENCIAS_VACACIONES` y catálogo de campañas | Vista completa sin filtros adicionales de fechas o estado; una fila por tramo de ausencia y campaña. |

Cada hoja de datos y su tabla tienen el mismo nombre, en minúsculas y con prefijo `t_`. Los nombres indican su granularidad (`diario`, `intervalo` o `historial`) o su contenido (`licencias_vacaciones`). Las nueve hojas de datos se ocultan con la opción normal de Excel y se pueden volver a mostrar. `ejemplo_de_uso` queda visible y activa; las demás hojas de análisis y gráficos permanecen visibles y pueden tener nombres propios.

Estos períodos son la configuración automática de `rev_plantilla_reportes.ipynb`, usando la fecha de Santiago. Las siete semanas cerradas terminan el último domingo; la actual es la octava y se va completando. Las seis semanas de forecast incluyen la actual. Los extremos de fechas son inclusivos. Los diarios incluyen también los días recientes: no sumar entre sí las tablas diarias y las de intervalos.

Las duraciones son **horas decimales**, sin dividir por 24. Los TMO y otros valores en segundos conservan segundos. Estados convierte sus doce duraciones SQL a horas y adapta sólo su RUT al formato con guion de Ejecutivos; no crea una grilla ni rellena ausencias. Fecha e intervalo son tipos reales de Excel, los RUT y UUID siguen siendo texto y los nulos quedan vacíos.

Estados conserva también las duraciones superiores a los 30 minutos del intervalo para revisarlas mediante alertas en Excel. El ETL puede reprocesar datos que Genesys corrija posteriormente; una nueva ejecución del reporte tomará esa versión de la vista. Las claves duplicadas y los cambios de esquema siguen deteniendo la extracción para proteger el grano y los totales.

Asignaciones conserva las seis columnas de origen y añade `campana_id`, `cliente` y `jefe_negocio`. Turnos conserva sus once columnas y añade `nombre`, `campana`, `contrato`, `campana_id`, `cliente`, `jefe_negocio` y `estado_asignacion`. Ambas usan RUT de texto con guion para relacionarlas con Ejecutivos. El catálogo completo de campañas aporta sus dimensiones; no se exige tener unidad de Peopleware.

`contrato` en Asignaciones y Turnos se exporta como `Int64`, con formato Excel `0`. Lo mismo ocurre con `person_id_pw`, `id_ausencia_buk` e `id_empleado_buk` en Licencias/Vacaciones. Estos cinco campos se convierten estrictamente desde el texto de SQL, conservando NULL y eliminando los ceros iniciales por solicitud del usuario. La base sigue almacenándolos como `varchar`; la conversión sólo afecta la exportación. Los RUT permanecen como texto con guion.

Licencias/Vacaciones conserva sus quince columnas originales y añade `campana_id`, `cliente` y `jefe_negocio`: dieciocho columnas. Su grano es el tramo de ausencia por campaña; `id_ausencia_buk` puede repetirse entre tramos o campañas y no se deduplica por ese ID. `dias_calendario_total_ausencia_buk` conserva el valor de origen como `Float64`, incluidas sus fracciones; no se suma, pondera ni distribuye por día durante la extracción. Las fechas mantienen tipo Date, los tres identificadores se exportan como `Int64` y el RUT usa texto con guion; `campana_id` sigue siendo entero. Se exporta la vista completa, sin limitar empleados a los que aparezcan en Ejecutivos u otras tablas. El catálogo completo aporta las dimensiones sin exigir asociación con Peopleware.

La asignación de cada turno se resuelve por RUT y `fecha`, con inicio y término inclusivos; una fecha de asignación nula deja abierto ese extremo. `fecha` debe coincidir con la fecha de `inicio_turno`. Los ocho campos de inicio y fin son fechas y horas reales de Excel, con formato `dd-mm-yyyy hh:mm:ss`, y conservan el día siguiente en turnos nocturnos. No se expanden turnos a intervalos ni se cambian las horas decimales de las otras tablas. Los turnos sin coincidencia o con coincidencias ambiguas conservan una sola fila en el general, con estado explícito; se excluyen de los cortes con diagnóstico.

Workloads diario suma cada volumen por separado. El TMO histórico se pondera por contestadas; el TMO de cada forecast, por su propio volumen. Sin peso positivo o con volumen positivo sin TMO, el TMO diario queda nulo. `requerido_horas` suma dotación por duración de los intervalos conocidos. Las variantes son alternativas: no sumar sus forecasts entre sí. El diario se calcula desde las series originales, antes de agrupar a 30 minutos. Se conservan las alertas de cobertura y de cambios de hora. `utc_offset_minutos` permanece en la tabla por intervalo para distinguir horas repetidas.

En las dos tablas Workloads, las parejas `forecast_<variante>`/`tmo_<variante>` se reservan para series de Peopleware. Si la plantilla conserva una pareja y la API no devuelve esa variante en el rango consultado, el generador mantiene ambas columnas y las deja vacías. Esto incluye variantes como `strategic`, `version3` o `version4`; no conserva los datos antiguos ni los sustituye por cero. Las parejas con fórmulas de columna siguen siendo calculadas; si hay fórmulas individuales sin ese metadato, el proceso se detiene antes de borrarlas. Una variante nueva que no tenga columnas en la plantilla requiere actualizar su estructura.

## Crear y personalizar la base

Ejecutar `rev_plantilla_reportes.ipynb`: crea **`archivos/plantilla_reportes_base.xlsx`** con nueve hojas de datos ocultas y nueve tablas. Si falta `ejemplo_de_uso`, crea esa hoja y la deja visible y activa. Actualiza las fechas al ejecutar; `MAX_ANOS_HISTORIA`, `SEMANAS_CERRADAS`, `SEMANAS_WORKLOADS_DESDE_LUNES` e `INICIO_PEOPLEWARE` permiten ajustar sus alcances. No requiere los tres Excel anteriores. Asignaciones y Licencias/Vacaciones conservan sus historiales completos, aunque el notebook de pruebas use un rango pequeño. Si la plantilla ya está personalizada, seguir usándola; ejecutar este notebook crea una base nueva. La ampliación de una plantilla existente debe preservar sus personalizaciones y guardar un respaldo previo.

Con fecha de ejecución 10/10/2026, solicita Colas y Ejecutivos diarios del 10/10/2021 al 10/10/2026, intervalos reales del 17/08/2026 al 10/10/2026, Workloads diario del 29/09/2025 al 15/11/2026 y Workloads por intervalo del 17/08/2026 al 15/11/2026. Sólo se exporta el histórico disponible. Guardar los cierres mensuales como libros separados para conservar el corte de cada ejecución.

Crear copias de la base para personalizar el reporte general, el de cliente y el de jefe. Conservar los nombres de tablas, hojas y columnas de origen; se pueden añadir hojas de análisis y columnas calculadas con fórmula uniforme, incluso intercaladas. Dejar espacio libre debajo de las tablas para su crecimiento. Los datos manuales adicionales deben ir fuera de las tablas de origen.

Cada salida se escribe sobre una copia y se comprueban sus valores, tipos y rangos antes de reemplazar el destino. Las tablas pueden crecer, reducirse o quedar con una fila vacía. Se conservan las fórmulas y los formatos de la plantilla; al reducir, se limpian las filas retiradas del antiguo cuerpo.

Después de llenar cada reporte, el generador calcula las fórmulas fuente y ejecuta «Actualizar todo»: las consultas Power Query incluidas en esa actualización alimentan el modelo de Power Pivot. Espera las operaciones pendientes, actualiza las tablas dinámicas y recalcula el análisis antes de guardar. La espera no tiene un plazo fijo; muestra progreso periódico mientras Excel trabaja. Los segmentadores conservan sus conexiones y selecciones. Un error detectado interrumpe ese archivo y conserva el destino anterior. Esta actualización puede aumentar el tiempo por reporte; la plantilla debe mantener configuradas sus consultas y sus fuentes.

Guardar y cerrar el destino en Excel antes de regenerarlo. La lectura compartida de Windows admite los handles habituales de OneDrive y comprueba cambios concurrentes.

## Generar los reportes

```powershell
.\.venv\Scripts\python.exe .\generar_reportes.py --desde 2021-10-10 --hasta 2026-10-11 --desde-intervalos 2026-08-17 --desde-workloads 2026-08-17 --hasta-workloads 2026-11-15
```

`--desde` es obligatorio para elegir conscientemente el histórico. `--hasta` por defecto es la fecha actual de Santiago. La plantilla por defecto es `archivos/plantilla_reportes_base.xlsx`. Las salidas son:

- `archivos/reporte_general.xlsx`.
- `archivos/archivos_por_cliente/`: un libro por cliente.
- `archivos/archivos_por_jefe/`: un libro por `jefe_negocio`, con nombre `reporte_por_jefe_nombredeljefe.xlsx`.
- `archivos/resumen_generacion.json`: períodos, conteos, cobertura, exclusiones y estado de los archivos generados.

El script permite elegir los períodos explícitamente. `--desde-intervalos` fija el lunes de inicio del detalle. Si se omite, usa las últimas ocho semanas terminando en `--hasta`; `--semanas-intervalos` ajusta esa ventana. `--desde-workloads` evita consultar Peopleware antes de su inicio. `--hasta-workloads` extiende el forecast y los turnos programados, conservando `--hasta` para las cuatro vistas de observaciones reales; por defecto coincide con `--hasta`. Asignaciones y Licencias/Vacaciones conservan sus historiales sin expandirlos por día ni aplicarles esos filtros de fechas. El tope de cinco años lo configura el notebook para las fuentes que usan esa ventana; el script respeta el histórico solicitado.

Consultar años de Workloads puede requerir muchas llamadas; no se promete que la API conserve todas las versiones históricas de forecast. `resumen_generacion.json` es un diagnóstico de la última ejecución, no una entrada: puede borrarse y se vuelve a crear al generar reportes.

```powershell
.\.venv\Scripts\python.exe .\generar_reportes.py --desde 2026-09-28 --hasta 2026-10-10 --hasta-workloads 2026-11-15 --listar
.\.venv\Scripts\python.exe .\generar_reportes.py --desde 2026-09-28 --hasta 2026-10-10 --hasta-workloads 2026-11-15 --cliente "Mutual" --jefe "Nombre exacto"
.\.venv\Scripts\python.exe .\generar_reportes.py --desde 2026-09-28 --hasta-workloads 2026-11-15 --plantilla .\archivos\plantilla_general.xlsx --plantilla-cliente .\archivos\plantilla_cliente.xlsx --plantilla-jefe .\archivos\plantilla_jefe.xlsx
```

`--listar` consulta y muestra los cortes sin crear Excel. `--cliente` y `--jefe` pueden repetirse; al omitirlos se generan todos los cortes disponibles. `--sin-general`, `--sin-clientes` y `--sin-jefes` omiten esas salidas. Los tres argumentos `--salida-*` permiten cambiar los destinos. Los selectores afectan a los archivos separados; el general conserva las fuentes completas.

Los cortes particionan cada tabla independientemente, sin unir hechos. Colas usa sus propias dimensiones; Workloads usa `planning_unit_id` de `analytics_base.map_campana_pw`. Ejecutivos y Estados utilizan el nombre exacto de unidad y comprueban el jefe. Asignaciones, Turnos y Licencias/Vacaciones usan las dimensiones de su campaña; Turnos requiere una asignación vigente única. Las asociaciones ambiguas o incompletas se excluyen sólo del corte correspondiente y se registran en el resumen; permanecen en el libro general para su revisión. El resumen también guarda los conteos por `estado_asignacion`. No se infiere el cliente por el nombre de un Workload.

## Respaldo

`archivados/bases_plantillas/` conserva los generadores independientes de Colas, Workloads y Ejecutivos. Sus tres Excel están en `archivados/bases_plantillas/archivos/`, y los notebooks escriben allí al ejecutarse. `archivados/excel_anteriores/` conserva los dos ejemplos, la plantilla anterior y los antiguos cortes por campaña. `archivados/` conserva los scripts anteriores de corte, las pruebas iniciales y `rev_forecast.ipynb`. Los módulos de extracción y escritura son compartidos; no hace falta ejecutar estos respaldos para el flujo vigente.

El temporal de Ejecutivos y `archivos/validacion/` se eliminaron. El contrato y las evidencias compactas de validación permanecen en `docs/PLAN_REPORTES.md`, `docs/VALIDACION_FUENTES.json` y `docs/VALIDACION_IMPLEMENTACION.json`; esos JSON documentan las comprobaciones anteriores y no son entradas del generador.


.\.venv\Scripts\python.exe .\generar_reportes.py --desde 2021-10-10 --hasta 2026-10-13 --desde-intervalos 2026-08-24 --desde-workloads 2026-08-24 --hasta-workloads 2026-11-29 --sin-clientes
