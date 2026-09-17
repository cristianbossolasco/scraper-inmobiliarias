# Plan de desarrollo: Modo Recorrido — bloque 3

Fecha: 22 de agosto de 2026

Estado: implementado y validado localmente; pendiente prueba GPS en teléfono y campo

Nombre del bloque: filtros útiles, publicación original y respuesta rápida

## 1. Resultado esperado

Este bloque debe permitir preparar una búsqueda inmobiliaria antes de manejar y
profundizar una propiedad interesante sin que la interfaz se vuelva más lenta.
Al finalizarlo, el usuario podrá:

1. combinar tipos de propiedad, dormitorios mínimos, rango de precio en USD,
   superficie cubierta mínima, lote mínimo y radio;
2. comprender exactamente qué filtros están activos durante el recorrido;
3. abrir de forma segura la publicación activa correspondiente a una ficha;
4. volver desde el aviso externo sin perder sesión, traza ni filtros;
5. recibir primero los datos útiles de la tarjeta, sin esperar a que termine de
   descargar la fotografía externa;
6. usar una API comprimida, instrumentada y con un número constante de queries.

No se agregará origen/destino, navegación giro a giro, audio, PWA ni túnel en
este bloque. Tampoco se descargará o proxificará una imagen desde Django durante
la solicitud de una ficha.

## 2. Diagnóstico de rendimiento

La demora percibida no proviene principalmente de SQLite en el radio de uso
normal. Las mediciones se hicieron sobre la base real, con una consulta de
calentamiento y 25 iteraciones por escenario.

| Escenario | Resultados | Queries | p50 | p95 | JSON crudo |
|---|---:|---:|---:|---:|---:|
| Casas, 350 m | 107 | 2 | 19,1 ms | 33,9 ms | 45.776 B |
| Todos los tipos, 350 m | 141 | 2 | 28,3 ms | 51,2 ms | 60.373 B |
| Casas, 800 m | 250 | 2 | 88,4 ms | 122,8 ms | 106.880 B |
| Casas, 350 m, USD 100k–250k | 87 | 2 | 22,7 ms | 32,2 ms | 37.276 B |
| Ficha individual | 1 | 1 | 16,5 ms | 26,7 ms | 465 B |

Una medición adicional de vista + servicio + `JsonResponse` mantuvo 350 m por
debajo de 63 ms p95. A 800–1.000 m el costo sube por materializar muchos modelos
Django; la serialización JSON no es el componente dominante.

Hallazgos de red y recursos:

- 60.373 B de JSON se comprimen a aproximadamente 4.672 B con gzip;
- 106.967 B se comprimen a aproximadamente 7.931 B;
- MapLibre ocupa 264.432 B comprimidos, pero ya tiene caché inmutable;
- la carga inicial observó 15 teselas de OpenStreetMap;
- una tesela de muestra tardó aproximadamente 200 ms y pesó 6.987 B;
- una foto real de aviso pesó 428.692 B y tardó 1,23 s en la muestra.

Conclusión: se priorizarán compresión del JSON, una proyección ORM más liviana,
instrumentación y render inmediato de la tarjeta. La foto seguirá siendo
asíncrona y nunca bloqueará dirección, precio, acciones o enlace.

## 3. Decisiones funcionales

### 3.1 Filtros

| Filtro | Comportamiento |
|---|---|
| Tipo | selección múltiple; vacío significa todos |
| Dormitorios | mínimo; una propiedad sin dato queda excluida |
| Precio | mínimo y/o máximo, siempre en USD |
| Cubierta | mínimo sobre `covered_area`; no usa otro campo como fallback |
| Lote | mínimo sobre `land_area`; no usa otro campo como fallback |
| Radio | 200, 350, 500 u 800 m |

Todos los filtros se combinan con `AND`. Un valor vacío significa “sin límite”.
Cuando exista precio mínimo o máximo, la API exigirá `currency="USD"`; nunca se
comparará un precio en pesos contra límites expresados en dólares.

Disponibilidad actual entre propiedades elegibles:

- dormitorios: aproximadamente 91%;
- superficie cubierta: aproximadamente 90%;
- lote: aproximadamente 55%;
- moneda USD: más de 99,9%.

La interfaz explicará que un aviso sin el dato solicitado no puede demostrar que
cumple el filtro y por eso queda fuera.

### 3.2 Configuración previa

La pantalla inicial mostrará:

- tipos como controles grandes de selección múltiple;
- dormitorios mínimos como `Todos`, `1+`, `2+`, `3+`, `4+` y `5+`;
- precio desde/hasta en USD con teclado numérico;
- cubierta y lote mínimos en m²;
- radio separado de las características inmobiliarias;
- resumen en lenguaje compacto antes de iniciar.

No se consultará la API al tocar cada control. Los cambios vivirán en un borrador
local y se aplicarán una sola vez al iniciar. Valores inválidos no generan
requests.

### 3.3 Durante el recorrido

Los inputs quedan bloqueados. Solo se muestra un resumen de hasta dos líneas:

```text
Casa + PH · 3+ dorm. · USD 100k–250k
Cub. 100+ · Lote 250+ · Radio 350 m
```

En una pantalla estrecha podrá condensarse como `5 filtros activos · Ver`; la
vista será informativa y no abrirá teclado durante la conducción.

### 3.4 Publicación original

La tarjeta incorporará `Ver publicación` y el resumen final incorporará `Ver
publicación original` por cada propiedad guardada.

Reglas:

- solo una publicación activa;
- preferir el mismo aviso que provee la fotografía;
- si ese aviso no existe, elegir el activo más reciente de forma determinista;
- aceptar solamente HTTPS absoluto, hostname válido, sin credenciales ni
  fragmento;
- ocultar la acción si no existe un enlace seguro;
- usar un `<a target="_blank" rel="noopener noreferrer external">`;
- no hacer `prefetch` ni abrir automáticamente;
- en un grupo, el usuario primero elige una publicación individual;
- durante el recorrido se muestra una advertencia de seguridad una vez por
  sesión; en el resumen final el enlace abre directamente.

La sesión se persiste antes de abandonar la pestaña. Al volver, Radar intentará
readquirir Wake Lock y continuará según las limitaciones del navegador.

## 4. Contrato de API

Solicitud aditiva a `POST /api/recorrido/cercanas/`:

```json
{
  "latitude": -34.59,
  "longitude": -58.64,
  "radius_m": 350,
  "property_types": ["house", "ph"],
  "price_currency": "USD",
  "price_min": 100000,
  "price_max": 250000,
  "bedrooms_min": 3,
  "covered_area_min_m2": 100,
  "land_area_min_m2": 250
}
```

La respuesta añadirá `applied_filters` normalizado. Esto permite confirmar que
el servidor aplicó exactamente los criterios que muestra la sesión.

La ficha sumará:

```json
{
  "covered_area_m2": 118,
  "land_area_m2": 280,
  "original_url": "https://inmobiliaria.example/aviso/123",
  "original_host": "inmobiliaria.example"
}
```

`area_m2` se conservará inicialmente por compatibilidad. Ningún endpoint móvil
devolverá `raw_data`, notas, evidencias, `manual_overrides` ni información de
administración.

## 5. Validación de entradas

- Rechazar booleanos, `NaN`, infinitos, negativos y notación exponencial.
- Dormitorios: entero entre 0 y 12.
- Precio: enteros USD dentro del rango válido del Radar; mínimo no mayor al
  máximo.
- Superficies: números positivos hasta 100.000 m².
- Campos ausentes o `null`: sin filtro.
- Repetir todas las validaciones en servidor; el cliente no es autoridad.
- Un error 400/500 conserva los marcadores y filtros anteriores.
- Una búsqueda sin resultados conserva sus filtros; nunca los relaja sola.

## 6. Diseño backend

### 6.1 Cercanas

1. Mantener RTree como primera reducción espacial.
2. Aplicar todos los nuevos filtros en SQL antes del bucle Haversine.
3. Mantener exactamente dos queries.
4. Sustituir la materialización de modelos completos por una proyección dedicada
   con `.values()` o equivalente probado.
5. No cargar `title`, dirección, `manual_overrides` ni campos de ficha.
6. Usar un diccionario de etiquetas en lugar de llamar repetidamente a
   `get_property_type_display()`.
7. Evitar el `EXISTS` duplicado en `SELECT` y `WHERE` usando un filtro directo.
8. Reducir el payload cercano a los campos que realmente usa el mapa, la
   proximidad y la agrupación.

Los índices actuales de precio, dormitorios, cubierta, lote, RTree y listing
activo son suficientes para el volumen actual. No se agregará un índice compuesto
especulativo. Solo se reconsiderará si 350 m supera 100 ms p95 o la ficha supera
50 ms p95 después de implementar la proyección.

### 6.2 Ficha

El enlace se obtendrá mediante `Subquery`, igual que la imagen, para conservar
una query y evitar N+1. No se descargará el destino desde el servidor.

Las correcciones manuales se preservan porque los filtros leen exclusivamente
campos canónicos de `Property`. No se escribirá ni recalculará ningún dato desde
un listing o `raw_data`.

### 6.3 Compresión e instrumentación

- Incorporar `GZipMiddleware` únicamente al host móvil, manteniendo WhiteNoise
  para los estáticos precomprimidos.
- Añadir `Server-Timing` para separar servicio/ORM y serialización del tiempo de
  red/túnel.
- Registrar métricas agregadas sin coordenadas precisas ni payloads.
- Crear un benchmark repetible con warmup, p50/p95, queries, candidatos RTree,
  resultados, truncación, bytes crudos y gzip.

No se agregará caché global por coordenada exacta: tendría poca reutilización,
complicaría favoritos/frescura y aumentaría la superficie de privacidad.

## 7. Diseño frontend y rendimiento percibido

- El formulario mantiene un borrador local; un único request aplica filtros.
- Una firma estable evita requests duplicados con filtros y posición equivalentes.
- `AbortController` cancela respuestas obsoletas.
- La tarjeta muestra inmediatamente precio/tipo provenientes de cercanas y un
  placeholder de tamaño fijo.
- Dirección, superficies y enlace aparecen en cuanto llega la ficha.
- La imagen se asigna después y no modifica el layout si tarda o falla.
- Solo se carga la imagen de la tarjeta activa.
- En el resumen, las imágenes usan dimensiones fijas, `loading="lazy"` y un
  primer lote máximo de 20 propiedades.
- MapLibre no se reinicializa al editar filtros ni al abrir/cerrar la tarjeta.
- La sesión subirá a versión 2 con migración local desde los filtros v1
  (`propertyType` y `radiusM`) para no perder un recorrido reciente.

El proxy o caché local de miniaturas queda como contingencia. Se evaluará solo si
la prueba en teléfono muestra foto p95 superior a 3 s o una tasa de error material.
Implementarlo correctamente exige allowlist de hosts, límites de bytes, MIME y
dimensiones, timeouts, redirecciones seguras y almacenamiento separado; no debe
improvisarse dentro del request de ficha.

## 8. Etapas de implementación

### Etapa A — Instrumentación y benchmark

- Crear benchmark reproducible.
- Añadir tiempos internos y cabecera `Server-Timing`.
- Guardar la línea base antes de cambiar consultas.

### Etapa B — Rendimiento backend

- Añadir gzip dinámico.
- Implementar proyección liviana de cercanas.
- Eliminar campos y trabajo redundantes.
- Verificar 2 queries, payload y resultados equivalentes con filtros por defecto.

### Etapa C — Filtros

- Ampliar parser y queryset.
- Añadir `applied_filters`.
- Implementar normalización y validación JS.
- Crear configuración previa y resumen bloqueado en marcha.
- Migrar sesión local v1 a v2.

### Etapa D — Publicación original

- Seleccionar y sanear listing activo en la ficha.
- Añadir acción en tarjeta individual.
- Añadir enlaces en favoritas del resumen.
- Persistir sesión antes de abrir y validar regreso.

### Etapa E — QA y ajuste

- Pruebas focalizadas Django y `node:test`.
- Suite completa.
- Retrato, paisaje y ancho de 320–390 px.
- Perfil frío/caliente en PC.
- Después del túnel: 50 muestras por escenario desde el teléfono y datos
  móviles, con scraper detenido y trabajando.

## 9. Objetivos medibles

| Métrica | Objetivo del bloque |
|---|---:|
| Cercanas 350 m local p95 / p99 | ≤ 100 / 150 ms |
| Cercanas 800 m local p95 | ≤ 250 ms |
| Ficha local p95 | ≤ 50 ms |
| Queries cercanas / ficha | 2 / 1 |
| JSON cercano máximo crudo | ≤ 150 KB |
| JSON cercano máximo gzip | ≤ 15 KB |
| Ficha | ≤ 2 KB |
| Serialización JSON p95 | ≤ 10 ms |
| Procesamiento JS de 250 resultados p95 | ≤ 10 ms |
| `setData` + render MapLibre en teléfono p95 | ≤ 50 ms |
| Persistencia local p95 | ≤ 25 ms y máximo cada 5 s |
| Estáticos esenciales comprimidos | ≤ 350 KB |
| Mapa utilizable por 4G frío / caliente | ≤ 3 s / 1,5 s |
| API 350 m por túnel p95 / p99 | ≤ 800 ms / 1,5 s |
| Ficha por túnel, sin foto, p95 | ≤ 600 ms |
| Foto opcional p95 | ≤ 3 s, siempre con fallback |

Los milisegundos no serán asserts frágiles de CI. Las pruebas automáticas fijarán
queries, contrato, seguridad, campos y tamaño; la latencia se medirá con el
benchmark sobre la base real.

## 10. Criterios de aceptación

- Los filtros por defecto producen el mismo universo que antes del bloque.
- `USD 100k–200k` nunca incluye ARS.
- `3+ dormitorios` excluye propiedades sin dormitorios informados.
- Cubierta y lote se aplican a sus campos exactos y se combinan con `AND`.
- Cancelar o un error no cambia filtros activos ni marcadores.
- Recuperar una sesión conserva todos los filtros.
- La API mantiene 2 queries y la ficha 1 con una o cien propiedades/listings.
- Ningún campo manual, favorito, nota o timestamp se modifica al filtrar.
- `original_url` proviene de un listing activo y seguro.
- Foto y enlace corresponden al mismo aviso cuando es posible.
- Un grupo nunca abre arbitrariamente un aviso representativo.
- El enlace externo no recibe `window.opener` ni referrer.
- Abrir un aviso y volver conserva traza, filtros y sesión.
- La tarjeta es utilizable antes de que cargue la foto.
- No se descargan imágenes de propiedades no abiertas.
- Gzip y `Server-Timing` funcionan en el host móvil.
- Se cumplen los límites de queries y payload.
- Las suites focalizadas, JavaScript, Django completa y QA visual pasan.

## 11. Decisión posterior

Después de este bloque se habilitará el túnel y se medirán los tiempos reales en
el teléfono. Con esos datos se decidirá si el siguiente paso es:

1. PWA y operación diaria;
2. miniaturas locales seguras;
3. audio;
4. abrir la propiedad en Google Maps;
5. navegación con origen y destino.

La prioridad se decidirá con datos de uso, no agregando todas esas funciones en
el mismo cambio.
