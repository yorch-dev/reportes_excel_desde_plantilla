# Plan de reportes: nueve tablas y una extracción por ejecución

Estado actual: ampliación a nueve tablas y política de hojas implementadas y
validadas el 14/10/2026.
El ajuste posterior de tipos numéricos de exportación está pendiente de su
validación final, descrita en un apartado separado.
La ampliación anterior a ocho tablas se implementó y validó el 13/10/2026.
La plantilla personalizada se amplió con cinco filas de muestra por tabla nueva,
conservando sus seis tablas anteriores, `Hoja1` y sus fórmulas. Se guardó la
versión anterior en `archivados/plantillas_anteriores/`.

La implementación inicial de seis tablas se completó y validó con una muestra real
del 08/10/2026 al 12/10/2026. El flujo vigente es `generar_reportes.py`, junto con
`rev_plantilla_reportes.ipynb` y `rev_pruebas_reportes.ipynb`.
La evidencia inicial está en [VALIDACION_FUENTES.json](VALIDACION_FUENTES.json),
y la validación integrada en [VALIDACION_IMPLEMENTACION.json](VALIDACION_IMPLEMENTACION.json).

Comprobaciones de la ampliación del 13/10/2026:

- 56 pruebas automáticas pasan: vigencias, extremos abiertos, ambigüedades,
  conservación de filas y nulos, cortes y fechas/horas Excel 1900/1904.
- Extracción real completa: 811 asignaciones × 9 columnas y 150.737 turnos ×
  18 columnas. Las seis y once columnas originales, respectivamente, coinciden
  con la fuente tras aplicar el formato de RUT con guion. No se modificó la base.
- Los 143.381 turnos con asignación vigente y los 7.356 sin asignación se
  conservaron. Los 5.643 turnos que cruzan medianoche mantienen ambas fechas.
- Generación real controlada: observaciones del 08/10/2026, programación y
  forecast hasta el 15/10/2026; libro general, un cliente y un jefe, con ocho
  tablas verificadas celda por celda. El general conserva 2.827 turnos, incluidos
  73 sin asignación. La plantilla mantuvo el mismo hash durante la generación.
- Los tres libros de comprobación fueron temporales. En esa versión, el
  comando habitual generaba ocho tablas en las salidas seleccionadas. Para
  usar los módulos actualizados desde un notebook, reiniciar su kernel.

## Ampliación del 14/10/2026: Licencias/Vacaciones y hojas

El contrato se amplía a nueve tablas: siete fuentes SQL y los dos resultados de
Workloads. `t_licencias_vacaciones` lee `REPORTING.LICENCIAS_VACACIONES` completa,
sin filtros adicionales por fecha o estado ni limitar empleados a los presentes
en Ejecutivos u otras tablas. Conserva sus quince columnas originales y añade
`campana_id`, `cliente` y `jefe_negocio`, hasta completar dieciocho columnas.

El grano es el tramo de ausencia por campaña. Un `id_ausencia_buk` puede aparecer en
varios tramos o campañas y no se usa como clave única para deduplicar. El total
de días de Buk conserva sus valores y fracciones originales: no sumar, ponderar,
recalcular ni expandir ausencias por día durante la extracción. Las fechas son
Date y los RUT mantienen texto con guion. Tras el ajuste de tipos de exportación,
`person_id_pw`, `id_ausencia_buk` e `id_empleado_buk` son Int64, al igual que
`campana_id`. El total de origen se conserva como Float64 en
`dias_calendario_total_ausencia_buk`.

Los cortes usan las dimensiones propias de la campaña desde el catálogo
completo, sin exigir asociación con Peopleware ni presencia del empleado en
otros hechos. Las filas sin campaña, ID o dimensión válidos permanecen en el
general y se excluyen únicamente del corte correspondiente con diagnóstico.
Las reglas de atribución de las ocho fuentes anteriores se mantienen.

La muestra de origen disponible para esta ampliación tiene 931 registros,
416 RUT y 63 campañas. Contiene `LICENCIA`, `VACACION` y `DIA_ADMINISTRATIVO`;
los extremos de sus tramos van del 10/04/2026 al 09/01/2027. Estos conteos
describen la vista revisada, no prueban que las salidas actuales ya se hayan
regenerado con la novena tabla.

Las nueve hojas de datos se ocultan mediante la opción normal de Excel.
`ejemplo_de_uso` queda visible y activa; las demás hojas de análisis y gráficos
permanecen visibles. Las futuras creaciones añaden `ejemplo_de_uso` si falta.
La plantilla personalizada se amplió con cinco filas de muestra de Licencias,
conservando las fórmulas `FormulaR1C1` de sus nueve hojas anteriores y los
metadatos de sus ocho tablas anteriores. Se guardó el respaldo previo en
`archivados/plantillas_anteriores/plantilla_reportes_base_antes_9_tablas_20261014_105209.xlsx`.

Validación inicial de esta ampliación, anterior al ajuste de tipos:

- 75 pruebas automáticas pasan, incluyendo preservación de tramos, IDs repetidos,
  fracciones, nulos, cortes, visibilidad y creación de la hoja de ejemplo.
- Extracción completa: 931 filas por 18 columnas, 416 RUT y 63 campañas.
  Las quince columnas originales concilian exactamente con la vista tras
  presentar el RUT con guion; no hay filas sin `campana_id`.
- La plantilla migrada contiene las nueve hojas de datos ocultas con la opción
  normal de Excel y `ejemplo_de_uso` visible y activa. Las hojas de análisis y
  gráficos permanecen visibles y las personalizaciones anteriores se conservan.
- Generación real controlada: observaciones SQL del 08/10/2026 y Peopleware del
  08/10/2026 al 15/10/2026; libro general, un cliente y un jefe, con las nueve
  tablas comprobadas celda por celda en XML y con su visibilidad verificada.
  Licencias conserva 931 filas en el general y 351 en cada corte seleccionado.
  Los 931 registros completos concilian entre todos los cortes de cliente y
  entre todos los cortes de jefe, sin pérdidas ni duplicación.
- Creación de una base nueva mediante `crear_plantilla`: una fila por fuente,
  nueve tablas de datos ocultas y `ejemplo_de_uso` creada, visible y activa.
- El hash de la plantilla personalizada no cambió durante la generación de
  reportes. Los libros de comprobación fueron temporales y se eliminaron;
  los reportes anteriores del usuario no se regeneraron en esta validación.

La próxima ejecución del comando habitual incorporará las nueve tablas en las
salidas seleccionadas. Reiniciar el kernel antes de usar los módulos nuevos
desde un notebook. La evidencia de la ampliación del 13/10/2026 y las
validaciones históricas siguientes se conserva como registro de esas versiones.

## Ajuste de tipos de exportación: validado el 14/10/2026

Por solicitud explícita del usuario, cinco columnas de exportación pasan a
Int64 con formato Excel `0`:

- `contrato` en Asignaciones y en Turnos.
- `person_id_pw`, `id_ausencia_buk` e `id_empleado_buk` en Licencias/Vacaciones.

La conversión desde el texto de origen es estricta, conserva NULL y elimina los
ceros iniciales de esos campos. SQL mantiene sus columnas `varchar`; no se
modifica la base. Los RUT siguen siendo texto con guion. Las fechas mantienen
Date, el total de días Buk conserva Float64 y `campana_id` sigue siendo entero.
No cambian los alcances, filtros, granos ni criterios de corte de las fuentes.

La revisión del origen identificó los códigos de contrato 12, 20, 24, 25, 30,
41 y 42. Los máximos actuales son seis dígitos para `person_id_pw` y cinco para
cada identificador Buk. Estos datos describen la revisión de origen, no una
restricción de longitud añadida al contrato de exportación.

La validación final de este ajuste completó 81 pruebas automatizadas y una
exportación real en Excel de 811 asignaciones, 526 turnos del 08/10/2026 y
931 registros de Licencias/Vacaciones. Los cinco campos llegaron como números
enteros; las columnas originales conciliaron con SQL tras la conversión y la
presentación del RUT con guion.

La plantilla personalizada se convirtió únicamente en esas cinco columnas:
130 filas de contrato en Asignaciones, 26.183 en Turnos y 185 por cada ID de
Licencias. Las 26.868 posiciones conservaron sus valores y nulos, con formato
`0`. Se compararon las celdas y fórmulas de sus once hojas y los metadatos de
sus nueve tablas; el resto del contenido y la visibilidad quedaron iguales.
El respaldo está en
`archivados/plantillas_anteriores/plantilla_reportes_base_antes_tipos_numericos_20261014_185349.xlsx`.
El libro de prueba fue temporal; los reportes anteriores no se regeneraron.

Las 75 pruebas y la evidencia del 14/10/2026 registradas en
el apartado anterior corresponden a la ampliación inicial de nueve tablas.

## Actualización al terminar cada reporte

El flujo principal activa `actualizar_conexiones=True` en el escritor compartido.
Después de escribir las tablas, calcula las fórmulas que Power Query pueda leer,
ejecuta `RefreshAll` una vez y espera consultas OLEDB/OLAP, los indicadores de
actividad y el cálculo de Excel. Luego actualiza las tablas dinámicas y vuelve
a calcular los resultados antes de guardar y verificar el libro. No aplica un
timeout fijo ni guarda mientras las operaciones observadas siguen activas.
Los avisos de progreso continúan durante las fases largas.

Los segmentadores y sus conexiones y selecciones se conservan. La actualización
respeta `RefreshWithRefreshAll` de las conexiones de la plantilla. No añade un
`Model.Refresh` posterior que vuelva a consultar todas las fuentes. Los errores
COM, fallos de QueryTables observables y `RefreshTable=False` detienen la copia;
el destino anterior queda protegido por la publicación atómica del escritor.
`RefreshAll` no aporta un Boolean global de éxito para consultas que cargan sólo
al modelo: la validación real debe comprobar que el modelo recibe los datos
nuevos, además de verificar tablas, estados y pivots.

Los generadores archivados mantienen el comportamiento anterior; la creación
de una base nueva no crea consultas ni reconstruye el modelo del usuario.
Validación del 14/10/2026: 91 pruebas automatizadas pasan. En una copia temporal
de la plantilla personalizada se redujeron las nueve tablas a dos filas por
tabla y se ejecutó el escritor con la actualización completa. Después de
guardar y volver a abrir, las nueve tablas del modelo tenían exactamente dos
filas; sus cantidades anteriores iban de 130 a 76.242 filas. También se
verificaron las celdas fuente y la ocultación de las hojas de datos.

Las once consultas conservaron sus nombres y fórmulas M; las doce conexiones,
los dos segmentadores y la tabla dinámica conservaron sus vínculos. El hash
de la plantilla original quedó igual. Sólo se generó un libro temporal, que
se eliminó; los reportes anteriores no se regeneraron. La prueba demuestra
la actualización del modelo de esta plantilla con una muestra reducida,
sin establecer el tiempo que requerirá el histórico completo.

Referencias: [RefreshAll](https://learn.microsoft.com/en-us/office/vba/api/excel.workbook.refreshall),
[espera OLEDB/OLAP](https://learn.microsoft.com/en-us/office/vba/api/excel.application.calculateuntilasyncqueriesdone),
[RefreshTable](https://learn.microsoft.com/en-us/office/vba/api/excel.pivottable.refreshtable),
[Model.Refresh](https://learn.microsoft.com/en-us/office/vba/api/excel.model.refresh).

## Contrato de fuentes y tablas

Las hojas y tablas de datos comparten nombre en minúsculas, con prefijo `t_` y
referencia a su granularidad o contenido. La nomenclatura no cambia el significado
de las fuentes.

| Hoja | Tabla | Fuente | Alcance y grano |
|---|---|---|---|
| t_colas_diario | `t_colas_diario` | `REPORTING.DATOS_COLAS_DIARIO` | Histórico disponible, diario; `campana_id IS NOT NULL`. |
| t_colas_intervalo | `t_colas_intervalo` | `REPORTING.DATOS_COLAS` | Siete semanas cerradas más la actual; observaciones por intervalo. |
| t_workloads_diario | `t_workloads_diario` | Peopleware: agregado diario de series originales | Un día por Workload; alcance histórico condicionado a lo que entregue la API. |
| t_workloads_intervalo | `t_workloads_intervalo` | Peopleware: datos por intervalo | Siete semanas cerradas y seis desde el lunes actual, a 30 minutos, con desplazamiento UTC. |
| t_ejecutivos_diario | `t_ejecutivos_diario` | `REPORTING.DATOS_EJECUTIVOS` | Histórico disponible de la vista diaria, conservando sus columnas y valores. |
| t_estados_agentes_intervalo | `t_estados_agentes_intervalo` | `REPORTING.DATOS_ESTADOS_AGENTES` | Siete semanas cerradas más la actual; observaciones reales de estado/conexión, ya mapeadas. |
| t_asignaciones_historial | `t_asignaciones_historial` | `REPORTING.HISTORIAL_ASIGNACIONES` | Historial completo con campaña, extremos abiertos y dimensiones de campaña. |
| t_turnos_diario | `t_turnos_diario` | `ANALYTICS_BASE.FACT_TURNOS` | Una fila por RUT/fecha desde `desde` hasta `hasta_workloads`, con campaña y contrato vigentes. |
| t_licencias_vacaciones | `t_licencias_vacaciones` | `REPORTING.LICENCIAS_VACACIONES` | Vista completa; tramo de ausencia por campaña, con fechas y días Buk originales. |

Colas, Workloads y Ejecutivos conservan su grano y significado. Los respaldos
archivados y las evidencias históricas mantienen la nomenclatura de su versión.

El notebook fija `hasta` en hoy, según Santiago, para los datos reales SQL.
El detalle comienza siete semanas antes del lunes actual: siete semanas completas
hasta el último domingo y la actual hasta hoy. Los diarios conservan el histórico
disponible con un tope de cinco años. Workloads empieza el 29/09/2025 o en el límite
de cinco años, si éste es posterior, y su `hasta_workloads` llega al domingo de la
sexta semana desde el lunes actual. Esa ventana de forecast incluye la actual y
no amplía las cuatro vistas SQL de observaciones reales. Turnos comparte el
extremo futuro del forecast. Las fechas son inclusivas.

El script admite `--desde-intervalos` para ese inicio exacto, junto con fechas
propias de Workloads. Sin inicio explícito, conserva la ventana móvil de 56 días
terminando en `--hasta`; `--semanas-intervalos` permite ajustarla. No se promete
histórico completo de Workloads hasta comprobar cobertura y límites de la API;
registrar el período solicitado y el realmente disponible. Los cierres mensuales
se conservan como copias separadas, sin cambiar la ventana de futuras ejecuciones.

## Datos, unidades y cálculos

- Leer directamente las cuatro vistas SQL; no reconstruir los KPI de Colas ni
  sus pesos o SLA. El usuario hará los análisis en Excel y PowerPivot.
- Conservar las horas decimales ya entregadas por Colas diario, incluido su
  redondeo de origen. No dividir las duraciones por 24.
- En Estados, convertir las duraciones de tipo intervalo SQL a horas mediante
  segundos totales / 3600, conservando nulos. Sólo la hora de reloj se exporta
  como fracción de día de Excel; los tiempos expresados en segundos siguen así.
- Normalizar RUT en Estados al formato acordado para asociarlo con Ejecutivos.
  No cambiar el RUT ni el resto de columnas de Ejecutivos. La vista de Estados
  ya aporta el mapeo; no añadir reconstrucciones de identidad o campaña.
- Conservar únicamente observaciones reales de Estados. No construir una grilla
  de todos los agentes por todos los intervalos ni rellenar ausencias con ceros.
- Exportar las duraciones de Estados tal como están en la vista, incluso cuando
  superan los 30 minutos del intervalo. El usuario construirá alertas en Excel y
  reprocesará en el ETL las correcciones posteriores de Genesys. Conservar las
  guardas de tipos, claves, raster y duraciones negativas.
- En Workloads, mantener las variantes de forecast como alternativas separadas.
  Al agregar, sumar volúmenes; ponderar TMO histórico por sus contestadas y TMO
  de cada forecast por el volumen de esa misma variante. Sin peso válido, dejar
  TMO nulo y registrar cobertura; no inventar pesos.
- El requerido diario se expresa en horas-agente: suma de requerido por duración
  real del intervalo en horas. A 30 minutos, equivale a `SUM(requerido) * 0,5`
  sobre los intervalos válidos. Conservar las guardas de actividades compartidas
  y jerarquías para evitar duplicarlo entre Workloads.
- Mantener la distinción entre nulo y cero, y las claves UTC internas. Exportar
  `utc_offset_minutos` en Workloads por intervalo para distinguir horas repetidas.
- Diarios e intervalos son hechos solapados para usos distintos. No apilarlos ni
  sumarlos entre sí, ni repetir KPI diarios de un agente en cada fila de Estados.
- Asignaciones y Turnos usan el catálogo completo de campañas, dentro del mismo
  snapshot de las otras vistas. La coincidencia es por RUT y fecha con vigencia
  inclusiva y extremos nulos abiertos; no multiplicar turnos por asignaciones.
- Conservar los turnos sin asignación y los ambiguos en el general, con estado
  explícito y diagnóstico de exclusión en cada corte. Mantener las seis fuentes
  anteriores con sus reglas de atribución actuales.
- Validar que `fecha = inicio_turno::date`. Preservar las ocho fechas y horas de
  programación, incluidos turnos y pausas que cruzan medianoche; no expandirlos
  a 48 filas diarias. Mantener RUT como texto y pausas nulas como vacías.

## Validación realizada y límites

Consultas SQL en lectura para el 08/10/2026 y revisión del Excel de Workloads
existente, sin nuevas consultas API ni COM. La evidencia agregada y los esquemas
completos están en el JSON enlazado; no contiene datos personales ni credenciales.

| Fuente | Filas comprobadas | Resultado de claves e intervalos |
|---|---:|---|
| Colas por intervalo | 2.241 | Sin duplicados en fecha/intervalo/id_cola/medio; intervalos de 30 minutos. |
| Colas diario | 340; 154 mapeadas | Sin duplicados en fecha/id_cola/medio/división; filtrar las 186 sin campaña. |
| Ejecutivos diario | 502 | Sin duplicados en RUT/fecha; tipado actual válido. |
| Estados agentes | 8.458 de 10.246 de origen | Sin duplicados en RUT/fecha/intervalo; raster fuente de 30 minutos. |
| Workloads, Excel existente | 40.320 | 30 Workloads, 28 días, 28 columnas; sin duplicados, valores negativos ni tipos inválidos. |

Estados excluye 1.662 registros de origen sin mapa Genesys y otros 126 por los
requisitos restantes de la vista. La salida conserva 1.046 valores nulos de
conectado: no representan conexión cero. Su RUT no lleva guion, mientras los
502 RUT de Ejecutivos sí; adaptar únicamente el formato exportado de Estados.

Workloads se comprobó para el 08/10 y el 12/10. El futuro conserva históricos
nulos; import y adjustments pueden tener volumen positivo sin TMO. El diario
mínimo tendrá 26 columnas: retirar intervalo y offset, y renombrar requerido a
requerido_horas. Añadir requerido_medio y horas_requerido_cubiertas sólo si se
necesitan esas dos medidas. Las series ausentes y el peso cero conservan NULL.

Esta muestra no prueba toda la historia, los cambios de hora ni las series API
nativas: el Excel no contiene sus arrays, raster original, outdated o volúmenes
omitidos antes de normalizar. Esas comprobaciones corresponden a la nueva
agregación de Workloads. Procesarla por bloques y conservar el detalle reciente,
sin acumular en memoria toda la historia de intervalos.

## Implementación realizada

Los tres frontends son `rev_plantilla_reportes.ipynb`, `generar_reportes.py` y
`rev_pruebas_reportes.ipynb`. El README describe su uso y sus argumentos.
El siguiente orden conserva el alcance del plan original ya implementado.

1. Usar el JSON enlazado para fijar columnas, tipos, mapeos y unidades. Cerrar el
   contrato diario con series nativas de Peopleware; verificar claves y cobertura
   en cada extracción, pues la muestra no demuestra todo el histórico.
2. Crear `fuentes_sql.py` para las cuatro lecturas SQL directas y adaptar
   `datos_reportes.py` para los dos resultados de Workloads. Reutilizar autenticación, asociaciones y guardas
   actuales; obtener cada dato necesario una vez y conservar los resultados.
   Consultar el diario histórico en SQL y acotar allí los intervalos, evitando
   descargar todo el histórico de intervalos para después agruparlo en Python.
3. Crear una función compartida de preparación que devuelva los seis DataFrames,
   formatos y diagnósticos de la misma ejecución. Cargar el mapa de dimensiones
   una vez. No volver a consultar fuentes para cada cliente o jefe.
4. Generalizar los cortes y definir `tablas_reportes` para las seis tablas.
   Aplicar los criterios existentes sin unir los hechos ni duplicar filas.
   El criterio de jefe seguirá siendo `jefe_negocio`. Mantener visibles las
   exclusiones por mapeos ambiguos o insuficientes.
5. Adaptar `rev_plantilla_reportes.ipynb` para consultar las funciones compartidas
   y crear directamente un respaldo con seis hojas/tablas. No depender de los
   tres Excel independientes ni ampliar su combinador. Separar este respaldo
   regenerable de las plantillas que el usuario modifica.
6. Crear un único `.py` principal: preparar los datos una vez, generar el Excel
   global y después los reportes en `archivos/archivos_por_cliente/` y
   `archivos/archivos_por_jefe/`, las carpetas existentes del usuario. Permitir seleccionar plantillas y fechas. Generar libros
   secuencialmente; conservar diagnóstico de éxitos y errores por archivo.
7. Crear `rev_pruebas_reportes.ipynb` usando las mismas funciones. Probar rangos y
   unidades pequeños, tipos, claves, cobertura, agregación de Workloads y una
   salida Excel; evitar copias de la lógica de producción.
8. Tras validar el flujo nuevo, mover los tres notebooks base a
   `archivados/bases_plantillas/` y archivar las entradas anteriores por cliente
   y jefe. Actualizar README y rutas; no archivar el flujo vigente antes de que
   exista un reemplazo probado.

## Preservación de plantillas y aceptación

- Mantener los libros y ajustes actuales del usuario hasta disponer del nuevo
  respaldo y completar la migración. No sobrescribirlos al probar.
- Reutilizar `excel_reportes.py`: escritura sobre copia, validación XML y
  publicación atómica; fechas/horas reales, texto para RUT y UUID, números y
  nulos conservados. El escritor ya admite más de tres tablas.
- Respetar columnas calculadas uniformes, incluso intercaladas; formatos de filas
  vigentes, otras hojas y elementos externos. Rechazar columnas extra manuales
  dentro de tablas fuente. Al reducir, limpiar sólo formatos del cuerpo retirado.
- Probar crecimiento, reducción y cero filas; esquema conservado mediante fila
  vacía cuando corresponda. Validar nombres, encabezados, tipos y valores de las
  seis tablas antes de publicar. Comprobar también tamaño y ausencia de cuerpos
  antiguos con datos o formatos residuales.
- Comprobar claves y ausencia de duplicación con datos reales. Validar los
  límites de Excel antes de exportar: una grilla de 400 agentes por 56 días y
  48 intervalos tendría 1.075.200 filas y excedería una hoja.
- Conciliar los cortes con el global, separando las filas excluidas por mapeo.
  Conciliar diario/intervalo sólo en el período común y con métricas compatibles.
- Mantener el refresco actual limitado a pivots locales. El usuario preparará sus
  cálculos y modelo PowerPivot; no reconstruirlos en Python ni prometer que el
  escritor actual refresca ese modelo o conexiones externas.

## Resultado de la migración

- Base nueva: `archivos/plantilla_reportes_base.xlsx`, seis hojas y seis tablas.
  La plantilla anterior de tres tablas está en `archivados/excel_anteriores/`.
- Muestra real: Colas diario 154 × 60; Colas 2.241 × 40; Workloads diario
  150 × 26; Workloads 7.200 × 28; Ejecutivos 2.510 × 37; Estados 8.458 × 21.
- Libro general y cortes de muestra para Mutual y Carla Palacios generados con
  la misma extracción, verificados en sus valores, tipos y rangos XML.
- Workloads diario concilia las series del 8 y del 12 de octubre con la evidencia
  anterior. Se conservan nulos y cobertura parcial. TMO usa sus propios pesos
  observados; si falta TMO para un peso positivo, el agregado queda nulo.
- Pruebas automatizadas: cortes, ventanas, extracción única, bloques de API,
  variantes dinámicas, pesos, nulos y días de 23/25 horas. Estas pruebas de DST
  son sintéticas; no prueban un cambio de hora real en la API ni en Estados.
- `PlanCortes` conserva referencias a los hechos y calcula propietarios una vez;
  filtra sólo el grupo que se está exportando. No mantiene todas las copias de
  clientes y jefes simultáneamente.
- Los tres notebooks base están en `archivados/bases_plantillas/`, con resolución
  de raíz adaptada, y sus Excel en su subcarpeta `archivos/`. Los scripts anteriores
  están en `archivados/`; ejemplos y cortes antiguos en `archivados/excel_anteriores/`.

El 10/10/2026 se ajustó únicamente la configuración de períodos y se ordenaron
los archivos anteriores. El notebook pide diarios SQL desde el 10/10/2021 hasta
el 10/10/2026; detalle real desde el 17/08/2026; Workloads diario desde el
29/09/2025 e intervalos desde el 17/08/2026, ambos hasta el 15/11/2026.
Las 20 pruebas automatizadas incluyen la separación del forecast futuro y el
corte SQL. No se regeneraron libros ni se consultó el histórico ampliado en este
ajuste; las evidencias JSON mantienen la muestra real anterior.

Queda por revisar el histórico y las plantillas personalizadas que el usuario
quiera utilizar. La muestra no demuestra toda la historia disponible, una ventana
real completa de 56 días ni el refresco del modelo de PowerPivot. El generador
comprueba claves, tipos, raster y límites de Excel en cada ejecución.
