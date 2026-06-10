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

## Automatización semanal

Ejecutar PowerShell como el usuario que utilizará la aplicación:

```powershell
.\scripts\install_scheduled_task.ps1
```

Por defecto se crea una tarea para los domingos a las 09:00. La PC debe estar
encendida; la opción `StartWhenAvailable` permite recuperar una ejecución omitida.

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
