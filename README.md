# Radar Inmobiliario Hurlingham

Aplicación web local para recopilar, comparar y geolocalizar propiedades residenciales
en venta en el partido de Hurlingham.

## Inicio rápido

```powershell
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

Abrir `http://127.0.0.1:8000/`.

`seed_demo` es opcional y solo carga tres propiedades ficticias para revisar la interfaz.

## Recolección

```powershell
# Auditoría de disponibilidad y robots.txt
python manage.py audit_sources

# Prueba limitada a la primera página
python manage.py scrape --source argencasas --max-pages 1

# Todas las fuentes habilitadas
python manage.py scrape --all

# Geocodificar avisos sin coordenadas
python manage.py geocode_pending --limit 250
```

Las fuentes habilitadas inicialmente son Mapaprop y Argencasas. Las inmobiliarias
locales sindicadas en esos portales se conservan como agencias del aviso, evitando
consultas duplicadas.

## Automatización nocturna

Ejecutar PowerShell como el usuario que utilizará la aplicación:

```powershell
.\scripts\install_scheduled_task.ps1
```

Por defecto se crea una tarea día por medio a las 03:00. La PC debe estar
encendida; la opción `StartWhenAvailable` permite recuperar una ejecución omitida.

El barrido de enlaces se ejecuta con `python manage.py audit_listing_links --apply`
(sin `--apply` solo informa). Recorre todas las publicaciones guardadas, incluso
inactivas, y registra cada resultado en `logs/link-audit-*.jsonl`. Solo marca bajas
confirmadas; errores, bloqueos y páginas sin estado reconocible quedan pendientes.
Conserva las correcciones manuales y no elimina registros. La tarea de Windows
`Radar - Barrido full de enlaces` ejecuta `scripts/run_link_audit.ps1` los domingos
a la 01:00.

Para continuar un barrido interrumpido, usar
`python manage.py audit_listing_links --apply --resume --report logs/link-audit-FECHA.jsonl`.
Conserva el informe y omite los pares ID/URL ya registrados, incluidos errores e
inciertos. RE/MAX reutiliza las páginas de búsqueda dentro de cada ejecución.

El barrido respeta `BLOCKED_SOURCE_SLUGS`, igual que la interfaz y el scraping:
Inmuebles Clarín queda excluido por duplicar Argenprop, incluso al reanudar.
Sus publicaciones históricas se conservan y no se vuelven a consultar.

### Argenprop con navegador (experimental, pendiente de acceso)

Instalar `requirements-browser.txt` y ejecutar `python -m playwright install chromium`.
Prueba sin modificar datos:

```powershell
python manage.py scrape_argenprop_browser --state tmp/argenprop-prueba.json --max-pages 1 --max-listings 3
```

Para una corrida completa, usar otro archivo `--state` y `--apply`, sin límites.
Repetir exactamente el comando reanuda el checkpoint. Las corridas completas
incluyen enlaces históricos y solo retiran respuestas 404/410 confirmadas.
El navegador es aislado, sin perfiles personales. Se detiene ante bloqueos o
robots inaccesible. El 20/09/2026 el navegador autónomo recibió 403 en robots.txt;
la prueba no modificó datos. No está activado en los jobs programados hasta que
pase una prueba real de lectura e importación.

## Geolocalización

- Las coordenadas publicadas por la fuente tienen prioridad.
- Las direcciones pendientes se consultan con Nominatim, con caché permanente y una
  solicitud por segundo como máximo.
- Un pin verde es exacto o confirmado manualmente.
- Un pin amarillo con halo es aproximado a intersección, calle o barrio.
- Las correcciones manuales nunca se sobrescriben durante un scraping posterior.

Para cambiar el servidor de mapas o geocodificación se pueden definir
`MAP_TILE_URL`, `MAP_ATTRIBUTION`, `NOMINATIM_URL` y `NOMINATIM_USER_AGENT`.

## Pruebas

```powershell
python manage.py test
node --check static\js\search-map.js
node --check static\js\detail-map.js
```

## Modo Recorrido móvil

El desarrollo del acceso móvil aislado se documenta en
[`docs/modo_recorrido_desarrollo.md`](docs/modo_recorrido_desarrollo.md), el
bloque de reconocimiento ya implementado en
[`docs/plan_modo_recorrido_bloque_2.md`](docs/plan_modo_recorrido_bloque_2.md) y
el bloque implementado de filtros, enlaces y rendimiento en
[`docs/plan_modo_recorrido_bloque_3.md`](docs/plan_modo_recorrido_bloque_3.md).
La base SQLite permanece en esta PC y el futuro túnel apuntará solamente al
proceso móvil restringido, nunca al Radar administrativo completo.

El modo de descubrimiento permite radios de hasta 1,5 km, conserva las propiedades
detectadas durante toda la salida y las muestra en el historial. Incluye cuaderno
de campo con notas, fotos de carteles y pendientes, memoria entre salidas y avisos
de voz opcionales. El uso y las comprobaciones están en el quinto bloque de la
documentación de Modo Recorrido. Aplicar las migraciones y regenerar los estáticos
antes de reiniciar el proceso móvil después de actualizar.

La base local usa SQLite, FTS5 para texto y RTree para prefiltrar búsquedas espaciales.
## Analisis, ubicacion y exportacion

- Si un aviso no trae una direccion clara, la ingestion intenta enriquecer la
  ubicacion con texto del detalle, descripcion, JSON-LD o datos crudos del scraper.
- La app guarda localidad, barrio/zona, direccion detectada, fuente de evidencia,
  confianza y observaciones cuando el listado y el detalle se contradicen.
- Las tarjetas y el detalle permiten marcar una propiedad como favorita, revisada u
  oculta. Ocultar no elimina datos: solo la excluye de los resultados por defecto.
- El detalle incluye notas personales por propiedad.
- Los filtros permiten ver favoritas, revisadas, pendientes, ocultas y publicaciones
  con ubicacion confiable o debil.
- `http://127.0.0.1:8000/estadisticas/` muestra metricas de mercado y calidad sobre
  los filtros actuales.
- Los botones de exportacion descargan CSV o Excel respetando los mismos filtros de
  busqueda, incluyendo flags personales, notas y datos de ubicacion detectada.
