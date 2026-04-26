from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlencode

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


LANGUAGE_CODE_DEFAULT = os.getenv("LANGUAGE_CODE", "en")
MARKET_CODE_DEFAULT = os.getenv("MARKET_CODE", "")
LOCALE_CODE_DEFAULT = os.getenv("LOCALE_CODE", "en-US")
TIMEZONE_NAME_DEFAULT = os.getenv("TIMEZONE_NAME", "UTC")
VALUE_LABEL_DEFAULT = os.getenv("VALUE_LABEL", "VALUE")
INPUT_VALUE_TOKEN_DEFAULT = os.getenv("INPUT_VALUE_TOKEN", "VAL")
SOURCE_BASE_URL = os.getenv("SOURCE_BASE_URL", "")
SOURCE_PATH_TEMPLATE = os.getenv("SOURCE_PATH_TEMPLATE", "")
SUMMARY_SECTION_HEADER = os.getenv("SUMMARY_SECTION_HEADER", "")
PRIMARY_MATCH_LABEL = os.getenv("PRIMARY_MATCH_LABEL", "")
SUMMARY_LABELS = {
    item.strip()
    for item in os.getenv("SUMMARY_LABELS", "").split("||")
    if item.strip()
}
SECTION_BREAK_PREFIXES = tuple(
    item.strip()
    for item in os.getenv("SECTION_BREAK_PREFIXES", "").split("||")
    if item.strip()
)
PRICE_RE = re.compile(
    rf"(?:from\s+)?(?:{re.escape(INPUT_VALUE_TOKEN_DEFAULT)}|[A-Z]{{3}})\s*([0-9,]+)"
)


@dataclass(frozen=True)
class SummaryEntry:
    label: str
    value: int
    source: str
    details: str


@dataclass(frozen=True)
class QuerySnapshot:
    input_a: str
    input_b: str
    url: str
    entries: list[SummaryEntry]


@dataclass(frozen=True)
class MatchRecord:
    input_a: str
    input_b: str
    source: str
    value: int
    details: str
    reference_url: str


def parse_list_env(name: str) -> list[str]:
    raw_value = os.getenv(name, "")
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def parse_int_env(name: str, fallback: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return fallback
    return int(raw_value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a scheduled page check and print matches that meet configured thresholds."
    )
    parser.add_argument(
        "--inputs-a",
        nargs="+",
        default=parse_list_env("QUERY_INPUTS_A"),
        metavar="KEY",
        help="First query input set. Falls back to QUERY_INPUTS_A when set.",
    )
    parser.add_argument(
        "--inputs-b",
        nargs="+",
        default=parse_list_env("QUERY_INPUTS_B"),
        metavar="KEY",
        help="Second query input set. Falls back to QUERY_INPUTS_B when set.",
    )
    parser.add_argument(
        "--filters",
        nargs="+",
        default=parse_list_env("FILTER_TOKENS"),
        metavar="TOKEN",
        help="Optional source-name filters. Falls back to FILTER_TOKENS when set.",
    )
    parser.add_argument(
        "--min-threshold",
        type=int,
        default=parse_int_env("MIN_THRESHOLD", 0),
        help="Minimum value to show. Falls back to MIN_THRESHOLD when set.",
    )
    parser.add_argument(
        "--max-threshold",
        type=int,
        default=parse_int_env("MAX_THRESHOLD", 999999),
        help="Maximum value to show. Falls back to MAX_THRESHOLD when set.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=12,
        help="How many matching records to print. Default: 12",
    )
    parser.add_argument(
        "--watch-minutes",
        type=float,
        default=0.0,
        help="Refresh every N minutes until interrupted. Default: 0 (run once)",
    )
    parser.add_argument(
        "--show-summary",
        action="store_true",
        help="Also print the parsed summary entries for each query combination.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Open Chromium visibly instead of headless mode for debugging.",
    )
    parser.add_argument(
        "--json-output",
        help="Optional file path to write a machine-readable JSON report.",
    )
    return parser.parse_args()


def format_key(key: str) -> str:
    return key.replace("-", " ").title()


def normalize_line(raw_line: str) -> str:
    line = raw_line.replace("\xa0", " ").strip()
    line = line.replace("\u20b1", f"{INPUT_VALUE_TOKEN_DEFAULT} ")
    line = line.replace("\u2014", " - ")
    line = line.replace("\u2013", " - ")
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def parse_value(line: str) -> int | None:
    match = PRICE_RE.search(line)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def collect_lines(body_text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in body_text.splitlines():
        line = normalize_line(raw_line)
        if not line or line.startswith("[Image"):
            continue
        lines.append(line)
    return lines


def find_section(lines: list[str], prefix: str) -> int | None:
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            return index
    return None


def parse_summary_entries(lines: list[str]) -> list[SummaryEntry]:
    section_index = find_section(lines, SUMMARY_SECTION_HEADER)
    if section_index is None:
        return []

    entries: list[SummaryEntry] = []
    index = section_index + 1
    while index < len(lines):
        line = lines[index]
        if any(line.startswith(prefix) for prefix in SECTION_BREAK_PREFIXES if prefix != "Popular airlines from"):
            break
        if line not in SUMMARY_LABELS:
            index += 1
            continue

        value = parse_value(lines[index + 1]) if index + 1 < len(lines) else None
        source = lines[index + 2] if index + 2 < len(lines) else "Unknown"
        detail_parts: list[str] = []
        cursor = index + 3
        while cursor < len(lines):
            next_line = lines[cursor]
            if next_line in SUMMARY_LABELS or any(next_line.startswith(prefix) for prefix in SECTION_BREAK_PREFIXES):
                break
            if next_line.startswith("The ") or next_line.startswith("View "):
                break
            detail_parts.append(next_line)
            cursor += 1

        if value is not None:
            entries.append(
                SummaryEntry(
                    label=line,
                    value=value,
                    source=source,
                    details=" | ".join(detail_parts),
                )
            )
        index = cursor

    return entries


def build_query_url(input_a: str, input_b: str) -> str:
    query_params = {"hl": LANGUAGE_CODE_DEFAULT}
    if MARKET_CODE_DEFAULT:
        query_params["gl"] = MARKET_CODE_DEFAULT
    path = SOURCE_PATH_TEMPLATE.format(input_a=input_a, input_b=input_b)
    return f"{SOURCE_BASE_URL.rstrip('/')}/{path.lstrip('/')}?{urlencode(query_params)}"


def fetch_snapshot(page, input_a: str, input_b: str) -> QuerySnapshot:
    url = build_query_url(input_a, input_b)

    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3500)
    body_text = page.locator("body").inner_text(timeout=10000)
    lines = collect_lines(body_text)

    display_a = format_key(input_a)
    display_b = format_key(input_b)
    if any("Are you a person or a robot?" in line for line in lines):
        raise RuntimeError(f"Automated access was blocked for {display_a} -> {display_b}")

    entries = parse_summary_entries(lines)
    if not entries:
        raise RuntimeError(f"No summary blocks found for {display_a} -> {display_b}")

    return QuerySnapshot(
        input_a=display_a,
        input_b=display_b,
        url=url,
        entries=entries,
    )


def matches_filter(source: str, filters: list[str]) -> bool:
    if not filters:
        return True
    source_lower = source.lower()
    return any(filter_token.lower() in source_lower for filter_token in filters)


def scan_routes(
    inputs_a: list[str],
    inputs_b: list[str],
    headful: bool,
) -> tuple[list[QuerySnapshot], list[str]]:
    snapshots: list[QuerySnapshot] = []
    failures: list[str] = []

    context_kwargs: dict[str, str] = {}
    if LOCALE_CODE_DEFAULT:
        context_kwargs["locale"] = LOCALE_CODE_DEFAULT
    if TIMEZONE_NAME_DEFAULT:
        context_kwargs["timezone_id"] = TIMEZONE_NAME_DEFAULT

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headful)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        for input_a in inputs_a:
            for input_b in inputs_b:
                try:
                    snapshot = fetch_snapshot(page, input_a, input_b)
                    snapshots.append(snapshot)
                except (PlaywrightTimeoutError, RuntimeError) as exc:
                    failures.append(str(exc))

        context.close()
        browser.close()

    return snapshots, failures


def format_value(amount: int) -> str:
    if VALUE_LABEL_DEFAULT:
        return f"{VALUE_LABEL_DEFAULT} {amount:,}"
    return f"{amount:,}"


def is_primary_match(entry: SummaryEntry) -> bool:
    return entry.label == PRIMARY_MATCH_LABEL


def collect_matches(
    snapshots: list[QuerySnapshot],
    filters: list[str],
    min_threshold: int,
    max_threshold: int,
) -> list[MatchRecord]:
    matches = [
        MatchRecord(
            input_a=snapshot.input_a,
            input_b=snapshot.input_b,
            source=entry.source,
            value=entry.value,
            details=entry.details,
            reference_url=snapshot.url,
        )
        for snapshot in snapshots
        for entry in snapshot.entries
        if is_primary_match(entry)
        and min_threshold <= entry.value <= max_threshold
        and matches_filter(entry.source, filters)
    ]
    matches.sort(
        key=lambda match: (
            match.value,
            match.source,
            match.input_a,
            match.input_b,
        )
    )
    return matches


def print_summary(snapshots: list[QuerySnapshot]) -> None:
    print("\nSummary")
    print("-" * 80)
    for snapshot in snapshots:
        print(f"{snapshot.input_a} -> {snapshot.input_b}")
        if not snapshot.entries:
            print("  no summary entries found")
            continue
        for entry in snapshot.entries:
            details = f" | {entry.details}" if entry.details else ""
            print(f"  {entry.label}: {format_value(entry.value)} | {entry.source}{details}")


def print_matches(matches: list[MatchRecord], top: int) -> None:
    print("\nMatching results")
    print("-" * 80)
    if not matches:
        print("No live results matched the current filters and thresholds.")
        return

    for index, match in enumerate(matches[:top], start=1):
        details = f" | {match.details}" if match.details else ""
        print(
            f"{index:>2}. {format_value(match.value):<12} | {match.source:<20} | "
            f"{match.input_a:<22} -> {match.input_b:<10}{details}"
        )


def write_json_report(
    report_path: str,
    scan_time: str,
    args: argparse.Namespace,
    snapshots: list[QuerySnapshot],
    matches: list[MatchRecord],
    failures: list[str],
) -> None:
    report = {
        "scan_time": scan_time,
        "value_label": VALUE_LABEL_DEFAULT,
        "min_threshold": args.min_threshold,
        "max_threshold": args.max_threshold,
        "inputs_a": args.inputs_a,
        "inputs_b": args.inputs_b,
        "filters": args.filters,
        "matched_count": len(matches),
        "queries_scanned": len(snapshots),
        "has_matches": bool(matches),
        "matches": [asdict(match) for match in matches],
        "failures": failures,
    }
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)


def print_failures(failures: list[str]) -> None:
    if not failures:
        return
    print("\nWarnings")
    print("-" * 80)
    for failure in failures:
        print(f"- {failure}")


def validate_args(args: argparse.Namespace) -> None:
    if not args.inputs_a:
        raise RuntimeError("No inputs configured for the first query set.")
    if not args.inputs_b:
        raise RuntimeError("No inputs configured for the second query set.")
    if args.min_threshold > args.max_threshold:
        raise RuntimeError("Minimum threshold cannot be greater than maximum threshold.")
    if not SOURCE_BASE_URL or not SOURCE_PATH_TEMPLATE:
        raise RuntimeError("Source URL configuration is incomplete.")
    if not SUMMARY_SECTION_HEADER or not PRIMARY_MATCH_LABEL or not SUMMARY_LABELS:
        raise RuntimeError("Summary label configuration is incomplete.")
    if not SECTION_BREAK_PREFIXES:
        raise RuntimeError("Section break configuration is incomplete.")


def run_once(args: argparse.Namespace) -> int:
    validate_args(args)

    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"Monitor run started at {scan_time}")
    print("Source pages are fetched live at runtime.")
    print(
        f"Showing matches between {format_value(args.min_threshold)} and {format_value(args.max_threshold)}."
    )

    snapshots, failures = scan_routes(args.inputs_a, args.inputs_b, args.headful)
    matches = collect_matches(
        snapshots,
        args.filters,
        args.min_threshold,
        args.max_threshold,
    )

    if args.json_output:
        write_json_report(args.json_output, scan_time, args, snapshots, matches, failures)

    if not snapshots:
        print_failures(failures)
        return 1

    if args.show_summary:
        print_summary(snapshots)
    print_matches(matches, args.top)
    print_failures(failures)
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    if args.watch_minutes <= 0:
        return run_once(args)

    interval_seconds = max(args.watch_minutes * 60, 30)
    try:
        while True:
            exit_code = run_once(args)
            print(f"\nNext refresh in {interval_seconds / 60:.1f} minute(s). Press Ctrl+C to stop.")
            print("=" * 80)
            time.sleep(interval_seconds)
            if exit_code != 0:
                return exit_code
    except KeyboardInterrupt:
        print("\nStopped by user.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())