from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import argparse
import json
import re
import sys
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_ORIGINS = {
    "manila": "Manila",
    "cebu-city": "Cebu City",
    "clark-angeles-city": "Clark (Angeles City)",
    "davao-city": "Davao City",
}

DEFAULT_DESTINATIONS = {
    "tokyo": "Tokyo",
    "osaka": "Osaka",
    "nagoya": "Nagoya",
    "fukuoka": "Fukuoka",
    "sapporo": "Sapporo",
}

SECTION_BREAK_PREFIXES = (
    "When is the cheapest time to fly?",
    "Popular airlines from",
    "Popular airports near",
    "Frequently asked questions",
    "Search more flights",
    "Additional Links",
)

PRICE_RE = re.compile(r"(?:from\s+)?PHP\s*([0-9,]+)")


@dataclass(frozen=True)
class OverviewOffer:
    label: str
    price_php: int
    airline: str
    details: str


@dataclass(frozen=True)
class AirlineFare:
    origin: str
    destination: str
    airline: str
    service: str
    price_php: int
    source_url: str


@dataclass(frozen=True)
class RouteSnapshot:
    origin: str
    destination: str
    url: str
    offers: list[OverviewOffer]
    airline_fares: list[AirlineFare]


@dataclass(frozen=True)
class MatchingFare:
    origin: str
    destination: str
    airline: str
    price_php: int
    details: str
    source_url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check current Google Travel route-page fares from the Philippines to Japan "
            "and print the cheapest results in the terminal."
        )
    )
    parser.add_argument(
        "--origins",
        nargs="+",
        default=list(DEFAULT_ORIGINS),
        metavar="SLUG",
        help=(
            "Origin city slugs to scan. "
            f"Defaults: {', '.join(DEFAULT_ORIGINS)}"
        ),
    )
    parser.add_argument(
        "--destinations",
        nargs="+",
        default=list(DEFAULT_DESTINATIONS),
        metavar="SLUG",
        help=(
            "Japan destination city slugs to scan. "
            f"Defaults: {', '.join(DEFAULT_DESTINATIONS)}"
        ),
    )
    parser.add_argument(
        "--airlines",
        nargs="+",
        default=[],
        metavar="NAME",
        help=(
            "Only show airline fares whose names contain one of these tokens. "
            "If omitted, the script prints all airlines it finds."
        ),
    )
    parser.add_argument(
        "--min-price",
        type=int,
        default=11000,
        help="Minimum round-trip price to show. Default: 11000",
    )
    parser.add_argument(
        "--max-price",
        type=int,
        default=13000,
        help="Maximum round-trip price to show. Default: 13000",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=12,
        help="How many matching round-trip fares to print. Default: 12",
    )
    parser.add_argument(
        "--watch-minutes",
        type=float,
        default=0.0,
        help="Refresh every N minutes until interrupted. Default: 0 (run once)",
    )
    parser.add_argument(
        "--show-route-overview",
        action="store_true",
        help="Also print the cheapest one-way and round-trip overview per route.",
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


def title_for_slug(slug: str, mapping: dict[str, str]) -> str:
    if slug in mapping:
        return mapping[slug]
    return slug.replace("-", " ").title()


def normalize_line(raw_line: str) -> str:
    line = raw_line.replace("\xa0", " ").strip()
    line = line.replace("\u20b1", "PHP ")
    line = line.replace("\u2014", " - ")
    line = line.replace("\u2013", " - ")
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def parse_price(line: str) -> int | None:
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


def parse_overview_offers(lines: list[str]) -> list[OverviewOffer]:
    section_index = find_section(lines, "Flights overview")
    if section_index is None:
        return []

    labels = {
        "Cheapest round-trip flights",
        "Cheapest one-way flight",
        "Last-minute weekend getaway",
        "Cheapest business class flights",
    }

    offers: list[OverviewOffer] = []
    index = section_index + 1
    while index < len(lines):
        line = lines[index]
        if any(line.startswith(prefix) for prefix in SECTION_BREAK_PREFIXES if prefix != "Popular airlines from"):
            break
        if line not in labels:
            index += 1
            continue

        price_php = parse_price(lines[index + 1]) if index + 1 < len(lines) else None
        airline = lines[index + 2] if index + 2 < len(lines) else "Unknown"
        detail_parts: list[str] = []
        cursor = index + 3
        while cursor < len(lines):
            next_line = lines[cursor]
            if next_line in labels or any(next_line.startswith(prefix) for prefix in SECTION_BREAK_PREFIXES):
                break
            if next_line.startswith("The ") or next_line.startswith("View "):
                break
            detail_parts.append(next_line)
            cursor += 1

        if price_php is not None:
            offers.append(
                OverviewOffer(
                    label=line,
                    price_php=price_php,
                    airline=airline,
                    details=" | ".join(detail_parts),
                )
            )
        index = cursor

    return offers


def parse_popular_airlines(
    lines: list[str],
    origin: str,
    destination: str,
    source_url: str,
) -> list[AirlineFare]:
    section_index = find_section(lines, f"Popular airlines from {origin} to {destination}")
    if section_index is None:
        return []

    fares: list[AirlineFare] = []
    index = section_index + 1
    while index + 2 < len(lines):
        airline = lines[index]
        if any(airline.startswith(prefix) for prefix in SECTION_BREAK_PREFIXES if prefix != "Popular airlines from"):
            break

        service = lines[index + 1]
        price_line = lines[index + 2]
        price_php = parse_price(price_line)
        if price_php is None:
            index += 1
            continue

        fares.append(
            AirlineFare(
                origin=origin,
                destination=destination,
                airline=airline,
                service=service,
                price_php=price_php,
                source_url=source_url,
            )
        )
        index += 3

    return fares


def build_route_url(origin_slug: str, destination_slug: str) -> str:
    return (
        "https://www.google.com/travel/flights/"
        f"flights-from-{origin_slug}-to-{destination_slug}.html?hl=en&gl=PH"
    )


def fetch_route_snapshot(page, origin_slug: str, destination_slug: str) -> RouteSnapshot:
    origin = title_for_slug(origin_slug, DEFAULT_ORIGINS)
    destination = title_for_slug(destination_slug, DEFAULT_DESTINATIONS)
    url = build_route_url(origin_slug, destination_slug)

    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3500)
    body_text = page.locator("body").inner_text(timeout=10000)
    lines = collect_lines(body_text)

    if any("Are you a person or a robot?" in line for line in lines):
        raise RuntimeError(f"Google blocked automated access for {origin} -> {destination}")

    offers = parse_overview_offers(lines)
    airline_fares = parse_popular_airlines(lines, origin, destination, url)
    if not offers and not airline_fares:
        raise RuntimeError(f"No fare blocks found for {origin} -> {destination}")

    return RouteSnapshot(
        origin=origin,
        destination=destination,
        url=url,
        offers=offers,
        airline_fares=airline_fares,
    )


def matches_airline_filter(airline: str, filters: list[str]) -> bool:
    if not filters:
        return True
    airline_lower = airline.lower()
    return any(filter_token.lower() in airline_lower for filter_token in filters)


def scan_routes(
    origins: list[str],
    destinations: list[str],
    headful: bool,
) -> tuple[list[RouteSnapshot], list[str]]:
    snapshots: list[RouteSnapshot] = []
    failures: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headful)
        context = browser.new_context(locale="en-PH", timezone_id="Asia/Manila")
        page = context.new_page()

        for origin_slug in origins:
            for destination_slug in destinations:
                try:
                    snapshot = fetch_route_snapshot(page, origin_slug, destination_slug)
                    snapshots.append(snapshot)
                except (PlaywrightTimeoutError, RuntimeError) as exc:
                    failures.append(str(exc))

        context.close()
        browser.close()

    return snapshots, failures


def format_money(amount: int) -> str:
    return f"PHP {amount:,}"


def is_round_trip_offer(offer: OverviewOffer) -> bool:
    return offer.label == "Cheapest round-trip flights"


def collect_matching_round_trip_fares(
    snapshots: list[RouteSnapshot],
    airline_filters: list[str],
    min_price: int,
    max_price: int,
) -> list[MatchingFare]:
    matches = [
        MatchingFare(
            origin=snapshot.origin,
            destination=snapshot.destination,
            airline=offer.airline,
            price_php=offer.price_php,
            details=offer.details,
            source_url=snapshot.url,
        )
        for snapshot in snapshots
        for offer in snapshot.offers
        if is_round_trip_offer(offer)
        and min_price <= offer.price_php <= max_price
        and matches_airline_filter(offer.airline, airline_filters)
    ]
    matches.sort(
        key=lambda match: (
            match.price_php,
            match.airline,
            match.origin,
            match.destination,
        )
    )
    return matches


def print_route_overview(snapshots: list[RouteSnapshot]) -> None:
    print("\nRoute overview")
    print("-" * 80)
    for snapshot in snapshots:
        print(f"{snapshot.origin} -> {snapshot.destination}")
        if not snapshot.offers:
            print("  no overview fares found")
            continue
        for offer in snapshot.offers:
            details = f" | {offer.details}" if offer.details else ""
            print(
                f"  {offer.label}: {format_money(offer.price_php)} | {offer.airline}{details}"
            )


def print_matching_round_trip_fares(matches: list[MatchingFare], top: int) -> None:
    print("\nMatching round-trip fares")
    print("-" * 80)
    if not matches:
        print(
            "No live round-trip fares matched the current airline filter and PHP range."
        )
        return

    for index, match in enumerate(matches[:top], start=1):
        details = f" | {match.details}" if match.details else ""
        print(
            f"{index:>2}. {format_money(match.price_php):<12} | {match.airline:<20} | "
            f"{match.origin:<22} -> {match.destination:<10}{details}"
        )


def write_json_report(
    report_path: str,
    scan_time: str,
    args: argparse.Namespace,
    snapshots: list[RouteSnapshot],
    matches: list[MatchingFare],
    failures: list[str],
) -> None:
    report = {
        "scan_time": scan_time,
        "min_price": args.min_price,
        "max_price": args.max_price,
        "origins": args.origins,
        "destinations": args.destinations,
        "airlines": args.airlines,
        "matched_count": len(matches),
        "routes_scanned": len(snapshots),
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


def run_once(args: argparse.Namespace) -> int:
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"Flight scan started at {scan_time}")
    print("Source: Google Travel route pages, fetched live at runtime.")
    print(
        "Note: Google states these prices can lag behind provider updates by less than 24 hours."
    )
    print(
        f"Showing only live round-trip fares from {format_money(args.min_price)} to {format_money(args.max_price)}."
    )

    snapshots, failures = scan_routes(args.origins, args.destinations, args.headful)
    matches = collect_matching_round_trip_fares(
        snapshots,
        args.airlines,
        args.min_price,
        args.max_price,
    )

    if args.json_output:
        write_json_report(args.json_output, scan_time, args, snapshots, matches, failures)

    if not snapshots:
        print_failures(failures)
        return 1

    if args.show_route_overview:
        print_route_overview(snapshots)
    print_matching_round_trip_fares(matches, args.top)
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