from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from properties.models import Source
from properties.scrapers.base import USER_AGENT
from properties.scrapers.registry import source_definitions


class Command(BaseCommand):
    help = "Comprueba disponibilidad y robots.txt de las fuentes conocidas."

    def handle(self, *args, **options):
        rows = []
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        for definition in source_definitions():
            parsed = urlparse(definition.base_url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            parser = RobotFileParser(robots_url)
            robots_status = "sin robots"
            try:
                response = session.get(robots_url, timeout=12)
                if response.ok:
                    parser.parse(response.text.splitlines())
                    robots_status = (
                        "permitido"
                        if parser.can_fetch(USER_AGENT, definition.search_url)
                        else "bloqueado"
                    )
                page = session.get(definition.search_url, timeout=18)
                http_status = page.status_code
            except requests.RequestException as exc:
                http_status = f"error: {exc.__class__.__name__}"
            Source.objects.update_or_create(
                slug=definition.slug,
                defaults={
                    "name": definition.name,
                    "base_url": definition.base_url,
                    "enabled": definition.enabled and robots_status != "bloqueado",
                    "crawl_delay_seconds": definition.crawl_delay,
                    "notes": definition.notes,
                    "last_audited_at": timezone.now(),
                },
            )
            rows.append((definition.name, http_status, robots_status, definition.enabled))
        for name, status, robots, enabled in rows:
            self.stdout.write(
                f"{name:38} HTTP {str(status):5} robots={robots:10} "
                f"v1={'sí' if enabled else 'no'}"
            )
