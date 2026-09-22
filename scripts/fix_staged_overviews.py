#! /usr/bin/env python3
"""
Repair overview resampling on COGs already staged in S3, in place, without going back
through the hub conversion.

Categorical rasters were converted with AVERAGE-resampled overviews. Averaging class
codes invents codes that do not exist, so zoomed-out map views show phantom classes.
Measured on a live product: OPERA_DSWx_S1_WTR_mosaic_2024-10-11_day.tif has native
classes 0/1/3/251/255 and NO class 2, yet its overviews return class 2 for 1.18% of
pixels, because 2 == (1 + 3) / 2.

This downloads each object, rebuilds its overviews with the resampling the collection
actually needs, verifies the full-resolution data is untouched, and uploads it back.

    # see what would change, touch nothing (default)
    ./scripts/fix_staged_overviews.py --collection opera-dswx-s1-wtr-daily

    # fix locally and inspect, still no upload
    ./scripts/fix_staged_overviews.py --collection opera-dswx-s1-wtr-daily --fix

    # fix and write back to S3
    ./scripts/fix_staged_overviews.py --collection opera-dswx-s1-wtr-daily --fix --execute

Requires `aws` and GDAL's `gdalinfo` / `gdal_translate` on PATH. Deliberately does NOT
import rasterio or osgeo, so it runs in any environment that has the GDAL binaries.

Which resampling a collection needs comes from harden_dataset_configs.py, so the two
scripts cannot drift apart.

Three findings this script depends on, each measured rather than assumed
-----------------------------------------------------------------------
1. `gdaladdo -r mode` REFUSES to touch a COG ("Updating it will generally result in
   losing part of the optimizations"). Forcing it with IGNORE_COG_LAYOUT_BREAK works but
   the output is no longer a COG. So we rewrite via `gdal_translate -of COG`.

2. TRADEOFF, be honest about it: that rewrite preserves full-resolution PIXELS exactly
   but RE-COMPRESSES the tiles. It is not a byte-level copy. There is no GDAL path that
   copies compressed tiles verbatim while relaying out the file. This script verifies the
   pixels, not the bytes.

3. `OVERVIEWS=IGNORE_EXISTING` is load-bearing. The COG driver's AUTO default REUSES the
   source's existing overviews, so without this flag the rewrite silently keeps the bad
   AVERAGE overviews and changes nothing.

Also: these files carry a stale default-domain OVERVIEW_RESAMPLING=AVERAGE tag (next to a
contradictory OVR_RESAMPLING_ALG=NEAREST) that CreateCopy would copy forward. Both are
overwritten with -mo at creation time.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "ingestion-data" / "staging" / "dataset-config"
HARDEN = REPO_ROOT / "scripts" / "harden_dataset_configs.py"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def require_tools() -> None:
    missing = [t for t in ("aws", "gdalinfo", "gdal_translate") if shutil.which(t) is None]
    if missing:
        sys.exit(f"missing required executable(s) on PATH: {', '.join(missing)}")


def required_resampling(collection: str) -> tuple[str, str]:
    """Ask harden_dataset_configs.py what this collection needs. Single source of truth."""
    proc = run([sys.executable, str(HARDEN), "--resampling", "--json",
                "--collection", collection])
    if proc.returncode != 0:
        sys.exit(f"could not determine resampling for {collection}:\n{proc.stderr}")
    rows = json.loads(proc.stdout)
    if not rows:
        sys.exit(f"no such collection: {collection}")
    row = rows[0]
    if row["kind"] == "unclassified":
        sys.exit(f"{collection} is unclassified in the resampling registry. Add it to "
                 f"CATEGORICAL or CONTINUOUS_HINTS in harden_dataset_configs.py rather "
                 f"than guessing here.")
    return row["overview_resampling"], row["reason"]


def list_objects(collection: str) -> tuple[str, str, list[str]]:
    """Return (bucket, prefix, keys) for the objects this collection claims."""
    cfg = json.loads((CONFIG_DIR / f"{collection}.json").read_text())
    di = cfg["discovery_items"][0]
    bucket, prefix = di["bucket"], di["prefix"]
    fname_re = re.compile(di["filename_regex"])
    proc = run(["aws", "s3", "ls", f"s3://{bucket}/{prefix}", "--recursive"])
    if proc.returncode != 0:
        sys.exit(f"aws s3 ls failed:\n{proc.stderr}")
    keys = []
    for line in proc.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        key = parts[3]
        if not key.startswith(prefix):
            continue
        if fname_re.search(os.path.basename(key)):
            keys.append(key)
    return bucket, prefix, keys


def probe(path: Path) -> dict:
    """Read structure from gdalinfo -json. No osgeo import needed."""
    proc = run(["gdalinfo", "-json", str(path)])
    if proc.returncode != 0:
        raise RuntimeError(f"gdalinfo failed on {path.name}:\n{proc.stderr}")
    info = json.loads(proc.stdout)
    band = info["bands"][0]
    img = info.get("metadata", {}).get("IMAGE_STRUCTURE", {}) or {}
    default_md = info.get("metadata", {}).get("", {}) or {}
    w, h = info["size"]
    block = band.get("block", [512, 512])
    return {
        "width": w,
        "height": h,
        "blocksize": max(block),
        "dtype": band.get("type"),
        "bands": len(info["bands"]),
        "nodata": band.get("noDataValue"),
        "compression": img.get("COMPRESSION"),
        "interleave": img.get("INTERLEAVE"),
        "predictor": img.get("PREDICTOR"),
        "layout": img.get("LAYOUT"),
        # GDAL records the truth here; the default-domain tag is often stale and wrong.
        "resampling": (img.get("OVERVIEW_RESAMPLING")
                       or default_md.get("OVERVIEW_RESAMPLING")),
        "overview_count": len(band.get("overviews", [])),
    }


def wanted_levels(width: int, height: int, blocksize: int) -> int:
    """Governed by the LONGER side, matching GDAL's COG driver (frmts/gtiff/cogdriver.cpp).

    rio-cogeo's own default uses min() and under-builds non-square rasters."""
    longest = max(width, height)
    if longest <= blocksize:
        return 0
    return math.ceil(math.log2(longest / blocksize))


def checksum(path: Path) -> str:
    """Full-resolution per-band checksum. Proves pixels survived the re-encode."""
    proc = run(["gdalinfo", "-checksum", str(path)])
    if proc.returncode != 0:
        raise RuntimeError(f"checksum failed on {path.name}:\n{proc.stderr}")
    return ",".join(re.findall(r"Checksum=(\d+)", proc.stdout))


def rebuild(src: Path, dst: Path, resampling: str, info: dict, zstd_level: int | None) -> None:
    cmd = ["gdal_translate", "-of", "COG", "-q", str(src), str(dst),
           "-co", f"OVERVIEW_RESAMPLING={resampling.upper()}",
           # Without this the COG driver REUSES the source's bad overviews (measured).
           "-co", "OVERVIEWS=IGNORE_EXISTING",
           "-co", f"BLOCKSIZE={info['blocksize']}",
           # The stale tags would otherwise be copied forward verbatim.
           "-mo", f"OVERVIEW_RESAMPLING={resampling.upper()}",
           "-mo", f"OVR_RESAMPLING_ALG={resampling.upper()}"]
    if info.get("compression"):
        cmd += ["-co", f"COMPRESS={info['compression']}"]
    if info.get("predictor") and info["predictor"] not in ("1", 1):
        cmd += ["-co", f"PREDICTOR={info['predictor']}"]
    if info.get("interleave"):
        cmd += ["-co", f"INTERLEAVE={info['interleave']}"]
    if zstd_level is not None and (info.get("compression") or "").upper() == "ZSTD":
        cmd += ["-co", f"LEVEL={zstd_level}"]
    # TIFF does not record compression LEVEL, so it cannot be recovered from the source.
    # Omitting it uses the GDAL default; --zstd-level lets you force the repo-wide value.
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"gdal_translate failed on {src.name}:\n{proc.stderr}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--collection", help="fix every object this collection claims")
    src.add_argument("--key", help="fix one s3 key (needs --bucket and --resampling-override)")
    ap.add_argument("--bucket", default="nasa-disasters-staging")
    ap.add_argument("--resampling-override", help="force a resampling instead of the registry")
    ap.add_argument("--fix", action="store_true",
                    help="actually rebuild locally (default is inspect only)")
    ap.add_argument("--execute", action="store_true",
                    help="upload repaired files back to S3. Requires --fix")
    ap.add_argument("--limit", type=int, help="stop after N objects")
    ap.add_argument("--zstd-level", type=int,
                    help="force ZSTD level (repo-wide value is 22; much slower)")
    ap.add_argument("--workdir", type=Path, help="keep downloads here instead of a temp dir")
    args = ap.parse_args(argv)

    if args.execute and not args.fix:
        sys.exit("--execute requires --fix")
    require_tools()

    if args.collection:
        resampling, reason = (args.resampling_override, "forced on the command line") \
            if args.resampling_override else required_resampling(args.collection)
        bucket, prefix, keys = list_objects(args.collection)
        print(f"collection : {args.collection}")
        print(f"resampling : {resampling}  ({reason})")
    else:
        if not args.resampling_override:
            sys.exit("--key requires --resampling-override")
        resampling, bucket, keys = args.resampling_override, args.bucket, [args.key]
        print(f"resampling : {resampling}  (forced)")

    if args.limit:
        keys = keys[:args.limit]
    print(f"objects    : {len(keys)}")
    print(f"mode       : {'UPLOAD TO S3' if args.execute else ('local fix only' if args.fix else 'inspect only')}")
    print("-" * 78)

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="fix_ovr_"))
    workdir.mkdir(parents=True, exist_ok=True)
    needed = fixed = skipped = failed = 0

    for key in keys:
        name = os.path.basename(key)
        local = workdir / name
        if not local.exists():
            proc = run(["aws", "s3", "cp", f"s3://{bucket}/{key}", str(local), "--only-show-errors"])
            if proc.returncode != 0:
                print(f"FAIL     {name}\n           download: {proc.stderr.strip()[:160]}")
                failed += 1
                continue
        try:
            info = probe(local)
        except RuntimeError as exc:
            print(f"FAIL     {name}\n           {exc}")
            failed += 1
            continue

        want_n = wanted_levels(info["width"], info["height"], info["blocksize"])
        have_res = (info["resampling"] or "?").upper()
        want_res = resampling.upper()
        res_wrong = have_res != want_res
        cnt_wrong = info["overview_count"] != want_n

        head = (f"{info['width']}x{info['height']} {info['dtype']} "
                f"ovr={info['overview_count']}/{want_n} resampling={have_res}->{want_res}")
        if not (res_wrong or cnt_wrong):
            print(f"OK       {name}\n           {head}  already correct")
            skipped += 1
            continue

        needed += 1
        if not args.fix:
            why = ", ".join(([f"resampling {have_res}"] if res_wrong else [])
                            + ([f"{info['overview_count']} levels, wants {want_n}"] if cnt_wrong else []))
            print(f"WOULD FIX {name}\n           {head}\n           reason: {why}")
            continue

        before = checksum(local)
        out = local.with_suffix(".fixed.tif")
        try:
            rebuild(local, out, resampling, info, args.zstd_level)
            after = checksum(out)
        except RuntimeError as exc:
            print(f"FAIL     {name}\n           {exc}")
            failed += 1
            out.unlink(missing_ok=True)
            continue

        post = probe(out)
        problems = []
        if after != before:
            problems.append(f"CHECKSUM CHANGED {before} -> {after}")
        if post["overview_count"] != want_n:
            problems.append(f"overview count {post['overview_count']}, wanted {want_n}")
        if post["layout"] != "COG":
            problems.append(f"output LAYOUT={post['layout']}, not COG")
        if problems:
            print(f"FAIL     {name}\n           " + "\n           ".join(problems))
            print(f"           left original untouched; bad output at {out}")
            failed += 1
            continue

        print(f"FIXED    {name}\n           {head}  checksum {before} preserved, LAYOUT=COG")
        fixed += 1

        if args.execute:
            proc = run(["aws", "s3", "cp", str(out), f"s3://{bucket}/{key}", "--only-show-errors"])
            if proc.returncode != 0:
                print(f"           UPLOAD FAILED: {proc.stderr.strip()[:160]}")
                failed += 1
                continue
            print(f"           uploaded -> s3://{bucket}/{key}")
            out.unlink(missing_ok=True)
            local.unlink(missing_ok=True)

    print("-" * 78)
    print(f"{len(keys)} object(s): {skipped} already correct, {needed} needed work, "
          f"{fixed} rebuilt, {failed} failed")
    if not args.fix and needed:
        print("\nrun again with --fix to rebuild locally, then --fix --execute to write back")
    elif args.fix and not args.execute and fixed:
        print(f"\nrepaired files are in {workdir}")
        print("re-run with --execute to upload them")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
