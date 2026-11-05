# CEEBI: plan incremental de refactorización

Este plan aplica las decisiones de `ARCHITECTURE_DECISIONS.md`. Es una hoja de
ruta: **no se ejecutaron cambios de código ni de PostgreSQL al redactarla**.
Las rutas de archivos son las actuales o propuestas.

## Orden y dependencias

```text
Fase 0 Línea base
  → Fase 1 Contrato y mapeo común
  → Fase 2 Persistencia encapsulada e historial
  → Fase 3 Identidad, concurrencia y ejecución confiable
  → Fase 4 Esquema y vistas BI
  → Fase 5 ORM gradual (solo si aporta valor)
  → Fase 6 Metabase embedding (tras validar instancia/licencia)
```

La fase 4 puede prepararse en paralelo con la fase 3 una vez fijados los
contratos de datos y la granularidad de las tablas versionadas. La fase 5 no es
prerrequisito para Metabase: Metabase leerá `bi`, no el ORM.

## Fase 0 — Línea base funcional

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

**Termina cuando:** la matriz campo-origen → extractor → DTO → persistencia está
documentada, y las pruebas reproducen las capacidades actuales de ambas
fuentes sin depender de páginas en vivo.

## Fase 1 — DTO común, adaptadores y normalización

**Objetivo:** garantizar un contrato Pydantic consistente para ambas
instituciones y hacer explícita la normalización.

**Archivos/componentes:** `etl/schemas.py`, `etl/extractors/base.py`,
`etl/extractors/pucp.py`, `etl/extractors/cibertec.py`; propuestos
`etl/normalization/` y módulos de esquema común/fuente.

**Acciones:** añadir al DTO común los campos analíticos acordados (docentes,
contactos, certificados y constancias además de los ya incluidos); pasar los
datos desde cada extractor y desde `pucp_course_to_dto()`; aislar campos
propios de fuente; documentar formatos normalizados y conservar raw payload
relevante. No convertir esquemas Pydantic en modelos Django.

**Implementable sin cambio de PostgreSQL:** ampliar Pydantic y los mapeos,
pruebas de campos, normalización, URL canónica y adaptadores.

**Dependencias:** fase 0.

**Riesgos:** defaults que oculten campos no mapeados; modificar el significado
de precio, duración o modalidad; perder datos que ya están en `raw_payload`.

**Pruebas necesarias:** datos obligatorios/opcionales, estructuras anidadas,
DTO completo por fuente; asserts de que profesor/contacto/certificados y
atributos propios pasan hasta la entrada de persistencia.

**Termina cuando:** ambos extractores producen el DTO común esperado y el
contrato distingue ausente, desconocido y explícitamente vacío.

## Fase 2 — Persistencia/repositorio e historial temporal

**Objetivo:** desacoplar la orquestación de SQL y preservar versiones de negocio
con la ejecución que las detectó.

**Archivos/componentes:** dividir `etl/services/etl_engine.py`; introducir
`etl/persistence/` con repositorios; documentar y preparar migraciones
aditivas para `DDL.txt` o un directorio de migraciones SQL versionadas.

**Acciones:** mover SQL del motor a operaciones explícitas de repositorio;
preservar escrituras idempotentes y transacciones actuales; añadir versiones
temporales para cursos/ofertas y atributos acordados, con vínculo a
`ingestion_log`; mantener el cierre de snapshots de precios existente.
No crear versiones cuando el hash canónico no cambie.

**Implementable en código sin cambio de DB:** extraer repositorios conservando
las tablas y SQL actuales, con pruebas de contrato.

**Requiere cambio de PostgreSQL:** tablas/columnas/constraints de historial,
FK a ejecución, y cualquier campo no persistido que se acuerde conservar.

**Dependencias:** fase 1 y diseño de granularidad temporal.

**Riesgos:** duplicar eventos entre `change_log` e historial; diferencias
concurrentes; registros sin historial previo; tratar ausencia por extracción
incompleta como eliminación real.

**Pruebas necesarias:** no-op no versiona; cambios de cada campo relevante
versionan una vez; toda versión referencia la ingesta correcta; transacción
rollback; ejecución repetida es idempotente; curso ausente en captura parcial
no se desactiva.

**Termina cuando:** se pueden consultar estados actuales y pasados, y ninguna
versión normalizada se elimina automáticamente.

## Fase 3 — Identidad y operación confiable

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

**Dependencias:** identidad aprobada y fase 2; pruebas de concurrencia antes de
habilitar la tarea automática.

**Riesgos:** URL con parámetros/tracking, cambios legítimos de URL, doble
captura por scheduler y ejecución manual, liberar locks ante terminación abrupta.

**Pruebas necesarias:** mismo recurso con URL normalizada converge; dos procesos
de una fuente producen un único ganador; fuentes distintas pueden correr según
política; fallo/interrupción queda detectable; reintento no duplica filas.

**Termina cuando:** captura diaria revisa fechas, selección manual sigue
funcionando y la exclusión/recovery está verificada en PostgreSQL de prueba.

## Fase 4 — Vistas `bi` y usuario de lectura

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
fase 2.

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

## Fase 5 — ORM gradual (condicional)

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

## Fase 6 — Integración Metabase

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

**Dependencias:** fase 4 desplegada y aceptada; verificación de capacidades y
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
| Versiones de curso/oferta y precios | Sin borrado automático | Fase 2 |
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
