"""Unit tests for scripts/collection_dates.py. No network: the fetcher is stubbed."""

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import collection_dates as cd  # noqa: E402


def feature(item_id="x", datetime=None, start=None, end=None):
    """A STAC item carrying only the datetime fields the report reads."""
    return {
        "id": item_id,
        "properties": {
            "datetime": datetime,
            "start_datetime": start,
            "end_datetime": end,
        },
    }


def page(features, next_url=None):
    links = [{"rel": "self", "href": "https://example.test/page"}]
    if next_url:
        links.append({"rel": "next", "href": next_url})
    return {"type": "FeatureCollection", "features": features, "links": links}


def loader(pages_by_url):
    """A stand-in for load_json that serves a canned response per URL."""

    def load(url):
        assert url in pages_by_url, f"unexpected request for {url}"
        return pages_by_url[url]

    return load


# --- api_base -------------------------------------------------------------------


def test_api_base_resolves_env():
    assert cd.api_base("dev").startswith("https://dev.disasters.openveda.cloud")
    assert cd.api_base("production") == "https://disasters.openveda.cloud/api/stac"


def test_api_base_override_wins_and_strips_slash():
    assert cd.api_base("dev", "https://example.test/api/stac/") == (
        "https://example.test/api/stac"
    )


def test_api_base_rejects_unknown_env():
    with pytest.raises(SystemExit):
        cd.api_base("nope")


# --- load_json / the SPA-shell gotcha -------------------------------------------


def test_load_json_parses_a_json_response():
    def fetcher(url):
        return "application/json", b'{"id": "landsat-nbr-daily"}'

    assert cd.load_json("https://example.test", fetcher)["id"] == "landsat-nbr-daily"


def test_load_json_raises_on_a_404():
    """Production answers a real 404 where dev serves the SPA shell."""

    def fetcher(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with pytest.raises(cd.CollectionNotFound):
        cd.load_json("https://example.test", fetcher)


def test_load_json_reraises_other_http_errors():
    def fetcher(url):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        cd.load_json("https://example.test", fetcher)


def test_load_json_raises_on_the_spa_shell():
    """A missing collection answers 200 with text/html, so status is not the check."""

    def fetcher(url):
        return "text/html; charset=utf-8", b"<!doctype html><html></html>"

    with pytest.raises(cd.CollectionNotFound):
        cd.load_json("https://example.test", fetcher)


# --- item_dates -----------------------------------------------------------------


def test_item_dates_prefers_datetime():
    assert cd.item_dates(feature(datetime="2025-01-12T00:00:00Z")) == (
        "2025-01-12",
        "2025-01-12",
    )


def test_item_dates_falls_back_to_start_end():
    """datetime_range configs publish datetime: null plus a start/end bracket."""
    item = feature(start="2024-09-05T00:00:00Z", end="2025-01-16T00:00:00Z")
    assert cd.item_dates(item) == ("2024-09-05", "2025-01-16")


def test_item_dates_uses_start_when_end_is_absent():
    item = feature(start="2026-04-14T03:59:50Z")
    assert cd.item_dates(item) == ("2026-04-14", "2026-04-14")


def test_item_dates_returns_none_for_an_undated_item():
    assert cd.item_dates(feature()) == (None, None)


# --- paging ---------------------------------------------------------------------


def test_fetch_items_follows_next_links():
    base, cid = "https://example.test/api/stac", "landsat-truecolor-daily"
    first = f"{base}/collections/{cid}/items?limit=500"
    second = f"{base}/collections/{cid}/items?limit=500&token=2"
    load = loader(
        {
            first: page([feature("a")], next_url=second),
            second: page([feature("b")]),
        }
    )
    features, truncated = cd.fetch_items(base, cid, load=load)
    assert [f["id"] for f in features] == ["a", "b"]
    assert truncated is False


def test_fetch_items_stops_at_max_pages_and_reports_truncation():
    base, cid = "https://example.test/api/stac", "big"
    first = f"{base}/collections/{cid}/items?limit=500"
    load = loader({first: page([feature("a")], next_url=first)})
    features, truncated = cd.fetch_items(base, cid, max_pages=1, load=load)
    assert len(features) == 1
    assert truncated is True


def test_fetch_items_handles_an_empty_collection():
    base, cid = "https://example.test/api/stac", "VIIRS_SNPP_DayNightBand_AtSensor_M15"
    first = f"{base}/collections/{cid}/items?limit=500"
    features, truncated = cd.fetch_items(base, cid, load=loader({first: page([])}))
    assert features == []
    assert truncated is False


# --- extent and gaps ------------------------------------------------------------


def test_declared_extent_reads_the_interval():
    meta = {"extent": {"temporal": {"interval": [["2023-01-15T00:00:00Z", None]]}}}
    assert cd.declared_extent(meta) == ("2023-01-15", None)


def test_declared_extent_tolerates_a_missing_extent():
    assert cd.declared_extent({}) == (None, None)


def test_largest_gap_finds_the_widest_stretch():
    dates = ["2024-06-24", "2024-06-25", "2024-10-20", "2024-10-21"]
    assert cd.largest_gap(dates) == (117, "2024-06-25", "2024-10-20")


def test_largest_gap_is_none_for_a_single_date():
    assert cd.largest_gap(["2025-01-12"]) is None


# --- summarize ------------------------------------------------------------------


def test_summarize_counts_items_per_date():
    features = [
        feature("a", datetime="2025-01-12T00:00:00Z"),
        feature("b", datetime="2025-01-12T06:00:00Z"),
        feature("c", datetime="2024-12-18T00:00:00Z"),
    ]
    summary = cd.summarize("sentinel2-nbr-daily", "https://x", {}, features, False)
    assert summary["item_count"] == 3
    assert summary["date_count"] == 2
    assert summary["dates"] == [
        {"date": "2024-12-18", "items": 1},
        {"date": "2025-01-12", "items": 2},
    ]
    assert summary["observed_start"] == "2024-12-18"
    assert summary["observed_end"] == "2025-01-12"


def test_summarize_flags_ranged_and_undated_items():
    features = [
        feature("ranged", start="2024-09-05T00:00:00Z", end="2025-01-16T00:00:00Z"),
        feature("undated"),
    ]
    summary = cd.summarize("aviris3-dnbr-daily", "https://x", {}, features, False)
    assert summary["ranged_items"] == 1
    assert summary["undated_items"] == 1
    # a ranged item is counted once, on its start date
    assert summary["dates"] == [{"date": "2024-09-05", "items": 1}]


# --- rendering ------------------------------------------------------------------


def _summary(features, truncated=False, meta=None):
    return cd.summarize("demo", "https://x", meta or {}, features, truncated)


def test_render_text_lists_every_date():
    out = cd.render_text(_summary([feature("a", datetime="2025-01-12T00:00:00Z")]))
    assert "collection  demo" in out
    assert "2025-01-12" in out


def test_render_markdown_builds_a_table():
    out = cd.render_markdown(_summary([feature("a", datetime="2025-01-12T00:00:00Z")]))
    assert out.startswith("## demo")
    assert "| Date | Items |" in out
    assert "| `2025-01-12` | 1 |" in out


def test_renderers_explain_an_empty_collection():
    empty = _summary([])
    assert "no items" in cd.render_text(empty)
    assert "no items" in cd.render_markdown(empty)


def test_renderers_warn_when_truncated():
    truncated = _summary([feature("a", datetime="2025-01-12T00:00:00Z")], True)
    assert "--max-pages" in cd.render_text(truncated)
    assert "--max-pages" in cd.render_markdown(truncated)


# --- CLI ------------------------------------------------------------------------


def test_parse_args_defaults_to_dev_and_text():
    args = cd.parse_args(["landsat-nbr-daily"])
    assert (args.collection, args.env, args.format) == (
        "landsat-nbr-daily",
        "dev",
        "text",
    )


def test_main_reports_a_missing_collection_with_exit_2(monkeypatch, capsys):
    def boom(url, fetcher=None):
        raise cd.CollectionNotFound(url)

    monkeypatch.setattr(cd, "load_json", boom)
    assert cd.main(["does-not-exist"]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_main_prints_json(monkeypatch, capsys):
    monkeypatch.setattr(cd, "load_json", lambda url, fetcher=None: {"title": "Demo"})
    monkeypatch.setattr(
        cd,
        "fetch_items",
        lambda *a, **kw: ([feature("a", datetime="2025-01-12T00:00:00Z")], False),
    )
    assert cd.main(["demo", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["collection"] == "demo"
    assert payload["dates"] == [{"date": "2025-01-12", "items": 1}]
