import json

from django.core.management.base import BaseCommand

from properties.scrapers.registry import get_adapter_classes


DEFAULT_SKIP = {"argenprop", "marcelo-russo"}


class Command(BaseCommand):
    help = "Audita cobertura de discovery sin guardar propiedades."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            action="append",
            dest="sources",
            help="Slug de fuente a auditar. Puede repetirse.",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=None,
            help="Limita paginas por fuente para una corrida rapida.",
        )
        parser.add_argument(
            "--max-listings",
            type=int,
            default=None,
            help="Limita URLs descubiertas por fuente.",
        )
        parser.add_argument(
            "--include-fixed",
            action="store_true",
            help="Incluye argenprop y marcelo-russo en la auditoria.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Imprime JSON en vez de tabla.",
        )

    def handle(self, *args, **options):
        wanted = set(options["sources"] or [])
        skip = set() if options["include_fixed"] else DEFAULT_SKIP
        rows = []

        for adapter_cls in get_adapter_classes(enabled_only=True):
            definition = adapter_cls.definition
            if wanted and definition.slug not in wanted:
                continue
            if definition.slug in skip:
                continue

            adapter = adapter_cls(
                max_pages=options["max_pages"],
                max_listings=options["max_listings"],
            )
            row = {
                "source": definition.slug,
                "name": definition.name,
                "declared_total": None,
                "urls_discovered": 0,
                "coverage_ratio": None,
                "pages_seen": 0,
                "status": "ok",
                "error": "",
            }
            try:
                urls = list(adapter.discover())
                stats = getattr(adapter, "discovery_stats", {}) or {}
                row.update(
                    {
                        "declared_total": stats.get("declared_total"),
                        "urls_discovered": stats.get("urls_discovered", len(urls)),
                        "coverage_ratio": stats.get("coverage_ratio"),
                        "pages_seen": stats.get("pages_seen", 0),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - audit should continue per source.
                row["status"] = "error"
                row["error"] = f"{exc.__class__.__name__}: {exc}"
            rows.append(row)

        if options["as_json"]:
            self.stdout.write(json.dumps(rows, ensure_ascii=True, indent=2))
            return

        self.stdout.write(
            f"{'Fuente':20} {'Declarado':>10} {'URLs':>8} {'Cobertura':>10} {'Paginas':>8} Estado"
        )
        for row in rows:
            ratio = (
                f"{row['coverage_ratio']}%"
                if row["coverage_ratio"] is not None
                else "-"
            )
            declared = row["declared_total"] if row["declared_total"] is not None else "-"
            self.stdout.write(
                f"{row['source']:20} {str(declared):>10} "
                f"{row['urls_discovered']:>8} {ratio:>10} "
                f"{row['pages_seen']:>8} {row['status']}"
            )
            if row["error"]:
                self.stdout.write(f"  {row['error']}")
