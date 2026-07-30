# Provenance

Each build writes source checksums; dataset provider/name/version/type; horizontal resolution and CRS; vertical CRS/datum or `unknown`; acquisition period; license/attribution; source URLs; original NoData and interpolation percentages; projection/resampling pipeline; horizontal scale; baseline; robust elevation range; vertical exaggeration; geometry method; output format writer; validation; and slicer evidence.

`build_manifest.json` binds artifacts to SHA-256 values. `build_config.resolved.yaml` records all defaults and CLI overrides. `original_nodata_mask.tif` preserves the pre-interpolation mask. Dataset rights are not replaced by the Apache-2.0 code license.
