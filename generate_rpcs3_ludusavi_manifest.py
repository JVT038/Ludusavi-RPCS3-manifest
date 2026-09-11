#!/usr/bin/env python3
"""
Generate a Ludusavi manifest for PS3 games emulated with RPCS3.

This version groups PS3 Title IDs by the actual game relationships maintained
by SerialStation instead of treating every Title ID as a separate Ludusavi
game entry.

Example result:

    Uncharted 2: Among Thieves:
      files:
        <xdgConfig>/rpcs3/dev_hdd0/home/*/savedata/BCAS20097*:
          when: [{os: linux}]
          tags: [save]
        <xdgConfig>/rpcs3/dev_hdd0/home/*/savedata/BCUS98123*:
          when: [{os: linux}]
          tags: [save]
        ...

This means Ludusavi shows one game, while its `files` mapping covers every
known PS3 Title ID for that game. Each Title ID path ends in `*`, because RPCS3
save directory suffixes are game-defined (for example `_0`, `_1`, `_P`).

Data sources:
- SerialStation API: authoritative game <-> PS3 Title ID relationships.
- The original PS3 serial catalog is optionally merged as a coverage fallback.

Dependencies:
    pip install requests beautifulsoup4 pyyaml jsonschema

Examples:
    python generate_rpcs3_ludusavi_manifest.py -o rpcs3.yaml
    python generate_rpcs3_ludusavi_manifest.py --rpcs3-root /path/to/rpcs3
    python generate_rpcs3_ludusavi_manifest.py --only-existing-saves \
        --rpcs3-root /path/to/rpcs3
    python generate_rpcs3_ludusavi_manifest.py --no-catalog-fallback

Notes:
- SerialStation's new API is explicitly marked as not final, so the script
  validates its response shape and can cache the downloaded JSON.
- Demos are excluded by default (`content_type == Game` only). Use
  --include-demos if you want demo save data included.
- The manifest uses Ludusavi's standard OS placeholders. A portable/custom
  RPCS3 installation can be generated with --portable-root.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import requests
import yaml
from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator

CATALOG_URL = "https://bishalqx980.github.io/playstation/ps3/gameslist.html"
SERIALSTATION_API = "https://api.serialstation.com/v1"
SCHEMA_URL = "https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest/master/data/schema.yaml"
RPCS3_SAVE_SOURCE_URL = "https://github.com/RPCS3/rpcs3/blob/master/rpcs3/Emu/Cell/Modules/cellSaveData.cpp"
LUDUSAVI_README_URL = "https://github.com/mtkennerly/ludusavi-manifest/blob/master/README.md"
SERIALSTATION_API_DOCS_URL = "https://api.serialstation.com/v1/docs"

SERIAL_RE = re.compile(r"\b([A-Z]{4}\d{5})\b")
LINE_RE = re.compile(r"^\s*([A-Z]{4}\d{5})\s*=\s*(.*?)\s*$")
USER_AGENT = "rpcs3-ludusavi-manifest-generator/2.0"


@dataclass(frozen=True)
class CatalogEntry:
    serial: str
    title: str


@dataclass
class GameGroup:
    """One logical game as represented by SerialStation or a catalog fallback."""

    key: str
    title: str
    serials: set[str] = field(default_factory=set)
    serialstation_id: str | None = None
    source: str = "serialstation"


def normalize_title(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "Unknown PS3 game"


def title_key(value: str) -> str:
    """Normalize titles for conservative exact-name fallback matching."""
    value = normalize_title(value).casefold()
    value = re.sub(r"[™®©]", "", value)
    value = re.sub(r"[’'`\"]", "", value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def fetch_json(session: requests.Session, url: str, params: dict, timeout: float) -> dict:
    response = session.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object from {response.url}")
    return data


def fetch_catalog(session: requests.Session, url: str, timeout: float) -> str:
    response = session.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def parse_catalog(raw_html: str) -> list[CatalogEntry]:
    """Parse the published serial = title list, tolerating minor HTML changes."""
    entries: "OrderedDict[str, CatalogEntry]" = OrderedDict()
    soup = BeautifulSoup(raw_html, "html.parser")

    text = soup.get_text("\n", strip=False)
    for raw_line in text.splitlines():
        line = normalize_title(raw_line)
        match = LINE_RE.match(line)
        if match:
            serial, title = match.groups()
            entries.setdefault(serial, CatalogEntry(serial, normalize_title(title)))

    for element in soup.find_all(["li", "td", "div", "p", "option", "a"]):
        line = normalize_title(element.get_text(" ", strip=True))
        match = LINE_RE.match(line)
        if match:
            serial, title = match.groups()
            entries.setdefault(serial, CatalogEntry(serial, normalize_title(title)))

    if not entries:
        flattened = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.I)
        flattened = re.sub(r"</(?:p|div|li|tr)>", "\n", flattened, flags=re.I)
        flattened = BeautifulSoup(flattened, "html.parser").get_text("\n")
        for raw_line in flattened.splitlines():
            line = normalize_title(raw_line)
            match = LINE_RE.match(line)
            if match:
                serial, title = match.groups()
                entries.setdefault(serial, CatalogEntry(serial, normalize_title(title)))

    if not entries:
        visible = soup.get_text(" ", strip=True)
        for match in re.finditer(
            r"\b([A-Z]{4}\d{5})\s*=\s*([^=]{1,300}?)(?=\b[A-Z]{4}\d{5}\s*=|$)",
            visible,
        ):
            serial = match.group(1)
            title = normalize_title(match.group(2))
            entries.setdefault(serial, CatalogEntry(serial, title))

    return list(entries.values())


def parse_serialstation_title_ids(data: dict, *, include_demos: bool) -> list[dict]:
    items = data.get("items")
    count = data.get("count")
    if not isinstance(items, list) or not isinstance(count, int):
        raise ValueError("SerialStation /title-ids response does not have the expected items/count shape")

    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title_id = item.get("title_id")
        content_type = str(item.get("content_type", "")).strip().casefold()
        systems = item.get("systems")
        games = item.get("games")
        if not isinstance(title_id, str) or not SERIAL_RE.fullmatch(title_id):
            continue
        if not isinstance(systems, list) or not any(str(s).casefold() in {"playstation 3", "ps3"} for s in systems):
            continue
        if not include_demos and content_type != "game":
            continue
        if not isinstance(games, list):
            games = []
        result.append(
            {
                "title_id": title_id.upper(),
                "name": normalize_title(str(item.get("name") or "Unknown PS3 game")),
                "content_type": content_type,
                "games": games,
            }
        )
    return result


def fetch_serialstation_title_ids(
    session: requests.Session,
    *,
    timeout: float,
    cache_path: Path | None,
    include_demos: bool,
) -> list[dict]:
    if cache_path and cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cached, dict):
            raise ValueError(f"SerialStation cache {cache_path} is not a JSON object")
        print(f"Using cached SerialStation data: {cache_path}")
        return parse_serialstation_title_ids(cached, include_demos=include_demos)

    all_items: list[dict] = []
    offset = 0
    limit = 100
    total: int | None = None

    print("Downloading PS3 Title ID relationships from SerialStation API...")
    while True:
        data = fetch_json(
            session,
            f"{SERIALSTATION_API}/title-ids/",
            {"system": "PS3", "limit": limit, "offset": offset},
            timeout,
        )
        items = data.get("items")
        count = data.get("count")
        if not isinstance(items, list) or not isinstance(count, int):
            raise ValueError("SerialStation /title-ids response does not have the expected items/count shape")
        if total is None:
            total = count
            print(f"SerialStation reports {total} PS3 Title IDs")
        all_items.extend(items)
        offset += len(items)
        if not items or offset >= count:
            break
        if len(items) < limit:
            break
        print(f"  fetched {offset}/{count} Title IDs")

    combined = {"items": all_items, "count": len(all_items)}
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote SerialStation cache: {cache_path}")

    return parse_serialstation_title_ids(combined, include_demos=include_demos)


def group_serialstation_games(title_ids: Iterable[dict]) -> tuple[list[GameGroup], dict[str, str]]:
    """Group Title IDs by SerialStation game UUID.

    Returns groups plus a title-id -> group-key map. A title ID can technically
    be associated with more than one game in the API, so it is added to every
    associated game rather than silently choosing one.
    """
    groups: "OrderedDict[str, GameGroup]" = OrderedDict()
    serial_to_group: dict[str, str] = {}

    for item in title_ids:
        serial = item["title_id"]
        api_games = item.get("games") or []
        valid_games = [g for g in api_games if isinstance(g, dict) and g.get("id") and g.get("name")]

        if not valid_games:
            # We cannot safely invent a UUID. Keep the Title ID available to a
            # later catalog fallback instead.
            continue

        for api_game in valid_games:
            game_id = str(api_game["id"])
            title = normalize_title(str(api_game["name"]))
            group = groups.get(game_id)
            if group is None:
                group = GameGroup(key=game_id, title=title, serialstation_id=game_id)
                groups[game_id] = group
            group.serials.add(serial)
            serial_to_group[serial] = game_id

    return list(groups.values()), serial_to_group


def merge_catalog_fallback(
    groups: list[GameGroup],
    catalog: Iterable[CatalogEntry],
    serial_to_group: dict[str, str],
) -> tuple[list[GameGroup], list[str], list[str]]:
    """Merge catalog-only IDs conservatively.

    Known SerialStation IDs are always retained. Catalog-only IDs are first
    attached to a SerialStation group with the same normalized title. If no
    exact title match exists, a new fallback group is created. This prevents
    fuzzy matching from accidentally merging unrelated games.
    """
    by_key = {g.key: g for g in groups}
    by_title: dict[str, list[GameGroup]] = defaultdict(list)
    for group in groups:
        by_title[title_key(group.title)].append(group)

    added_to_existing: list[str] = []
    fallback_groups: list[str] = []

    for entry in catalog:
        serial = entry.serial.upper()
        if serial in serial_to_group:
            continue

        candidates = by_title.get(title_key(entry.title), [])
        if len(candidates) == 1:
            candidates[0].serials.add(serial)
            serial_to_group[serial] = candidates[0].key
            added_to_existing.append(serial)
            continue

        # If there are multiple SerialStation games with the same title, do
        # not guess which one owns the catalog-only ID.
        key = f"catalog:{serial}"
        group = GameGroup(
            key=key,
            title=entry.title,
            serials={serial},
            source="catalog-fallback",
        )
        by_key[key] = group
        groups.append(group)
        serial_to_group[serial] = key
        fallback_groups.append(serial)

    return groups, added_to_existing, fallback_groups


def discover_local_saves(rpcs3_root: Path) -> dict[str, list[Path]]:
    """Return RPCS3 save directories grouped by Title ID prefix."""
    savedata_root = rpcs3_root / "dev_hdd0" / "home"
    found: dict[str, list[Path]] = defaultdict(list)
    if not savedata_root.is_dir():
        return dict(found)

    for user_dir in sorted(savedata_root.iterdir()):
        save_root = user_dir / "savedata"
        if not save_root.is_dir():
            continue
        for entry in sorted(save_root.iterdir()):
            if not entry.is_dir():
                continue
            match = SERIAL_RE.match(entry.name.upper())
            if match:
                found[match.group(1)].append(entry)
    return dict(found)


def filter_groups_to_local(groups: list[GameGroup], local: dict[str, list[Path]]) -> list[GameGroup]:
    local_serials = set(local)
    result = []
    for group in groups:
        matched = group.serials & local_serials
        if matched:
            # Do not emit paths for known Title IDs that do not exist locally
            # when --only-existing-saves is requested.
            result.append(
                GameGroup(
                    key=group.key,
                    title=group.title,
                    serials=matched,
                    serialstation_id=group.serialstation_id,
                    source=group.source,
                )
            )
    return result


def manifest_for_games(
    groups: Iterable[GameGroup],
    *,
    include_linux: bool,
    include_windows: bool,
    include_mac: bool,
    portable_root: str | None,
) -> OrderedDict:
    manifest: OrderedDict[str, dict] = OrderedDict()
    used_names: set[str] = set()

    for group in sorted(groups, key=lambda g: (g.title.casefold(), g.key)):
        name = group.title
        if name in used_names:
            # Same display name but distinct SerialStation games: retain both
            # rather than silently overwriting a YAML mapping key.
            if group.serialstation_id:
                name = f"{name} [{group.serialstation_id}]"
            else:
                name = f"{name} [catalog fallback]"
        used_names.add(name)

        files: OrderedDict[str, dict] = OrderedDict()
        for serial in sorted(group.serials):
            if portable_root:
                path = str(
                    Path(portable_root).expanduser()
                    / "dev_hdd0"
                    / "home"
                    / "*"
                    / "savedata"
                    / f"{serial}*"
                )
                files[path] = {"tags": ["save"]}
            else:
                files[f"<root>/dev_hdd0/home/*/savedata/{serial}*"] = {
                    "tags": ["save"],
                }
                files[f"<root>/custom_configs/config_{serial}.yml"] = {
                    "tags": ["config"],
                }

        manifest[name] = {
            "files": files,
            "notes": [
                {
                    "message": (
                        f"RPCS3 PS3 savedata for {len(group.serials)} known Title ID(s). "
                        "Each Title ID uses a trailing wildcard because the save directory suffix is game-defined."
                    )
                }
            ],
        }

    return manifest


def dump_yaml(manifest: OrderedDict, output: Path) -> None:
    class Dumper(yaml.SafeDumper):
        pass

    def represent_ordered_dict(dumper, data):
        return dumper.represent_dict(data.items())

    Dumper.add_representer(OrderedDict, represent_ordered_dict)
    rendered = yaml.dump(
        manifest,
        Dumper=Dumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


def validate_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Generated YAML root must be a mapping/object")

    for game_name, entry in data.items():
        if not isinstance(game_name, str) or not isinstance(entry, dict):
            raise ValueError(f"Invalid game entry {game_name!r}")
        files = entry.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError(f"Entry {game_name!r} must contain a non-empty files mapping")
        for path, details in files.items():
            if not isinstance(path, str) or not path:
                raise ValueError(f"Invalid file path in {game_name!r}")
            if not isinstance(details, dict):
                raise ValueError(f"File rule {path!r} in {game_name!r} is not a mapping")
            if "tags" in details and not isinstance(details["tags"], list):
                raise ValueError(f"tags for {game_name!r}/{path!r} must be a list")
            if "when" in details and not isinstance(details["when"], list):
                raise ValueError(f"when for {game_name!r}/{path!r} must be a list")
    return data


def validate_against_official_schema(data: dict, *, schema_url: str, timeout: float) -> None:
    response = requests.get(
        schema_url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/plain,text/yaml,*/*"},
        timeout=timeout,
    )
    response.raise_for_status()
    schema = yaml.safe_load(response.text)
    if not isinstance(schema, dict):
        raise ValueError("Downloaded Ludusavi schema is not a YAML mapping")

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        details = []
        for error in errors[:10]:
            location = "/".join(str(part) for part in error.absolute_path) or "<root>"
            details.append(f"{location}: {error.message}")
        suffix = "" if len(errors) <= 10 else f" (showing first 10 of {len(errors)})"
        raise ValueError(
            "Generated manifest does not validate against Ludusavi schema"
            + suffix
            + ":\n  "
            + "\n  ".join(details)
        )


def print_summary(
    groups: list[GameGroup],
    catalog: list[CatalogEntry],
    local: dict[str, list[Path]] | None,
    *,
    serialstation_title_ids: int,
    catalog_fallback_count: int,
    include_demos: bool,
) -> None:
    serial_count = sum(len(g.serials) for g in groups)
    print(f"Logical game entries: {len(groups)}")
    print(f"Known PS3 Title IDs represented: {serial_count}")
    print(f"SerialStation Title IDs considered: {serialstation_title_ids}")
    print(f"Original catalog entries: {len(catalog)}")
    print(f"Catalog-only fallback Title IDs: {catalog_fallback_count}")
    print(f"Demo Title IDs included: {'yes' if include_demos else 'no'}")

    if local is not None:
        local_serials = set(local)
        known_serials = {s for g in groups for s in g.serials}
        print(f"Local RPCS3 Title IDs with save directories: {len(local)}")
        print(f"Local Title IDs represented by generated groups: {len(local_serials & known_serials)}")
        unknown = sorted(local_serials - known_serials)
        if unknown:
            print("\nLocal Title IDs not represented in the generated manifest:")
            for serial in unknown:
                for path in local[serial]:
                    print(f"  {serial}: {path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", type=Path, default=Path("rpcs3.yaml"))
    parser.add_argument("--catalog-url", default=CATALOG_URL)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--serialstation-cache",
        type=Path,
        help="Optional JSON cache for the SerialStation /title-ids/ response.",
    )
    parser.add_argument(
        "--catalog-cache",
        type=Path,
        help="Optional HTML cache for the original PS3 serial catalog.",
    )
    parser.add_argument("--no-cache-write", action="store_true")
    parser.add_argument(
        "--no-catalog-fallback",
        action="store_true",
        help="Use only SerialStation game relationships; do not merge catalog-only Title IDs.",
    )
    parser.add_argument(
        "--include-demos",
        action="store_true",
        help="Include SerialStation Title IDs whose content_type is Demo.",
    )
    parser.add_argument(
        "--rpcs3-root",
        type=Path,
        help="Optional RPCS3 root to inspect locally (directory containing dev_hdd0).",
    )
    parser.add_argument(
        "--portable-root",
        type=str,
        help="Generate a machine-specific manifest for this RPCS3 root instead of standard OS paths.",
    )
    parser.add_argument(
        "--only-existing-saves",
        action="store_true",
        help="Only emit Title IDs for save directories currently found under --rpcs3-root.",
    )
    parser.add_argument("--no-linux", action="store_true")
    parser.add_argument("--no-windows", action="store_true")
    parser.add_argument("--no-mac", action="store_true")
    parser.add_argument("--no-schema-validation", action="store_true")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.only_existing_saves and not args.rpcs3_root:
        parser.error("--only-existing-saves requires --rpcs3-root")
    if not args.portable_root and args.no_linux and args.no_windows and args.no_mac:
        parser.error("All OS rules are disabled")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    try:
        # 1. SerialStation: Title ID -> actual game UUID/name.
        title_ids = fetch_serialstation_title_ids(
            session,
            timeout=args.timeout,
            cache_path=args.serialstation_cache,
            include_demos=args.include_demos,
        )
        groups, serial_to_group = group_serialstation_games(title_ids)
        print(f"SerialStation logical games found: {len(groups)}")

        # 2. Original catalog: coverage fallback for IDs SerialStation does not
        # know yet. This is deliberately conservative: exact title match only.
        catalog: list[CatalogEntry] = []
        catalog_fallback_count = 0
        added_to_existing = 0
        if not args.no_catalog_fallback:
            if args.catalog_cache and args.catalog_cache.is_file():
                raw_html = args.catalog_cache.read_text(encoding="utf-8", errors="replace")
                print(f"Using cached PS3 catalog: {args.catalog_cache}")
            else:
                print(f"Downloading PS3 serial catalog: {args.catalog_url}")
                raw_html = fetch_catalog(session, args.catalog_url, args.timeout)
                if args.catalog_cache and not args.no_cache_write:
                    args.catalog_cache.parent.mkdir(parents=True, exist_ok=True)
                    args.catalog_cache.write_text(raw_html, encoding="utf-8")
                    print(f"Wrote PS3 catalog cache: {args.catalog_cache}")
            catalog = parse_catalog(raw_html)
            if not catalog:
                raise RuntimeError("No PS3 serials were parsed from the catalog")
            groups, added, fallback = merge_catalog_fallback(groups, catalog, serial_to_group)
            added_to_existing = len(added)
            catalog_fallback_count = len(fallback)
            print(f"Catalog-only IDs attached to an existing game by exact title: {added_to_existing}")
            print(f"Catalog-only IDs requiring fallback game entries: {catalog_fallback_count}")

        # 3. Optional local scan.
        local: dict[str, list[Path]] | None = None
        if args.rpcs3_root:
            local = discover_local_saves(args.rpcs3_root.expanduser().resolve())
            print(f"Found {len(local)} local RPCS3 Title IDs with save directories")
            if args.only_existing_saves:
                groups = filter_groups_to_local(groups, local)
                print(f"Filtered to {len(groups)} logical games with existing saves")

        if not groups:
            raise RuntimeError("No logical games remain after filtering")

        manifest = manifest_for_games(
            groups,
            include_linux=not args.no_linux,
            include_windows=not args.no_windows,
            include_mac=not args.no_mac,
            portable_root=args.portable_root,
        )
        dump_yaml(manifest, args.output)

        parsed_manifest = validate_yaml(args.output)
        if not args.no_schema_validation:
            validate_against_official_schema(parsed_manifest, schema_url=SCHEMA_URL, timeout=args.timeout)
            print("Official Ludusavi schema validation: OK")

        print_summary(
            groups,
            catalog,
            local,
            serialstation_title_ids=len(title_ids),
            catalog_fallback_count=catalog_fallback_count,
            include_demos=args.include_demos,
        )
        print(f"\nGenerated {len(manifest)} Ludusavi game entries: {args.output}")
        print("YAML validation: OK")
        print("\nReferences:")
        print(f"  SerialStation API: {SERIALSTATION_API_DOCS_URL}")
        print(f"  PS3 catalog:       {args.catalog_url}")
        print(f"  Ludusavi:          {LUDUSAVI_README_URL}")
        print(f"  Schema:             {SCHEMA_URL}")
        print(f"  RPCS3 save source:  {RPCS3_SAVE_SOURCE_URL}")
        return 0

    except requests.RequestException as exc:
        print(f"ERROR: network request failed: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
