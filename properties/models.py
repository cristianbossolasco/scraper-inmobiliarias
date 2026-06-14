import builtins
from decimal import Decimal

from django.db import models
from django.utils import timezone


class Agency(models.Model):
    name = models.CharField(max_length=180, unique=True)
    website = models.URLField(blank=True)
    phone = models.CharField(max_length=80, blank=True)
    email = models.EmailField(blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "agencies"

    def __str__(self):
        return self.name


class Source(models.Model):
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=120)
    base_url = models.URLField()
    enabled = models.BooleanField(default=True)
    crawl_delay_seconds = models.PositiveSmallIntegerField(default=2)
    notes = models.TextField(blank=True)
    last_audited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Property(models.Model):
    class Type(models.TextChoices):
        HOUSE = "house", "Casa"
        PH = "ph", "PH"
        DUPLEX = "duplex", "Dúplex"
        APARTMENT = "apartment", "Departamento"
        COUNTRY_HOUSE = "country_house", "Quinta"
        LAND = "land", "Terreno"
        OTHER = "other", "Otro"

    class Status(models.TextChoices):
        ACTIVE = "active", "Activa"
        RESERVED = "reserved", "Reservada"
        SOLD = "sold", "Vendida"
        SUSPENDED = "suspended", "Suspendida"
        REMOVED = "removed", "Retirada"

    class ConditionCategory(models.TextChoices):
        NEW = "new", "A estrenar"
        RENOVATED = "renovated", "Refaccionada"
        USED = "used", "Usada"
        NEEDS_WORK = "needs_work", "A refaccionar"
        UNKNOWN = "unknown", "Sin dato"

    class LocationSource(models.TextChoices):
        LISTING = "listing", "Listado"
        DETAIL = "detail", "Detalle"
        DESCRIPTION = "description", "Descripcion"
        MAP = "map", "Mapa"
        INFERRED = "inferred", "Inferida"
        UNKNOWN = "unknown", "Desconocida"

    class LocationConfidence(models.TextChoices):
        HIGH = "high", "Alta"
        MEDIUM = "medium", "Media"
        LOW = "low", "Baja"
        UNKNOWN = "unknown", "Desconocida"

    fingerprint = models.CharField(max_length=64, unique=True)
    property_type = models.CharField(
        max_length=30, choices=Type.choices, default=Type.OTHER
    )
    operation = models.CharField(max_length=20, default="sale", db_index=True)
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    address = models.CharField(max_length=300, blank=True)
    normalized_address = models.CharField(max_length=300, blank=True, db_index=True)
    locality = models.CharField(max_length=100, blank=True, db_index=True)
    neighborhood = models.CharField(max_length=120, blank=True, db_index=True)
    currency = models.CharField(max_length=8, blank=True, db_index=True)
    price = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True, db_index=True
    )
    rooms = models.PositiveSmallIntegerField(null=True, blank=True)
    bedrooms = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    bathrooms = models.DecimalField(
        max_digits=4, decimal_places=1, null=True, blank=True, db_index=True
    )
    garages = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    toilets = models.PositiveSmallIntegerField(null=True, blank=True)
    covered_area = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, db_index=True
    )
    total_area = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    land_area = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, db_index=True
    )
    uncovered_area = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    semicovered_area = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    front_width = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    lot_depth = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    building_floors = models.PositiveSmallIntegerField(null=True, blank=True)
    age_years = models.PositiveSmallIntegerField(null=True, blank=True)
    condition_category = models.CharField(
        max_length=20,
        choices=ConditionCategory.choices,
        default=ConditionCategory.UNKNOWN,
        db_index=True,
    )
    features = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    detected_locality = models.CharField(max_length=100, blank=True, db_index=True)
    detected_neighborhood = models.CharField(max_length=120, blank=True, db_index=True)
    detected_address = models.CharField(max_length=300, blank=True)
    detected_latitude = models.FloatField(null=True, blank=True)
    detected_longitude = models.FloatField(null=True, blank=True)
    inferred_neighborhood = models.CharField(max_length=120, blank=True, db_index=True)
    inferred_neighborhood_method = models.CharField(max_length=40, blank=True)
    inferred_neighborhood_distance_m = models.FloatField(null=True, blank=True)
    zone_conflict = models.BooleanField(default=False, db_index=True)
    zone_needs_review = models.BooleanField(default=False, db_index=True)
    zone_inference_evidence = models.JSONField(default=dict, blank=True)
    zone_inferred_at = models.DateTimeField(null=True, blank=True)
    location_source = models.CharField(
        max_length=20, choices=LocationSource.choices, default=LocationSource.UNKNOWN
    )
    location_confidence = models.CharField(
        max_length=20,
        choices=LocationConfidence.choices,
        default=LocationConfidence.UNKNOWN,
        db_index=True,
    )
    location_notes = models.TextField(blank=True)
    location_evidence = models.JSONField(default=dict, blank=True)
    security_coverage_score = models.FloatField(null=True, blank=True, db_index=True)
    security_risk_score = models.FloatField(null=True, blank=True, db_index=True)
    security_level = models.CharField(max_length=20, blank=True, db_index=True)
    security_zone_label = models.CharField(max_length=120, blank=True, db_index=True)
    security_source = models.CharField(max_length=160, blank=True)
    security_evidence = models.JSONField(default=dict, blank=True)
    security_scored_at = models.DateTimeField(null=True, blank=True)
    is_favorite = models.BooleanField(default=False, db_index=True)
    is_hidden = models.BooleanField(default=False, db_index=True)
    reviewed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    personal_notes = models.TextField(blank=True)
    manual_overrides = models.JSONField(default=dict, blank=True)
    data_manually_corrected_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(fields=["currency", "price"]),
            models.Index(fields=["property_type", "status"]),
            models.Index(fields=["locality", "neighborhood"]),
            models.Index(fields=["detected_locality", "detected_neighborhood"]),
            models.Index(fields=["is_hidden", "is_favorite", "reviewed_at"]),
            models.Index(
                fields=["operation", "is_hidden", "last_seen_at"],
                name="properties__operat_47d71a_idx",
            ),
            models.Index(
                fields=["operation", "price"],
                name="properties__operat_976d5e_idx",
            ),
            models.Index(
                fields=["operation", "land_area"],
                name="properties__operat_224f57_idx",
            ),
            models.Index(
                fields=["operation", "covered_area"],
                name="properties__operat_86aa71_idx",
            ),
            models.Index(
                fields=["operation", "status", "is_hidden", "last_seen_at"],
                name="prop_op_status_hidden_seen_idx",
            ),
            models.Index(
                fields=["property_type", "condition_category", "age_years"],
                name="prop_type_cond_age_idx",
            ),
        ]

    def __str__(self):
        return self.title

    @property
    def price_per_m2(self):
        area = self.covered_area or self.total_area or self.land_area
        if self.price is None or not area:
            return None
        return (self.price / Decimal(area)).quantize(Decimal("0.01"))

    @property
    def primary_listing(self):
        return self.listings.filter(active=True).order_by("-last_seen_at").first()

    @property
    def data_quality_score(self):
        checks = [
            self.price is not None,
            bool(self.covered_area or self.total_area or self.land_area),
            hasattr(self, "location"),
            self.listings.filter(images__isnull=False).exists(),
            self.listings.exists(),
            self.listings.filter(agency__isnull=False).exists(),
        ]
        return round(sum(1 for check in checks if check) / len(checks) * 100)


class PropertyLocation(models.Model):
    class Precision(models.TextChoices):
        EXACT = "exact", "Exacta"
        INTERSECTION = "intersection", "Intersección"
        STREET = "street", "Calle"
        NEIGHBORHOOD = "neighborhood", "Barrio"
        MANUAL = "manual", "Confirmada manualmente"

    property = models.OneToOneField(
        Property, related_name="location", on_delete=models.CASCADE
    )
    latitude = models.FloatField()
    longitude = models.FloatField()
    precision = models.CharField(max_length=20, choices=Precision.choices)
    query = models.CharField(max_length=400, blank=True)
    provider = models.CharField(max_length=60, default="source")
    confidence = models.FloatField(default=0)
    manually_corrected = models.BooleanField(default=False)
    outside_target = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.latitude:.5f}, {self.longitude:.5f}"

    @builtins.property
    def is_exact(self):
        return self.precision in {self.Precision.EXACT, self.Precision.MANUAL}


class PropertyLocationIntelligence(models.Model):
    class MatchMethod(models.TextChoices):
        COORDINATES = "coordinates", "Coordenadas"
        ZONE = "zone", "Zona inferida"
        NONE = "none", "Sin match"

    property = models.OneToOneField(
        Property, related_name="location_intelligence", on_delete=models.CASCADE
    )
    overall_score = models.FloatField(null=True, blank=True, db_index=True)
    level = models.CharField(max_length=20, blank=True, db_index=True)
    zone_name = models.CharField(max_length=120, blank=True, db_index=True)
    match_method = models.CharField(
        max_length=20, choices=MatchMethod.choices, default=MatchMethod.NONE, db_index=True
    )
    confidence = models.CharField(max_length=40, blank=True, db_index=True)
    transport_score = models.FloatField(null=True, blank=True, db_index=True)
    education_score = models.FloatField(null=True, blank=True, db_index=True)
    health_score = models.FloatField(null=True, blank=True, db_index=True)
    flood_penalty_score = models.FloatField(null=True, blank=True, db_index=True)
    urban_informality_score = models.FloatField(null=True, blank=True, db_index=True)
    environmental_penalty_score = models.FloatField(null=True, blank=True)
    development_potential_score = models.FloatField(null=True, blank=True)
    in_flood_risk_zone = models.BooleanField(null=True, blank=True, db_index=True)
    nearest_renabap_m = models.FloatField(null=True, blank=True)
    nearest_sube_point_m = models.FloatField(null=True, blank=True)
    nearest_school_m = models.FloatField(null=True, blank=True)
    nearest_health_center_m = models.FloatField(null=True, blank=True)
    components = models.JSONField(default=dict, blank=True)
    risks = models.JSONField(default=dict, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    source_signature = models.CharField(max_length=300, blank=True)
    scored_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-overall_score", "zone_name"]
        indexes = [
            models.Index(
                fields=["overall_score", "level"],
                name="properties__locati_b9d0c4_idx",
            ),
            models.Index(
                fields=["zone_name", "overall_score"],
                name="properties__locati_79cf14_idx",
            ),
        ]

    def __str__(self):
        label = self.zone_name or "Sin zona"
        score = "-" if self.overall_score is None else round(self.overall_score, 1)
        return f"{label}: {score}"


class LocationHistory(models.Model):
    property = models.ForeignKey(
        Property, related_name="location_history", on_delete=models.CASCADE
    )
    latitude = models.FloatField()
    longitude = models.FloatField()
    precision = models.CharField(max_length=20)
    provider = models.CharField(max_length=60)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at"]


class Listing(models.Model):
    source = models.ForeignKey(Source, related_name="listings", on_delete=models.PROTECT)
    agency = models.ForeignKey(
        Agency, related_name="listings", on_delete=models.SET_NULL, null=True, blank=True
    )
    property = models.ForeignKey(
        Property, related_name="listings", on_delete=models.CASCADE
    )
    external_id = models.CharField(max_length=160)
    url = models.URLField(max_length=800)
    source_status = models.CharField(max_length=50, blank=True)
    active = models.BooleanField(default=True, db_index=True)
    missing_runs = models.PositiveSmallIntegerField(default=0)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    raw_data = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="unique_source_listing"
            )
        ]
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(
                fields=["property", "active"],
                name="properties__propert_c585c9_idx",
            ),
            models.Index(
                fields=["agency", "active"],
                name="properties__agency__9a4f75_idx",
            ),
            models.Index(
                fields=["source", "active"],
                name="properties__source__e7ad06_idx",
            ),
            models.Index(
                fields=["source", "source_status", "active"],
                name="listing_src_status_active_idx",
            ),
        ]

    def __str__(self):
        return f"{self.source}: {self.external_id}"


class ListingIdentity(models.Model):
    source = models.ForeignKey(
        Source, related_name="listing_identities", on_delete=models.PROTECT
    )
    external_id = models.CharField(max_length=160)
    url = models.URLField(max_length=800, blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_seen_reason = models.CharField(max_length=50, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"], name="unique_source_listing_identity"
            )
        ]
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(
                fields=["source", "last_seen_at"],
                name="props_ident_src_seen_idx",
            ),
        ]

    def __str__(self):
        return f"{self.source}: {self.external_id}"


class ListingImage(models.Model):
    listing = models.ForeignKey(
        Listing, related_name="images", on_delete=models.CASCADE
    )
    url = models.URLField(max_length=1000)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["listing", "url"], name="unique_listing_image"
            )
        ]


class ListingSnapshot(models.Model):
    listing = models.ForeignKey(
        Listing, related_name="snapshots", on_delete=models.CASCADE
    )
    observed_at = models.DateTimeField(auto_now_add=True)
    content_hash = models.CharField(max_length=64)
    price = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    status = models.CharField(max_length=30, blank=True)
    payload = models.JSONField(default=dict)

    class Meta:
        ordering = ["-observed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["listing", "content_hash"], name="unique_listing_snapshot"
            )
        ]


class ScrapeRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "En curso"
        SUCCESS = "success", "Correcta"
        PARTIAL = "partial", "Parcial"
        FAILED = "failed", "Fallida"

    source = models.ForeignKey(
        Source, related_name="runs", on_delete=models.SET_NULL, null=True, blank=True
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    discovered = models.PositiveIntegerField(default=0)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    error_log = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]


class ScrapeJob(models.Model):
    class Runner(models.TextChoices):
        WEB = "web", "Web"
        COMMAND = "command", "Consola"

    class Mode(models.TextChoices):
        TRIAL = "trial", "Prueba"
        COMPLETE = "complete", "Completo"

    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        RUNNING = "running", "En curso"
        SUCCESS = "success", "Correcta"
        PARTIAL = "partial", "Parcial"
        FAILED = "failed", "Fallida"
        CANCELLED = "cancelled", "Cancelada"
        INTERRUPTED = "interrupted", "Interrumpida"

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    selected_sources = models.JSONField(default=list)
    worker_config = models.JSONField(default=dict)
    runner = models.CharField(max_length=16, choices=Runner.choices, default=Runner.WEB)
    scrape_mode = models.CharField(
        max_length=16, choices=Mode.choices, default=Mode.COMPLETE
    )
    max_pages = models.PositiveIntegerField(null=True, blank=True)
    start_page = models.PositiveIntegerField(null=True, blank=True)
    max_listings = models.PositiveIntegerField(null=True, blank=True)
    geocode_limit = models.PositiveIntegerField(null=True, blank=True)
    mark_missing = models.BooleanField(default=True)
    request_timeout_seconds = models.PositiveIntegerField(null=True, blank=True)
    max_errors_per_source = models.PositiveIntegerField(null=True, blank=True)
    retry_urls = models.JSONField(default=dict, blank=True)
    cancel_requested = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    error_log = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class ScrapeJobSource(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        DISCOVERING = "discovering", "Descubriendo"
        RUNNING = "running", "En curso"
        SUCCESS = "success", "Correcta"
        PARTIAL = "partial", "Parcial"
        FAILED = "failed", "Fallida"
        CANCELLED = "cancelled", "Cancelada"
        INTERRUPTED = "interrupted", "Interrumpida"

    job = models.ForeignKey(
        ScrapeJob, related_name="sources", on_delete=models.CASCADE
    )
    source = models.ForeignKey(Source, related_name="job_sources", on_delete=models.PROTECT)
    slug = models.SlugField()
    name = models.CharField(max_length=120)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    workers = models.PositiveIntegerField(default=1)
    total_discovered = models.PositiveIntegerField(default=0)
    total_to_process = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    geocode_pending = models.PositiveIntegerField(default=0)
    geocoded = models.PositiveIntegerField(default=0)
    geocode_failed = models.PositiveIntegerField(default=0)
    current_url = models.URLField(max_length=1000, blank=True)
    error_urls = models.JSONField(default=list, blank=True)
    logs = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["job_id", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "slug"], name="unique_job_source"
            )
        ]


class GeocodeCache(models.Model):
    query = models.CharField(max_length=400, unique=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    precision = models.CharField(max_length=20, blank=True)
    confidence = models.FloatField(default=0)
    provider_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class OperationJob(models.Model):
    class Kind(models.TextChoices):
        PIPELINE = "pipeline", "Pipeline"
        SCRAPE = "scrape", "Scraping"
        GEOCODE = "geocode", "Geocoding"
        INFER_ZONES = "infer_zones", "Inferencia de zonas"
        SCORE_SECURITY = "score_security", "Scoring seguridad"
        SCORE_LOCATION_INTELLIGENCE = (
            "score_location_intelligence",
            "Scoring inteligencia territorial",
        )
        REPAIR_ADDRESSES = "repair_addresses", "Reparar direcciones"
        REPAIR_NEIGHBORHOODS = "repair_neighborhoods", "Reparar barrios"
        REPAIR_LOCALITIES = "repair_localities", "Reparar localidades"
        REPAIR_AGENCIES = "repair_agencies", "Reparar agencias"
        REPAIR_METRICS = "repair_metrics", "Reparar metricas"
        REPAIR_MERGED_LISTINGS = "repair_merged_listings", "Separar fusiones"
        MERGE_PROPERTIES = "merge_properties", "Fusionar duplicados"

    class Mode(models.TextChoices):
        DRY_RUN = "dry_run", "Simulacion"
        APPLY = "apply", "Aplicar"

    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        RUNNING = "running", "En curso"
        SUCCESS = "success", "Correcta"
        PARTIAL = "partial", "Parcial"
        FAILED = "failed", "Fallida"
        CANCELLED = "cancelled", "Cancelada"
        INTERRUPTED = "interrupted", "Interrumpida"

    kind = models.CharField(
        max_length=40, choices=Kind.choices, default=Kind.PIPELINE, db_index=True
    )
    title = models.CharField(max_length=160, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    mode = models.CharField(
        max_length=16, choices=Mode.choices, default=Mode.DRY_RUN, db_index=True
    )
    scope = models.JSONField(default=dict, blank=True)
    params = models.JSONField(default=dict, blank=True)
    result_summary = models.JSONField(default=dict, blank=True)
    total_steps = models.PositiveIntegerField(default=0)
    completed_steps = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    changed = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    logs = models.TextField(blank=True)
    cancel_requested = models.BooleanField(default=False)
    source_job = models.ForeignKey(
        "self",
        related_name="apply_jobs",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        label = self.title or self.get_kind_display()
        return f"{label} #{self.pk}"


class OperationJobStep(models.Model):
    job = models.ForeignKey(
        OperationJob, related_name="steps", on_delete=models.CASCADE
    )
    order = models.PositiveSmallIntegerField(default=0)
    kind = models.CharField(max_length=40, choices=OperationJob.Kind.choices)
    status = models.CharField(
        max_length=20,
        choices=OperationJob.Status.choices,
        default=OperationJob.Status.PENDING,
        db_index=True,
    )
    mode = models.CharField(
        max_length=16, choices=OperationJob.Mode.choices, default=OperationJob.Mode.DRY_RUN
    )
    params = models.JSONField(default=dict, blank=True)
    total = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    changed = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    logs = models.TextField(blank=True)
    error_log = models.TextField(blank=True)
    result_summary = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["job_id", "order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "order"], name="unique_operation_step_order"
            )
        ]

    def __str__(self):
        return f"{self.job_id}:{self.order} {self.kind}"
