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
- pantalla móvil inicial con MapLibre, GPS en primer plano, filtros de tipo/radio y marcadores de precio;
- Waitress y WhiteNoise para ejecutar con `DEBUG=False`;
- pruebas de seguridad, filtros espaciales y preservación de datos manuales.

Todavía no se habilitó el túnel ni se creó un usuario real. Tampoco están terminados el resumen completo, audio, alertas automáticas, manifest/service worker ni la instalación PWA.

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

## Siguiente bloque

1. Persistencia local de la sesión y traza simplificada.
2. Avisos de proximidad y deduplicación.
3. Audio opt-in.
4. Resumen visual del recorrido.
5. Manifest, iconos y service worker seguro.
6. Scripts Start/Test/Stop y primera prueba mediante Quick Tunnel.

