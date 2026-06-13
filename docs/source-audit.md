# Auditoria inicial de fuentes

Fecha: 6 de junio de 2026.

| Fuente | Cobertura observada | Estructura | Restricciones | Decision |
|---|---:|---|---|---|
| Mapaprop | Mas de 200 casas y oferta residencial adicional | HTML renderizado, enlaces estables, direccion, campos y coordenadas embebidas | `/api/` prohibida; paginas publicas permitidas | Habilitada |
| Argencasas / SIBA | Mas de 100 casas y agencias locales | JSON-LD `RealEstateListing`, fichas detalladas, paginacion | `Crawl-delay: 2`; `/motor/` prohibido | Habilitada |
| Argenprop | Busqueda publica muestra mas de 1.000 casas en el partido | Listados HTML y fichas con JSON-LD `House`; sin coordenadas nativas | Paginacion publica permitida; el scraper toma el maximo publicado por el HTML | Habilitada |
| MercadoProp | Sitemap AR contiene unas 45 URLs con Hurlingham/Villa Tesei/William Morris | JSON-LD `RealEstateListing`, proveedor, direccion, imagenes y coordenadas frecuentes | `robots.txt` permite el sitio y declara sitemap | Habilitada |
| Miglierini | Listado local con varias casas de Hurlingham, Villa Club y Villa Tesei | WordPress con enlaces `/propiedad/` y sitemap propio | `Crawl-delay: 10` | Adaptador incluido, deshabilitado |
| Odriozola | Listado local con 20+ fichas residenciales | HTML de fichas y coordenadas embebidas en varias publicaciones | Robots intermitente; requiere fixtures mas completos | Adaptador incluido, deshabilitado |
| Beaudroit | Inmobiliaria local de Hurlingham | Home grande, fichas no expuestas de forma directa en auditoria inicial | Permitido por `robots.txt` | Auditoria profunda pendiente |
| Becerra Propiedades | Catalogo local de alta calidad | Fichas con campos separados | Respuestas HTTP 500 intermitentes a clientes automatizados | Adaptador incluido, deshabilitado |
| Aliaga Propiedades | Fichas detalladas con superficies y caracteristicas | HTML claro y referencias estables | Falta confirmar listado publico estable por zona | Adaptador incluido, deshabilitado |
| Fincas Bienes Raices | Oferta local | El buscador deriva a Argencasas | Se evita duplicar consulta | Cubierta por Argencasas |
| Riquelme Propiedades | Amplia oferta local | Sindicada en Mapaprop | Sitio propio pendiente de estabilidad | Cubierta por Mapaprop |
| Aurellana | Oferta local | Sindicada en Mapaprop | Sitio propio pendiente de estabilidad | Cubierta por Mapaprop |
| Buscainmueble | Mismo listado observado que Argenprop | HTML equivalente a Argenprop | Replica catalogo | Excluida por duplicacion |
| MemudoYa | Red tecnica similar a Mapaprop; Tesei Propiedades expone 12 links y coordenadas | Nuxt, HTML publico, `/api/` prohibida | Duplicaria fuente Mapaprop | Excluida |
| Zonaprop | Volumen muy alto | Listados estructurados por link directo y segmentacion dinamica por precio | Trial limitado: deshabilitada para `--all`, posible challenge Cloudflare, solo paginas publicas 1-5 por segmento | Adaptador trial |
| MercadoLibre Inmuebles | Volumen muy alto | API oficial de items/search y detalle por item | Requiere token si la busqueda publica queda limitada | Integrar por API oficial, no por login web |
| Facebook Marketplace | Volumen potencial alto | Sin API publica oficial de busqueda Marketplace | Requiere login y tiene alta friccion anti-bot | No automatizar; solo importacion manual de URLs/CSV |
| Grupos de Facebook | Publicaciones locales potencialmente utiles | API de grupos restringida/deprecada para terceros | Requiere permisos/admin/revision; no es fuente abierta estable | No automatizar; solo importacion manual |
| Instagram | Cuentas de inmobiliarias y publicaciones publicas | Graph API parcial para cuentas profesionales/hashtags limitados | No permite busqueda global libre de inmuebles locales | Evaluar por cuentas concretas o importacion manual |
| OLX Argentina | Sin cobertura confiable actual | Operacion local discontinuada/cerrada | No hay catalogo estable Argentina | Descartada |
| La Voz Clasificados | Clasificados publicos | Pendiente de auditoria | Baja prioridad geografica para Hurlingham | Auditar solo si aparece volumen local |

## Criterios

1. No se consumen APIs privadas ni rutas prohibidas.
2. Cada fuente tiene demora configurable, reintentos limitados y User-Agent identificable.
3. Las agencias sindicadas se guardan como inmobiliaria aunque la fuente tecnica sea el portal.
4. Una fuente se habilita solo cuando su pagina de resultados y sus fichas son estables.
5. Ejecutar `python manage.py audit_sources` antes de habilitar una fuente nueva.
6. No se automatizan sesiones personales, logins, captchas ni grupos privados. Las fuentes sociales se incorporan por importacion manual o APIs oficiales con permisos.

## MercadoLibre API

- El adaptador `mercadolibre` usa `https://api.mercadolibre.com/sites/MLA/search` y detalle `https://api.mercadolibre.com/items/{id}`.
- Variables recomendadas: `MELI_ACCESS_TOKEN` cuando MercadoLibre limite la busqueda publica.
- La fuente queda deshabilitada por defecto hasta validar una corrida limitada con `--max-pages 1 --max-listings 10`.

## Riesgos operativos

- El HTML externo puede cambiar sin aviso; los parsers se validan con fixtures y una ejecucion limitada antes de cada despliegue.
- Argenprop no aporta coordenadas confiables en HTML publico; sus ubicaciones pasan por geocodificacion y pueden quedar como aproximadas.
- Los mosaicos publicos de OpenStreetMap no deben usarse para trafico elevado. El endpoint es configurable para migrar a un proveedor contratado o servidor propio.
