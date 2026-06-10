---
title: Modelado de Base de Datos
---

# DER de la Base de Datos

**Archivo de BD:** `C:\Users\corebi\Documents\Scraper Inmobiliarias\db.sqlite3`  
**Consulta de esquema:** 2026-06-09 (local, `C:\Users\corebi\Documents\Scraper Inmobiliarias`)

## DER Relevante

```mermaid
erDiagram
    properties_agency {
        BIGINT id PK
        VARCHAR(180) name
        VARCHAR(200) website
        VARCHAR(80) phone
        VARCHAR(254) email
    }

    properties_source {
        BIGINT id PK
        VARCHAR(50) slug
        VARCHAR(120) name
        VARCHAR(200) base_url
        BOOL enabled
        SMALLINT crawl_delay_seconds
        TEXT notes
        DATETIME last_audited_at
    }

    properties_property {
        BIGINT id PK
        VARCHAR(64) fingerprint
        VARCHAR(30) property_type
        VARCHAR(20) operation
        VARCHAR(300) title
        DECIMAL(16,2) price
        VARCHAR(8) currency
        SMALLINT rooms
        SMALLINT bedrooms
        DECIMAL(4,1) bathrooms
        SMALLINT garages
        SMALLINT toilets
        DECIMAL(10,2) covered_area
        DECIMAL(10,2) total_area
        DECIMAL(10,2) land_area
        VARCHAR(20) status
        VARCHAR(20) location_source
        VARCHAR(20) location_confidence
        BOOL is_favorite
        BOOL is_hidden
        DATETIME reviewed_at
    }

    properties_propertylocation {
        BIGINT id PK
        BIGINT property_id FK
        FLOAT latitude
        FLOAT longitude
        VARCHAR(20) precision
        VARCHAR(60) provider
        FLOAT confidence
        BOOL manually_corrected
        BOOL outside_target
    }

    properties_locationhistory {
        BIGINT id PK
        BIGINT property_id FK
        FLOAT latitude
        FLOAT longitude
        VARCHAR(20) precision
        VARCHAR(60) provider
        DATETIME changed_at
    }

    properties_listing {
        BIGINT id PK
        BIGINT source_id FK
        BIGINT property_id FK
        BIGINT agency_id FK
        VARCHAR(160) external_id
        VARCHAR(800) url
        VARCHAR(50) source_status
        BOOL active
        SMALLINT missing_runs
    }

    properties_listingimage {
        BIGINT id PK
        BIGINT listing_id FK
        VARCHAR(1000) url
        SMALLINT position
    }

    properties_listingsnapshot {
        BIGINT id PK
        BIGINT listing_id FK
        DATETIME observed_at
        VARCHAR(64) content_hash
        DECIMAL(16,2) price
        VARCHAR(8) currency
        VARCHAR(30) status
    }

    properties_scraperun {
        BIGINT id PK
        BIGINT source_id FK
        VARCHAR(20) status
        DATETIME started_at
        DATETIME finished_at
    }

    properties_scrapejob {
        BIGINT id PK
        VARCHAR(20) status
        JSON selected_sources
        JSON worker_config
        VARCHAR(16) runner
        VARCHAR(16) scrape_mode
        INT max_pages
        INT max_listings
    }

    properties_scrapejobsource {
        BIGINT id PK
        BIGINT job_id FK
        BIGINT source_id FK
        VARCHAR(120) slug
        VARCHAR(120) name
        VARCHAR(20) status
        SMALLINT workers
        INT total_discovered
        INT total_to_process
        INT processed
        INT geocode_pending
        INT geocoded
        INT geocode_failed
    }

    properties_geocodecache {
        BIGINT id PK
        VARCHAR(400) query
        FLOAT latitude
        FLOAT longitude
        VARCHAR(20) precision
        FLOAT confidence
        DATETIME created_at
    }

    properties_property ||--|| properties_propertylocation : "1:1 (opcional)"
    properties_property ||--o{ properties_locationhistory : "1:N"
    properties_property ||--o{ properties_listing : "1:N"
    properties_listing }o--|| properties_source : "N:1"
    properties_listing }o--o| properties_agency : "N:0..1"
    properties_listing ||--o{ properties_listingimage : "1:N"
    properties_listing ||--o{ properties_listingsnapshot : "1:N"
    properties_source ||--o{ properties_scraperun : "1:N"
    properties_scrapejob ||--o{ properties_scrapejobsource : "1:N"
    properties_source ||--o{ properties_scrapejobsource : "1:N"
```

> Nota: `properties_geocodecache` funciona como cache de consultas de geocodificacion y no tiene FK con `properties_property`.

## DER Full

```mermaid
erDiagram
    auth_user {
        BIGINT id PK
        VARCHAR(150) password
        BOOLEAN is_superuser
        VARCHAR(150) username
        VARCHAR(254) email
        BOOLEAN is_staff
        BOOLEAN is_active
        DATETIME date_joined
    }

    auth_group {
        BIGINT id PK
        VARCHAR(150) name UK
    }

    auth_permission {
        BIGINT id PK
        BIGINT content_type_id FK
        VARCHAR(100) codename
        VARCHAR(255) name
    }

    django_content_type {
        BIGINT id PK
        VARCHAR(100) app_label
        VARCHAR(100) model
    }

    auth_group_permissions {
        BIGINT id PK
        BIGINT group_id FK
        BIGINT permission_id FK
    }

    auth_user_groups {
        BIGINT id PK
        BIGINT user_id FK
        BIGINT group_id FK
    }

    auth_user_user_permissions {
        BIGINT id PK
        BIGINT user_id FK
        BIGINT permission_id FK
    }

    django_admin_log {
        BIGINT id PK
        BIGINT user_id FK
        BIGINT content_type_id FK
        VARCHAR(100) object_id
        SMALLINT action_flag
        TEXT change_message
        DATETIME action_time
    }

    django_session {
        VARCHAR(40) session_key PK
        TEXT session_data
        DATETIME expire_date
    }

    django_migrations {
        BIGINT id PK
        VARCHAR(255) app
        VARCHAR(255) name
        VARCHAR(255) applied
    }

    properties_agency {
        BIGINT id PK
        VARCHAR(180) name
    }

    properties_source {
        BIGINT id PK
        VARCHAR(50) slug UK
        VARCHAR(120) name
    }

    properties_property {
        BIGINT id PK
        VARCHAR(64) fingerprint UK
        VARCHAR(20) status
        BOOL is_favorite
        BOOL is_hidden
    }

    properties_propertylocation {
        BIGINT id PK
        BIGINT property_id FK
        FLOAT latitude
        FLOAT longitude
    }

    properties_locationhistory {
        BIGINT id PK
        BIGINT property_id FK
        FLOAT latitude
        FLOAT longitude
    }

    properties_listing {
        BIGINT id PK
        BIGINT source_id FK
        BIGINT property_id FK
        BIGINT agency_id FK
        VARCHAR(160) external_id
        BOOL active
    }

    properties_listingimage {
        BIGINT id PK
        BIGINT listing_id FK
        VARCHAR(1000) url
    }

    properties_listingsnapshot {
        BIGINT id PK
        BIGINT listing_id FK
        VARCHAR(64) content_hash
    }

    properties_scraperun {
        BIGINT id PK
        BIGINT source_id FK
        VARCHAR(20) status
    }

    properties_scrapejob {
        BIGINT id PK
        VARCHAR(20) status
        JSON selected_sources
    }

    properties_scrapejobsource {
        BIGINT id PK
        BIGINT job_id FK
        BIGINT source_id FK
        VARCHAR(120) slug
        VARCHAR(120) name
    }

    properties_geocodecache {
        BIGINT id PK
        VARCHAR(400) query UK
    }

    property_fts {
        INTEGER rowid PK
        TEXT content
    }
    property_fts_config {
        TEXT k PK
        TEXT v
    }
    property_fts_data {
        INTEGER docid PK
        BLOB content
    }
    property_fts_docsize {
        INTEGER docid PK
        INTEGER size
    }
    property_fts_idx {
        INTEGER segid PK
        INTEGER term
        INTEGER pgno
    }
    property_location_rtree {
        INTEGER id PK
        REAL min_lat
        REAL max_lat
        REAL min_lng
        REAL max_lng
    }

    auth_group ||--o{ auth_group_permissions : "1:N"
    auth_permission ||--o{ auth_group_permissions : "1:N"
    auth_user ||--o{ auth_user_groups : "1:N"
    auth_group ||--o{ auth_user_groups : "1:N"
    auth_user ||--o{ auth_user_user_permissions : "1:N"
    auth_permission ||--o{ auth_user_user_permissions : "1:N"

    django_content_type ||--o{ auth_permission : "1:N"
    django_content_type ||--o{ django_admin_log : "1:N"
    auth_user ||--o{ django_admin_log : "1:N"

    properties_source ||--o{ properties_listing : "1:N"
    properties_agency ||--o{ properties_listing : "1:N"
    properties_property ||--o| properties_propertylocation : "1:1 (opcional)"
    properties_property ||--o{ properties_locationhistory : "1:N"
    properties_property ||--o{ properties_listing : "1:N"
    properties_listing ||--o{ properties_listingimage : "1:N"
    properties_listing ||--o{ properties_listingsnapshot : "1:N"
    properties_source ||--o{ properties_scraperun : "1:N"
    properties_scrapejob ||--o{ properties_scrapejobsource : "1:N"
    properties_source ||--o{ properties_scrapejobsource : "1:N"

    properties_property --|> property_location_rtree : "sin FK explicita (id=property_id), sincronizacion via signals"
    properties_property --|> property_fts : "sin FK explicita (rowid=id), sincronizacion via signals"
    property_fts ||--|{ property_fts_data : "estructura interna FTS5"
    property_fts ||--|{ property_fts_docsize : "estructura interna FTS5"
    property_fts ||--|{ property_fts_idx : "estructura interna FTS5"
    property_fts ||--|{ property_fts_config : "configuracion FTS5"
```

> Nota tecnica: `property_location_rtree` y `property_fts` no tienen FK explicita en el esquema DDL de Django; se actualizan por sincronizacion desde `properties/services/indexes.py` y `properties/signals.py`.
