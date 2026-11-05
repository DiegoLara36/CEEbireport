# Fase 1 — Línea base de contratos y extractores

## Alcance y entorno

Esta fase documenta el comportamiento actual y añade pruebas offline. No se
cambió la lógica de producción, ETLEngine, DDL, configuración o dependencias. No
se ejecutaron capturas ni se conectó con PostgreSQL.

El trabajo se realizó sobre la rama existente `refactor/fase-1-contracts`; al
iniciar, el árbol estaba limpio. No se creó ni cambió de rama para preservar el
contexto local ya preparado.

## Mapa de esquemas Pydantic

Todos los esquemas están hoy en `etl/schemas.py`. “Opcional” incluye campos con
valor por defecto, aunque no estén anotados con `Optional`.

| Clase | Rol | Obligatorio | Opcional/default | Validaciones |
|---|---|---|---|---|
| `ScheduleItemSchema` | Auxiliar común de horario | día 1–7, inicio, fin | Ninguno | `check_times`: fin posterior al inicio. |
| `ProfessorSchema` | Auxiliar de docente | nombre de al menos 2 caracteres | especialidad | Ninguna adicional. |
| `ContactSchema` | Auxiliar de contacto | nombre de al menos 2 caracteres | teléfono, email | Ninguna adicional. |
| `CourseExtractedSchema` | Contrato específico PUCP de extracción | `external_id`, `course_url`, `name`, `external_offering_code`, `price_amount` | fuente PUCP, categoría, modalidad, fechas, duración, certificados, contenido, contacto/docente, moneda PEN, datos raw | tipo literal de modalidad/moneda; duración y precio no negativos; fecha final no anterior a inicial. |
| `ScrapedDiscountDTO` | Auxiliar comercial común | tipo, valor > 0 | segmento, descripción, condiciones | Porcentaje máximo 100. |
| `ScrapedPaymentPlanDTO` | Auxiliar comercial común | cuotas ≥ 1 | tarjetas vacías, interés 0 o mayor | Ninguna adicional. |
| `ScrapedPriceTierDTO` | Auxiliar de precio por segmento | segmento, importe ≥ 0 | moneda PEN, notas | Moneda limitada a PEN/USD. |
| `ScrapedOfferingDTO` | DTO común de oferta/cohorte | `list_amount`, `final_amount` | código, modalidad, fechas/duración/horarios, moneda PEN, descuentos, niveles, planes, raw | no negativos; fin no anterior al inicio; genera código `PERMANENT` o código derivado por fecha/horario. |
| `ScrapedCourseDTO` | DTO común de curso | fuente (PUCP/CIBERTEC), id externo, URL, nombre | categoría, descripción, duración, certificados, listas de contenido/requisitos, ofertas, raw | nombre de ≥3 caracteres; requiere ≥1 oferta. |

Relaciones: `ScrapedCourseDTO` contiene ofertas; una oferta contiene horarios,
descuentos, niveles de precio y planes de pago. `CourseExtractedSchema` también
contiene horarios y docente/contacto, pero es anterior/específico de PUCP y no
es el contrato persistido por `ETLEngine`.

`pucp_course_to_dto()` adapta la extracción PUCP al contrato común y deriva
niveles de precio, descuentos y planes desde `price_details_raw`. No es un
esquema Pydantic; es el transformador entre dos contratos.

## Recorrido de campos

```text
HTML/catálogo institucional
  ├─ PucpExtractor: HTTP → BeautifulSoup → CourseExtractedSchema
  │      └─ pucp_course_to_dto() → ScrapedCourseDTO
  └─ CibertecExtractor: Playwright → HTML/BeautifulSoup → ScrapedCourseDTO
            ↓
ETLEngine._extract_one()
            ↓
ETLEngine._persist_course() / _persist_offering()
            ↓
PostgreSQL: course, course_offering, tablas hijas, price_snapshot, auditoría
```

### Matriz PUCP vs. Cibertec

| Grupo de datos | PUCP: extracción y DTO | Cibertec: extracción y DTO | Persistencia de `ETLEngine` |
|---|---|---|---|
| Institución e identidad | Fuente constante `PUCP`; ID es el último segmento URL, no un ID institucional confirmado. | Fuente constante `CIBERTEC`; ID es el último segmento URL. | Fuente se resuelve por `source`; ID externo único por fuente en DDL. URL se guarda en `course`. |
| Nombre, descripción, categoría | `CourseExtractedSchema`; categoría también puede tener fallback del catálogo. | `h1`, primer párrafo largo, breadcrumb/fallback de catálogo. | `course.name`, `description`, `category_id`, `course_url`. |
| Fechas | Fechas de inicio/fin en español; el parser de PUCP usa mes/año actual e ignora el año explícito capturado por el regex. | Fecha de inicio por cohorte; no se extrae fecha de fin. Año inferido según mes actual. | Fechas de oferta se guardan y participan del hash. |
| Duración | Horas en extracción PUCP. | Horas y semanas, desde cohorte o lista; horas también se copian al nivel curso. | Horas a nivel curso y horas/semanas a oferta. |
| Modalidad y horario | Modalidad inferida principalmente por URL de brochure; horario convertido en `ScheduleItemSchema`. | Modalidad heurística desde texto; horario desde controles/cohorte. | Modalidad por FK y horarios en `offering_schedule`. |
| Docentes y contactos | Se parsea un docente (nombre/especialidad) y contacto (solo nombre); se pierden al adaptar al DTO común. | No se extraen actualmente. | El motor común no persiste ninguno. |
| Certificados | Certificado/constancia parseados. | Booleanos heurísticos por texto de página. | El motor persiste ambos booleanos de curso; no crea `offering_certificate`. |
| Contenido y requisitos | Temario itemizado. No se llenan prerrequisitos, hardware ni software. | Contenido depende de módulos interactivos; prerrequisitos/hardware/software por encabezados HTML. | Contenido, requisitos previos y técnicos en tablas hijas, pero se reemplazan solo al crearse/cambiar el curso según hash. |
| Precios, moneda, descuentos, cuotas | Importe principal; detalles raw se transforman a niveles, descuentos y cuotas. | Lista/final, descuento y tarjetas/cuotas inferidos del texto; moneda fijada a PEN. | Snapshot de lista/final/moneda; tiers, descuentos, asociación al snapshot y planes. |
| Raw específico | Público objetivo, brochure y textos de fecha/precio en raw del offering; slug en raw de curso. | Módulos/cohortes/beneficios en raw de curso/oferta. | `dto.raw_payload` de curso se ignora; raw de oferta se copia a snapshot solo al crear/cambiar precio. |

Campos opcionales presentes en el DTO pero no extraídos por uno o ambos sitios
incluyen certificados de participación (se parsea por ambas fuentes),
prerrequisitos/requisitos técnicos (solo Cibertec), horarios (ambas) y
duración-semanas/sesiones (Cibertec solo semanas hoy). No se parsean docentes ni
contactos en el camino común.

## Extractores: métodos y comportamiento

### PUCP

- `BaseExtractor.fetch_page()` usa `requests.Session`, timeout, `raise_for_status`,
  captura bytes y acumula `raw_pages`.
- `PucpExtractor.fetch_catalog_index()` lee `data-pages`, construye URLs de
  paginación, deduplica URLs y salta páginas posteriores que fallen con error
  de requests. El fallo de la primera página se propaga. Entre páginas espera
  un segundo por defecto.
- `PucpExtractor.parse_course_detail()` lee campos por posición (índices 0–18
  de `.jet-listing-dynamic-field__content`), asigna slug URL como ID y arma el
  esquema PUCP.
- Helpers convierten fecha, precio, duración, temario, brochure y horario.
  Valores inválidos pueden producir `ValueError`/errores de validación.
- Dependencias específicas: `requests`, BeautifulSoup, Django `timezone` y
  `slugify`. No usa Playwright.

### Cibertec

- `CibertecExtractor.start()` inicia Playwright sync, Chromium headless y una
  página; `close()` cierra browser/runtime.
- `fetch_catalog_index()` navega el catálogo, espera selector; timeout del
  selector inicial guarda HTML y devuelve lista vacía. Recorre paginación JS,
  deduplica URL y captura campos de tarjeta. Timeout después de pulsar “siguiente”
  termina la paginación sin propagar el error.
- `fetch_course()` visita detalle; interactúa con botones de módulos y cohortes,
  captura HTML, luego llama al parser.
- `parse_course_detail()` usa `BeautifulSoup`; recibe opcionalmente los módulos,
  cohortes y datos de catálogo que recolectó `fetch_course`.
- Errores HTTP se representan como `requests.HTTPError`; otros errores de
  Playwright/selector se propagan para que el motor los clasifique.
- Dependencias específicas: Playwright/Chromium, BeautifulSoup, requests para
  tipo de excepción, Django timezone y slugify.

### Diferencias de contrato

PUCP hereda `BaseExtractor`, emplea HTTP sin navegador y produce primero
`CourseExtractedSchema`; Cibertec no hereda esa base, requiere interacción
dinámica con navegador y produce directamente `ScrapedCourseDTO`. El desacople
actual es, por tanto, parcial: la interfaz no garantiza un mismo tipo de
resultado en cada extractor.

## ETLEngine y PucpETLService

### Camino activo de ETLEngine

`ETLEngine._make_extractor()` selecciona PUCP/Cibertec mediante condiciones.
`run()` asegura la fuente, crea `ingestion_log`, obtiene índice, llama
`_extract_one()`, persiste curso/oferta, evalúa mínimo de cursos, marca ausentes
y finaliza. El comando `run_ingestion` usa este motor para ambas instituciones;
`seed_catalog` también usa el motor, limitado a PUCP.

`_extract_one()` maneja la diferencia de contrato: Cibertec devuelve DTO por
`fetch_course`; PUCP descarga detalle, parsea esquema propio y lo adapta.
`run()` registra excepciones de extracción por curso en cuarentena; errores
request también aumentan el contador HTTP. Un error fuera del bloque por curso
lleva al manejo general de falla. Siempre intenta cerrar el extractor.

La persistencia usa SQL parametrizado y `transaction.atomic()` por curso, además
de transacciones para fuente, inicio/final y resultados. Inserta por
`(source_id, external_id)`, compara hashes, registra `change_log` y hace
snapshot de precio solo si cambia monto/moneda. El DDL contiene trigger para
cerrar el snapshot anterior. Las transacciones y constraints no se probaron
contra PostgreSQL en esta fase.

### Idempotencia y riesgos detectados

- El hash de curso incluye solo nombre, descripción, duración y certificado.
  No incluye categoría, URL, constancia, contenido o requisitos. Un cambio
  únicamente en esos atributos no entra al branch de actualización y puede no
  reflejarse.
- El hash de oferta incluye fechas, modalidad, duración y horarios; excluye
  `sessions_count` y otros atributos. Al modificar oferta sí se reemplazan los
  horarios; sin cambio de hash, no.
- `course_content_item`, prerequisitos y requisitos técnicos se reemplazan
  cuando cambia hash del curso o se crea, pero no se sincronizan si la captura
  no cambia campos incluidos en ese hash.
- Descuentos/tier/planes existentes se insertan o comparan parcialmente; la
  ausencia posterior de uno no necesariamente lo desactiva/cierra.
- `offerings_changed` no cuenta cambios de precio solamente.
- La unicidad del DDL previene duplicados persistentes, pero no se implementa
  lock de ejecución por fuente. Dos procesos simultáneos pueden competir; para
  un curso nuevo, una carrera puede fallar en la constraint única.
- Si una captura supera el umbral mínimo, `_mark_missing()` puede marcar como
  inactivos cursos/ofertas no vistos. La salvaguarda usa porcentaje de cursos,
  no certeza de que cada página del catálogo fue descargada.
- Se guarda `PARTIAL` al comenzar; un cierre forzado podría dejar una ingesta
  incompleta en ese estado.
- El `raw_payload` del curso no se persiste en el camino ETLEngine. El raw del
  offering se guarda solo al insertar un snapshot de precio; un cambio de
  atributos académicos sin cambio de precio no actualiza ese payload.

### Ruta alterna PucpETLService

`PucpETLService` dentro de `etl/extractors/pucp.py` es una ruta autónoma de
extracción y persistencia PUCP, pero no la invocan los comandos actuales. Duplica
fuente/ingesta, SQL de curso/oferta, precios, contenido y auditoría.

Funcionalidades exclusivas frente al motor común: persiste
`CourseExtractedSchema.professor`, `contact` y certificados de oferta; guarda
algunos datos de extracción en `price_snapshot.raw_payload`; mantiene un
reporte en memoria de elementos omitidos. Esto no significa que el flujo activo
los aproveche.

Diferencias que impiden considerarlo equivalente: usa hash de curso pero su
upsert actualiza el curso en cada ejecución y registra siempre `CREATED` o
`UPDATED`; reemplaza contenido/horarios solo si la lista nueva no está vacía,
por lo que puede retener filas antiguas; compara el precio actual por
`list_amount`/moneda, y escribe el mismo importe como lista y final; no usa la
resiliencia, el umbral mínimo ni el mismo esquema DTO común; no implementa
`close()` explícito del extractor. Debe conservarse durante la línea base, pero
no introducirse como alternativa activa sin decidir la consolidación.

## Pruebas añadidas y ejecución

Fixtures agregados bajo `etl/fixtures/`:

- dos páginas de catálogo PUCP (duplicado, curso válido y URL excluida);
- detalle PUCP con campos posicionales representativos;
- catálogo Cibertec y segunda página;
- detalle Cibertec con breadcrumbs, contenido, requisitos, certificados,
  precios y texto comercial.

`etl/test_fase1_baseline.py` prueba validaciones Pydantic, recorrido de
paginación PUCP y Cibertec simulando sus clientes, parseo de detalles, adaptación
PUCP al DTO, rutas `_extract_one()`, errores de catálogo, y el comportamiento de
`_persist_price()` para reutilizar o crear un snapshot usando un cursor simulado.
El navegador, las peticiones de red y el cursor se simulan; los fixtures son
archivos locales. Esto no sustituye pruebas de integración con PostgreSQL.

Comando ya ejecutado:

```powershell
.\.venv\Scripts\python.exe manage.py test etl.test_fase1_baseline etl.tests --verbosity 2
```

La ejecución final incluyó además `catalog.tests`:

```powershell
.\.venv\Scripts\python.exe manage.py test etl.test_fase1_baseline etl.tests catalog.tests --verbosity 2
```

Resultado: **26 pruebas aprobadas**: 16 nuevas pruebas de línea base, las 7
pruebas preexistentes de `etl/tests.py` y las 3 pruebas de `catalog/tests.py`.
Django indicó `Skipping setup of unused database(s): default`; no se creó ni se
utilizó una base de pruebas. Un primer intento detectó que el fixture Cibertec
no reflejaba el contenedor `div` esperado por el parser de requisitos; se
corrigió el fixture, no el código de producción, y se repitió la suite con
éxito.

## Recomendaciones para la Fase 2

1. Añadir tests de regresión para diferencias en categoría, URL, constancia,
   contenido, requisitos, horario y atributos de oferta antes de cambiar hashes.
2. Acordar qué campos de omisión se deben promover desde los esquemas de fuente
   al DTO común: al menos profesor/contacto PUCP, campos anidados de docente y
   contacto, end_date Cibertec y payload específico.
3. Definir la representación/validez de IDs externos vs. URL canónica y cómo
   reconciliar cambios de URL antes de tocar constraints.
4. Crear pruebas de integración contra una base PostgreSQL **de pruebas
   explícitamente aislada**, con settings de test separados y una verificación
   que rechace la configuración de la base principal. Cubrir transacciones,
   historial, trigger de precios, concurrencia e idempotencia.
5. Consolidar a futuro la responsabilidad de persistencia en una sola ruta;
   no eliminar `PucpETLService` hasta demostrar que sus campos exclusivos quedan
   cubiertos y sus datos históricos están preservados.
6. Diseñar historia de versiones completas de curso/oferta por encima del
   `change_log`; los hashes actuales no cubren todos los atributos acordados.

## Aclaración posterior a la Fase 2

Las recomendaciones anteriores registran lo que quedaba pendiente al cerrar la
línea base; no describen el estado actual. La Fase 2 agregó docentes y contactos
al DTO común y los transfiere desde PUCP. Cibertec marca esos campos como
`NOT_COLLECTED`. Ninguno se persiste todavía. El DTO común, los adaptadores y
normalizadores ahora están documentados en `ARCHITECTURE_DECISIONS.md` y
`REFACTORING_PLAN.md`.

Siguen pendientes para la Fase 3: cambiar el hash usado por `ETLEngine`,
persistir atributos relevantes y su historial, completar integración de datos
que no existen en los esquemas actuales (por ejemplo, fecha final Cibertec) y
realizar pruebas aisladas contra PostgreSQL. Cibertec ya no sustituye un precio
ausente por `0`; sin embargo, el ETL/DDL vigente requiere un precio para la
persistencia, así que ese registro incompleto debe tratarse en la futura fase
de persistencia. No se modificó la persistencia ni el DDL en la Fase 2.
