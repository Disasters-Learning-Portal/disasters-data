#!/usr/bin/env bash
# Download one representative raster per product whose data kind (categorical vs
# continuous) cannot be settled from its name or its dataset-config alone.
#
# Why this matters: overview resampling has to match the data. `mode` on continuous data
# is the same mistake as `average` on class codes, just in reverse. Every product below
# is one I could NOT classify with confidence from names, so harden_dataset_configs.py
# should not be guessing at them.
#
#   ./scripts/fetch_resampling_samples.sh              # -> ~/Downloads/resampling_samples
#   ./scripts/fetch_resampling_samples.sh /some/dir
#
# Re-runnable: files already present are skipped. Smallest file in each product folder is
# chosen, so this is structure-representative but cheap.
#
# Deliberately NOT sampled:
#   ProgramData/Satellogic/NDVI/    smallest file is 1.05 GB, and NDVI is unambiguously
#                                   continuous - no sample needed to know that
#   ProgramData/Planet/Overviews/   ArcGIS CRF pyramid scratch (Test_CRF_PixelCache,
#                                   LERCCompression), not a product
#   ProgramData/Pleiades/           an empty 0-byte directory marker, no files

set -uo pipefail

BUCKET="nasa-disasters-staging"
DEST="${1:-$HOME/Downloads/resampling_samples}"

# group<TAB>key
SAMPLES=$(cat <<'EOF'
unconfigured	ProgramData/AVIRIS-3/dnbr/Palisades_AV3_provisional_dNBR_866nm_2198nm_2024-09-05_2025-01-16_day.tif
unconfigured	ProgramData/AVIRIS-3/dnbr/dnbrColor/Palisades_AV3_provisional_dNBR_866nm_2198nm_color_2024-09-05_2025-01-16_day.tif
unconfigured	ProgramData/Blackmarble/TestHD/finalBMHD_VNP46A3_MonthlyComposite_2024-08_monthly.tif
unconfigured	ProgramData/Planet/CloudMask/Planet_cloudMask_merged_2025-08-03_day.tif
unconfigured	ProgramData/Sentinel-1_2/Combined_classification_Unburnt_to_Extreme_2025-01-12_day.tif
unconfigured	ProgramData/Sentinel-2/CloudMask/CentralTX_S2C_cloudMask_merged_2025-07-17_day.tif
unconfigured	ProgramData/UAVSAR/Grayscale/UAVSAR_Grayscale_flight25023_mosaic_2025-07-09_day.tif
unconfigured	ProgramData/UAVSAR/PredictedScore/sangab_30412_25023_005_L090_UNet_predicted_score_2025-07-09_day.tif
unconfigured	ProgramData/UAVSAR/garrett_please_sort/ClassifiedUAVSARI_CopyRas_2025-07_monthly.tif
ambiguous	ProgramData/UAVSAR/QuicklookClassified/uavsar_15102_002_quicklook_class_tampab_2024-10-13T17:00:00Z.tif
ambiguous	ProgramData/UAVSAR/UNetClassified/uavsar_guadal_11013_25023_002_L090_UNet_class_2025-07-09_day.tif
ambiguous	ProgramData/UAVSAR/UNetClassified/uavsar_flight25023_mosaic_UNet_class_grayscale_202507_monthly.tif
ambiguous	ProgramData/UAVSAR/Displacement/PV_DISP_UAVSAR_T27558_d2024-09-18_2024-09-30_day.tif
distalert	ProgramData/OPERA/DistAlert/OPERA-DIST-ALERT-HLS-VEG-ANOM-MAX_2025-06-26_day.tif
distalert	ProgramData/OPERA/DistAlert/OPERA-DIST-ALERT-HLS-VEG-DIST-STATUS_2025-06-26_day.tif
distalert	ProgramData/OPERA/DistAlert/OPERA_L3_DIST-ALERT-S1_V1_GEN-DIST-STATUS_mosaic_2025-07-11_day.tif
distalert	ProgramData/OPERA/DistAlertS1/disturbance_track64_2025-01-09_day.tif
confirm	ProgramData/Blackmarble/cloudMask/blackmarble_VNP46A2_CloudMask_2026-04-18_day.tif
confirm	ProgramData/Sentinel-1/DmgAssessment/s1_dmgassessment_Jan16_Delivery_LA_damage_v0_2025-01-09_day.tif
confirm	ProgramData/Sentinel-1/HydroSAR_WM/S1A_IW_DVR_RTC20_G_gpuned_DF85_reclassified_WM_2024-10-03T23:27:56Z.tif
EOF
)

mkdir -p "$DEST" || { echo "cannot create $DEST" >&2; exit 1; }
echo "destination: $DEST"
echo

ok=0; skipped=0; failed=0
while IFS=$'\t' read -r group key; do
  [ -z "${key:-}" ] && continue
  name=$(basename "$key")
  target="$DEST/${group}__${name}"
  if [ -f "$target" ]; then
    echo "SKIP  ${group}/${name}"
    skipped=$((skipped+1))
    continue
  fi
  printf 'GET   %-13s %s\n' "$group" "$name"
  if aws s3 cp "s3://${BUCKET}/${key}" "$target" --only-show-errors; then
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
  ls -1lh "$DEST" | tail -n +2 | awk '{printf "  %7s  %s\n", $5, $9}'
fi
