#! /usr/bin/env python3
"""
Audit dataset configs for the defect classes we have actually hit in production, and
record which collections carry categorical data so their COG overviews get the right
resampling.

Two jobs:

  --audit        (default) static checks over every dataset-config JSON
  --resampling   emit the per-collection overview resampling table

Nothing here talks to S3 or the STAC API, so it is safe to run anywhere and fast enough
to sit in CI. Checks that genuinely need an object listing are called out in the
docstrings below and deliberately not attempted.

Why the resampling table exists
-------------------------------
`convert_to_cog` in disasters-product-algorithms built overviews with AVERAGE resampling
regardless of what the data means. Averaging class codes invents codes that do not exist.

Measured on a live product,
  s3://nasa-disasters-staging/ProgramData/OPERA/DSWx/OPERA_DSWx_S1_WTR_mosaic_2024-10-11_day.tif
the native histogram (computed block by block) contains classes 0, 1, 3, 251 and 255 and
NO class 2, yet a decimated read - which is served from the overviews - returns class 2
for 1.19% of pixels. 2 == (1 + 3) / 2. The phantom class is the averaging.

Categorical rasters therefore need `mode`; continuous ones keep `average`. The table below
is the authoritative list of which is which, so the re-encode tool does not have to guess.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "ingestion-data" / "staging" / "dataset-config"

# --------------------------------------------------------------------------------------
# Resampling registry
# --------------------------------------------------------------------------------------
# Collections whose pixel values are CLASS CODES rather than measurements. Averaging or
# interpolating these produces values that mean nothing. Every entry carries the evidence
# it was classified on, so the list can be audited rather than trusted.
CATEGORICAL = {
    "opera-dswx-s1-wtr-daily": "native classes 0/1/3/251/255 (verified by histogram)",
    "opera-dswx-s1-bwtr-daily": "native classes 0/1/251/255 (verified by histogram)",
    "opera-dswx-hls-wtr-daily": "native classes 0/1/2/253/255 (verified by histogram)",
    "opera-dswx-hls-bwtr-daily": "native classes 0/1/252/253/255 (verified by histogram)",
    "opera-dswxchangemap-composite": (
        "Float32 but categorical: exactly three values -1/0/+1 (verified by histogram). "
        "Displayed with a continuous ramp, which is why a colormap-based guess would "
        "misclassify it - this is the case the registry exists for"
    ),
    "opera-distalert-gen-dist-status-daily": "7 class codes 0-6 (verified by histogram)",
    "opera-distalert-s1-daily": "3 class codes 0/1/2 (verified by histogram)",
    "blackmarble-cloudmask-daily": "2 class codes 0/1 (verified by histogram)",
    "sentinel1-dmgassessment-daily": "single class code 1 (verified by histogram)",
    "sentinel1-hydrosar-wm-iw-dvr-subdaily": "5 class codes 0-4, Int8 (verified by histogram)",
    "uavsar-unetclassified-daily": "2 class codes 1/255 (verified by histogram)",
    "uavsar-unetclassified-composite": "U-Net class labels; same product family as the daily",
    "uavsar-unetclassifiedgrayscale-composite": "4 class codes 29/76/150/173 (verified by histogram)",
    "uavsar-quicklookclassified-subdaily": "3 class codes 1/128/255 (verified by histogram)",
    "ecostress-qc-subdaily": "QC bitfield - bit flags, never interpolate",
}

# Products with no dataset-config yet, verified categorical by histogram. Listed so that
# whoever writes their config does not have to rediscover this.
CATEGORICAL_UNCONFIGURED = {
    "ProgramData/Planet/CloudMask/": "2 values 1/999 (UInt16)",
    "ProgramData/Sentinel-2/CloudMask/": "2 values 1/999 (UInt16)",
    "ProgramData/UAVSAR/Grayscale/": "5 codes 0/29/76/150/173 - same palette as UNetClassified grayscale",
    "ProgramData/Sentinel-1_2/": "5 burn-severity classes 1-5 (Unburnt..Extreme)",
    "ProgramData/AVIRIS-3/dnbr/dnbrColor/": "3-band colorized dNBR, only 5 discrete levels per band",
    "ProgramData/UAVSAR/garrett_please_sort/": "single code 255 across 100% of pixels - inspect before configuring",
    # NOT categorical, recorded here because it shares a directory with ones that are:
    # ProgramData/OPERA/DistAlert/*VEG-ANOM-MAX* is CONTINUOUS (66 distinct values, 10..255).
    # A single config over that directory would apply one resampling to both. It must not.
}

# Verified continuous by histogram. Needed where the collection name matches no hint and
# no discrete colormap is declared, so it would otherwise come back "unclassified".
CONTINUOUS = {
    "opera-distalert-veg-anom-max-daily": (
        "67 distinct values: a smooth unimodal 10-74 spanning every integer, plus 0 and "
        "255 (verified by histogram). Shares a directory with VEG-DIST-STATUS, which is "
        "categorical - so resampling must be decided per file, never per directory"
    ),
}

# Collections that are genuine measurements or imagery; averaging is correct for these.
# Anything not in either table is reported as UNCLASSIFIED rather than assumed.
CONTINUOUS_HINTS = (
    "truecolor", "colorir", "naturalcolor", "rgb", "ndvi", "nbr", "dnbr", "mndwi",
    "backscatter", "lst", "lsterror", "displacement", "imerg", "brdf", "gec", "pca",
    "charash", "hd", "gunw", "swir", "panchromatic", "total", "wood", "lis",
)

RESAMPLING = {"categorical": "mode", "continuous": "average"}


def classify(collection: str, cfg: dict) -> tuple[str, str]:
    """Return (kind, reason). Curated table wins; then declared colormap; then name hint."""
    if collection in CATEGORICAL:
        return "categorical", CATEGORICAL[collection]
    if collection in CONTINUOUS:
        return "continuous", CONTINUOUS[collection]
    for render in (cfg.get("renders") or {}).values():
        if isinstance(render.get("colormap"), dict):
            return "categorical", "declares a discrete colormap"
    stem = collection.rsplit("-", 1)[0]
    for hint in CONTINUOUS_HINTS:
        if hint in stem:
            return "continuous", f"name contains {hint!r}"
    return "unclassified", "not in the registry and no discrete colormap declared"


# --------------------------------------------------------------------------------------
# Audit checks
# --------------------------------------------------------------------------------------
# Each returns a list of human-readable problem strings. Severity: "error" blocks, "warn"
# is advisory. Checks needing an S3 listing (prefix overlap between configs, files matched
# by no asset regex, ambiguous asset regexes) are NOT done here - they need `aws s3 ls`
# output and belong in a separate pass.

def check_identity(name: str, cfg: dict) -> list[tuple[str, str]]:
    """Filename, `collection`, and every discovery_items[].collection must agree."""
    out = []
    if cfg.get("collection") != name:
        out.append(("error", f"`collection` is {cfg.get('collection')!r}, filename says {name!r}"))
    for i, di in enumerate(cfg.get("discovery_items") or []):
        if di.get("collection") != name:
            out.append(("error", f"discovery_items[{i}].collection is {di.get('collection')!r}, expected {name!r}"))
    return out


def check_prefix_slash(name: str, cfg: dict) -> list[tuple[str, str]]:
    """S3 prefix matching is literal. `.../DistAlert` also matches `.../DistAlertS1/...`."""
    out = []
    for i, di in enumerate(cfg.get("discovery_items") or []):
        prefix = di.get("prefix", "")
        if prefix and not prefix.endswith("/"):
            out.append(("error", f"discovery_items[{i}].prefix {prefix!r} has no trailing slash - "
                                 f"it will also match sibling prefixes that share this stem"))
    return out


def check_nodata(name: str, cfg: dict) -> list[tuple[str, str]]:
    """Render-extension `nodata` is typed ["number","string"]. null is not valid; omitting it is."""
    out = []
    for rn, rv in (cfg.get("renders") or {}).items():
        if "nodata" in rv and rv["nodata"] is None:
            out.append(("error", f"renders.{rn}.nodata is null - invalid per the render "
                                 f"extension schema; omit the field instead"))
    return out


def check_colormap_rescale(name: str, cfg: dict) -> list[tuple[str, str]]:
    """titiler stretches to 0-255 BEFORE applying a colormap, so class codes stop matching
    the colormap keys and the layer falls through to greyscale."""
    out = []
    for rn, rv in (cfg.get("renders") or {}).items():
        if isinstance(rv.get("colormap"), dict) and "rescale" in rv:
            out.append(("error", f"renders.{rn} pairs a discrete colormap with rescale - "
                                 f"the colormap will not apply"))
        if isinstance(rv.get("colormap"), str):
            out.append(("error", f"renders.{rn}.colormap is a pre-serialized string; the "
                                 f"schema declares an object"))
    return out


def check_categorical_rescale(name: str, cfg: dict) -> list[tuple[str, str]]:
    """A categorical layer stretched linearly renders its *invalid* classes brightest.

    This is what `opera-dswx-daily` did: rescale [0,255] put open water (class 1) at
    near-black while fill (255) and cloud (253) went near-white."""
    kind, _ = classify(name, cfg)
    if kind != "categorical":
        return []
    out = []
    for rn, rv in (cfg.get("renders") or {}).items():
        if "rescale" not in rv or isinstance(rv.get("colormap"), dict):
            continue
        if rv.get("colormap_name"):
            # A continuous ramp over class codes is only correct when the rescale bounds
            # coincide with the actual value range - true for the DSWx change maps, whose
            # three values -1/0/+1 land exactly on the ramp's ends and midpoint. That
            # cannot be confirmed from the config alone, so this is advisory.
            out.append(("warn", f"renders.{rn} applies the continuous ramp "
                                f"{rv['colormap_name']!r} with rescale {rv['rescale']} to "
                                f"categorical data. Correct only if those bounds match the "
                                f"real class range - verify against the raster"))
        else:
            # No colormap at all: a bare linear stretch of class codes into greyscale.
            # This is what opera-dswx-daily did - rescale [0,255] put open water (class 1)
            # at near-black while fill (255) and cloud (253) rendered near-white, making
            # the invalid classes the brightest thing on the map.
            out.append(("error", f"renders.{rn} linearly rescales categorical data "
                                 f"({rv['rescale']}) with no colormap - class codes are not "
                                 f"a continuous range and will render as greyscale; use a "
                                 f"discrete colormap and drop rescale"))
    return out


def check_render_assets(name: str, cfg: dict) -> list[tuple[str, str]]:
    """A render naming an asset the items do not carry produces a broken rendered_preview.

    Seen live on opera-dswx-daily: `rendered_preview_s1bwtrchngmap` pointed at a key that
    was not on the item."""
    out = []
    declared = set()
    for di in cfg.get("discovery_items") or []:
        declared |= set((di.get("assets") or {}).keys())
    for rn, rv in (cfg.get("renders") or {}).items():
        for a in rv.get("assets") or []:
            if a not in declared:
                out.append(("error", f"renders.{rn}.assets references {a!r}, which no "
                                     f"discovery_items[].assets declares"))
    item_assets = set((cfg.get("item_assets") or {}).keys())
    if item_assets and item_assets != declared:
        only_ia = sorted(item_assets - declared)
        if only_ia:
            out.append(("warn", f"item_assets has keys the discovery assets do not: {only_ia}. "
                                f"Ingest replaces item_assets with a single cog_default key, so "
                                f"this is cosmetic, but it misleads readers"))
    return out


def check_multi_asset_grouping(name: str, cfg: dict) -> list[tuple[str, str]]:
    """Multi-asset configs key items on the id_regex capture, so two files sharing that
    capture AND an asset key collapse onto one item and one silently wins.

    Single-asset configs take the id from the filename stem and cannot collide this way."""
    out = []
    for i, di in enumerate(cfg.get("discovery_items") or []):
        assets = di.get("assets") or {}
        if len(assets) > 1:
            out.append(("warn", f"discovery_items[{i}] declares {len(assets)} assets, so items "
                                f"group by the id_regex capture. Two files sharing that capture "
                                f"and an asset key will collapse onto one item. Verify against a "
                                f"real listing, or split into single-asset collections"))
    return out


def check_overlapping_asset_regexes(name: str, cfg: dict) -> list[tuple[str, str]]:
    """Two asset regexes that can both match one filename make asset assignment
    order-dependent. `.*S1.*BWTR.*` and `.*S1.*BWTR.*ChngMap.*` both matched the change
    maps, and the broader one won."""
    out = []
    for i, di in enumerate(cfg.get("discovery_items") or []):
        assets = di.get("assets") or {}
        keys = sorted(assets)
        for a in keys:
            for b in keys:
                if a >= b:
                    continue
                ra, rb = assets[a].get("regex", ""), assets[b].get("regex", "")
                # Cheap containment heuristic: if one pattern is a prefix-extension of the
                # other with no negative lookahead guarding it, they can overlap.
                if ra and rb and "(?!" not in ra and "(?!" not in rb:
                    core_a = ra.replace(".*", "").replace("\\", "")
                    core_b = rb.replace(".*", "").replace("\\", "")
                    if core_a and core_b and (core_a in core_b or core_b in core_a):
                        out.append(("warn", f"discovery_items[{i}] asset regexes {a!r} and {b!r} "
                                            f"may both match the same file; neither uses a negative "
                                            f"lookahead to disambiguate"))
    return out


CHECKS = (
    check_identity,
    check_prefix_slash,
    check_nodata,
    check_colormap_rescale,
    check_categorical_rescale,
    check_render_assets,
    check_multi_asset_grouping,
    check_overlapping_asset_regexes,
)


def load_configs(config_dir: Path) -> dict[str, dict]:
    out = {}
    for path in sorted(config_dir.glob("*.json")):
        try:
            out[path.stem] = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            out[path.stem] = {"__unparseable__": str(exc)}
    return out


def run_audit(configs: dict[str, dict], only: str | None) -> int:
    errors = warns = 0
    for name, cfg in configs.items():
        if only and only != name:
            continue
        if "__unparseable__" in cfg:
            print(f"\n{name}\n  ERROR  not valid JSON: {cfg['__unparseable__']}")
            errors += 1
            continue
        found = []
        for check in CHECKS:
            found.extend(check(name, cfg))
        if not found:
            continue
        print(f"\n{name}")
        for severity, msg in found:
            print(f"  {severity.upper():<5}  {msg}")
            if severity == "error":
                errors += 1
            else:
                warns += 1
    print(f"\n{'-' * 78}")
    print(f"{len(configs)} configs checked: {errors} error(s), {warns} warning(s)")
    return 1 if errors else 0


def run_resampling(configs: dict[str, dict], only: str | None, as_json: bool) -> int:
    rows = []
    for name, cfg in configs.items():
        if only and only != name:
            continue
        if "__unparseable__" in cfg:
            continue
        kind, reason = classify(name, cfg)
        rows.append({
            "collection": name,
            "kind": kind,
            "overview_resampling": RESAMPLING.get(kind),
            "reason": reason,
        })
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        width = max((len(r["collection"]) for r in rows), default=10)
        print(f"{'collection':<{width}}  {'kind':<13} {'resampling':<11} reason")
        for r in sorted(rows, key=lambda r: (r["kind"], r["collection"])):
            print(f"{r['collection']:<{width}}  {r['kind']:<13} "
                  f"{str(r['overview_resampling'] or '-'):<11} {r['reason']}")
        n_unclassified = sum(1 for r in rows if r["kind"] == "unclassified")
        if n_unclassified:
            print(f"\n{n_unclassified} collection(s) unclassified - add them to CATEGORICAL or "
                  f"CONTINUOUS_HINTS rather than letting the re-encode tool guess.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resampling", action="store_true",
                    help="print the overview-resampling table instead of auditing")
    ap.add_argument("--collection", help="restrict to one collection")
    ap.add_argument("--json", action="store_true", help="machine-readable output (--resampling only)")
    ap.add_argument("--config-dir", type=Path, default=CONFIG_DIR,
                    help=f"default: {CONFIG_DIR}")
    args = ap.parse_args(argv)

    if not args.config_dir.is_dir():
        print(f"no such directory: {args.config_dir}", file=sys.stderr)
        return 2
    configs = load_configs(args.config_dir)
    if not configs:
        print(f"no dataset configs found in {args.config_dir}", file=sys.stderr)
        return 2
    if args.collection and args.collection not in configs:
        print(f"no such collection: {args.collection}", file=sys.stderr)
        return 2

    if args.resampling:
        return run_resampling(configs, args.collection, args.json)
    return run_audit(configs, args.collection)


if __name__ == "__main__":
    raise SystemExit(main())
