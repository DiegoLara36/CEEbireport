# CEEBI: decisiones de arquitectura

Este documento registra las decisiones aprobadas para evolucionar CEEBI. Son
decisiones objetivo; **no significa que ya estén implementadas**. La prioridad
es conservar las capturas funcionales de PUCP y Cibertec.

## Contexto actual verificado

- `etl/services/etl_engine.py` coordina PUCP y Cibertec, transforma al DTO común,
  ejecuta SQL mediante `django.db.connection` y registra cambios e ingestas.
- `etl/extractors/pucp.py` contiene `PucpExtractor`, centrado en extracción y
  parseo con `requests`/BeautifulSoup. La antigua clase `PucpETLService` de
  persistencia SQL fue eliminada al cerrar la Fase 4.
- `etl/extractors/cibertec.py` usa Playwright y construye `ScrapedCourseDTO`.
- `etl/schemas.py` declara contratos Pydantic específicos y comunes. Desde la
  Fase 2, el DTO común incluye docentes, contactos y estados de observación;
  `etl/adapters.py` adapta PUCP y Cibertec, con normalizadores en
  `etl/normalization.py`.
- `catalog/management/commands/run_ingestion.py` admite selección por fuente y
  `--force`. Consulta `next_run_at`, pero no implementa por sí solo un reloj,
  lock de ejecución ni recuperación de procesos abandonados.
- `DDL.txt` describe tablas operativas en el esquema por defecto, historial
  temporal de precios, triggers e índices. También contiene `DROP TABLE ...`;
  no se debe tratar como una migración segura para una base con datos.
- No se encontraron modelos Django de negocio, vistas SQL en `bi`, ni
  integración de Metabase. `catalog/views.py` consulta PostgreSQL directamente
  y presenta actualmente PUCP.

## Decisiones adoptadas

### ADR-01 — Versionado histórico

Conservar indefinidamente el historial normalizado de precios y crear versiones
de los atributos relevantes de cursos y ofertas cuando cambien, nunca por una
captura sin cambios. Cada versión debe referenciar la ejecución ETL que la
detectó. Se incluyen, según corresponda, fechas, modalidad, duración, horarios,
categorías y características académicas y comerciales.

El `change_log` actual no basta como versión completa: `course` y
`course_offering` se actualizan en sitio; algunos datos hijos se reemplazan; el
`diff` no es una fotografía íntegra y no incluye todos los campos. Se diseñará
versionado temporal consultable sin retirar las tablas actuales hasta validar
la migración.

Implementación Fase 3: `course_version`, `course_offering_version` y
`offering_commercial_version` guardan snapshots JSONB con `valid_from`,
`valid_to`, fingerprint y referencia a `ingestion_log`. Constraints de
exclusión temporal impiden intervalos superpuestos. `price_snapshot`,
`offering_price_tier` y `payment_plan` conservan sus propios historiales; el
historial comercial no copia los importes persistidos. Las versiones conservan
una referencia obligatoria y restrictiva a `ingestion_log`; la fila mínima de
cada corrida no se elimina y los textos de diagnóstico se pueden redactar.

### ADR-02 — Identidad de cursos y ofertas

Usar primero el identificador estable provisto por la institución. Cuando no
exista, derivar una clave de una URL canónica normalizada, con una ruta de
reconciliación auditable para cambios de URL o colisiones. El nombre no será
clave única. Curso y edición/oferta son entidades conceptualmente distintas.

Hoy `course.external_id` es obligatorio y la unicidad es
`(source_id, external_id)`; el DTO común también exige `external_id`. Habilitar
el fallback requerirá acordar su representación antes de cambiar el esquema o
los datos.

Implementación Fase 3: `course.canonical_url` y `course_identity_alias`
permiten reconciliar un identificador externo nuevo con una URL canónica ya
conocida. La identidad sigue sin basarse en el nombre. La unicidad de URL es
por fuente y solo aplica a valores no nulos.

Fase 4 valida que la identidad externa/alias y la URL canónica no apunten a
cursos distintos. Una colisión se envía al flujo de cuarentena, mientras que
un ID externo nuevo con URL conocida se registra como alias auditable.

### ADR-03 — Homologación

La equivalencia entre cursos de distintas instituciones se administrará
mediante grupos explícitos de homologación. No se inferirá por igualdad o
similitud de nombre. Homologación y categoría son dimensiones independientes.

`DDL.txt` ya contempla `homologation_group` y `course_homologation`; deberán
verificarse sus restricciones, flujos de administración y su uso en consultas
BI antes de considerar disponible la comparación.

### ADR-04 — Contrato de datos Pydantic

Conservar Pydantic para validar el resultado de cada extractor y el DTO común.
El contrato común incluirá identificación, institución, nombre/descripción,
categoría, fechas, duración, modalidad, horarios, docentes, precios/monedas,
descuentos, cuotas, certificados/constancias, contenidos y contactos. Los
atributos exclusivos de una fuente pueden residir en estructuras específicas,
sin sustituir los campos comunes ni perder el dato original relevante.

Antes de persistir, probar cada mapeo de extremo a extremo:
HTML/fixture → extracción → DTO validado → repositorio → tablas. Los campos
presentes hoy en un esquema de extracción no se considerarán guardados si no
cruzan explícitamente las capas.

### ADR-05 — BI en PostgreSQL

Mantener las tablas operativas en su esquema actual durante las primeras fases.
Crear un esquema independiente `bi` con vistas SQL de lectura. Metabase tendrá
un rol separado con `USAGE` en `bi` y lectura de las vistas necesarias; no
recibirá permisos de escritura ni acceso directo innecesario a tablas
operativas. No trasladar tablas existentes como parte de esta etapa.

La capa BI expondrá vistas para catálogo actual, comparación homologada,
historial de precios, ofertas, resumen ETL y cursos por categoría. Deben
documentarse granularidad, claves y filtros de cada vista. La vista comparativa
solo agrupará registros homologados.

### ADR-06 — Persistencia y propiedad del esquema

Mantener Pydantic y una arquitectura híbrida. En el corto plazo, el DDL/SQL
directo seguirá siendo responsable de las escrituras ETL existentes. Evaluar
Django ORM primero para CRUD y relaciones sencillas; mantener SQL donde sus
características de PostgreSQL, el rendimiento o la consulta analítica lo
justifiquen.

Implementación Fase 3: `ETLEngine` coordina extracción/validación y delega
persistencia en `IngestionRepository`, `CourseRepository`,
`OfferingRepository` y `PriceRepository`. La escritura sigue usando SQL directo
y transacciones Django; no se inició una migración global al ORM.

La propiedad del esquema se declarará por tabla: mientras DDL/SQL sea dueño,
Django migrations no deberá modificar esa tabla; al transferir propiedad, se
registrarán los modelos y la migración de estado sin recrear tablas, cambiar
IDs ni descartar historial. La transferencia solo ocurrirá con pruebas y plan
de reversión.

### ADR-07 — Automatización y concurrencia

Usar inicialmente el Programador de tareas de Windows para invocar diariamente
`python manage.py run_ingestion`. El comando seguirá comprobando `next_run_at`
y procesando fuentes vencidas; se conservarán ejecuciones manuales, individuales
y conjuntas.

Implementar exclusión mutua por fuente, estados que permitan detectar una
captura incompleta/abandonada y una política de recuperación. El servidor web
no inicia ETL al atender solicitudes. No añadir Celery ni otro orquestador
hasta que exista necesidad operacional demostrable.

Implementación Fase 4: `ETLEngine` usa un advisory lock de sesión por
`source_id`; PostgreSQL lo libera al cerrarse la sesión. Al adquirirlo, la
corrida convierte los `RUNNING` remanentes a `ABANDONED`. Las ejecuciones
programadas vuelven a validar `next_run_at` dentro del lock; `--force` sigue
siendo explícito y manual. Los estados de ciclo de vida son independientes del
resultado de calidad `OK`/`PARTIAL`, guardado en `counts`.

### ADR-08 — Retención

Política inicial, configurable y aplicada solo tras definir resolución y
auditoría:

| Datos | Retención inicial |
|---|---:|
| Versiones normalizadas de cursos, ofertas y precios | Indefinida |
| Registro mínimo de ejecuciones ETL | Indefinida |
| Payload (`counts`) y texto diagnóstico (`error_msg`) de ejecuciones | 24 meses |
| HTML y datos crudos de extracción | 90 días |
| Cuarentenas | 90 días después de su resolución |

No ejecutar limpieza automática del historial normalizado. Hoy
`ETLEngine._purge_raw_pages()` elimina `raw_page` según
`retention_raw_page_days`; la migración impide borrar filas de
`ingestion_log`. Para cumplir los 90 días de datos crudos también habrá que revisar
`raw_payload` que esté embebido en snapshots u otros registros históricos:
conservar los valores normalizados indefinidamente no implica conservar el
HTML o payload original indefinidamente. La migración 004 conserva los
diagnósticos mínimos de corrida y añade `resolved`/`resolved_at` para aplicar
la retención de cuarentenas sin confundir su resolución con una mera revisión.

Implementación Fase 4: la tarea aplica las retenciones configurables
`retention_raw_page_days`, `retention_resolved_quarantine_days` y
`retention_ingestion_diagnostic_months`. Limpia `ingestion_log.counts` y
`error_msg` sin eliminar las filas ni sus claves de proveniencia. `resolved_at`
se asigna mediante trigger al marcar una cuarentena como resuelta.

### ADR-09 — Monedas

Guardar cada importe como decimal, conservar el importe original y su código
ISO 4217. Los reportes no sumarán ni compararán importes de monedas distintas
como si fueran equivalentes. Una conversión futura requerirá tabla de tipos de
cambio históricos y fecha/fuente de conversión; no se introduce ahora.

El DTO conserva códigos alfabéticos de tres letras y reconoce símbolos
inequívocos; `price_snapshot`, niveles y planes operativos continúan restringidos
a `PEN`/`USD`. La Fase 3 registra por separado la observación monetaria, el
importe/moneda crudos y estados como no publicado, desconocido, moneda no
identificada o no soportada. Una observación no soportada no crea un snapshot
ficticio. La validación Pydantic comprueba el formato, no la asignación del
código en el registro ISO.

### ADR-10 — Metabase y seguridad de integración

Django continúa como aplicación web y Metabase como visualizador. La primera
opción será Guest Embedding con JWT firmado en el servidor Django; el secreto
se obtiene de una variable de entorno y nunca se envía al navegador. Mantener
la opción de evolucionar a embedding autenticado con permisos diferenciados.

Antes de implementar, confirmar en la instancia objetivo versión, edición,
licencia y capacidades de embedding. La base de datos de metadatos de Metabase
será distinta de la base de negocio consultada.

## Estado de implementación al cierre de la Fase 3

- `etl/schemas.py` separa el contrato de extracción PUCP del DTO común de
  curso/ofertas y conserva los campos planos que consume `ETLEngine`. El DTO
  común agrega docentes, contactos, certificados, requisitos, categoría
  normalizada, modalidad normalizada, duración con unidad y estados de
  observación (`OBSERVED`, `NOT_FOUND`, `NOT_COLLECTED`, `UNKNOWN`).
- `etl/adapters.py` transforma los contratos PUCP y Cibertec. Las funciones
  independientes de HTML están en `etl/normalization.py`: fechas con año
  explícito, importes decimales, moneda solo con evidencia suficiente,
  unidades de duración, modalidad y categoría.
- Se conservan los códigos de modalidad existentes para la persistencia; los
  valores normalizados son campos distintos. Categoría normalizada no implica
  homologación: `homologation_code` queda sin asignar.
- `ScrapedCourseDTO.canonical_fingerprint()` crea una huella determinista del
  contenido normalizado, ignorando los payloads crudos y ordenando horarios.
  Es una API de datos para pruebas y evolución; `ETLEngine` todavía usa su hash
  anterior y no consume esta huella.
- La Fase 3 integra fingerprints independientes en repositorios. Ignoran los
  payloads crudos y diferencias de formato de modalidad/horario y normalizan
  listas no ordenadas; los estados de observación evitan borrar datos que no
  fueron recopilados.
- Los docentes se resuelven por nombre dentro del `source_id` mediante
  `professor_source_link`; la paginación incompleta se señala explícitamente y
  bloquea `mark_missing`, incluso cuando se cumple el umbral mínimo.
- Precios sin importe no se convierten en cero: `offering_price_observation`
  conserva el estado y los datos originales opcionales. Solo se insertan
  `price_snapshot` cuando existe precio publicado con moneda soportada.
- Docentes, especialidades y enlaces a ofertas se guardan en las tablas
  existentes; contactos admiten varios por curso tras retirar su unicidad
  singular. Certificados conservan la información descriptiva en la versión y
  su tipo consultable en `offering_certificate`, con precio NULL si no se
  conoce.
- `database/migrations/003_persistence_history.sql` valida `btree_gist` antes
  de modificar el esquema y conserva las filas mínimas de `ingestion_log`.
  Bootstrap, migración y pruebas de integración pasaron en `ceebi_phase3_test`.
- Las pruebas offline de ETL/catálogo pasan **52/52**; la suite PostgreSQL pasa
  **8/8** casos de integración y **3/3** comprobaciones de seguridad.
- `PucpETLService` se conserva sin cambios y no se ejecutaron capturas reales.
  La Fase 3 está validada en la base aislada; la Fase 4 no se ha iniciado.

## Clasificación de cambios

**Implementables en código sin cambiar el esquema actual:** contrato Pydantic y
adaptadores PUCP/Cibertec con normalización básica (completados en la Fase 2);
normalizar URLs en un componente testeable; hacer que el comando diario use
`next_run_at`; separar orquestación, extracción y repositorio; añadir pruebas
con fixtures; definir contratos y borradores SQL de vistas `bi` sin ejecutarlos
en la base de negocio.

**Requieren diseño/migración de PostgreSQL:** versionado temporal completo y
referencia a `ingestion_log`; soporte persistente para identidad por URL
canónica y reconciliación; permisos/esquema/vistas `bi`; constraints y tipos de
moneda ISO; estados, locks y recuperación de ejecuciones; marcas de resolución
de cuarentena y configuración de retenciones; rol de lectura de Metabase.
Aplicar todo solo mediante migraciones aditivas revisadas y probadas sobre una
copia representativa, nunca ejecutando directamente el DDL destructivo como
migración.

## Estado de implementación al cierre de la Fase 4

- `database/migrations/004_reliability.sql` amplía el ciclo de vida de
  `ingestion_log` y agrega la marca auditable de resolución y los defaults de
  retención.
- `ETLEngine` obtiene un advisory lock de sesión por `source_id` antes de
  capturar la fuente, reporta conflictos sin solapar trabajos, recupera filas
  `RUNNING` abandonadas y libera el lock normalmente en `finally`.
- El comando diario vuelve a revisar `next_run_at` dentro del lock; la purga
  corre aunque no haya fuentes vencidas. `--source` y `--force` mantienen su
  semántica manual.
- Cambios de `external_id` se reconcilian por `canonical_url` con alias
  persistente. Si el ID/alias y la URL identifican cursos diferentes, se
  produce una colisión explícita que el motor envía a cuarentena.
- `PucpETLService` fue eliminado; `PucpExtractor` conserva únicamente la lógica
  de extracción y transformación.
- La suite offline completa pasa **58/58** pruebas; el runner PostgreSQL de
  Fases 3/4 pasa **14/14** casos, incluidas las comprobaciones del destino
  aislado.
- La configuración recomendada del Programador de tareas y los comandos
  operativos están en `FASE_4_OPERACION.md`. La Fase 5 no se inició.

**Exigen pruebas antes de habilitar:** igualdad de mapeo PUCP/Cibertec;
idempotencia y ausencia de duplicados; captura sin cambios no crea versión;
cambios relevantes crean versión con `ingestion_id`; cambios de URL y
reconciliación; atomicidad y recuperación tras fallo; dos procesos compitiendo
por la misma fuente; acceso del rol Metabase solo a vistas; filtros BI y
comparaciones sin mezclar monedas.

## Reglas de evolución

1. No reemplazar extractores funcionales en la misma fase que cambia
   persistencia.
2. No borrar ni recrear tablas pobladas; conservar PK/FK y auditar cada
   transferencia de propiedad a ORM.
3. Tratar capturas históricas parciales con cautela: no afirmar que un campo fue
   eliminado cuando no fue extraído o validado.
4. Versionar la definición SQL de cada vista BI en el repositorio y desplegar
   permisos de lectura por separado de las credenciales de Django.
5. Cambiar una sola frontera arquitectónica a la vez y exigir pruebas de
   regresión de PUCP y Cibertec antes de avanzar.
