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

## Web reference maps

The bundled offline land geometry is redistributed by `world-atlas` from Natural
Earth physical land data (public domain). It is used as a geographic reference,
not as country or administrative geometry. A separate public-domain Natural Earth
v5.1.2 boundary layer is bundled from pinned land, claim, maritime and China maritime
supplement GeoJSON sources. Source URLs, SHA-256 hashes, scale, original and selected
classifications, and reviewed feature IDs are recorded in
`web/src/data/reference-boundaries.provenance.json`. Original coordinates are retained.
China-related lines use the source's CN worldview; the excluded Taiwan-east arc
and Doklam association are recorded explicitly. Maritime strokes use the source's
nine-stroke China supplement without adding inferred geometry. Other regions keep their default
classifications. This presentation layer does not supply manufacturing geometry.

The optional online basemap uses OpenStreetMap Shortbread vector tiles. OSM data
is licensed under ODbL; visible attribution links to
https://www.openstreetmap.org/copyright. The OSMF tile service has separate usage
conditions: https://operations.osmfoundation.org/policies/vector/. Requests are
on-demand through the loopback runtime, cached with seven-day online freshness in
the browser and a bounded persistent local cache, and never prefetched into offline
packages. Cache-only mode reuses previously viewed tiles without contacting the service. The relay forwards only bounded Shortbread tile
coordinates to the fixed OSMF host using the runtime network/proxy settings. Public
service availability is not guaranteed. Local fonts are used for reference labels.

Optional terrain browsing uses Mapzen Terrain Tiles from the AWS Open Data
`elevation-tiles-prod/terrarium` collection:
https://registry.opendata.aws/terrain-tiles/. These are mixed-source elevation
reference tiles, not the manufacturing DEM. Source-dependent terms and required
credits are listed by the publisher at
https://github.com/tilezen/joerd/blob/master/docs/attribution.md; do not label the
entire collection public domain or Apache-2.0. The map links both Mapzen and that
source attribution list. Tiles remain in the operator's runtime cache, with
available source/version headers, original encoded bytes and checksums retained.
Shaded relief is rendered locally from those heights; code bundles contain no
upstream terrain tiles. The public bucket permits no-account access and the listed
source licences permit storage/reuse subject to their terms; there is no OSMF
raster-tile offline-pack rule attached to this separate elevation collection.

Terrain source credits include USGS (3DEP, GMTED2010 and SRTM), NOAA (ETOPO1),
ArcticDEM (DigitalGlobe imagery; NSF awards 1043681, 1559691 and 1542736),
Commonwealth of Australia / Geoscience Australia (2017), offene Daten Österreichs
(Austrian DGM), Canada (Open Government Licence), European Union / Copernicus
(EU-DEM), INEGI (Mexico, Continental relief 2016), LINZ / New Zealand Government
(Crown copyright 2011), Kartverket (Norway), and Environment Agency (UK, 2015).
The linked publisher notices retain the detailed credit wording and underlying
licence links. Preserve those notices when redistributing source or derived data.

Optional place search reuses OSM/Nominatim data under ODbL and displays OSM
attribution on candidate lists and selected locations. The public Nominatim endpoint
has separate conditions: https://operations.osmfoundation.org/policies/nominatim/.
It is enabled only after an explicit service choice, for manually submitted queries,
with a shared rate limiter and persistent response cache. No autocomplete, bulk
geocoding or automatic public-service fallback is implemented. Public distribution
must assess the service policy and aggregate capacity; a different compatible
endpoint can be configured without changing code. Search responses and saved
locations are runtime/user data, not bundled datasets.

An optional local standard-map original is supplied by the user as a JPEG and
source metadata in runtime state. The download serves verified original bytes; the viewer uses locally prepared
pixel tiles without geographic registration. The local display pyramid stays with
the user data and records the source and tile digests;
it does not redistribute that image with TopoForge or change its dataset terms
to Apache-2.0. Source title, URL, digest, pixel dimensions and any supplied licence
or attribution information remain in the local metadata. The viewer itself does
not register or replace any geographic boundary geometry.
