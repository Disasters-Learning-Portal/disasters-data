#!/usr/bin/env python3
"""Report the dates a published STAC collection actually holds.

    python3 scripts/collection_dates.py landsat-truecolor-daily
    python3 scripts/collection_dates.py opera-dswx-daily --env production
    python3 scripts/collection_dates.py blackmarble-brdf-daily --format json

Answers "what is in this collection, and for which dates?" straight from the API, so a
config's `datetime_range` and `id_regex` can be checked against what ingest published.

Stdlib only: the repo pins no HTTP client, and CI installs nothing extra.

Exit codes: 0 success (including an empty collection), 2 collection not found,
1 anything else.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import date

APIS = {
    "dev": "https://dev.disasters.openveda.cloud/api/stac",
    "production": "https://disasters.openveda.cloud/api/stac",
}

PAGE_SIZE = 500
MAX_PAGES = 40
TIMEOUT = 120


class CollectionNotFound(Exception):
    """The API served the SPA shell instead of JSON, so the id does not exist."""


def api_base(env, override=None):
    """Resolve the STAC root, with an explicit --api winning over --env."""
    if override:
        return override.rstrip("/")
    try:
        return APIS[env]
    except KeyError:
        raise SystemExit(f"unknown --env {env!r}; choose from {sorted(APIS)}")


def fetch(url):
    """GET a URL, returning (content_type, body). Network boundary; stubbed in tests."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return response.headers.get("Content-Type", ""), response.read()


def load_json(url, fetcher=fetch):
    """GET and parse JSON.

    A missing collection looks different per deployment: dev answers 200 with the SPA
    shell (`text/html`), production answers a real 404. Both mean "no such id", so
    check the content type as well as the status.
    """
    try:
        content_type, body = fetcher(url)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise CollectionNotFound(url) from err
        raise
    if "json" not in content_type.lower():
        raise CollectionNotFound(url)
    return json.loads(body)


def item_dates(feature):
    """(start, end) as YYYY-MM-DD, or (None, None) when the item carries no date.

    `datetime_range` configs publish `datetime: null` plus a start/end bracket, so both
    shapes have to be read.
    """
    props = feature.get("properties") or {}
    stamp = props.get("datetime")
    if stamp:
        return stamp[:10], stamp[:10]
    start = props.get("start_datetime")
    if not start:
        return None, None
    end = props.get("end_datetime") or start
    return start[:10], end[:10]


def next_href(doc):
    """The `rel=next` paging link, if the response carries one."""
    for link in doc.get("links") or []:
        if link.get("rel") == "next" and link.get("href"):
            return link["href"]
    return None


def fetch_items(
    base, collection, page_size=PAGE_SIZE, max_pages=MAX_PAGES, load=load_json
):
    """All items for a collection, as (features, truncated)."""
    url = f"{base}/collections/{collection}/items?limit={page_size}"
    features = []
    for _ in range(max_pages):
        doc = load(url)
        page = doc.get("features") or []
        features.extend(page)
        if not page:
            return features, False
        url = next_href(doc)
        if not url:
            return features, False
    return features, True


def declared_extent(meta):
    """The temporal extent the collection advertises, as (start, end) dates."""
    temporal = ((meta.get("extent") or {}).get("temporal") or {}).get("interval")
    if not temporal:
        return None, None
    start, end = (list(temporal[0]) + [None, None])[:2]
    return (start or "")[:10] or None, (end or "")[:10] or None


def largest_gap(dates):
    """(days, from, to) for the widest stretch between consecutive dates."""
    if len(dates) < 2:
        return None
    widest = None
    previous = date.fromisoformat(dates[0])
    for value in dates[1:]:
        current = date.fromisoformat(value)
        span = (current - previous).days
        if widest is None or span > widest[0]:
            widest = (span, previous.isoformat(), current.isoformat())
        previous = current
    return widest


def summarize(collection, base, meta, features, truncated):
    """Fold items into the date report the renderers print."""
    per_date = Counter()
    ranged = 0
    undated = 0
    for feature in features:
        start, end = item_dates(feature)
        if not start:
            undated += 1
            continue
        per_date[start] += 1
        if end != start:
            ranged += 1
    dates = sorted(per_date)
    declared_start, declared_end = declared_extent(meta)
    return {
        "collection": collection,
        "api": base,
        "title": meta.get("title") or "",
        "declared_start": declared_start,
        "declared_end": declared_end,
        "item_count": len(features),
        "date_count": len(dates),
        "observed_start": dates[0] if dates else None,
        "observed_end": dates[-1] if dates else None,
        "dates": [{"date": d, "items": per_date[d]} for d in dates],
        "ranged_items": ranged,
        "undated_items": undated,
        "largest_gap": largest_gap(dates),
        "truncated": truncated,
    }


def _notes(summary):
    """Caveats worth printing under either renderer."""
    notes = []
    if summary["truncated"]:
        notes.append(
            f"Stopped at the {MAX_PAGES}-page cap; raise --max-pages for the full list."
        )
    if summary["ranged_items"]:
        notes.append(
            f"{summary['ranged_items']} item(s) span more than one day; "
            "each is counted on its start date."
        )
    if summary["undated_items"]:
        notes.append(
            f"{summary['undated_items']} item(s) carry no datetime at all -- "
            "usually an id_regex that failed to capture a date."
        )
    if not summary["item_count"]:
        notes.append(
            "The collection exists but holds no items. WMTS passthrough collections "
            "(GIBS) are empty by design; otherwise ingest published nothing."
        )
    return notes


def render_text(summary):
    lines = [
        f"collection  {summary['collection']}",
        f"api         {summary['api']}",
    ]
    if summary["title"]:
        lines.append(f"title       {summary['title']}")
    lines.append(
        f"declared    {summary['declared_start'] or '-'}"
        f" -> {summary['declared_end'] or '-'}"
    )
    lines.append(
        f"items       {summary['item_count']} over"
        f" {summary['date_count']} distinct date(s)"
    )
    if summary["dates"]:
        lines.append(
            f"observed    {summary['observed_start']} -> {summary['observed_end']}"
        )
        lines.append("")
        lines.append("date          items")
        for entry in summary["dates"]:
            lines.append(f"{entry['date']}    {entry['items']}")
        gap = summary["largest_gap"]
        if gap:
            lines.append("")
            lines.append(f"largest gap   {gap[0]} days ({gap[1]} -> {gap[2]})")
    for note in _notes(summary):
        lines.append(f"note        {note}")
    return "\n".join(lines)


def render_markdown(summary):
    heading = summary["collection"]
    if summary["title"]:
        heading += f" -- {summary['title']}"
    lines = [
        f"## {heading}",
        "",
        f"- API: `{summary['api']}`",
        f"- Declared extent: `{summary['declared_start'] or '-'}`"
        f" to `{summary['declared_end'] or '-'}`",
        f"- Items: **{summary['item_count']}** over"
        f" **{summary['date_count']}** distinct date(s)",
    ]
    if summary["dates"]:
        lines.append(
            f"- Observed range: `{summary['observed_start']}`"
            f" to `{summary['observed_end']}`"
        )
        gap = summary["largest_gap"]
        if gap:
            lines.append(f"- Largest gap: **{gap[0]} days** (`{gap[1]}` to `{gap[2]}`)")
        lines.extend(["", "| Date | Items |", "| --- | --- |"])
        for entry in summary["dates"]:
            lines.append(f"| `{entry['date']}` | {entry['items']} |")
    for note in _notes(summary):
        lines.extend(["", f"> {note}"])
    return "\n".join(lines) + "\n"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("collection", help="STAC collection id")
    parser.add_argument(
        "--env",
        default="dev",
        choices=sorted(APIS),
        help="which STAC API to query (default: dev)",
    )
    parser.add_argument("--api", help="explicit STAC root, overriding --env")
    parser.add_argument(
        "--format",
        default="text",
        choices=["text", "markdown", "json"],
        help="output format (default: text)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=MAX_PAGES,
        help=f"page cap, {PAGE_SIZE} items per page (default: {MAX_PAGES})",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    base = api_base(args.env, args.api)
    try:
        meta = load_json(f"{base}/collections/{args.collection}")
        features, truncated = fetch_items(
            base, args.collection, max_pages=args.max_pages
        )
    except CollectionNotFound:
        print(
            f"collection {args.collection!r} does not exist on {base}\n"
            "(the API answered 404, or 200 with the SPA shell instead of JSON)",
            file=sys.stderr,
        )
        return 2
    except urllib.error.URLError as err:
        print(f"request to {base} failed: {err}", file=sys.stderr)
        return 1

    summary = summarize(args.collection, base, meta, features, truncated)
    if args.format == "json":
        print(json.dumps(summary, indent=2))
    elif args.format == "markdown":
        print(render_markdown(summary))
    else:
        print(render_text(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
