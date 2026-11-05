# Fase 3 — Validación de integración PostgreSQL

## Resultado de preflight

La integración no se ejecutó. No hay una configuración identificable de base
PostgreSQL de pruebas y no se puede demostrar que una conexión del proyecto
apunte exclusivamente a una base descartable. No se leyó ni usó el contenido
secreto de `.env`; no se abrió conexión, no se enumeraron bases de datos y no
se ejecutó SQL contra PostgreSQL.

Entorno detectado sin conexión:

| Componente | Estado |
|---|---|
| Rama | `refactor/fase-3-persistence` |
| PostgreSQL local | Servicio `postgresql-x64-16` activo |
| Cliente `psql` | Disponible, PostgreSQL 16.6 |
| Driver Python `psycopg` | Disponible en `.venv` |
| Docker Desktop | Instalado, servicio detenido |
| Configuración `CEEBI_TEST_DB_*` / `TEST_DB_*` | No encontrada |
| Credenciales administrativas exclusivas de pruebas | No configuradas |
| Configuración Django | Solo `DATABASES['default']`, basada en `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` |

Las variables `DB_*` provienen del `.env` normal del proyecto y no se trataron
como credenciales de prueba. No se usaron ni se inspeccionaron sus valores.
El único destino válido para continuar debe ser una base dedicada con nombre
inequívoco, por ejemplo `ceebi_phase3_test`, y un rol exclusivo de pruebas.
El servidor local exige autenticación SCRAM-SHA-256 también para loopback;
crear una base/rol requiere credenciales administrativas de prueba que no están
disponibles. No se intentó autenticar con las credenciales del proyecto.

## Revisión estática de la migración

`database/migrations/003_persistence_history.sql` está escrita como una
transacción y no contiene `DROP TABLE` ni `CASCADE`. Agrega identidad por URL,
aliases, tablas de versiones, observaciones de precio, referencias a ingestas,
y relaja la unicidad de contactos. El DDL anterior describe las tablas y
columnas a las que hace referencia la migración.

No se puede confirmar solo por inspección que aplique sobre la estructura real,
que conserve todos los datos existentes, ni que sus índices y restricciones
sean aceptados por la versión/permisos de la instancia objetivo. Tampoco se
comprobó el funcionamiento de los triggers de precios. En particular:

- `CREATE EXTENSION IF NOT EXISTS btree_gist` requiere que la extensión esté
  instalada o que el rol tenga privilegio para crearla. Ese privilegio no se
  verificó. La migración no contiene un preflight que dé un error explicativo
  antes de empezar los cambios.
- Las restricciones `EXCLUDE USING gist` dependen de `btree_gist` para la
  igualdad de IDs y deben verificarse con inserciones superpuestas/no
  superpuestas en PostgreSQL.
- Las tablas con `CREATE TABLE IF NOT EXISTS` no validan ni reparan una tabla
  preexistente con estructura divergente.
- `DDL.txt` contiene sentencias destructivas en su sección inicial; no se debe
  ejecutar como bootstrap ni como migración. La prueba necesita una estructura
  previa a Fase 3 preparada de forma no destructiva, o una copia aislada de una
  estructura compatible.
- Se han añadido referencias `ingestion_id_original` a la misma tabla
  `ingestion_log` con `ON DELETE SET NULL`; por tanto, no son una referencia
  permanente independiente si se eliminan ejecuciones.

La migración **no se ejecutó**. La compatibilidad con una aplicación anterior
tampoco se probó. Aunque las nuevas columnas son aditivas, el cambio de
certificados quita defaults de precio/moneda. Las columnas quedan nullable, por
lo que inserts que omitan esos campos no fallan por `NOT NULL`, pero una
versión previa puede observar NULL donde antes obtenía PEN/0. Esto requiere
prueba con las consultas/consumidores legados antes del despliegue.

## Pruebas ejecutadas y no ejecutadas

Se ejecutaron las pruebas Django offline:

- `python manage.py test etl.test_phase3_persistence --verbosity 1`: 10
  pruebas aprobadas; no prepara ni usa la base de datos `default`.
- La suite reportada previamente para `etl` y `catalog`: 47 pruebas offline
  aprobadas.

No se realizaron pruebas de integración PostgreSQL: migración, preservación de
registros, inserciones/actualizaciones, snapshots, constraints temporales,
vigencias contiguas, rollback, idempotencia, relaciones, triggers o
compatibilidad anterior. No se generaron datos personales reales ni se
ejecutaron capturas.

## Riesgos encontrados por revisión estática

1. **Identidad de docentes entre instituciones:** `OfferingRepository` busca
   `professor` globalmente por `lower(full_name)` antes de insertar el enlace
   a `source` (aprox. líneas 867–878 de
   `etl/persistence/repositories.py`). Dos personas homónimas de distintas
   instituciones pueden fusionarse. La búsqueda y el lock deben limitarse a
   `(source_id, nombre normalizado)`; una homologación entre instituciones
   debería ser explícita y posterior.
2. **Catálogo incompleto y desactivación:** PUCP omite errores de páginas
   posteriores y devuelve los resultados parciales (`etl/extractors/pucp.py`,
   líneas 67–72). Cibertec termina paginación ante timeout y devuelve el
   catálogo reunido (`etl/extractors/cibertec.py`, líneas 247–249). El motor
   solo evita `mark_missing` cuando hubo errores por curso; un fallo de
   paginación no necesariamente incrementa `quarantined`
   (`etl/services/etl_engine.py`, líneas 126–132). El umbral mínimo del motor
   no demuestra integridad del catálogo. Por tanto, una captura parcial podría
   marcar cursos como inactivos. Se necesita una señal explícita de catálogo
   completo antes de permitir esa operación; no se implementa aquí la
   refactorización de concurrencia/recuperación de la Fase 4.
3. **Retención y trazabilidad:** la propuesta de conservar metadatos mínimos
   de corridas indefinidamente es compatible con los FK actuales si
   `ingestion_log` no se borra. Se recomienda conservar `id`, fuente,
   inicio/fin, estado y conteos; anonimizar o borrar `error_msg` detallado tras
   24 meses y aplicar retención a `raw_page`, cuarentena/alertas y otros
   registros auxiliares con su propia política. Si se requiere conservar
   diagnósticos detallados, archivarlos por separado con retención limitada.
   Las versiones históricas deben mantener FK a la fila mínima de la corrida,
   en lugar de depender de `ON DELETE SET NULL` para un registro que se quiere
   poder identificar siempre. Esta decisión requiere ajustar/revisar la
   migración antes de aplicarla.

## Correcciones implementadas

Ninguna en esta etapa: faltan el destino y la autorización/configuración de
prueba necesarios para validar los cambios con PostgreSQL. Se documentan los
riesgos arriba para resolverlos antes de declarar aceptada la Fase 3.

## Requisitos para continuar de forma segura

Opción preferida: preparar una base vacía dedicada llamada, por ejemplo,
`ceebi_phase3_test`, con un rol exclusivo de pruebas; proporcionar la conexión
mediante variables de entorno `CEEBI_TEST_DB_NAME`, `CEEBI_TEST_DB_USER`,
`CEEBI_TEST_DB_PASSWORD`, `CEEBI_TEST_DB_HOST` y `CEEBI_TEST_DB_PORT`. No enviar
contraseñas por el chat ni apuntar esas variables a la base del `.env`.

Antes de ejecutar una migración será necesario autorizar un settings/runner de
pruebas que falle de forma cerrada si el nombre no coincide con la base
permitida, y preparar una estructura previa a la migración sin ejecutar
sentencias destructivas. También se necesita confirmar si el rol de pruebas
puede usar `btree_gist` o si la extensión ya está disponible.

Hasta completar esa preparación, el resultado es **preflight estático,
integración bloqueada**. No iniciar la Fase 4.
