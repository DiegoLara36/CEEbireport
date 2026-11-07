# Fase 3 — Validación de integración PostgreSQL

## Resultado

La suite se ejecutó en la base aislada `ceebi_phase3_test`. El bootstrap no
destructivo y la migración terminaron correctamente. **Pasaron los 8 casos de
integración PostgreSQL y las 3 comprobaciones de seguridad del runner.** No se
usó la base configurada para la aplicación.

| Componente | Estado |
|---|---|
| Rama | `refactor/fase-3-persistence` |
| PostgreSQL local | Servicio activo; `psql` 16.6 y `psycopg` disponibles |
| Variables `CEEBI_TEST_DB_*` | Cargadas desde configuración local |
| Runner PostgreSQL seguro | Preflight y bootstrap aprobados |
| Pruebas offline ETL/catálogo | 52/52 aprobadas |
| Pruebas de integración PostgreSQL | 8/8 aprobadas |
| Comprobaciones de seguridad del runner | 3/3 aprobadas |

El preflight requiere `CEEBI_TEST_DB_USER` y `CEEBI_TEST_DB_PASSWORD`; el
nombre predeterminado es `ceebi_phase3_test`. Antes de abrir cualquier
conexión, rechaza nombres que no terminen en `test` y nombres iguales a
`DB_NAME` de `.env`. El runner acepta los valores `CEEBI_TEST_DB_*` del entorno
del proceso o de `.env`, dando prioridad al entorno, y no usa las credenciales
`DB_*` de la aplicación.

## Correcciones incorporadas

1. **Identidad de docentes:** `OfferingRepository` bloquea y busca por nombre
   dentro del `source_id`, uniendo `professor_source_link`. Un homónimo de otra
   institución no se reutiliza por coincidencia global de nombre.
2. **Catálogo incompleto:** PUCP y Cibertec exponen `catalog_complete`. Un fallo
   de página PUCP o timeout durante la paginación Cibertec lo deja falso.
   `ETLEngine` omite `mark_missing` y registra estado `PARTIAL` si el catálogo
   es incompleto, aunque se cumpla el porcentaje mínimo. También bloquea esa
   reconciliación si `limit` recorta el listado.
3. **Proveniencia y retención:** las versiones históricas tienen
   `ingestion_id NOT NULL` con `ON DELETE RESTRICT`; se eliminó el vínculo
   redundante `ingestion_id_original`. La migración instala un trigger que
   impide borrar filas de `ingestion_log`, conservando la proveniencia mínima.
   La política permite redactar `error_msg` y purgar `raw_page` y cuarentenas.
   Se añadió preflight para disponibilidad y privilegios de `btree_gist`.

## Runner y cobertura PostgreSQL

Ejecutar desde la raíz, tras configurar las variables localmente o en `.env`:

```powershell
$env:CEEBI_TEST_DB_NAME = "ceebi_phase3_test"
$env:CEEBI_TEST_DB_USER = "<rol exclusivo de pruebas>"
$env:CEEBI_TEST_DB_PASSWORD = "<contraseña del rol de pruebas>"
$env:CEEBI_TEST_DB_HOST = "localhost"
$env:CEEBI_TEST_DB_PORT = "5432"
python -m etl.run_phase3_pg_integration
```

El runner comprueba que la base está vacía antes de aplicar `DDL.txt` filtrando
todas las sentencias `DROP TABLE`; si encuentra un esquema parcial, aborta sin
reconstruirlo. Después aplica
`database/migrations/003_persistence_history.sql` y ejecuta
`etl/test_phase3_pg_integration.py`. La suite comprueba migración y FK
permanente, exclusión temporal, trigger de `price_snapshot`, idempotencia,
nuevas versiones ante cambios, reconciliación por URL canónica, rollback por
curso y separación de homónimos entre fuentes.

La primera ejecución encontró que el fingerprint inicial de una oferta difería
del usado al comparar capturas posteriores. Se corrigió
`OfferingRepository._insert_offering` para guardar el mismo fingerprint
canónico; la segunda ejecución pasó toda la suite: **11/11 pruebas** (8 casos
PostgreSQL y 3 comprobaciones de seguridad).

## Pruebas offline ejecutadas

Comando:

```powershell
python manage.py test etl catalog --verbosity 1
```

Resultado: **52 pruebas offline aprobadas**; las 8 pruebas PostgreSQL se
ejecutan mediante el runner dedicado. La prueba adicional del motor confirma
que un catálogo incompleto no invoca `mark_missing`, aun superando el umbral.

La Fase 3 queda validada. La Fase 4 no se ha iniciado.
