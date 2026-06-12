#!/usr/bin/env python3
"""Download official public crime data sources for Hurlingham.

This script only stores raw official datasets and a small download manifest.
It does not infer neighborhood-level crime and does not touch the Django app.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_HEADERS = {
    "User-Agent": "radar-inmobiliario-hurlingham-crime-geojson/1.0",
    "Accept": "text/csv,application/zip,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_ckan_url(source: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    api_url = source["ckan_package_api"]
    response = requests.get(api_url, headers=DEFAULT_HEADERS, timeout=60)
    response.raise_for_status()
    payload = response.json()
    resources = payload.get("result", {}).get("resources") or []
    preferred = [str(item).upper() for item in source.get("preferred_formats") or []]
    if not resources:
        raise RuntimeError(f"No CKAN resources found for {source['key']}: {api_url}")

    for expected_format in preferred:
        for resource in resources:
            resource_format = str(resource.get("format") or "").upper()
            if resource_format == expected_format and resource.get("url"):
                return str(resource["url"]), {
                    "ckan_package_api": api_url,
                    "resource_id": resource.get("id"),
                    "resource_name": resource.get("name"),
                    "resource_format": resource.get("format"),
                    "resource_last_modified": resource.get("last_modified"),
                }

    for resource in resources:
        if resource.get("url"):
            return str(resource["url"]), {
                "ckan_package_api": api_url,
                "resource_id": resource.get("id"),
                "resource_name": resource.get("name"),
                "resource_format": resource.get("format"),
                "resource_last_modified": resource.get("last_modified"),
            }
    raise RuntimeError(f"No downloadable CKAN resource found for {source['key']}: {api_url}")


def resolve_url(source: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if source.get("resolver") == "ckan_package":
        return resolve_ckan_url(source)
    if source.get("url"):
        return str(source["url"]), {}
    raise RuntimeError(f"Source has no url or supported resolver: {source.get('key')}")


def download_file(url: str, target: Path, *, overwrite: bool, retries: int) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 0 and not overwrite:
        return {
            "status": "existing",
            "target": str(target),
            "size_bytes": target.stat().st_size,
            "downloaded_at": None,
            "headers": {},
        }

    tmp = target.with_suffix(target.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            print(f"GET {url}")
            with requests.get(url, headers=DEFAULT_HEADERS, stream=True, timeout=120) as response:
                response.raise_for_status()
                with tmp.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                for move_attempt in range(1, 8):
                    try:
                        tmp.replace(target)
                        break
                    except PermissionError:
                        if move_attempt == 7:
                            raise
                        time.sleep(0.75 * move_attempt)
                headers = {
                    "content_type": response.headers.get("Content-Type"),
                    "content_length": response.headers.get("Content-Length"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "etag": response.headers.get("ETag"),
                }
            return {
                "status": "downloaded",
                "target": str(target),
                "size_bytes": target.stat().st_size,
                "downloaded_at": utc_now(),
                "headers": headers,
            }
        except Exception as exc:  # noqa: BLE001 - CLI should report any source failure.
            last_error = exc
            print(f"WARN attempt {attempt}/{retries} failed for {url}: {exc}", file=sys.stderr)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Could not download {url}: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Download official crime data sources.")
    parser.add_argument("--config", default="config/crime_sources.json")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--manifest", default=None)
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_json(config_path)
    out_dir = Path(args.out_dir or config.get("raw_dir") or "data/raw/crime")
    manifest_path = Path(args.manifest) if args.manifest else out_dir / "download_manifest.json"

    entries = []
    ok = True
    for source in config.get("sources", []):
        key = source.get("key") or source.get("name") or "source"
        try:
            url, resolved = resolve_url(source)
            target = out_dir / str(source["target"])
            result = download_file(url, target, overwrite=args.overwrite, retries=args.retries)
            entry = {
                "key": key,
                "name": source.get("name"),
                "role": source.get("role"),
                "required": bool(source.get("required", True)),
                "url": url,
                "target": str(target),
                "notes": source.get("notes"),
                **resolved,
                **result,
            }
            entries.append(entry)
            print(f"{entry['status'].upper()} {target} ({entry['size_bytes']:,} bytes)")
        except Exception as exc:  # noqa: BLE001
            ok = False
            entries.append(
                {
                    "key": key,
                    "name": source.get("name"),
                    "role": source.get("role"),
                    "required": bool(source.get("required", True)),
                    "status": "error",
                    "error": str(exc),
                }
            )
            print(f"ERROR {key}: {exc}", file=sys.stderr)

    manifest = {
        "generated_at": utc_now(),
        "config": str(config_path),
        "raw_dir": str(out_dir),
        "sources": entries,
    }
    write_json(manifest_path, manifest)
    print(f"Wrote {manifest_path}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
