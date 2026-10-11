# Plan de reportes: seis tablas y una extracción por ejecución

Estado: implementación de seis tablas completada y validada con una muestra real
del 08/10/2026 al 12/10/2026. El flujo vigente es `generar_reportes.py`, junto con
`rev_plantilla_reportes.ipynb` y `rev_pruebas_reportes.ipynb`.
La evidencia inicial está en [VALIDACION_FUENTES.json](VALIDACION_FUENTES.json),
y la validación integrada en [VALIDACION_IMPLEMENTACION.json](VALIDACION_IMPLEMENTACION.json).

## Contrato de fuentes y tablas

Conservar los nombres actuales de las tablas por intervalo y de Ejecutivos;
añadir tres tablas sin cambiar el significado de las existentes.

| Hoja | Tabla | Fuente | Alcance y grano |
|---|---|---|---|
| Colas_diario | `t_colas_diario` | `REPORTING.DATOS_COLAS_DIARIO` | Histórico disponible, diario; `campana_id IS NOT NULL`. |
| Colas | `t_datos` | `REPORTING.DATOS_COLAS` | Siete semanas cerradas más la actual; observaciones por intervalo. |
| Workloads_diario | `t_workloads_diario` | Peopleware: agregado diario de series originales | Un día por Workload; alcance histórico condicionado a lo que entregue la API. |
| Workloads | `t_workloads` | Peopleware: datos por intervalo | Siete semanas cerradas y seis desde el lunes actual, a 30 minutos, con desplazamiento UTC. |
| Ejecutivos | `t_ejecutivos` | `REPORTING.DATOS_EJECUTIVOS` | Histórico disponible de la vista diaria, conservando sus columnas y valores. |
| Estados_agentes | `t_estados_agentes` | `REPORTING.DATOS_ESTADOS_AGENTES` | Siete semanas cerradas más la actual; observaciones reales de estado/conexión, ya mapeadas. |

`t_datos`, `t_workloads` y `t_ejecutivos` conservan su grano y significado actual.
Revisar los cálculos y gráficos al añadir los nuevos diarios, sin renombrar las
referencias que ya utiliza el usuario.

El notebook fija `hasta` en hoy, según Santiago, para los datos reales SQL.
El detalle comienza siete semanas antes del lunes actual: siete semanas completas
hasta el último domingo y la actual hasta hoy. Los diarios conservan el histórico
disponible con un tope de cinco años. Workloads empieza el 29/09/2025 o en el límite
de cinco años, si éste es posterior, y su `hasta_workloads` llega al domingo de la
sexta semana desde el lunes actual. Esa ventana de forecast incluye la actual y
no amplía las consultas SQL. Las fechas son inclusivas.

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
