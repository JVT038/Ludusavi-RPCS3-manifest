#!/usr/bin/env python3
"""
Generate a Ludusavi manifest for PS3 games emulated with RPCS3.

The script downloads the published PS3 serial/game list, maps PS3 title IDs
(e.g. BCUS98123) to game names, and writes a Ludusavi-compatible YAML
manifest whose RPCS3 save paths use the title-ID prefix:

    .../dev_hdd0/home/*/savedata/BCUS98123*

This deliberately matches all savedata directories beginning with the serial,
so game-specific suffixes such as _0, _1, _P, or other names are included.

Dependencies:
    pip install requests beautifulsoup4 pyyaml jsonschema

Examples:
    python generate_rpcs3_ludusavi_manifest.py -o rpcs3.yaml
    python generate_rpcs3_ludusavi_manifest.py --rpcs3-root /path/to/rpcs3
    python generate_rpcs3_ludusavi_manifest.py --only-existing-saves \
        --rpcs3-root /path/to/rpcs3

Notes:
- The manifest contains entries for every PS3 serial discovered in the
  published catalog by default. This is intentional: Ludusavi evaluates the
  paths as globs when scanning and will only find entries that exist locally.
- Standard RPCS3 configuration roots are represented with Ludusavi's built-in
  OS placeholders. A portable/custom RPCS3 directory cannot be represented by
  a portable custom placeholder in a generic manifest, so the standard OS
  locations are used unless --portable-root is requested.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
import yaml
from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator

CATALOG_URL = "https://bishalqx980.github.io/playstation/ps3/gameslist.html"
SCHEMA_URL = "https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest/master/data/schema.yaml"
RPCS3_SAVE_SOURCE_URL = "https://github.com/RPCS3/rpcs3/blob/master/rpcs3/Emu/Cell/Modules/cellSaveData.cpp"
LUDUSAVI_README_URL = "https://github.com/mtkennerly/ludusavi-manifest/blob/master/README.md"

# The published list uses serials such as BCUS98123, BLES00932, BCES00065, etc.
# Keep the pattern strict enough to avoid accidental matches in prose.
SERIAL_RE = re.compile(r"\b([A-Z]{4}\d{5})\b")
LINE_RE = re.compile(r"^\s*([A-Z]{4}\d{5})\s*=\s*(.*?)\s*$")

USER_AGENT = "rpcs3-ludusavi-manifest-generator/1.0"


@dataclass(frozen=True)
class Game:
    serial: str
    title: str


def fetch_catalog(url: str, timeout: float) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def normalize_title(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    # Avoid YAML surprises from an empty title while preserving the original
    # catalog wording as much as practical.
    return value or "Unknown PS3 game"


def parse_catalog(raw_html: str) -> list[Game]:
    """Parse the catalog robustly, handling both the current text-like page and tables."""
    games: "OrderedDict[str, Game]" = OrderedDict()

    soup = BeautifulSoup(raw_html, "html.parser")

    # First pass: table/list rows, preserving their text.
    text = soup.get_text("\n", strip=False)
    for raw_line in text.splitlines():
        line = normalize_title(raw_line)
        match = LINE_RE.match(line)
        if match:
            serial, title = match.groups()
            games.setdefault(serial, Game(serial=serial, title=normalize_title(title)))

    # Second pass: some versions of the page may put entries inside HTML
    # elements without a clean one-entry-per-line text representation.
    for element in soup.find_all(["li", "td", "div", "p", "option", "a"]):
        line = normalize_title(element.get_text(" ", strip=True))
        match = LINE_RE.match(line)
        if match:
            serial, title = match.groups()
            games.setdefault(serial, Game(serial=serial, title=normalize_title(title)))

    # Third pass: fallback over raw textual source. This handles minor markup
    # changes such as <br> without making the parser dependent on one DOM shape.
    if not games:
        flattened = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.I)
        flattened = re.sub(r"</(?:p|div|li|tr)>", "\n", flattened, flags=re.I)
        flattened = BeautifulSoup(flattened, "html.parser").get_text("\n")
        for raw_line in flattened.splitlines():
            line = normalize_title(raw_line)
            match = LINE_RE.match(line)
            if match:
                serial, title = match.groups()
                games.setdefault(serial, Game(serial=serial, title=normalize_title(title)))

    # Final fallback: find serial/title pairs in the raw text. We only use a
    # restricted context after '=' so random serial mentions are not promoted.
    if not games:
        visible = normalize_title(soup.get_text(" ", strip=True))
        for match in re.finditer(r"\b([A-Z]{4}\d{5})\s*=\s*([^=]{1,300}?)(?=\b[A-Z]{4}\d{5}\s*=|$)", visible):
            serial = match.group(1)
            title = normalize_title(match.group(2))
            games.setdefault(serial, Game(serial=serial, title=title))

    return list(games.values())


def yaml_quote_key(value: str) -> str:
    # We emit quoted keys consistently because game names can contain ':', '#',
    # quotes, apostrophes, brackets, etc.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def manifest_for_games(
    games: Iterable[Game],
    *,
    include_linux: bool,
    include_windows: bool,
    include_mac: bool,
    portable_root: str | None,
) -> OrderedDict:
    manifest: OrderedDict[str, dict] = OrderedDict()

    for game in games:
        # Put the serial in the manifest title. This keeps different regional
        # releases/editions from colliding when the catalog uses the same name.
        key = f"{game.title} [{game.serial}]"

        files: OrderedDict[str, dict] = OrderedDict()

        if portable_root:
            # A user-supplied portable path is inherently machine-specific.
            # Use it as an unrestricted path, since the user explicitly asked
            # for a single RPCS3 installation rather than standard locations.
            path = str(Path(portable_root).expanduser() / "dev_hdd0" / "home" / "*" / "savedata" / f"{game.serial}*")
            files[path] = {"tags": ["save"]}
        else:
            files[f"<root>/dev_hdd0/home/*/savedata/{game.serial}*"] = {
                "when": [{"os": "linux"}],
                "tags": ["save"],
            }
        manifest[key] = {
            "files": files,
            "notes": [
                {
                    "message": (
                        f"RPCS3 PS3 savedata matched by title ID prefix {game.serial}. "
                        "The trailing directory name is game-defined; the wildcard intentionally covers all matching save directories."
                    )
                }
            ],
        }

    return manifest


def dump_yaml(manifest: OrderedDict, output: Path) -> None:
    class LiteralDumper(yaml.SafeDumper):
        pass

    # Preserve insertion order (important for deterministic output).
    def represent_ordered_dict(dumper, data):
        return dumper.represent_dict(data.items())

    LiteralDumper.add_representer(OrderedDict, represent_ordered_dict)

    rendered = yaml.dump(
        manifest,
        Dumper=LiteralDumper,
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

    # Structural checks mirroring the parts of Ludusavi's generic schema that
    # matter for this generator. The official schema intentionally permits
    # arbitrary OS/store strings, so we do not over-constrain those fields.
    for game_name, entry in data.items():
        if not isinstance(game_name, str):
            raise ValueError("Every game key must be a string")
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {game_name!r} is not a mapping")
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
            if "when" in details:
                when = details["when"]
                if not isinstance(when, list):
                    raise ValueError(f"when for {game_name!r}/{path!r} must be a list")
                for constraint in when:
                    if not isinstance(constraint, dict):
                        raise ValueError(f"invalid when constraint for {game_name!r}/{path!r}")

    return data


def validate_against_official_schema(
    data: dict,
    *,
    schema_url: str,
    timeout: float,
) -> None:
    """Validate the generated manifest against Ludusavi's current generic schema."""
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
            "Generated manifest does not validate against Ludusavi schema" + suffix + ":\n  " + "\n  ".join(details)
        )



def discover_local_saves(rpcs3_root: Path) -> dict[str, list[Path]]:
    """Return save directories grouped by detected PS3 title ID prefix."""
    savedata_root = rpcs3_root / "dev_hdd0" / "home"
    found: dict[str, list[Path]] = {}
    if not savedata_root.is_dir():
        return found

    for user_dir in sorted(savedata_root.iterdir()):
        save_root = user_dir / "savedata"
        if not save_root.is_dir():
            continue
        for entry in sorted(save_root.iterdir()):
            if not entry.is_dir():
                continue
            match = SERIAL_RE.match(entry.name)
            if match:
                found.setdefault(match.group(1), []).append(entry)
    return found


def print_summary(games: list[Game], local: dict[str, list[Path]] | None) -> None:
    print(f"Catalog entries: {len(games)}")
    if local is None:
        return

    serials_with_saves = set(local)
    matched = sum(1 for game in games if game.serial in serials_with_saves)
    unmatched_local = sorted(serials_with_saves - {game.serial for game in games})
    print(f"Local RPCS3 serials with save directories: {len(local)}")
    print(f"Catalog serials represented locally: {matched}")

    if local:
        print("\nDetected local save directories:")
        for serial in sorted(local):
            for path in local[serial]:
                print(f"  {serial}: {path}")
    if unmatched_local:
        print("\nLocal serials not found in catalog:")
        for serial in unmatched_local:
            print(f"  {serial}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", type=Path, default=Path("rpcs3.yaml"), help="Output YAML file (default: rpcs3.yaml)")
    parser.add_argument("--catalog-url", default=CATALOG_URL, help=f"PS3 catalog URL (default: {CATALOG_URL})")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default: 30)")
    parser.add_argument(
        "--rpcs3-root",
        type=Path,
        help="Optional RPCS3 root to inspect locally (the directory containing dev_hdd0).",
    )
    parser.add_argument(
        "--portable-root",
        type=str,
        help="Generate a machine-specific manifest for this RPCS3 root instead of standard OS paths.",
    )
    parser.add_argument(
        "--only-existing-saves",
        action="store_true",
        help="Only generate manifest entries for serials that currently have a local RPCS3 save directory.",
    )
    parser.add_argument("--no-linux", action="store_true", help="Do not emit Linux rules")
    parser.add_argument("--no-windows", action="store_true", help="Do not emit Windows rules")
    parser.add_argument("--no-mac", action="store_true", help="Do not emit macOS rules")
    parser.add_argument(
        "--catalog-cache",
        type=Path,
        help="Optional local HTML cache. If present it is used instead of downloading; otherwise it is written after download.",
    )
    parser.add_argument("--no-cache-write", action="store_true", help="Do not write a catalog cache")
    parser.add_argument("--no-schema-validation", action="store_true", help="Skip validation against the current official Ludusavi schema")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        # A portable root makes sense only as a local/machine-specific manifest.
        if args.portable_root and args.only_existing_saves and not args.rpcs3_root:
            parser.error("--only-existing-saves requires --rpcs3-root")

        raw_html: str
        if args.catalog_cache and args.catalog_cache.is_file():
            raw_html = args.catalog_cache.read_text(encoding="utf-8", errors="replace")
            print(f"Using cached catalog: {args.catalog_cache}")
        else:
            print(f"Downloading PS3 catalog: {args.catalog_url}")
            raw_html = fetch_catalog(args.catalog_url, args.timeout)
            if args.catalog_cache and not args.no_cache_write:
                args.catalog_cache.parent.mkdir(parents=True, exist_ok=True)
                args.catalog_cache.write_text(raw_html, encoding="utf-8")
                print(f"Wrote catalog cache: {args.catalog_cache}")

        games = parse_catalog(raw_html)
        if not games:
            raise RuntimeError(
                "No PS3 serials were parsed from the catalog. The site format may have changed; "
                f"inspect {args.catalog_url} or provide --catalog-cache."
            )

        local: dict[str, list[Path]] | None = None
        if args.rpcs3_root:
            local = discover_local_saves(args.rpcs3_root.expanduser().resolve())

        if args.only_existing_saves:
            serials = set(local or {})
            games = [game for game in games if game.serial in serials]
            print(f"Filtered to {len(games)} catalog entries with existing local saves.")

        if not games:
            raise RuntimeError("No games remain after filtering.")

        manifest = manifest_for_games(
            games,
            include_linux=not args.no_linux,
            include_windows=not args.no_windows,
            include_mac=not args.no_mac,
            portable_root=args.portable_root,
        )

        # Fail early rather than generating a useless file when every platform
        # output has accidentally been disabled.
        if not manifest:
            raise RuntimeError("Manifest would be empty.")
        if not args.portable_root and args.no_linux and args.no_windows and args.no_mac:
            raise RuntimeError("All OS rules are disabled. Remove one of --no-linux/--no-windows/--no-mac.")

        dump_yaml(manifest, args.output)
        parsed_manifest = validate_yaml(args.output)
        if not args.no_schema_validation:
            validate_against_official_schema(parsed_manifest, schema_url=SCHEMA_URL, timeout=args.timeout)
            print("Official Ludusavi schema validation: OK")

        print_summary(games, local)
        print(f"\nGenerated {len(manifest)} Ludusavi entries: {args.output}")
        print("YAML validation: OK")
        print("\nRelevant references:")
        print(f"  Catalog:   {args.catalog_url}")
        print(f"  Ludusavi:  {LUDUSAVI_README_URL}")
        print(f"  Schema:    {SCHEMA_URL}")
        print(f"  RPCS3:     {RPCS3_SAVE_SOURCE_URL}")
        return 0

    except requests.RequestException as exc:
        print(f"ERROR: could not download catalog: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
