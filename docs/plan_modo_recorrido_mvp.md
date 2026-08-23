# Plan de desarrollo: Modo Recorrido MVP

Fecha: 22 de agosto de 2026  
Estado: listo para comenzar desarrollo  
Decisión de infraestructura: Django y SQLite permanecen en la PC; el celular accede por HTTPS mediante Cloudflare Tunnel.

## 1. Objetivo

Construir una PWA móvil, instalable desde Chrome en Android, que permita iniciar un recorrido, seguir la posición del teléfono, ver propiedades en venta cercanas con su precio, guardar favoritas y consultar un resumen al finalizar.

El MVP debe funcionar sin copiar la base al celular, sin publicar el Radar administrativo y sin construir todavía una aplicación Android nativa.

## 2. Decisiones cerradas

- `db.sqlite3` seguirá siendo la única base canónica.
- Los scrapers, la edición, las exportaciones y las operaciones seguirán disponibles solo en la PC.
- El celular usará una interfaz separada en `/recorrido/`.
- El acceso remoto se hará por un proceso Django móvil separado, ligado a `127.0.0.1:8002`.
- Cloudflare Tunnel apuntará exclusivamente al proceso móvil.
- La primera validación remota usará un Quick Tunnel supervisado.
- La instalación estable como PWA requerirá después un hostname estable mediante un túnel nombrado.
- El mapa seguirá usando MapLibre. No se integrará dentro de la aplicación Google Maps.
- La línea del resumen será el trayecto GPS registrado, no navegación giro a giro.
- No se crearán modelos de recorridos en el MVP. La sesión, traza y encuentros se conservarán temporalmente en el teléfono.
- El único cambio permitido desde la interfaz móvil será marcar o desmarcar una propiedad como favorita mediante un endpoint específico.
- El audio será opcional y estará apagado por defecto.

## 3. Evidencia de la auditoría actual

La inspección de la base y el código encontró:

- 4.364 propiedades activas, visibles y en venta.
- 4.342 con alguna ubicación.
- 4.164 con precisión `exact` o `manual`.
- Aproximadamente 4.112 con precio considerado válido por las reglas de calidad.
- 2.844 casas activas con ubicación `exact` o `manual`.
- La base SQLite ocupa aproximadamente 204 MB.
- El RTree espacial está sincronizado y permite prefiltrar por radio.
- La API GeoJSON general puede devolver cientos de propiedades y más de 400 KB para un radio urbano pequeño.
- Una consulta dedicada, con campos mínimos, puede resolver los candidatos en decenas de milisegundos.
- Existen numerosos grupos de coordenadas repetidas; entre las propiedades elegibles hay 110 coordenadas compartidas por cinco o más publicaciones, que reúnen 774 propiedades.
- Actualmente no hay usuarios creados ni autenticación aplicada a las vistas del Radar.
- El plan de energía observado suspende la PC, lo que interrumpiría servidor y túnel.

Consecuencia: no se reutilizará directamente `/api/propiedades/`, no se publicará el URLconf administrativo y no se presentará toda coordenada publicada como una fachada confirmada.

## 4. Criterio de éxito del MVP

Desde un teléfono Android usando datos móviles se debe poder:

1. Abrir una URL HTTPS estable o temporal.
2. Iniciar sesión con un usuario sin privilegios administrativos.
3. Conceder ubicación precisa al tocar “Iniciar recorrido”.
4. Ver el mapa centrado y orientado según el movimiento.
5. Ver precios de propiedades cercanas sin saturar la pantalla.
6. Recibir una tarjeta cuando una propiedad o grupo esté cerca.
7. Marcar una propiedad como favorita.
8. Finalizar y ver duración, distancia, traza y propiedades encontradas.
9. Instalar la interfaz como PWA una vez disponible un hostname estable.
10. Recibir estados claros si falla GPS, Internet, servidor o túnel.

No debe existir acceso remoto a administración, scrapers, exportaciones, notas, edición, geocodificación ni operaciones.

## 5. Arquitectura

```text
PC
├── SQLite: db.sqlite3 (única base)
├── Radar completo local
│   └── 127.0.0.1:8000
│       ├── búsqueda y estadísticas
│       ├── edición y curación
│       ├── scraping y operaciones
│       └── exportaciones
│
└── Radar móvil restringido
    └── 127.0.0.1:8002
        ├── login/logout
        ├── /recorrido/
        ├── API de propiedades cercanas
        ├── favorito acotado
        ├── manifest/service worker/iconos
        └── health check sin datos
              ↑
       Cloudflare Tunnel HTTPS
              ↑
        Chrome/PWA en celular
```

El servidor móvil tendrá un `ROOT_URLCONF` propio. Las rutas desconocidas y todas las rutas administrativas responderán `404`, aunque un enlace sea adivinado.

## 6. Alcance funcional

### Incluido

- Pantalla previa con explicación, conexión y botón de inicio.
- Permiso de geolocalización solicitado a partir de una acción del usuario.
- Seguimiento GPS en primer plano.
- Posición, círculo de precisión y rumbo.
- Mapa de pantalla completa con etiquetas de precio.
- Agrupación de coordenadas coincidentes y control de colisiones visuales.
- Radio configurable inicialmente entre 200 y 1.000 metros; valor predeterminado: 350 metros.
- Filtros básicos: tipo y rango de precio.
- Tarjeta de proximidad con precio, tipo, distancia, dormitorios y superficie disponible.
- Favorito mediante un endpoint móvil estrecho.
- Alertas de voz opt-in mediante `speechSynthesis`.
- Wake Lock cuando el navegador lo permita.
- Resumen de la sesión actual.
- Estados de error y recuperación.
- Manifest, iconos, modo `standalone` y service worker mínimo.
- Login y configuración de exposición segura.
- Scripts guiados para iniciar, comprobar y detener el servicio móvil.

### Fuera del MVP

- Google Maps embebido o alterado.
- Navegación giro a giro.
- Android Auto.
- APK, Play Store o aplicación Android nativa.
- Funcionamiento continuo con la pantalla bloqueada.
- Geofencing del sistema operativo.
- Mapas o propiedades completos sin conexión.
- Comandos de voz.
- Filtro “solo propiedades adelante”.
- Historial permanente de recorridos en el servidor.
- Sincronización de una copia de SQLite con el celular.
- Edición de propiedades, notas, ocultamiento o corrección de coordenadas desde el teléfono.

## 7. Seguridad y separación del Radar local

### Proceso móvil

Se crearán configuraciones y WSGI específicos para el puerto móvil:

- `DEBUG=False` obligatorio.
- `DJANGO_SECRET_KEY` obligatorio, sin fallback de desarrollo.
- `ALLOWED_HOSTS` limitado al host exacto del túnel y loopback.
- `CSRF_TRUSTED_ORIGINS` limitado al origen HTTPS exacto.
- `SECURE_PROXY_SSL_HEADER` para interpretar correctamente HTTPS detrás de Cloudflare.
- Cookies de sesión y CSRF con `Secure`; sesión HTTP-only y `SameSite=Lax`.
- Respuestas móviles con `Cache-Control: private, no-store` cuando contengan datos o autenticación.
- Servidor Waitress ligado únicamente a `127.0.0.1:8002`.
- Archivos estáticos servidos con WhiteNoise cuando `DEBUG=False`.
- Ningún puerto entrante abierto en el router o firewall.

Se creará un usuario dedicado:

- activo;
- no staff;
- no superusuario;
- contraseña larga y exclusiva;
- sesión suficientemente larga para iniciar sesión antes de salir.

### Rutas permitidas por el host móvil

- `/`
- `/recorrido/`
- `/accounts/login/`
- `/accounts/logout/`
- `/api/recorrido/cercanas/`
- `/api/recorrido/propiedad/<id>/favorito/`
- `/manifest.webmanifest`
- `/service-worker.js`
- `/sin-conexion/`
- `/salud/`
- `/static/...`

Todo lo demás, incluyendo `/admin/`, `/scraping/`, `/export/`, `/estadisticas/`, `/api/propiedades/` y las APIs operativas o de edición, debe responder `404` en el proceso móvil.

### Coordenadas personales

La ubicación del teléfono no se guardará en el servidor. La consulta cercana usará `POST` con JSON y CSRF, aunque conceptualmente sea una lectura, para evitar que latitud y longitud aparezcan en URL, historial y logs de acceso.

## 8. Calidad y elegibilidad de propiedades

### Candidatos del mapa

La API móvil incluirá solamente propiedades que cumplan todas estas condiciones:

- `operation = sale`;
- `status = active`;
- `is_hidden = false`;
- precio válido y mayor que cero;
- al menos una publicación activa;
- `PropertyLocation` existente;
- `outside_target = false`;
- precisión `exact` o `manual`;
- confianza media/alta o pin manual.

Nunca se mostrarán como propiedad puntual ubicaciones de tipo calle, intersección o barrio.

### Niveles mostrados al usuario

- `confirmed`: pin manual; puede mostrarse como “Ubicación confirmada”.
- `address`: dirección numerada geocodificada con evidencia suficiente; “Ubicación por dirección”.
- `published`: coordenada aportada por el aviso o su mapa; “Ubicación publicada; puede ser aproximada”.

La palabra “exacta” no se usará para prometer que el pin identifica la fachada, salvo confirmación manual real.

### Coordenadas repetidas

- Dos o más propiedades en el mismo punto se agruparán.
- El marcador indicará cantidad y precio mínimo, por ejemplo: “5 propiedades · desde USD 98k”.
- Una coordenada compartida por muchas publicaciones no generará anuncios individuales.
- Los grupos sospechosos no podrán disparar el mensaje “esta casa”; se anunciarán como propiedades publicadas en esa zona.
- La lógica quedará aislada en un servicio y tendrá pruebas con coordenadas coincidentes.

## 9. Contrato de la API móvil

### Propiedades cercanas

```http
POST /api/recorrido/cercanas/
Content-Type: application/json
X-CSRFToken: ...

{
  "latitude": -34.589,
  "longitude": -58.641,
  "radius_m": 350,
  "property_types": ["house"],
  "price_min": 80000,
  "price_max": 180000
}
```

Validaciones:

- latitud y longitud obligatorias y dentro de rango;
- radio predeterminado 350 m;
- radio mínimo 200 m y máximo 1.000 m;
- tipos limitados a los valores conocidos;
- precios no negativos y rango coherente;
- máximo 250 resultados;
- respuesta ordenada por distancia;
- indicador `truncated` si se alcanza el límite.

Respuesta compacta orientativa:

```json
{
  "center": {"latitude": -34.589, "longitude": -58.641},
  "radius_m": 350,
  "generated_at": "2026-08-22T14:00:00Z",
  "count": 2,
  "truncated": false,
  "properties": [
    {
      "id": 123,
      "latitude": -34.5887,
      "longitude": -58.6408,
      "distance_m": 82,
      "currency": "USD",
      "price": 142000,
      "price_short": "USD 142k",
      "type": "house",
      "type_label": "Casa",
      "bedrooms": 3,
      "bathrooms": 2,
      "area_m2": 180,
      "location_reliability": "published",
      "is_favorite": false,
      "group_count": 1
    }
  ]
}
```

La consulta se implementará así:

1. Caja envolvente calculada desde el radio.
2. IDs candidatos mediante el RTree existente.
3. Query selectiva con `.values(...)` y verificación de publicación activa.
4. Haversine para la distancia real.
5. Agrupación por coordenada, orden y límite.

Objetivos locales para 350 m:

- respuesta menor a 250 ms en condiciones normales;
- payload menor a 150 KB;
- no más de dos consultas principales por actualización.

### Favorito

```http
POST /api/recorrido/propiedad/123/favorito/
Content-Type: application/json
X-CSRFToken: ...

{"is_favorite": true}
```

Este endpoint solo podrá modificar `is_favorite`. Debe demostrar mediante pruebas que no altera oculto, revisado, notas, `manual_overrides`, `data_manually_corrected_at` ni el pin manual.

## 10. Comportamiento del cliente

### Estados principales

- `idle`: esperando inicio.
- `requesting_permission`: solicitando ubicación.
- `acquiring_location`: buscando una posición utilizable.
- `tracking`: recorrido activo.
- `low_accuracy`: GPS insuficiente, sin afirmar proximidad precisa.
- `offline`: se mantiene el último mapa y se avisa que los datos están desactualizados.
- `permission_denied`: instrucciones para habilitar ubicación.
- `server_unavailable`: PC, Django o túnel sin respuesta.
- `ended`: resumen de sesión.

### Geolocalización

Se usará `navigator.geolocation.watchPosition` con valores iniciales:

```js
{
  enableHighAccuracy: true,
  maximumAge: 3000,
  timeout: 12000
}
```

Reglas:

- El marcador del usuario se actualiza con cada posición aceptada.
- La API se consulta con la primera posición utilizable.
- Se vuelve a consultar después de moverse aproximadamente 60 metros y al menos 5 segundos desde la consulta anterior.
- Se permite una consulta de respaldo después de 15 segundos si hubo movimiento relevante.
- No se consulta repetidamente mientras el usuario permanece detenido.
- `AbortController` cancela respuestas viejas.
- Si una respuesta falla, los marcadores anteriores permanecen atenuados con hora de actualización.
- Se mostrará la precisión GPS y no se lanzarán alertas de proximidad cuando el error sea demasiado grande.

### Seguimiento y rumbo

- El mapa comienza centrado, con zoom cercano y ligera inclinación.
- Se usa `coords.heading` cuando esté disponible.
- Si falta, se calcula el rumbo entre posiciones suficientemente separadas.
- Si el usuario desplaza el mapa manualmente, el seguimiento se pausa.
- Un botón grande vuelve a centrar y reanuda el seguimiento.

### Alertas

- Umbral inicial: 100–120 metros.
- Una propiedad o grupo se anuncia una sola vez por sesión.
- Solo habrá una tarjeta activa.
- Coordenadas coincidentes se anuncian como grupo.
- El audio estará apagado inicialmente.
- Al activarlo, se usará voz `es-AR` si existe y mensajes breves.
- Se impondrá una pausa mínima entre anuncios y no se acumulará una cola.

Ejemplo: “Casa, ciento cuarenta y dos mil dólares, a cien metros”.

### Sesión local y resumen

Se almacenarán temporalmente en el navegador:

- hora de inicio y fin;
- puntos GPS reducidos o simplificados;
- distancia estimada;
- IDs encontrados;
- IDs ya anunciados;
- favoritos de la sesión;
- filtros.

Se usará almacenamiento de sesión/local controlado para sobrevivir a una recarga accidental, pero sin enviar la traza al servidor. Al finalizar se podrá borrar la ubicación registrada desde la propia pantalla.

## 11. PWA

El manifest tendrá como mínimo:

- `name`: “Radar Inmobiliario”;
- `short_name`: “Radar”;
- `id`: `/recorrido/`;
- `start_url`: `/recorrido/`;
- `display`: `standalone`;
- colores aprobados;
- iconos de 192 y 512 px;
- icono `maskable`.

El service worker se servirá desde `/service-worker.js` para controlar el alcance raíz.

Solo se cachearán:

- CSS y JavaScript propios;
- iconos;
- manifest;
- shell estático mínimo;
- pantalla “Sin conexión”.

No se cachearán:

- `/api/`;
- login/logout;
- HTML autenticado con datos;
- coordenadas o propiedades;
- teselas de OpenStreetMap;
- imágenes inmobiliarias externas.

La PWA será instalable, pero no será offline: sin Internet o sin túnel no habrá datos actuales ni mapa completo. Geolocalización, service workers y Wake Lock requieren un contexto seguro HTTPS. Véanse [Geolocation `watchPosition`](https://developer.mozilla.org/en-US/docs/Web/API/Geolocation/watchPosition), [instalación de PWA](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Guides/Making_PWAs_installable), [Service Worker](https://developer.mozilla.org/en-US/docs/Web/API/Service_Worker_API) y [Screen Wake Lock](https://developer.mozilla.org/en-US/docs/Web/API/Screen_Wake_Lock_API).

## 12. Diseño de archivos previsto

### Nuevos

- `config/mobile_settings.py`
- `config/mobile_urls.py`
- `config/mobile_wsgi.py`
- `properties/drive_views.py`
- `properties/services/drive_mode.py`
- `properties/mobile_auth.py` o middleware equivalente
- `templates/properties/drive.html`
- `templates/registration/login.html`
- `templates/properties/offline.html`
- `static/css/drive-mode.css`
- `static/js/drive-utils.js`
- `static/js/drive-mode.js`
- `static/pwa/manifest.webmanifest`
- `static/pwa/icons/...`
- `static/pwa/service-worker.js`
- `scripts/Start-RadarMovil.ps1`
- `scripts/Test-RadarMovil.ps1`
- `scripts/Stop-RadarMovil.ps1`
- `scripts/Backup-RadarSqlite.ps1` o comando Django equivalente
- documentación de instalación y operación móvil

### A modificar

- `requirements.txt` para Waitress/WhiteNoise si se confirma esta estrategia.
- `properties/tests.py` o módulos de prueba nuevos si se decide dividir la suite.
- `.gitignore` solo si aparecen artefactos operativos nuevos.
- `README.md` con enlace a la guía móvil.

No se espera una migración de modelos para este MVP.

## 13. Plan de implementación

### Etapa 0 — Backup y puerta de calidad

Entregables:

- Backup consistente usando la API online de SQLite o con escritores detenidos.
- `PRAGMA quick_check` sobre la copia.
- Restauración de prueba en una ruta temporal.
- Servicio documentado que clasifique ubicaciones para conducción.
- Fixture/pruebas para coordenadas confirmadas, publicadas, imprecisas y repetidas.

Puerta de salida:

- Se puede restaurar la base.
- Está definido qué pin puede mostrarse y cuál puede generar alerta.
- No se modificó ninguna corrección manual.

### Etapa 1 — Host móvil seguro

Entregables:

- `mobile_settings`, `mobile_urls` y `mobile_wsgi`.
- Login/logout y usuario dedicado.
- Waitress/WhiteNoise en loopback.
- Cabeceras y cookies seguras.
- Rutas administrativas inexistentes en el host móvil.
- Health check mínimo.

Puerta de salida:

- Anónimo no recibe datos.
- Autenticado entra a la pantalla vacía de recorrido.
- `/admin/`, `/scraping/`, `/export/` y APIs administrativas responden `404` en el puerto 8002.
- `manage.py check --deploy` no presenta advertencias críticas para la configuración móvil.

### Etapa 2 — API cercana

Entregables:

- Servicio espacial compacto.
- Validación de parámetros.
- Agrupación de coordenadas repetidas.
- Endpoint de propiedades cercanas.
- Endpoint acotado de favorito.
- `no-store` y autenticación/CSRF.

Puerta de salida:

- Filtros y distancias correctos.
- Payload y latencia dentro de objetivos.
- Ningún campo interno o personal en la respuesta.
- Tests de preservación de correcciones manuales.

### Etapa 3 — Interfaz de recorrido

Entregables:

- Layout fiel a los mockups.
- Pantalla preinicio.
- Mapa de pantalla completa.
- Marcadores de precio y clusters/grupos.
- Controles de filtros, centrado y finalización.
- Adaptación a `100dvh`, safe areas, retrato y paisaje.

Puerta de salida:

- La pantalla funciona con datos simulados y reales en emulación móvil.
- Los controles táctiles miden al menos 44–48 px.
- La atribución del mapa permanece visible.

### Etapa 4 — Motor de movimiento

Entregables:

- `watchPosition` y estados de permiso/GPS.
- Seguimiento, rumbo y precisión.
- Throttling por tiempo y distancia.
- Cancelación de requests obsoletos.
- Proximidad, deduplicación y tarjeta inferior.
- Wake Lock con recuperación al volver a primer plano.
- Audio opt-in.

Puerta de salida:

- No se generan consultas continuas estando detenido.
- La pantalla se recupera de pérdida de red y visibilidad.
- Una propiedad o grupo no se anuncia repetidamente.

### Etapa 5 — Resumen y PWA

Entregables:

- Registro local simplificado de la traza.
- Cálculo de duración y distancia.
- Resumen con propiedades únicas y favoritas.
- Manifest, iconos, service worker y offline shell.
- Instrucciones de instalación Android.

Puerta de salida:

- La PWA se instala y abre en modo standalone.
- Ninguna API ni dato sensible aparece en Cache Storage.
- Finalizar libera GPS, Wake Lock y audio.

### Etapa 6 — Quick Tunnel supervisado

Entregables:

- Scripts Start/Test/Stop.
- Host y origen exactos obtenidos por ejecución.
- Verificación pública anónima y autenticada.
- Backup previo.
- Checklist de energía de Windows.

Puerta de salida:

- Con Wi-Fi apagado, el teléfono accede mediante datos móviles.
- El anónimo solo ve login y nunca datos.
- Detener el proceso/túnel corta el acceso.
- El Radar del puerto 8000 no es alcanzable desde el hostname móvil.

### Etapa 7 — Prueba de campo

Orden obligatorio:

1. Escritorio con ubicación simulada.
2. Teléfono quieto.
3. Caminata corta.
4. Auto con acompañante operando y observando.
5. Revisión de errores, batería, latencia y precisión.

No se hará la primera validación interactuando con la pantalla mientras la misma persona conduce.

Puerta de salida:

- GPS y consultas se comportan de forma estable.
- Los marcadores no saturan la pantalla.
- Los avisos son útiles y no molestos.
- La recuperación ante túnel/Internet caído es comprensible.

### Etapa 8 — Hostname estable

Después de validar utilidad:

- crear túnel nombrado;
- asignar dominio/hostname estable;
- proteger con Cloudflare Access además del login Django;
- ejecutar `cloudflared` como servicio de Windows;
- ejecutar el servidor móvil al iniciar sesión o sistema;
- automatizar backup y health check;
- instalar definitivamente la PWA desde ese origen.

Cloudflare Tunnel crea conexiones salientes sin abrir puertos entrantes. Los Quick Tunnels son apropiados para pruebas; el túnel nombrado proporciona el hostname estable requerido para uso habitual. Véanse [Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/) y [configuración de un túnel](https://developers.cloudflare.com/tunnel/setup/).

## 14. Estrategia de pruebas

### Backend automático

- autenticación anónima/autenticada;
- allowlist de rutas del proceso móvil;
- coordenadas y radios inválidos;
- radio máximo;
- orden real por distancia;
- inclusión/exclusión en el borde;
- límite y `truncated`;
- exclusión de alquileres, vendidas, ocultas, sin precio, sin publicación activa y ubicaciones imprecisas;
- preservación de pin y overrides manuales;
- favorito solo modifica `is_favorite`;
- serializer sin notas, edición, evidencia o URLs administrativas;
- manifest y service worker con MIME/cabeceras correctos.

### JavaScript automático

Conviene extraer funciones puras a `drive-utils.js` y usar `node --test` para:

- Haversine/bearing del cliente;
- umbrales de movimiento;
- deduplicación de alertas;
- agrupación de coordenadas;
- formato corto de precios;
- simplificación y distancia de la traza.

### Validación de cada cambio

- `python manage.py test` o tests focalizados durante iteración.
- `python manage.py makemigrations --check --dry-run`.
- `python manage.py check`.
- `python manage.py check --deploy --settings=config.mobile_settings` con entorno seguro.
- `node --check` para cada archivo JavaScript modificado.
- `node --test` para utilidades extraídas.
- inspección visual en navegador y dispositivo real.
- `git status --short` antes y después.

## 15. Operación y backups

Antes de cada prueba remota:

1. Confirmar que la PC está conectada a corriente.
2. Desactivar suspensión mientras está enchufada; la pantalla sí puede apagarse.
3. Crear backup consistente y verificarlo.
4. Evitar migraciones, reparaciones o restauraciones durante el recorrido.
5. Inicialmente evitar que coincida con el scraping semanal.
6. Iniciar servidor móvil y túnel con el script.
7. Ejecutar el health check.
8. Iniciar sesión antes de subir al auto.
9. Al terminar, finalizar recorrido y detener Quick Tunnel si era una prueba.

No se debe copiar `db.sqlite3` mientras haya escrituras activas. El backup deberá usar la API de backup de SQLite o detener los escritores, ejecutar `quick_check` y probar una restauración. Solo la copia terminada puede sincronizarse a otra unidad.

## 16. Riesgos principales

| Prioridad | Riesgo | Mitigación |
|---|---|---|
| P0 | Exponer scrapers, edición o exportaciones | Proceso y URLconf móvil separados con allowlist |
| P0 | `DEBUG=True`, secreto débil o acceso anónimo | Configuración móvil cerrada y autenticación obligatoria |
| P0 | Confundir coordenada publicada con la fachada | Niveles de confiabilidad, texto explícito y agrupación |
| P1 | Demasiados resultados en radio pequeño | API compacta, límite, colisión de etiquetas y grupos |
| P1 | Suspensión de Windows | Deshabilitar suspensión enchufado y validar recuperación |
| P1 | Base sin backup recuperable | Backup online, `quick_check` y restauración probada |
| P1 | Quick Tunnel cambia de origen | Usarlo solo para pruebas; túnel nombrado antes de instalación estable |
| P1 | GPS pobre o permiso denegado | Estados explícitos y bloqueo de alertas imprecisas |
| P2 | Bloqueo SQLite durante scraping | Queries pequeñas, pocas escrituras y prueba concurrente |
| P2 | Consumo de batería | Throttling, Wake Lock controlable y liberación al finalizar |
| P2 | Caída de Internet/túnel | Últimos datos atenuados y estado de desconexión |
| P2 | Datos sensibles cacheados | `no-store` y service worker con allowlist estática |

## 17. Definición de terminado

El MVP estará terminado cuando:

- todas las rutas móviles estén autenticadas o sean explícitamente públicas y sin datos;
- el host móvil no exponga ninguna capacidad administrativa;
- la API cumpla filtros, límites, latencia y tamaño definidos;
- los pines manuales y correcciones existentes permanezcan intactos;
- el diseño aprobado funcione en teléfono real, retrato y paisaje;
- GPS, pérdida de conexión, Wake Lock y audio tengan fallback comprensible;
- el resumen represente únicamente la sesión actual;
- la PWA sea instalable desde un hostname estable;
- ninguna respuesta sensible quede cacheada;
- la suite y verificaciones focalizadas pasen;
- exista backup restaurable;
- exista una guía exacta para iniciar, instalar, probar, detener y recuperar el servicio.

## 18. Primer bloque de desarrollo recomendado

Comenzar con Etapas 0, 1 y 2:

1. Backup verificable.
2. Proceso móvil aislado y autenticado.
3. API cercana compacta con pruebas.

Ese bloque entrega la base segura y medible sobre la que después se construye la interfaz, sin exponer todavía el sistema ni depender del teléfono.
