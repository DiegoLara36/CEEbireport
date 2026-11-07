# Fase 3 — Persistencia e historización

## Alcance y estado

Implementación en `refactor/fase-3-persistence`. La Fase 3 separa la
coordinación de ETL de las escrituras SQL y usa fingerprints por entidad. Se
corrigieron los riesgos de identidad de docentes, catálogos incompletos y
retención de la proveniencia. No se modificó ni eliminó `PucpETLService`; los
extractores mantienen su navegación.

La suite offline tiene **52 pruebas aprobadas**. En PostgreSQL aislado pasaron
los **8 casos de integración** y las **3 comprobaciones de seguridad** del
runner. El bootstrap base y la migración se aplicaron en `ceebi_phase3_test`.
La Fase 3 queda validada; la Fase 4 no se ha iniciado.

## Arquitectura antes y después

Antes, `ETLEngine` coordinaba extracción y contenía consultas/inserciones para
cursos, ofertas, horarios, precios, descuentos, planes e historial. La ruta
`PucpETLService` conserva una implementación PUCP separada.

```text
PUCP/Cibertec → DTO común → ETLEngine con SQL de entidades → PostgreSQL
```

Después:

```text
Extractores → Adaptadores/DTO Pydantic
                    ↓
              ETLEngine
          ┌─────────┴─────────┐
 IngestionRepository   CourseRepository
                           ↓
                  OfferingRepository
                     ┌─────┴──────┐
              PriceRepository  tablas auxiliares
                     └─────┬──────┘
                        PostgreSQL
```

`ETLEngine` conserva selección de fuente, orden de proceso, reglas de
cuarentena, validación de umbral y transacciones. Los repositorios concentran
el SQL. No se migró al ORM.

## Repositorios y persistencia

| Repositorio | Responsabilidad |
|---|---|
| `IngestionRepository` | Fuente y corrida ETL, páginas crudas, cuarentena, finalización, expiración de registros y entidades ausentes. |
| `CourseRepository` | Identidad por ID/URL, aliases, categoría, curso, contactos, contenido, requisitos y versión del curso. |
| `OfferingRepository` | Oferta, modalidad, horarios, docentes, certificados, versión académica y versión comercial. |
| `PriceRepository` | Snapshot de precio publicado, observación de precio, niveles, planes de pago y asociaciones de descuentos. |

Cada curso se guarda dentro de su propia transacción. Si falla la persistencia,
esta transacción se revierte y el fallo se registra en cuarentena en una
transacción separada. Una corrida parcial con cursos en cuarentena o paginación
incompleta no marca como inactivos los cursos/ofertas que no fueron observados.
El umbral mínimo porcentual no anula la señal `catalog_complete`; la opción
`limit` también bloquea `mark_missing` cuando recorta el listado.

Los `change_log` siguen siendo auditoría de eventos; no reemplazan las
fotografías temporales.

## Fingerprints

- Curso: DTO normalizado, campos observacionales y URL canónica. Se excluyen
  payloads crudos y URL con parámetros/fragmentos de tracking.
- Oferta: fechas, duración/unidades, modalidad normalizada, horarios,
  publicación y estado. Horarios se ordenan; texto original de horario y
  modalidad no causa por sí solo una nueva versión.
- Comercial: estado/importe/moneda, descuentos, condiciones y estado actual de
  tiers/planes existentes. Las listas no ordenadas se ordenan para comparar.
- Ninguna huella usa timestamp de captura ni HTML crudo. El formato decimal se
  canoniza para evitar cambios artificiales de escala.
- `NOT_COLLECTED` y `UNKNOWN` conservan el último dato conocido; no se
  convierten en ausencia confirmada ni borran relaciones.

## Modelo temporal e identidad

`course_version`, `course_offering_version` y
`offering_commercial_version` contienen `fingerprint`, atributos JSONB,
`valid_from`, `valid_to` y `ingestion_id` obligatorio con `ON DELETE RESTRICT`.
Las restricciones de exclusión GIST garantizan que no haya intervalos
superpuestos por entidad. Los snapshots vigentes se cierran y solo se insertan
versiones nuevas cuando cambia el fingerprint.

El historial comercial referencia `price_snapshot`; no vuelve a copiar su
importe. `price_snapshot`, `offering_price_tier` y `payment_plan` conservan sus
triggers e historiales existentes. `ingestion_log` conserva indefinidamente la
fila mínima de proveniencia; un trigger impide eliminarla. Se puede redactar
`error_msg` y purgar `raw_page` o cuarentenas conforme a su retención, pero no
borrar corridas ni desvincular versiones.

`OfferingRepository` resuelve docentes mediante `professor_source_link` y
`source_id`; docentes homónimos de fuentes distintas no se fusionan por nombre.

La identidad busca primero `(source_id, external_id)` y después alias/URL
canónica. No usa nombre como clave. La migración añade unicidad de URL canónica
por institución cuando el valor no es NULL. La reconciliación automática no
resuelve colisiones de URLs; estas producen error para evitar fusionar cursos
distintos.

## Precios, monedas y datos complementarios

`offering_price_observation` distingue precio publicado, no publicado,
desconocido, moneda no identificada y moneda no soportada. Guarda importes y
códigos originales opcionales. Solo se crea `price_snapshot` para importe
publicado y moneda PEN/USD; `None` nunca se transforma en cero.
Una observación explícita de precio no publicado o de moneda no identificada/no
soportada cierra el snapshot previo como no vigente, sin crear otro. Un precio
desconocido o no recopilado no invalida automáticamente el último snapshot
conocido.

Los tiers y planes soportados se guardan en sus tablas históricas existentes.
Los tiers con moneda/segmento no persistibles y los planes sin importe final se
conservan en la versión comercial; no se descartan silenciosamente. Descuentos
y condiciones se conservan en `discount` y en la versión comercial, y solo se
asocian a un snapshot cuando el precio actual está publicado y es persistible.

Se reutilizan `professor`, `professor_source_link`, `offering_professor`,
`course_contact`, `offering_certificate`, `offering_schedule`,
`course_content_item`, `course_prerequisite_item` y
`course_tech_req_item`. La especialidad se conserva en `professor_source_link`
para no hacer global un dato que puede variar entre instituciones. La
migración quita la restricción de un contacto por
curso sin borrar contactos. Cuando el costo de certificado no está informado,
se guarda NULL; los ceros históricos existentes no se reinterpretan ni se
modifican. Certificados, docentes, contactos y el resto de datos normalizados
también quedan en los snapshots JSONB.

## Cambios de esquema y despliegue

`database/migrations/003_persistence_history.sql` agrega:

- URL canónica y aliases para reconciliación.
- Tablas temporales de curso, oferta y estado comercial.
- Observaciones de precios.
- Referencias a corridas para precio, tiers y planes.
- Restricciones de no solapamiento y soporte de contactos/certificados sin
  precio conocido.
- Preflight de disponibilidad/permisos de `btree_gist` y protección para que
  nunca se eliminen las filas de proveniencia de `ingestion_log`.

La migración requiere permiso para instalar `btree_gist`. Debe respaldarse la
base y probarse en una base aislada antes de desplegar. El runner
`python -m etl.run_phase3_pg_integration` valida el nombre y configura
exclusivamente `CEEBI_TEST_DB_*`; por defecto el nombre es `ceebi_phase3_test`.
Solo aplica el DDL base filtrado de sentencias `DROP TABLE` en una base pública
vacía, y aborta ante un esquema parcial. `DDL.txt` no debe ejecutarse como
migración sobre una base existente.

## Archivos de esta fase

- Modificados: `etl/persistence/repositories.py`,
  `etl/services/etl_engine.py`, `etl/test_fase1_baseline.py`,
  `ARCHITECTURE_DECISIONS.md`, `REFACTORING_PLAN.md`.
- Añadidos: `database/migrations/003_persistence_history.sql`,
  `etl/test_phase3_persistence.py`, `etl/test_phase3_pg_integration.py`,
  runner/configuración PostgreSQL aislada y `FASE_3_PERSISTENCIA.md`.
- Modificados los extractores PUCP/Cibertec para comunicar la completitud de
  paginación. `PucpETLService` se conserva sin cambios.

## Verificación

Se ejecutaron las pruebas Django de `etl` y `catalog`: **52/52 offline
aprobadas**. Con `python -m etl.run_phase3_pg_integration` pasaron **11/11
pruebas**: los 8 casos PostgreSQL solicitados y 3 comprobaciones de seguridad
del runner. Se validaron migración limpia, FK restrictiva y retención de
ingestas, exclusión temporal, trigger de precio, idempotencia, nuevas versiones
ante cambios, reconciliación por URL canónica, rollback por curso y separación
de docentes homónimos. No se utilizó la base `default` de la aplicación.

La migración se ejecutó solo sobre la base aislada de pruebas; su despliegue en
una base operativa sigue sujeto a respaldo y procedimiento de despliegue. Esta
validación no inicia ni implementa la Fase 4.

`PucpETLService` sigue siendo una ruta SQL duplicada no utilizada por los
comandos actuales; su consolidación queda fuera de esta fase. La Fase 4 no se
ha iniciado.
