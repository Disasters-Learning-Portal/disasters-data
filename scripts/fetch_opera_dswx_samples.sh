#!/usr/bin/env bash
# Download one representative OPERA DSWx product per proposed collection group so the
# rasters can be inspected natively (gdalinfo -hist gives exact class values; titiler's
# /statistics is a decimated read and will not).
#
#   ./scripts/fetch_opera_dswx_samples.sh              # -> ~/Downloads/opera_dswx_samples
#   ./scripts/fetch_opera_dswx_samples.sh /some/dir    # -> /some/dir
#
# Re-runnable: files already present are skipped.

set -uo pipefail

BUCKET="nasa-disasters-staging"
PREFIX="ProgramData/OPERA/DSWx/"
DEST="${1:-$HOME/Downloads/opera_dswx_samples}"

# group<TAB>filename
SAMPLES=$(cat <<'EOF'
s1-wtr	OPERA_DSWx_S1_WTR_mosaic_2024-10-11_day.tif
s1-wtr	DSWx-S1_WTR_2024-09-26_day.tif
s1-bwtr	OPERA_L3_DSWX-S1_V1_BWTR_mosaic_2025-07-11_day.tif
s1-bwtr	DSWx-S1_BWTR_2024-09-26_day.tif
hls-bwtr	OPERA_L3_DSWX-HLS_V1_BWTR_mosaic_2025-07-12_day.tif
hls-bwtr	OPERA_DSWx-HLS_V1_BWTR_2025-06-23_day.tif
hls-bwtr-b02	post_event_ARIA_OPERA_DSWx-HLS_B02_Florida_2023-09-06_day.tif
hls-bwtr-b02	pre_event_ARIA_OPERA_DSWx-HLS_B02_Florida_2023-08-24_day.tif
hls-wtr	OPERA_DSWx_HLS_WTR_mosaic_2024-09-27_to_2024-10-07_day.tif
hls-floodmap	OPERA_DSWx_HLS_FloodMap_2024-04-21_to_2024-05-06_day.tif
hls-nosnowice	OPERA_DSWx_HLS_S2B_Mosaic_NoSnowIce_2024-04-21_day.tif
hls-mosaic	OPERA_L3_DSWx_HLS_S2B_L8_mosaic_2024-05-06_day.tif
EOF
)

mkdir -p "$DEST" || { echo "cannot create $DEST" >&2; exit 1; }
echo "destination: $DEST"
echo

ok=0; skipped=0; failed=0
while IFS=$'\t' read -r group name; do
  [ -z "${name:-}" ] && continue
  target="$DEST/${group}__${name}"
  if [ -f "$target" ]; then
    echo "SKIP  (already present)  ${group}/${name}"
    skipped=$((skipped+1))
    continue
  fi
  printf 'GET   %-14s %s\n' "$group" "$name"
  if aws s3 cp "s3://${BUCKET}/${PREFIX}${name}" "$target" --only-show-errors; then
    ok=$((ok+1))
  else
    echo "FAIL  ${group}/${name}" >&2
    failed=$((failed+1))
  fi
done <<< "$SAMPLES"

echo
echo "downloaded=$ok skipped=$skipped failed=$failed"
echo
if [ "$ok" -gt 0 ] || [ "$skipped" -gt 0 ]; then
  echo "files:"
  ls -1lh "$DEST" | tail -n +2 | awk '{printf "  %6s  %s\n", $5, $9}'
fi
