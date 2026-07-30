# Elevation Data License Matrix

TopoForge code is Apache-2.0. Dataset rights remain separate and are written into every `provenance.json`. The official-source review date is 2026-07-31; `docs/data-sources.md` contains the detailed citations and current access evidence.

| Dataset/route | Type | Access | Commercial use | Cache/redistribution | Required handling |
| --- | --- | --- | --- | --- | --- |
| Local user raster | Declared by user | Local file | Source-dependent | Source-dependent | Preserve user-supplied license, attribution, checksum, CRS, and datum status |
| Copernicus DEM GLO-30/GLO-90 AWS 2021 | DSM | Public HTTPS/S3, no key | Permitted under the applicable Copernicus terms | Permitted with notices | Store exact adapted/unmodified attribution, disclaimer, non-endorsement, release, URL, ETag, and checksum |
| Copernicus DEM via CDSE | DSM | Free registration/authenticated APIs | Permitted under GLO-30-F obligations | Permitted with obligations | Keep credentials private; distinguish CDSE release from AWS 2021 mirror |
| NASADEM HGT V001 | Radar-derived mixed/void-filled surface | Earthdata OAuth | NASA-led data is CC0 unless marked otherwise | Permitted | Cite NASA/LP DAAC; preserve source/NUM/water masks; do not label DTM |
| USGS 3DEP | Bare-earth DTM for standard DEM products | Public TNM/S3/WCS | Public domain | Permitted | Credit U.S. Geological Survey; preserve asset-level horizontal/vertical metadata |
| OpenTopography API | Dataset-dependent | Personal/Enterprise API key | Dataset terms plus service plan apply | Underlying dataset terms | Never ship/log keys; hosted/for-profit integration uses appropriate Enterprise terms |
| GEBCO_2026 | Mixed topography/bathymetry | Public downloads/subsets | Expressly permitted | Permitted | Acknowledge GEBCO; preserve TID/source caveats and heterogeneous vertical-reference warning |
| FABDEM V1-2 | DTM-like corrected surface | Public repository | Non-commercial route only | Non-commercial/ShareAlike obligations | Explicit opt-in mode; excluded from the default commercial-compatible policy |
| TopoForge synthetic fixtures | Analytic test surface | Repository | Permitted | Permitted | Apache-2.0; never describe as real terrain |

Third-party terrain data is not bundled. Slicer executables are external AGPL-3.0 applications; TopoForge contains only an Apache-2.0 subprocess adapter.
