# Fase 4 — Operación y confiabilidad

## Despliegue

Aplicar en orden `database/migrations/003_persistence_history.sql` y
`database/migrations/004_reliability.sql` después de crear el esquema base. La
migración 004 cambia los estados de `ingestion_log`, agrega `resolved_at` y
configura los valores iniciales de retención. No elimina corridas ni versiones.

Las ejecuciones usan un advisory lock de sesión PostgreSQL identificado por
`source_id`. Dos procesos para la misma fuente no se solapan; el segundo se
omite con `SOURCE_LOCKED`. Si la conexión del proceso termina abruptamente,
PostgreSQL libera el lock. La siguiente corrida que lo adquiera marca como
`ABANDONED` los registros `RUNNING` previos y agenda su recuperación como fallo.

Los estados de ciclo de vida son `RUNNING`, `COMPLETED`, `FAILED` y
`ABANDONED`. El resultado de calidad (`OK` o `PARTIAL`) se guarda en `counts` y
no sustituye el estado terminal. El catálogo incompleto o los cursos en
cuarentena nunca provocan `mark_missing`.

## Programador de tareas de Windows

Crear una tarea con acción **Iniciar un programa**:

| Campo | Valor recomendado |
|---|---|
| Programa o script | `C:\Users\diego\PycharmProjects\CEEbireport\.venv\Scripts\python.exe` |
| Agregar argumentos | `manage.py run_ingestion` |
| Iniciar en | `C:\Users\diego\PycharmProjects\CEEbireport` |
| Desencadenador | Diariamente, a las `03:00` hora local del servidor |

En **General**, seleccionar la cuenta de servicio que puede leer el proyecto,
su `.env` y acceder a PostgreSQL; elegir **Ejecutar tanto si el usuario inició
sesión como si no**. En **Configuración**, habilitar **Ejecutar la tarea lo
antes posible si se omitió un inicio programado** y elegir **No iniciar una
nueva instancia** si la tarea ya se está ejecutando. No agregar `--force`: el
comando selecciona fuentes activas cuyo `next_run_at` venció y el motor vuelve
a comprobarlo después de adquirir el lock.

Ejecuciones manuales conservan estos comandos:

```powershell
& "C:\Users\diego\PycharmProjects\CEEbireport\.venv\Scripts\python.exe" manage.py run_ingestion --source PUCP --force
& "C:\Users\diego\PycharmProjects\CEEbireport\.venv\Scripts\python.exe" manage.py run_ingestion --source CIBERTEC --force
```

`--source` sin `--force` sigue respetando `next_run_at`. El lock protege también
las llamadas directas al `ETLEngine`, incluidas las ejecuciones manuales del
comando.

## Retención

Los valores se configuran en `system_setting`:

| Clave | Inicial | Efecto |
|---|---:|---|
| `retention_raw_page_days` | 90 | Elimina filas antiguas de `raw_page`. |
| `retention_resolved_quarantine_days` | 90 | Elimina cuarentenas revisadas 90 días después de `resolved_at`; las no resueltas se conservan. |
| `retention_ingestion_diagnostic_months` | 24 | Limpia `ingestion_log.counts` y `error_msg` de corridas antiguas. |

La tarea ejecuta la purga incluso cuando ninguna fuente está vencida. Las filas
de `ingestion_log`, sus estados, fechas y las versiones históricas no se
eliminan; por eso siguen vigentes sus claves foráneas de proveniencia. Al pasar
una cuarentena a `resolved = TRUE`, un trigger registra `resolved_at`.
La resolución administrativa se registra actualizando `resolved = TRUE` en la
fila correspondiente; `reviewed` permanece como indicador separado de revisión.

## Validación PostgreSQL

El runner protegido usa exclusivamente `CEEBI_TEST_DB_*`, exige que el nombre
termine en `test` y rechaza el `DB_NAME` de `.env`. Sobre la base aislada,
ejecuta:

```powershell
python -m etl.run_phase3_pg_integration
```

El runner prepara el esquema base solo si está vacío, sin ejecutar sentencias
`DROP TABLE`, aplica las migraciones 003/004 y corre las suites PostgreSQL de
Fase 3 y Fase 4. La base de pruebas puede conservar registros sintéticos
confirmados en las pruebas de lock/recuperación; no debe apuntarse a una base
de aplicación.

Resultado de cierre: **58/58** pruebas offline y **14/14** casos del runner
PostgreSQL pasan; los 14 incluyen las pruebas de integración de Fases 3/4 y las
validaciones de seguridad de configuración.
