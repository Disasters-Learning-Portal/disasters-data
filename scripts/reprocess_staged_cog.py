#! /usr/bin/env python3
"""
Re-convert staged rasters that are structurally wrong, locally, and put them back.

Companion to fix_staged_overviews.py, which is surgical: it rebuilds overviews on files
that are ALREADY valid COGs. This one is for files that are wrong at the container level
and need a full rewrite - not tiled, no overviews, wrong interleave, no compression.

The case that prompted it: ProgramData/Sentinel-1_2/Combined_classification_*.tif has a
block size of 6219x1 (stripped, not tiled) and zero overviews. It is not a COG at all, so
there is no overview to repair.

    # what is wrong, touch nothing (default)
    ./scripts/reprocess_staged_cog.py --collection sentinel1_2-burnseverity-daily
    ./scripts/reprocess_staged_cog.py --prefix ProgramData/Sentinel-1_2/

    # rewrite locally and inspect, still no upload
    ./scripts/reprocess_staged_cog.py --prefix ProgramData/Sentinel-1_2/ --fix

    # rewrite and put back, keeping the originals on disk
    ./scripts/reprocess_staged_cog.py --prefix ProgramData/Sentinel-1_2/ --fix --execute \
        --backup-dir ~/staged_backups

Requires `aws`, `gdalinfo`, `gdal_translate`. Uses osgeo only for --deep checks.

THIS RE-ENCODES. Full-resolution PIXELS are preserved and verified byte-for-byte via a
full-band checksum, but the compressed tiles are rewritten. There is no GDAL path that
relays out a container while copying compressed tiles verbatim. If the checksum, size,
CRS, geotransform or nodata change, the upload is refused.

Defect classes detected
-----------------------
  not-cog        Image Structure LAYOUT is not COG
  not-tiled      block is striped (height 1) or not square
  blocksize      tiled, but not the 512 the rest of the archive uses
  no-overviews   zero overviews on a raster large enough to need them
  overview-count level count != ceil(log2(max(w,h)/blocksize)), the max-dimension rule
                 GDAL's COG driver uses. rio-cogeo's default uses min() and under-builds
                 non-square rasters.
  resampling     overviews built with the wrong algorithm for the data kind. Averaging
                 class codes invents codes that do not exist: OPERA DSWx WTR holds
                 {0,1,3,251,255} natively yet its overviews return class 2, because
                 2 == (1+3)/2.
  interleave     multispectral (>3 bands, not RGB/RGBA) without INTERLEAVE=BAND. A 3-of-8
                 band read costs 4.19 MB/block pixel-interleaved vs 1.57 MB band-interleaved.
                 REPORTED BUT NOT FIXED HERE: GDAL 3.10.3's COG driver rejects the option
                 ("Warning 6: driver COG does not support creation option INTERLEAVE"), so
                 it is accepted, ignored, and would falsely look applied. Fixing it needs
                 -of GTiff with manual overview construction, or a newer GDAL. RGB/RGBA
                 renderings are excluded - they are always read whole, so PIXEL is right.
  no-compression uncompressed raster
  nodata-absent  (--deep) declared nodata value does not occur in the pixels. rio-tiler
                 masks on the declared value, finds nothing, and renders fill as data.
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
TARGET_BLOCKSIZE = 512


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def require_tools() -> None:
    missing = [t for t in ("aws", "gdalinfo", "gdal_translate") if shutil.which(t) is None]
    if missing:
        sys.exit(f"missing required executable(s) on PATH: {', '.join(missing)}")


def registry_resampling(collection: str | None) -> tuple[str | None, str]:
    if not collection:
        return None, "no collection given; resampling left as-is unless --resampling"
    proc = run([sys.executable, str(HARDEN), "--resampling", "--json", "--collection", collection])
    if proc.returncode != 0:
        return None, f"registry lookup failed: {proc.stderr.strip()[:120]}"
    rows = json.loads(proc.stdout or "[]")
    if not rows:
        return None, f"{collection} not in the registry"
    return rows[0]["overview_resampling"], rows[0]["reason"]


def list_keys(bucket: str, prefix: str, collection: str | None) -> list[str]:
    fname_re = None
    if collection:
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
        if not key.startswith(prefix) or not key.lower().endswith(".tif"):
            continue
        if fname_re and not fname_re.search(os.path.basename(key)):
            continue
        keys.append(key)
    return bucket, prefix, keys


def probe(path: Path) -> dict:
    proc = run(["gdalinfo", "-json", str(path)])
    if proc.returncode != 0:
        raise RuntimeError(f"gdalinfo failed on {path.name}: {proc.stderr.strip()[:200]}")
    info = json.loads(proc.stdout)
    band = info["bands"][0]
    img = (info.get("metadata", {}) or {}).get("IMAGE_STRUCTURE", {}) or {}
    default_md = (info.get("metadata", {}) or {}).get("", {}) or {}
    w, h = info["size"]
    bx, by = band.get("block", [0, 0])
    return {
        "width": w, "height": h, "block_x": bx, "block_y": by,
        "dtype": band.get("type"), "bands": len(info["bands"]),
        "nodata": band.get("noDataValue"),
        "compression": img.get("COMPRESSION"),
        "interleave": img.get("INTERLEAVE"),
        "predictor": img.get("PREDICTOR"),
        "layout": img.get("LAYOUT"),
        "resampling": img.get("OVERVIEW_RESAMPLING") or default_md.get("OVERVIEW_RESAMPLING"),
        "overview_count": len(band.get("overviews", [])),
        "colorinterp": [b.get("colorInterpretation") for b in info["bands"]],
        "crs": info.get("coordinateSystem", {}).get("wkt", "")[:80],
        "gt": info.get("geoTransform"),
    }


def is_multispectral(info: dict) -> bool:
    """True for >3-band data where a read touches a SUBSET of bands.

    A 4-band RGBA or 3-band RGB rendering is always read whole, so PIXEL interleave is
    correct for it. The INTERLEAVE=BAND win applies to multispectral stacks: a 3-of-8 band
    read costs 4.19 MB/block pixel-interleaved against 1.57 MB band-interleaved."""
    if info["bands"] <= 3:
        return False
    ci = [(c or "").lower() for c in info.get("colorinterp") or []]
    return not set(ci) <= {"red", "green", "blue", "alpha", "gray", "undefined", ""} or \
        ci.count("undefined") + ci.count("") > 1


def wanted_levels(w: int, h: int, blocksize: int = TARGET_BLOCKSIZE) -> int:
    longest = max(w, h)
    return 0 if longest <= blocksize else math.ceil(math.log2(longest / blocksize))


def nodata_occurs(path: Path) -> bool | None:
    """Does the declared nodata value actually appear? Invisible to gdalinfo alone."""
    try:
        from osgeo import gdal
        import numpy as np
    except ImportError:
        return None
    gdal.UseExceptions()
    ds = gdal.Open(str(path))
    b = ds.GetRasterBand(1)
    nd = b.GetNoDataValue()
    if nd is None:
        return None
    _, by = b.GetBlockSize()
    step = max(by, 1) * 8
    for y in range(0, ds.RasterYSize, step):
        a = b.ReadAsArray(0, y, ds.RasterXSize, min(step, ds.RasterYSize - y))
        if a is not None and bool(np.any(a == nd)):
            return True
    return False


def diagnose(info: dict, want_res: str | None, deep_nodata: bool | None) -> list[str]:
    faults = []
    if info["layout"] != "COG":
        faults.append(f"not-cog (LAYOUT={info['layout']})")
    if info["block_y"] == 1:
        faults.append(f"not-tiled (striped, block {info['block_x']}x1)")
    elif info["block_x"] != info["block_y"]:
        faults.append(f"not-tiled (block {info['block_x']}x{info['block_y']} not square)")
    elif info["block_x"] != TARGET_BLOCKSIZE:
        faults.append(f"blocksize {info['block_x']}, archive uses {TARGET_BLOCKSIZE}")
    want_n = wanted_levels(info["width"], info["height"])
    if want_n and info["overview_count"] == 0:
        faults.append(f"no-overviews (needs {want_n})")
    elif info["overview_count"] != want_n:
        faults.append(f"overview-count {info['overview_count']}, rule wants {want_n}")
    if want_res and (info["resampling"] or "").upper() != want_res.upper():
        faults.append(f"resampling {info['resampling']}, wants {want_res.upper()}")
    if is_multispectral(info) and (info["interleave"] or "").upper() != "BAND":
        faults.append(f"interleave {info['interleave']} on {info['bands']} multispectral bands, "
                      f"wants BAND (NOT fixable by this script - see module docstring)")
    if not info["compression"]:
        faults.append("no-compression")
    if deep_nodata is False:
        faults.append(f"nodata-absent: declared {info['nodata']} never occurs in the pixels")
    return faults


def checksum(path: Path) -> str:
    proc = run(["gdalinfo", "-checksum", str(path)])
    if proc.returncode != 0:
        raise RuntimeError(f"checksum failed on {path.name}")
    return ",".join(re.findall(r"Checksum=(\d+)", proc.stdout))


def reconvert(src: Path, dst: Path, info: dict, resampling: str | None,
              zstd_level: int | None) -> None:
    compress = (info.get("compression") or "ZSTD").upper()
    cmd = ["gdal_translate", "-of", "COG", "-q", str(src), str(dst),
           "-co", f"BLOCKSIZE={TARGET_BLOCKSIZE}",
           "-co", f"COMPRESS={compress}",
           # The COG driver's AUTO default REUSES the source's overviews, so a rewrite
           # without this silently keeps whatever was wrong with them.
           "-co", "OVERVIEWS=IGNORE_EXISTING"]
    if resampling:
        cmd += ["-co", f"OVERVIEW_RESAMPLING={resampling.upper()}",
                # These tags are stale on many products and CreateCopy copies them forward.
                "-mo", f"OVERVIEW_RESAMPLING={resampling.upper()}",
                "-mo", f"OVR_RESAMPLING_ALG={resampling.upper()}"]
    # INTERLEAVE is deliberately NOT passed. Measured on GDAL 3.10.3:
    #   "Warning 6: driver COG does not support creation option INTERLEAVE"
    # It is accepted, warned about, and ignored, so passing it would look like it worked.
    if info.get("predictor") and str(info["predictor"]) != "1":
        cmd += ["-co", f"PREDICTOR={info['predictor']}"]
    if zstd_level is not None and compress == "ZSTD":
        cmd += ["-co", f"LEVEL={zstd_level}"]
    est_gb = (info["width"] * info["height"] * info["bands"]) / 1e9
    if est_gb > 3:
        cmd += ["-co", "BIGTIFF=YES"]
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"gdal_translate failed on {src.name}: {proc.stderr.strip()[:300]}")


def verify(before_info: dict, before_sum: str, out: Path, resampling: str | None) -> list[str]:
    """Refuse the upload unless the rewrite is provably faithful."""
    problems = []
    after = probe(out)
    if checksum(out) != before_sum:
        problems.append("FULL-RESOLUTION CHECKSUM CHANGED - pixels were altered")
    for f in ("width", "height", "bands", "dtype", "nodata", "crs"):
        if before_info[f] != after[f]:
            problems.append(f"{f} changed: {before_info[f]!r} -> {after[f]!r}")
    if before_info["gt"] and after["gt"]:
        if any(abs(a - b) > 1e-9 for a, b in zip(before_info["gt"], after["gt"])):
            problems.append("geotransform changed")
    if after["layout"] != "COG":
        problems.append(f"output LAYOUT={after['layout']}, not COG")
    want_n = wanted_levels(after["width"], after["height"])
    if after["overview_count"] != want_n:
        problems.append(f"output has {after['overview_count']} overviews, wanted {want_n}")
    if resampling and (after["resampling"] or "").upper() != resampling.upper():
        problems.append(f"output resampling {after['resampling']}, wanted {resampling.upper()}")
    # Interleave is intentionally not checked here: the COG driver cannot set it, so a
    # mismatch is a known limitation rather than an unfaithful rewrite. diagnose() still
    # reports it so it is visible.
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--collection", help="every object this collection claims")
    src.add_argument("--prefix", help="every .tif under this s3 prefix")
    src.add_argument("--key", help="one s3 key")
    ap.add_argument("--bucket", default="nasa-disasters-staging")
    ap.add_argument("--resampling", help="override the registry (mode|average|nearest|cubic)")
    ap.add_argument("--deep", action="store_true",
                    help="also check the declared nodata actually occurs (reads pixels)")
    ap.add_argument("--fix", action="store_true", help="rewrite locally (default: inspect only)")
    ap.add_argument("--execute", action="store_true", help="upload results. Requires --fix")
    ap.add_argument("--backup-dir", type=Path,
                    help="keep each original here before overwriting it in S3")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--zstd-level", type=int, help="ZSTD level (repo-wide value is 22, slow)")
    ap.add_argument("--workdir", type=Path)
    args = ap.parse_args(argv)

    if args.execute and not args.fix:
        sys.exit("--execute requires --fix")
    if args.execute and not args.backup_dir:
        print("note: --execute without --backup-dir overwrites the staged object with no "
              "local copy kept. Ctrl-C now if that is not what you want.\n")
    require_tools()

    resampling, why = (args.resampling, "forced on the command line") if args.resampling \
        else registry_resampling(args.collection)
    if args.collection:
        bucket, prefix, keys = list_keys(args.bucket, "", args.collection)
    elif args.prefix:
        bucket, prefix, keys = list_keys(args.bucket, args.prefix, None)
    else:
        bucket, keys = args.bucket, [args.key]
    if args.limit:
        keys = keys[:args.limit]

    print(f"objects    : {len(keys)}")
    print(f"resampling : {resampling or '(unchanged)'}  ({why})")
    print(f"mode       : {'UPLOAD' if args.execute else ('local rewrite' if args.fix else 'inspect only')}")
    print("-" * 78)

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="reprocess_"))
    workdir.mkdir(parents=True, exist_ok=True)
    if args.backup_dir:
        args.backup_dir.mkdir(parents=True, exist_ok=True)

    clean = broken = rewritten = failed = 0
    for key in keys:
        name = os.path.basename(key)
        local = workdir / name
        if not local.exists():
            p = run(["aws", "s3", "cp", f"s3://{bucket}/{key}", str(local), "--only-show-errors"])
            if p.returncode != 0:
                print(f"FAIL     {name}\n           download: {p.stderr.strip()[:160]}")
                failed += 1
                continue
        try:
            info = probe(local)
        except RuntimeError as exc:
            print(f"FAIL     {name}\n           {exc}")
            failed += 1
            continue

        deep = nodata_occurs(local) if args.deep else None
        faults = diagnose(info, resampling, deep)
        head = (f"{info['width']}x{info['height']} {info['dtype']} bands={info['bands']} "
                f"block={info['block_x']}x{info['block_y']} ovr={info['overview_count']} "
                f"layout={info['layout']}")
        if not faults:
            print(f"OK       {name}\n           {head}")
            clean += 1
            continue

        broken += 1
        print(f"{'BROKEN  ' if not args.fix else 'FIXING  '} {name}\n           {head}")
        for f in faults:
            print(f"           - {f}")
        if not args.fix:
            continue

        before_sum = checksum(local)
        out = local.with_suffix(".reprocessed.tif")
        try:
            reconvert(local, out, info, resampling, args.zstd_level)
        except RuntimeError as exc:
            print(f"           FAILED: {exc}")
            failed += 1
            out.unlink(missing_ok=True)
            continue

        problems = verify(info, before_sum, out, resampling)
        if problems:
            print("           REFUSING - rewrite is not faithful:")
            for p in problems:
                print(f"             * {p}")
            print(f"           original untouched; bad output kept at {out}")
            failed += 1
            continue

        after = probe(out)
        print(f"           -> block={after['block_x']} ovr={after['overview_count']} "
              f"resampling={after['resampling']} layout=COG, checksum {before_sum} preserved")
        rewritten += 1

        if args.execute:
            if args.backup_dir:
                shutil.copy2(local, args.backup_dir / name)
            p = run(["aws", "s3", "cp", str(out), f"s3://{bucket}/{key}", "--only-show-errors"])
            if p.returncode != 0:
                print(f"           UPLOAD FAILED: {p.stderr.strip()[:160]}")
                failed += 1
                continue
            print(f"           uploaded -> s3://{bucket}/{key}"
                  + (f" (original kept at {args.backup_dir / name})" if args.backup_dir else ""))
            out.unlink(missing_ok=True)
            local.unlink(missing_ok=True)

    print("-" * 78)
    print(f"{len(keys)} object(s): {clean} clean, {broken} with defects, "
          f"{rewritten} rewritten, {failed} failed")
    if not args.fix and broken:
        print("\nrun with --fix to rewrite locally, then --fix --execute --backup-dir DIR to put back")
    elif args.fix and not args.execute and rewritten:
        print(f"\nrewritten files are in {workdir}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
