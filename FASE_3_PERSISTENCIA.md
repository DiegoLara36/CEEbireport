# Fase 3 — Persistencia e historización

## Alcance y estado

Implementación preparada en `refactor/fase-3-persistence`. La Fase 3 separa la
coordinación de ETL de las escrituras SQL, usa fingerprints por entidad y
prepara una migración aditiva. No se modificó ni eliminó `PucpETLService`; los
extractores mantienen su navegación. No se ejecutaron capturas, migraciones ni
operaciones contra PostgreSQL.

La suite offline terminó con **47 pruebas aprobadas**. La aceptación de
despliegue queda pendiente: no existe una base PostgreSQL aislada configurada
para probar la migración, integridad temporal y transacciones reales.

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
transacción separada. Una corrida parcial con cursos en cuarentena no marca
como inactivos los cursos/ofertas que no fueron observados.

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
`valid_from`, `valid_to` y `ingestion_id`. Las restricciones de exclusión GIST
garantizan que no haya intervalos superpuestos por entidad. Los snapshots
vigentes se cierran y solo se insertan versiones nuevas cuando cambia el
fingerprint.

El historial comercial referencia `price_snapshot`; no vuelve a copiar su
importe. `price_snapshot`, `offering_price_tier` y `payment_plan` conservan sus
triggers e historiales existentes. Las referencias de versión a `ingestion_log`
usan `ON DELETE SET NULL`: las versiones permanecen aunque se purguen logs,
pero se pierde la asociación a la corrida. Esto requiere resolver la
incompatibilidad con la retención de 24 meses antes de purgar ejecuciones.

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

La migración requiere permiso para instalar `btree_gist`. Debe respaldarse la
base y probarse en una base aislada antes de desplegar. No se ejecutó ni se
preparó un rollback destructivo: ante rollback de aplicación se deja el esquema
aditivo intacto; las tablas de historial no deben eliminarse porque contienen
datos. `DDL.txt` sigue siendo referencia de esquema y no debe ejecutarse como
migración.

## Archivos de esta fase

- Modificados: `etl/persistence/repositories.py`,
  `etl/services/etl_engine.py`, `etl/test_fase1_baseline.py`,
  `ARCHITECTURE_DECISIONS.md`, `REFACTORING_PLAN.md`.
- Añadidos: `database/migrations/003_persistence_history.sql`,
  `etl/test_phase3_persistence.py`, `FASE_3_PERSISTENCIA.md`.
- Conservado sin cambios: `etl/extractors/pucp.py` y `PucpETLService`.

## Verificación y pendientes

Se ejecutaron las pruebas Django offline de `etl` y `catalog`: **47/47
aprobadas**, incluyendo fixtures PUCP/Cibertec y pruebas de huellas,
observaciones, URL canónica y ausencia/moneda no soportada. No se utilizó la
base configurada como `default`.

Antes de considerar desplegable la fase quedan pendientes pruebas en PostgreSQL
aislado para:

1. Aplicación y revisión de la migración sobre una copia representativa.
2. Inserción/repetición de una captura, cambios/no-cambios y duplicados por
   identidad.
3. Intervalos, trigger de precio, transacción y rollback ante fallos.
4. Persistencia consultable de docentes, contactos, certificados, descuentos,
   tiers y planes.
5. Política de retención que preserve el vínculo entre versiones y ejecuciones.

`PucpETLService` sigue siendo una ruta SQL duplicada no utilizada por los
comandos actuales; su consolidación queda fuera de esta fase. La Fase 4 no debe
iniciarse sin autorización y sin cerrar las pruebas de integración pendientes.
