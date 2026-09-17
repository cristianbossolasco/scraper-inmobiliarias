# Modo Recorrido: estado de desarrollo

## Bloque implementado

La primera base del Modo Recorrido ya incluye:

- proceso Django móvil aislado mediante `config.mobile_settings` y `config.mobile_urls`;
- login obligatorio y usuario sin privilegios;
- inexistencia de rutas administrativas en el host móvil;
- API compacta de propiedades cercanas con RTree y Haversine;
- exclusión de propiedades ocultas, inactivas, sin precio válido o con ubicación imprecisa;
- agrupación de coordenadas repetidas;
- endpoint limitado para favoritos;
- pantalla móvil con MapLibre, GPS en primer plano, filtros inmobiliarios y marcadores de precio;
- Waitress y WhiteNoise para ejecutar con `DEBUG=False`;
- pruebas de seguridad, filtros espaciales y preservación de datos manuales.

Todavía no se habilitó el túnel. Ya existe un usuario móvil local sin privilegios.
El segundo bloque incorporó la ficha con foto y dirección, proximidad automática,
traza local, recuperación y resumen completo. El tercero agregó filtros avanzados,
enlaces al aviso original y optimizaciones medibles. Siguen pendientes la validación
GPS en teléfono y campo, audio, manifest/service worker y la instalación PWA.

## Probar solamente en la PC

Instalar dependencias:

```powershell
python -m pip install -r requirements.txt
```

Crear el usuario móvil de forma interactiva:

```powershell
python manage.py create_mobile_user --username radar-mobile
```

La contraseña no se pasa como argumento ni queda en el historial de PowerShell. El comando crea un usuario activo que no es staff ni superusuario.

Recolectar archivos estáticos:

```powershell
$env:DJANGO_MOBILE_STRICT = "0"
python manage.py collectstatic --noinput --settings=config.mobile_settings
```

Iniciar el host móvil local:

```powershell
$env:DJANGO_MOBILE_STRICT = "0"
python -m waitress --listen=127.0.0.1:8002 config.mobile_wsgi:application
```

Abrir:

```text
http://127.0.0.1:8002/accounts/login/
```

El modo no estricto se permite únicamente en loopback para desarrollo. No debe usarse detrás de un túnel.

## Condiciones para el futuro túnel

El proceso público deberá iniciarse con:

- `DJANGO_MOBILE_STRICT=1`;
- `DJANGO_SECRET_KEY` aleatoria, larga y fuera del repositorio;
- `DJANGO_MOBILE_HOST` igual al hostname exacto entregado por el túnel;
- archivos estáticos recolectados;
- Waitress ligado exclusivamente a `127.0.0.1:8002`.

El túnel nunca debe apuntar al puerto 8000 del Radar completo.

## Backup inicial

Antes de comenzar este bloque se generó un backup online de SQLite y se verificó con `PRAGMA quick_check`. Los backups quedan fuera del repositorio, en:

```text
C:\Users\corebi\Documents\Scraper Inmobiliarias Backups
```

No se debe copiar directamente `db.sqlite3` mientras haya procesos escribiendo.

## Validación disponible

```powershell
python manage.py test properties.test_drive_mode
python manage.py test
node --check static\js\drive-mode.js
python manage.py makemigrations --check --dry-run
python manage.py check
```

Para comprobar la configuración endurecida se deben definir primero las tres variables móviles y ejecutar:

```powershell
python manage.py check --deploy --settings=config.mobile_settings
```

La advertencia de HSTS se mantiene intencionalmente hasta contar con un hostname estable. No debe habilitarse HSTS sobre un Quick Tunnel descartable.

## Segundo bloque implementado localmente

El desarrollo y sus mockups se documentan en
[`plan_modo_recorrido_bloque_2.md`](plan_modo_recorrido_bloque_2.md). Ya están
implementados:

- ficha bajo demanda con foto HTTPS de un aviso activo, dirección y etiquetas
  independientes de confiabilidad de dirección y coordenada;
- resolución de imágenes relativas y fallback cuando una imagen falta o falla;
- tarjeta de reconocimiento y navegación entre publicaciones agrupadas;
- proximidad automática a 120 m con precisión, antigüedad, rumbo, histéresis y
  deduplicación;
- filtros configurados antes de iniciar y controles compactos durante la marcha;
- traza local discreta, filtrado de jitter/saltos y persistencia temporal con
  recuperación después de recargar;
- resumen completo con duración, distancia aproximada, ubicaciones encontradas
  y favoritas de la sesión;
- MapLibre servido localmente, CSP móvil y ausencia de JavaScript remoto con
  acceso a la traza;
- pruebas Django del host/API y pruebas `node:test` de los cálculos GPS.

Validación local del 22 de agosto de 2026:

- 13 pruebas focalizadas del modo recorrido aprobadas;
- 5 pruebas JavaScript de geografía, proximidad y traza aprobadas;
- suite Django completa: 298 pruebas aprobadas;
- `collectstatic`, `check`, control de migraciones y revisión visual sin errores;
- API de cercanas sobre la base real: cinco mediciones entre 49 y 116 ms, con
  payload de aproximadamente 50 KB en la muestra utilizada.

Antes de considerar cerrado el bloque faltan la validación GPS en un teléfono,
una caminata corta y la prueba en auto con acompañante.

Navegación giro a giro, audio, instalación PWA y túnel siguen fuera de este
bloque y se abordarán después de validar esta experiencia.

## Tercer bloque implementado localmente

El siguiente bloque se documenta en
[`plan_modo_recorrido_bloque_3.md`](plan_modo_recorrido_bloque_3.md) e incluye:

- tipos múltiples, dormitorios mínimos, rango de precio USD, superficie
  cubierta mínima y lote mínimo;
- acceso seguro a la publicación original desde la ficha y el resumen;
- compresión de las respuestas JSON, proyección ORM más liviana y métricas
  `Server-Timing`;
- tarjeta utilizable sin esperar la descarga de la foto externa;
- benchmark repetible de queries, payload y latencia.

También se migran automáticamente las sesiones locales v1 al formato v2 y los
filtros aplicados quedan visibles durante el recorrido y en el resumen.

Validación local del 22 de agosto de 2026:

- 16 pruebas focalizadas del modo recorrido aprobadas;
- 7 pruebas JavaScript aprobadas;
- suite Django completa: 301 pruebas aprobadas;
- `collectstatic`, `check`, control de migraciones, sintaxis y revisión responsive
  en 390 × 844 sin errores;
- compresión HTTP verificada (`Content-Encoding: gzip`);
- casas a 350 m: p50 5,4 ms, p95 7,3 ms, 2 queries y 35,3 KB de JSON
  (2,8 KB comprimido), frente a 19,1/33,9 ms antes del bloque;
- radio de 800 m sin filtro: p50 28,1 ms, p95 52 ms, 2 queries y 82,3 KB
  de JSON (6,3 KB comprimido), frente a 88,4/122,8 ms antes del bloque.

El comando de medición es de solo lectura:

```powershell
python manage.py benchmark_drive_mode --latitude=-34.59 --longitude=-58.64 --radius=350 --iterations=10 --property-type=house
```

Las fotografías externas siguen fuera del SLA del API: la ficha ya es utilizable
sin esperar su descarga y conserva un fallback visual cuando la fuente falla.

## Cuarto bloque: seguimiento estable e historial

Implementado el 8 de septiembre de 2026. No agrega navegación giro a giro.

### Conducción y controles

- Al detenerse, se conserva la última orientación fiable. Las muestras GPS con
  mala precisión, antiguas o con saltos inverosímiles no mueven la cámara.
- El rumbo se suaviza por el arco más corto; la velocidad y el desplazamiento
  corroboran un cambio para reducir la oscilación estando quieto.
- Acercar o alejar con dos dedos mantiene el seguimiento y el zoom elegido.
  Arrastrar deliberadamente el mapa activa «Mapa libre».
- «Centrar auto» / «Seguir auto» queda visible arriba a la izquierda en vertical,
  separado del soporte inferior derecho. En horizontal se ubica a la izquierda
  y los paneles a la derecha.

### Guardado y consulta

1. Finalizar el recorrido: se conserva una copia local y se intenta guardar en
   SQLite, en la PC. El resumen informa «Guardado en tu PC» o guardado pendiente.
2. Abrir «Mis recorridos» desde el inicio o el resumen para consultar fechas,
   distancia aproximada, duración, filtros, traza y propiedades de la sesión.
3. Si falla la conexión, el recorrido queda en una cola persistente del navegador.
   Se reintenta al recuperar conexión, abrir la aplicación o pulsar «Reintentar».
   La misma sesión no se duplica si se perdió la confirmación del servidor.
4. «Borrar copia de este teléfono» elimina solo la copia local del resumen;
   «Eliminar del historial» borra los datos del recorrido en la PC, con confirmación.
   Se conserva una marca mínima de eliminación para impedir su recreación por un
   reintento atrasado, sin traza, fichas, filtros, fechas de viaje ni métricas.

El historial requiere autenticación y cada usuario ve únicamente sus recorridos.
La cola del teléfono también está separada por usuario. No se crean históricos
retroactivos a partir de datos inexistentes: una sesión anterior solo puede
importarse si todavía está disponible en el almacenamiento de ese navegador.

**Importante con los túneles temporales:** el almacenamiento del teléfono
pertenece al navegador y a la dirección web concreta. Un nuevo enlace de túnel
no puede leer pendientes de otro enlace. Finalizar y confirmar el guardado en la
PC antes de cambiar de URL, borrar datos del navegador o cerrar sesión. Los
recorridos ya guardados en SQLite sí se pueden consultar desde otro enlace o
dispositivo con el mismo usuario. Esto no es una PWA offline ni garantiza GPS
con el navegador en segundo plano o la pantalla bloqueada.

### Implementación y límites

- Tabla `DriveHistory`, migración aditiva `0025_drivehistory`; no modifica
  propiedades ni correcciones manuales.
- API móvil `/api/recorrido/historial/` y detalle por UUID; autenticación,
  CSRF, aislamiento de usuario, paginación y respuestas sin caché.
- Listados livianos; la traza completa se carga únicamente al abrir un detalle.
- Validación en servidor, recomputación de distancia, hasta 1500 puntos,
  500 ubicaciones, 1000 fichas y 2 MiB por sesión. Las distancias son estimadas.
- Las imágenes y publicaciones se conservan como enlaces: pueden dejar de
  funcionar si el anunciante las retira.

### Comprobaciones reproducibles

```powershell
python manage.py test properties.test_drive_mode properties.test_drive_history --noinput
python manage.py makemigrations --check --dry-run
node --test static/js/drive-utils.test.js static/js/drive-navigation.test.js static/js/drive-history.test.js
node --check static/js/drive-mode.js
node --check static/js/drive-history.js
node --check static/js/drive-navigation.js
node scripts/test_drive_browser.cjs
```

La prueba de navegador usa Chrome y una base SQLite desechable, separada de la
base real. Comprueba GPS simulado, gestos táctiles de zoom/arrastre, seguimiento,
resumen, historial y reintento de guardado después de recargar, en vertical y
horizontal. Sus capturas quedan en `tmp/drive-block4/`. Usa Playwright del runtime
de Codex o la ruta indicada en `PLAYWRIGHT_MODULE`; el lanzador de prueba no debe
exponerse mediante un túnel.

La migración se aplicó después de crear el backup online
`before-drive-history-20260908-202958.sqlite3` en la carpeta de backups externa.
SQLite pasó `PRAGMA quick_check` antes y después, conservando las 6129 propiedades.
Pasaron 26 pruebas Django y 25 JavaScript, la comprobación de migraciones,
`collectstatic` y `check`, y ocho escenarios de navegador sin errores JavaScript.
Las comprobaciones geométricas incluyen la traza fuera del resumen en ambas
orientaciones y el caso de un recorrido con un único punto.
No se ejecutó nuevamente la suite completa de scrapers,
porque este bloque no modifica scraping, ingesta ni correcciones de propiedades.

La validación GPS final debe hacerse en el teléfono, primero detenido y luego
con acompañante: las pruebas simuladas no reproducen todos los sensores ni
las restricciones de energía de cada dispositivo.

## Quinto bloque: descubrimientos y cuaderno de campo

Implementado el 17 de septiembre de 2026.

### Uso

1. Antes de iniciar, elegir radio: 200, 350, 500, 800, 1.000 o 1.500 m.
   Los avisos de voz están apagados inicialmente; se activan con la casilla
   «Avisos de voz breves» y se pueden silenciar durante la salida.
2. Cada propiedad devuelta por el radar se incorpora a la salida desde la primera
   consulta. Alejarse no elimina el descubrimiento. Coral indica cercanía actual,
   gris un descubrimiento fuera del radio y violeta una favorita de la salida.
   El contador separa cercanas de descubiertas, sin confundir propiedades con
   ubicaciones compartidas. «Pasaste cerca» sigue usando 120 m y no afirma que se
   haya visto la fachada ni visitado el inmueble.
3. Estando detenido, «Nota / cartel» registra el punto GPS actual y su precisión.
   Desde una ficha, «Observación / pendiente» utiliza la ubicación publicada de
   esa propiedad, expresamente diferenciada de una ubicación confirmada en calle.
   Se pueden guardar observaciones rápidas, texto, una foto y un próximo paso:
   revisar, volver, volver de día o consultar al anunciante.
4. «Encontré un cartel» crea un registro independiente, aunque la casa no exista
   en la base. En el cuaderno se pueden consultar hasta diez avisos elegibles
   cercanos a 200 m del punto, abrir sus publicaciones y compararlos. No se
   establece una coincidencia automática ni se modifica la propiedad canónica.
5. «Dictar nota», cuando el navegador lo ofrece, incorpora texto al formulario.
   Identifica la casa o punto destinatario y permite revisar antes de guardar.
   Usa el servicio de reconocimiento del navegador, que puede requerir conexión.
   No hay escucha permanente ni navegación por comandos de voz.
6. Al terminar, el resumen muestra favoritas, notas, carteles y pendientes.
   Los descubrimientos aparecen en lista y mapa, con filtros por todas, nuevas,
   pasé cerca, favoritas y pendientes. Las listas cargan 20 fichas por lote y las
   fotos se descargan al acercarse a la parte visible de la lista.
7. «Cuaderno y pendientes» permite revisar otras salidas y marcar tareas resueltas.
   «Ver lugar en el mapa» centra el punto; «Volver al cuaderno» recupera la consulta.
   Eliminar un recorrido elimina también sus notas y fotos, conservando favoritas.

### Conservación y compatibilidad

- Las sesiones nuevas usan versión 3; las sesiones anteriores mantienen su
  versión y se informa que solo registraban encuentros a 120 m. Se dibujan las
  propiedades antiguas que tienen coordenadas guardadas. No se reconstruyen
  descubrimientos que nunca se registraron.
- El precio y las coordenadas se capturan al descubrir la casa. Al guardar el
  recorrido, el servidor completa los campos de ficha que faltan sin reemplazar
  el precio ni el punto capturados. Dirección, enlace y foto completados en ese
  momento reflejan la base al guardar, no garantizan su estado al pasar.
- Se recuerdan IDs de todos los recorridos guardados del usuario y pendientes
  del teléfono. Las trazas superpuestas corresponden a las diez últimas salidas.
  Si no se puede consultar esa memoria, las casas se indican sin comparar: no se
  clasifican falsamente como nuevas. Los avisos se limitan a 120 m, con al menos
  30 segundos entre mensajes; no se repiten en la sesión ni anuncian como nuevas
  las casas ya conocidas. Grupos sospechosos de coordenadas no disparan audio.
- La consulta conserva el máximo de 250 resultados y ahora avisa visiblemente
  cuando quedan más. Cada salida admite 3.000 propiedades/ubicaciones, 1.500
  puntos de traza y hasta 6 MiB de entrada. Al llegar al límite de descubrimientos,
  se pide finalizar e iniciar otra salida; no se descartan los primeros hallazgos.
- Notas y fotos pendientes se conservan por usuario en el almacenamiento del
  navegador y se reintentan al recuperar conexión, abrir la app o actualizar el
  cuaderno. Si no hay espacio, el formulario permanece abierto con el contenido.
  Al cambiar el enlace temporal no se traslada el almacenamiento local: confirmar
  sincronización antes de cambiar de URL. Las notas ya guardadas se leen desde la PC.
- La migración aditiva `0026_drivefieldnote` agrega observaciones privadas con
  revisión y clave de reintento. Las fotos se comprimen, validan y recodifican como
  JPEG sin metadatos; se guardan en SQLite y solo se sirven a su propietario.
  Los conflictos conservan la versión pendiente para revisarla, sin pisar una
  edición de otro dispositivo. Las eliminaciones dejan una marca mínima que
  impide recrear una nota por un reintento atrasado.
- Las notas no modifican direcciones, pines, correcciones manuales ni notas
  canónicas de las propiedades. El historial de recorridos sigue siendo inmutable.

### Verificación realizada

- 34 pruebas Django de recorrido, historial y cuaderno.
- 38 pruebas JavaScript de GPS, filtros, guardado, descubrimientos y cuaderno.
- 11 escenarios de navegador con SQLite desechable: casa detectada a 250 m,
  salida del radio, persistencia en mapa e historial, filtro de cercanía,
  cartel con foto sin conexión, reintento, pendiente resuelto, memoria entre
  salidas y audio simulado sin repeticiones. También GPS, gestos, encuadre en
  vertical/horizontal y recuperación del recorrido después de recargar.
- `check`, validación de migraciones, sintaxis JS, `git diff --check` y
  `collectstatic` del host móvil. No se repitió la suite general de scrapers.
- Benchmark de 1.500 m para casas, diez muestras sobre la base real: p50 38,1 ms,
  p95 65,7 ms, dos queries, 250 resultados, 82,3 KB de JSON / 6,3 KB comprimido.
- Backup online `before-drive-discovery-20260917-083332.sqlite3` en la carpeta
  externa de backups. `quick_check` correcto; comparación SHA-256 antes/después
  confirmó iguales las 6.139 propiedades, 5.964 ubicaciones, 8.004 publicaciones
  y seis recorridos existentes. Migración aplicada y estáticos regenerados.

```powershell
python manage.py test properties.test_drive_mode properties.test_drive_history properties.test_drive_notebook --noinput
node --test static/js/drive-utils.test.js static/js/drive-navigation.test.js static/js/drive-history.test.js static/js/drive-discovery.test.js static/js/drive-notebook.test.js
node scripts/test_drive_browser.cjs
```

Pendiente de validación física: salida con teléfono real, calidad de voz/GPS,
micrófono y cámara. Los controles de voz se detectan por disponibilidad, sin
prometer soporte uniforme: [Web Speech API, MDN](https://developer.mozilla.org/en-US/docs/Web/API/Web_Speech_API).
El enfoque de una ficha usa desplazamiento de cámara sin retener márgenes; el
resumen limpia márgenes antes de encuadrar, según la API de
[MapLibre](https://maplibre.org/maplibre-gl-js/docs/API/type-aliases/EaseToOptions/).
