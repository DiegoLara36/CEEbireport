# CEEBI: plan incremental de refactorización

Este plan aplica las decisiones de `ARCHITECTURE_DECISIONS.md`. Las Fases 1 a 4
están implementadas y validadas.

## Orden y dependencias

```text
Fase 1 Línea base (completada)
  → Fase 2 Contrato común y normalización (completada)
  → Fase 3 Persistencia encapsulada e historial temporal
  → Fase 4 Identidad, concurrencia y ejecución confiable
  → Fase 5 Esquema y vistas BI
  → Fase 6 ORM gradual (solo si aporta valor)
  → Fase 7 Metabase embedding (tras validar instancia/licencia)
```

La Fase 5 puede iniciarse una vez aprobado su alcance. La Fase 6 no es
prerrequisito para Metabase: Metabase leerá `bi`, no el ORM.

## Fase 1 — Línea base funcional (completada)

**Objetivo:** establecer comportamiento y cobertura de PUCP y Cibertec antes de
refactorizar.

**Archivos/componentes:** `etl/tests.py`, `catalog/tests.py`,
`etl/extractors/pucp.py`, `etl/extractors/cibertec.py`,
`etl/services/etl_engine.py`, `catalog/management/commands/` y `DDL.txt`.

**Acciones:** documentar comandos activos; distinguir `ETLEngine` de
`PucpETLService` (este último no es invocado por los comandos actuales);
inventariar qué campos se extraen, mapean y guardan; añadir fixtures HTML
representativas si no existen.

**Dependencias:** ninguna.

**Riesgos:** asumir que una columna DDL equivale a una funcionalidad cargada;
fixtures que no representen cambios de estructura web.

**Pruebas necesarias:** tests unitarios sin red para parseo, fechas, moneda,
horarios, DTO y reglas de identidad; tests del resultado actual del ETL sobre
PostgreSQL de pruebas o transacciones aisladas.

**Resultado:** `FASE_1_LINEA_BASE.md`, fixtures HTML de ambas fuentes y pruebas
offline establecen el comportamiento previo a la refactorización. El informe
documenta el recorrido de campos, las brechas de persistencia y el riesgo del
hash actual de `ETLEngine`.

## Fase 2 — DTO común, adaptadores y normalización (completada)

**Objetivo:** garantizar un contrato Pydantic consistente para ambas
instituciones y hacer explícita la normalización.

**Archivos/componentes:** `etl/schemas.py`, `etl/adapters.py`,
`etl/normalization.py`, `etl/extractors/pucp.py`,
`etl/extractors/cibertec.py` y `etl/test_phase2_normalization.py`.

**Resultado:** contratos diferenciados de extracción y DTO común con campos
analíticos, estados de observación y esquemas auxiliares. Los adaptadores
normalizan categoría, modalidad, duración y monedas cuando existe evidencia;
conservan códigos legados de modalidad y payloads específicos. Se añade una
huella canónica determinista que todavía no usa el motor.

**Implementable sin cambio de PostgreSQL:** completado para contratos,
adaptadores y normalizadores. No se convirtieron esquemas Pydantic en modelos
Django.

**Dependencias:** Fase 1.

**Riesgos:** defaults que oculten campos no mapeados; modificar el significado
de precio, duración o modalidad; perder datos que ya están en `raw_payload`.

**Pruebas ejecutadas:** fixtures PUCP/Cibertec, campos anidados y opcionales,
normalización, equivalencia semántica, moneda ambigua y determinismo de huella.
La Fase 1 se mantiene y su prueba de snapshot ahora especifica PEN
explícitamente, sin depender de un default de moneda.

**Resultado:** ambos extractores producen DTOs válidos; docentes/contactos de
PUCP llegan al DTO, y Cibertec mantiene esos datos como `NOT_COLLECTED`. El DDL
y la persistencia permanecen intactos.

**Límites conocidos:** la huella común todavía no sustituye los hashes del
motor; la base sigue admitiendo solo PEN/USD; y el ETL/DDL vigente necesita
importes para persistir una oferta. Los datos de moneda/precio desconocidos
quedan identificados, pero su cuarentena o persistencia opcional se resolverá
en la Fase 3.

## Fase 3 — Persistencia/repositorio e historial temporal

**Objetivo:** desacoplar la orquestación de SQL y preservar versiones de negocio
con la ejecución que las detectó.

**Implementación:** `etl/services/etl_engine.py` delega en
`etl/persistence/repositories.py` (`IngestionRepository`, `CourseRepository`,
`OfferingRepository`, `PriceRepository`). Se mantienen SQL directo y límites
transaccionales por curso; los fallos por curso van a cuarentena. Una captura
con cursos en cuarentena o paginación incompleta no desactiva entidades no
vistas.

**Historial:** `course_version`, `course_offering_version` y
`offering_commercial_version` guardan snapshots JSONB, fingerprints, referencias
a ingestas y vigencias `[valid_from, valid_to)`. Constraints GIST evitan
intervalos superpuestos. `price_snapshot` y las tablas históricas existentes de
tiers/cuotas se conservan; no se copia en las versiones comerciales el importe
que ya tiene historial propio.

**Identidad y datos:** `course.canonical_url` y `course_identity_alias` habilitan
reconciliación por URL normalizada. `offering_price_observation` representa
precios ausentes/no soportados sin cero artificial. Se reutilizan las tablas de
docentes, contactos, certificados, horarios, contenidos y requisitos; la
migración permite varios contactos por curso y precios/currency NULL para
certificados sin dato publicado. La búsqueda de docentes está limitada por
fuente; paginación incompleta bloquea la desactivación de entidades ausentes.

**Migración:** `database/migrations/003_persistence_history.sql` es aditiva,
versionada, incluye preflight de `btree_gist` y conserva `ingestion_log` como
proveniencia permanente. La base aislada debe contener el esquema base
bootstrappeado desde `DDL.txt` sin sus sentencias `DROP TABLE`.

**Pruebas ejecutadas:** 52 pruebas offline pasan. En `ceebi_phase3_test`, el
bootstrap, la migración y los 8 casos de integración PostgreSQL pasan; también
pasan las 3 comprobaciones de seguridad del runner.

**Estado:** Fase 3 cerrada y validada en una base aislada. No se inició la
Fase 4.

## Fase 4 — Identidad y operación confiable

**Objetivo:** asegurar claves estables, capturas por fuente sin solapamiento y
recuperación visible de procesos interrumpidos.

**Archivos/componentes:** `etl/schemas.py`, extractores y adaptadores,
`etl/services/etl_engine.py`, `catalog/management/commands/run_ingestion.py`,
`etl/services/resilience_service.py`; PostgreSQL para constraints, lock/
concesión y estados de ejecución si se necesitan persistir.

**Acciones:** priorizar IDs externos; normalizar URL para fallback y ofrecer
reconciliación manual/auditable; no usar nombres como clave. Implementar lock
por fuente, estado RUNNING/terminación y mecanismo para detectar/reconciliar
locks abandonados. Mantener `--source`, `--force` y ejecución conjunta.
Documentar la tarea diaria de Windows que invoca `run_ingestion` sin `--force`.

**Implementable inicialmente en código:** normalizador, interfaz de
reconciliación, elección de fuentes por `next_run_at` y comandos existentes.

**Requiere cambio de PostgreSQL:** restricciones/columna de clave fallback,
persistencia de reconciliación, lock durable o metadata de ejecución, estados
adicionales si se elige no reutilizar `PARTIAL`.

**Dependencias:** identidad aprobada y Fase 3; pruebas de concurrencia antes de
habilitar la tarea automática.

**Riesgos:** URL con parámetros/tracking, cambios legítimos de URL, doble
captura por scheduler y ejecución manual, liberar locks ante terminación abrupta.

**Pruebas necesarias:** mismo recurso con URL normalizada converge; dos procesos
de una fuente producen un único ganador; fuentes distintas pueden correr según
política; fallo/interrupción queda detectable; reintento no duplica filas.

**Implementación Fase 4:** la migración `004_reliability.sql` adopta los estados
`RUNNING`, `COMPLETED`, `FAILED` y `ABANDONED`, agrega la resolución fechada de
cuarentenas y registra valores iniciales de retención. `ETLEngine` mantiene un
advisory lock de sesión por fuente, recupera corridas huérfanas al obtenerlo y
revalida `next_run_at` en ejecuciones programadas. La identidad compara el
candidato externo/alias con la URL canónica; conflictos entre cursos se
rechazan y siguen la ruta normal de cuarentena. La tarea de Windows está
documentada en `FASE_4_OPERACION.md`.

**Validación:** pruebas offline y suites PostgreSQL de Fases 3/4 ejecutadas
correctamente en la base aislada. El runner prueba competencia por lock,
liberación al cerrar la sesión, recuperación de corridas abandonadas y
retención sin borrar proveniencia histórica.

**Estado:** Fase 4 implementada y validada. No se inició la Fase 5.

## Fase 5 — Vistas `bi` y usuario de lectura

**Objetivo:** habilitar consultas analíticas aisladas sin mover las tablas
operativas.

**Archivos/componentes:** nuevas definiciones SQL versionadas, por ejemplo
`bi/sql/001_schema_and_views.sql`; documentación de despliegue y permisos.
Fuentes: `source`, `category`, `course`, `course_offering`, tablas de precio,
horario, homologación e `ingestion_log`.

**Acciones:** crear esquema `bi`; implementar vistas de cursos actuales,
comparación homologada, historial de precios, oferta académica, resumen ETL y
cursos por categoría; documentar granularidad, moneda y filtros; crear rol de
solo lectura limitado a las vistas requeridas. No mover las tablas operativas.

**Implementable sin cambiar tablas de negocio:** preparar y revisar archivos SQL
versionados para el esquema, vistas y permisos.

**Requiere cambio de PostgreSQL:** desplegar `CREATE SCHEMA`, vistas, grants y
revocaciones en la instancia. Las vistas históricas completas dependen de la
Fase 3.

**Dependencias:** contrato de cada vista y esquema de historial definido; la
comparación depende además de homologaciones administrables.

**Riesgos:** granularidad incorrecta que multiplique filas al unir precios,
horarios y docentes; permisos indirectos excesivos; comparar montos de monedas
distintas.

**Pruebas necesarias:** conteos/resultados frente a consultas operativas
conocidas, una moneda por importe, filtros por institución/categoría/modalidad/
fechas, homologación explícita y prueba negativa de escritura/acceso a tablas
operativas para el usuario BI.

**Termina cuando:** Metabase puede leer las vistas requeridas y su rol no puede
modificar ni consultar directamente datos operativos no autorizados.

## Fase 6 — ORM gradual (condicional)

**Objetivo:** usar Django ORM donde reduzca complejidad de CRUD y relaciones,
sin desplazar SQL ETL ni cambiar datos por principio.

**Archivos/componentes:** modelos nuevos en la aplicación que sea dueña del
dominio; repositorios de `etl/persistence/`; configuración de migrations y un
registro de ownership tabla por tabla.

**Acciones:** seleccionar una operación CRUD sencilla; mapear tablas existentes
sin recrearlas cuando corresponda; conservar constraints, triggers, PKs,
índices parciales y relaciones. Mantener el SQL actual para upserts ETL y
consultas BI hasta demostrar equivalencia y beneficio.

**Implementable sin migrar esquema:** consultas ORM de solo lectura y modelos
con `managed = False` cuando sean apropiados; verificar su comportamiento con
el esquema real.

**Requiere cambio de PostgreSQL:** solo si se acuerda una transferencia de
propiedad o se necesita una nueva estructura. Toda transición debe ser aditiva
y explícita; no permitir que migrations cree o elimine accidentalmente tablas
que siguen bajo ownership de DDL.

**Dependencias:** repositorios estables y mapa de ownership aprobado.

**Riesgos:** doble ownership, SQL del ORM que difiere del actual, alteración de
constraints o pérdida de triggers/índices especiales.

**Pruebas necesarias:** comparar lecturas y CRUD con comportamiento actual;
inspeccionar el SQL de migración generado; prueba de reversión; verificar que
las claves y el historial permanezcan idénticos.

**Termina cuando:** el CRUD escogido usa ORM con ventaja demostrada, sin que
migrations modifique tablas no transferidas ni cambie datos/IDs.

## Fase 7 — Integración Metabase

**Objetivo:** mostrar dashboards desde Django con Guest Embedding firmado por
servidor, sin introducir el secreto en el cliente.

**Archivos/componentes:** futura vista/servicio y template Django; configuración
por variables de entorno; documentación operacional. No existe una instancia
configurada en el repositorio verificado.

**Acciones:** antes de escribir integración, verificar versión, edición,
licencia, tipo de embedding y permisos disponibles en la instancia real.
Separar la base de metadatos de Metabase de la base de negocio. Firmar JWT
únicamente en backend; limitar dashboard, filtros y parámetros admitidos.
Mantener el diseño compatible con embedding autenticado si luego hacen falta
permisos por usuario.

**Implementable sin cambio en las tablas operativas:** integración web y
configuración de secretos una vez validada la instancia.

**Requiere configuración externa:** instancia/licencia de Metabase, base de
metadatos separada, usuario de PostgreSQL de solo lectura sobre `bi`, variables
secretas y política de despliegue.

**Dependencias:** Fase 5 desplegada y aceptada; verificación de capacidades y
licencia.

**Riesgos:** filtrar JWT/secreto, filtros manipulables, permisos de lectura más
amplios de lo previsto o dependencia de una capacidad no licenciada.

**Pruebas necesarias:** firma y expiración del JWT, secreto ausente/mal
configurado, autorización de dashboard/filtros, inspección de HTML/network para
que el secreto no se envíe al navegador y validación del rol BI.

**Termina cuando:** el dashboard esperado se muestra desde Django, con
autorización verificable y sin secretos expuestos.

## Retenciones y despliegue operativo

La retención se incorporará cuando existan marcas de resolución y parámetros
configurables, no como una limpieza general en la primera refactorización:

| Clase de dato | Acción planificada | Dependencia |
|---|---|---|
| Versiones de curso/oferta y precios | Sin borrado automático | Fase 3 |
| `ingestion_log` | Política configurable de 24 meses | Configuración y tarea de mantenimiento probada |
| HTML y payloads crudos (`raw_page`, `raw_payload`) | 90 días; revisar el borrado actual de `_purge_raw_pages()` y separar payload crudo de valores normalizados de snapshots | Parámetro aplicado de forma consistente sin borrar el historial normalizado |
| Cuarentena resuelta | 90 días desde `resolved_at` | Añadir/definir campo y flujo de resolución |

La tarea diaria de Windows ejecutará el comando desde el directorio del proyecto
y con el intérprete virtual correspondiente. Debe ejecutarse sin `--force` para
respetar `next_run_at`; las instrucciones de detener/deshabilitar la tarea son
operativas y no sustituyen el lock que el proceso necesita.

## Lista de control antes de desplegar una fase

- [ ] Cambios de esquema aditivos, revisados y aplicados primero en una copia de
  la base; nunca ejecutar `DDL.txt` destructivo sobre datos existentes.
- [ ] Pruebas de regresión de PUCP y Cibertec, usando fixtures y sin depender de
  la disponibilidad momentánea de las fuentes.
- [ ] Identidad y constraints revisados contra registros existentes; no usar
  nombres como claves.
- [ ] Transacciones, versionado, idempotencia, concurrencia y fallos probados.
- [ ] Grants BI probados con el rol real de solo lectura.
- [ ] Rollback y observabilidad de errores definidos para la fase.
- [ ] Sin tarea automática habilitada hasta pasar pruebas de concurrencia y
  recuperación.
