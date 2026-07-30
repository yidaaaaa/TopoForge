# Elevation data sources

Checked: **2026-07-31 UTC**. This is an engineering record of the terms and
interfaces found at the cited sources; the source terms govern the data.
TopoForge must record the exact dataset release, asset URL, access time, source
metadata, checksum, license URL, and attribution used by each build.

## Status vocabulary

- **Verified** means the statement was present in a current official page,
  license, catalog record, package file, or a live endpoint response checked on
  the date above.
- **Operational conclusion** means a conservative TopoForge policy derived from
  verified facts.
- **Unresolved** means the source material was silent, inconsistent, or could
  change independently of TopoForge. These items must not be presented as fact
  in generated provenance.

## Recommended no-key global route

Use the **Copernicus DEM AWS Open Data COG mirror**, preferring GLO-30 Public
where a tile exists and falling back to GLO-90 for land tiles missing from the
30 m mirror.

Verified:

- The public buckets are `s3://copernicus-dem-30m/` and
  `s3://copernicus-dem-90m/`; the AWS registry explicitly gives
  `--no-sign-request` commands and says no AWS account is required.
- HTTPS bucket listing, `tileList.txt`, and a real GLO-30 COG returned HTTP 200
  without credentials during this review.
- The mirror contains the **Copernicus DEM 2021 release** as Cloud Optimized
  GeoTIFF. It is not the same release channel as the newer Copernicus Data Space
  Ecosystem (CDSE) catalog.
- GLO-30 is a DSM. GLO-90 is also a DSM. Neither is a bare-earth DTM.
- Ocean areas have no tiles. A missing tile is therefore not, by itself,
  evidence that every sample in the requested AOI is ocean.

Operational conclusion:

- Provider id: `copernicus-aws`; resolved dataset ids must be
  `copernicus-dem-glo-30-aws-2021` or `copernicus-dem-glo-90-aws-2021` per
  source tile, never a synthetic blended dataset name.
- Map the resolved dataset to its own licence id:
  `Copernicus-DEM-GLO-30-F` for GLO-30 and
  `Copernicus-DEM-GLO-90-F` for GLO-90. The grants are similar, but their
  adapted-product and liability notices name different products and are not
  interchangeable.
- Treat `tileList.txt` or a successfully resolved STAC item as the coverage
  authority. Do not infer availability from a filename formula alone.
- Cache each immutable source object with its URL, ETag, Last-Modified value,
  byte length, and SHA-256. The GLO-30/GLO-90 free-and-open licenses permit
  reproduction and distribution, subject to their attribution, disclaimer,
  non-endorsement, and downstream-notice requirements.
- Do not silently turn absent coastal/ocean samples into zero. Preserve a mask
  and use an explicit water-surface policy or a bathymetry provider.
- Keep bucket, STAC, and tile-list endpoints configurable. The advertised STAC
  bucket currently exposes `dem_cop_30.json`; `/catalog.json` returned 404 in
  this check.

Unresolved:

- The AWS registry still describes a small set of unreleased GLO-30 tiles,
  while CDSE reports a later free-and-open release of previously restricted
  countries. The mirror advertises the 2021 release and must be treated as
  older and potentially incomplete until its own tile list changes.
- AWS publishes no application-specific availability SLA or fixed request-rate
  guarantee on the reviewed registry page. Use bounded concurrency, retries,
  range requests, and the local cache.

Sources:

- [AWS Registry of Open Data: Copernicus DEM](https://registry.opendata.aws/copernicus-dem/)
- [AWS mirror layout and processing notes](https://copernicus-dem-30m.s3.amazonaws.com/readme.html)
- [GLO-30 tile list](https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/tileList.txt)
- [GLO-30 STAC collection](https://copernicus-dem-30m-stac.s3.amazonaws.com/dem_cop_30.json)
- [Copernicus DEM license bundle, issue 2025-02-21](https://dataspace.copernicus.eu/sites/default/files/media/files/2025-06/copernicus_contributing_mission_data_access_v2_cop_dem_licenses.pdf)

## Decision matrix

| Route or dataset | Terrain semantics | Resolution and coverage | Account/key | Commercial path | Cache and redistribution | TopoForge role |
| --- | --- | --- | --- | --- | --- | --- |
| User-supplied local GeoTIFF/DEM | Declared by the user and checked against metadata | Asset dependent | None | Unknown until the user supplies applicable rights; local processing does not establish redistribution rights | Cache only inside the user's workspace by default; do not redistribute source or derivatives as license-compatible without a declared basis | Highest-priority MVP source |
| Copernicus AWS GLO-30 + GLO-90 | DSM | 30 m where mirrored; 90 m worldwide land fallback | None | Compatible with the applicable custom free/open license obligations | Permitted with attribution, disclaimer, non-endorsement, and downstream notices | Default no-key global land route |
| Copernicus GLO-30 via CDSE | DSM | 30 m worldwide land | Free CDSE registration; authenticated APIs/bulk credentials | Compatible with GLO-30-F license obligations | Reproduction/distribution/adaptation permitted with obligations | Newer authenticated Copernicus route |
| NASADEM HGT V001 | Radar-derived, void-filled mixed surface; not a verified DTM | 1 arc-second, land from 60 N to 56 S | Earthdata Login/OAuth; no user-supplied API key string | General NASA/ESDIS reuse permission applies absent a marking; a NASADEM-specific CC0 designation was not found | General ESDIS terms permit reproduction/distribution of unmarked NASA material; preserve citation and check each asset for a contrary marking | Authenticated global fallback |
| USGS 3DEP DEM | Bare-earth DTM for standard DEM products | 1 m where available; seamless about 10 m and 30 m; U.S. and territories, product dependent | None through TNM Access/public S3/WCS | Public domain | Cache and redistribution allowed; USGS requests credit | Preferred regional U.S. source |
| OpenTopography APIs | Dataset dependent | Global DEMs and regional high-resolution data | Personal API key; quotas; Enterprise key for for-profit integration | Dataset license may permit commercial use, but commercial API integration needs Enterprise terms | Underlying data may be redistributed under its dataset terms; no explicit cache-retention rule was found | Optional user-configured service |
| GEBCO_2026 Grid | Mixed land topography and ocean bathymetry | 15 arc-seconds, global | None for direct/subset download | Public domain; commercial exploitation expressly permitted | Copy, publish, distribute, adapt, and cache with acknowledgment | Default bathymetry/mixed-surface candidate |
| FABDEM V1-2 | Machine-learning-corrected DTM-like surface | 1 arc-second, land tiles from 60 S to 80 N | None for repository downloads | **Not compatible with the default commercial path** | Non-commercial caching/sharing only; attribution and ShareAlike apply conservatively | Explicit opt-in non-commercial provider only |

## User-supplied local GeoTIFF or DEM

Local data is the highest-priority MVP source, but a file format is not a
licence. TopoForge must not infer ownership, commercial permission, or
redistribution permission from the fact that a user can read a GeoTIFF.

Provider policy:

- Provider id: `local-raster`; the resolved dataset id is a stable user label
  plus the source SHA-256, not only a mutable filename.
- Record the original byte checksum, size, modification time, raster driver,
  dimensions, sample dtype, transform, horizontal CRS, stated vertical
  CRS/datum, unit, NoData value/mask, and any embedded copyright/source tags.
- Preserve relevant sidecars and metadata in the build manifest. A missing
  vertical datum remains `unknown`; an EPSG horizontal CRS does not establish a
  vertical datum.
- Require an explicit user declaration for dataset name, source, licence id or
  terms URL, attribution, and whether commercial use and redistribution are
  allowed. Store `unknown` rather than inventing values.
- Local processing is allowed without uploading the source. Until rights are
  declared, cache only inside the user's workspace and mark source and derived
  artifacts `redistribution_status: unknown`; do not advertise the build as
  commercially compatible or redistributable.
- Never copy a local source into examples, test fixtures, release archives, or
  a shared cache unless its terms explicitly allow that use. Repository
  fixtures must be independently generated or separately licensed.

## Copernicus DEM GLO-30

### Verified Copernicus dataset facts

- CDSE identifies GLO-30 as a **Digital Surface Model** representing the
  top-reflective surface, including buildings, infrastructure, and vegetation.
- TanDEM-X acquisition was primarily 2011-2015; older DEMs can appear in filled
  areas. Water bodies, shorelines, airports, and implausible structures received
  editing.
- Worldwide land coverage is about 149 million km2 at 30 m. The public product
  uses approximately one-arc-second spacing, with longitude-dependent row
  widths at high latitudes.
- DGED/DTED products use geographic coordinates, WGS 84 (EPSG:4326), and
  **EGM2008 (EPSG:3855)** as the stated vertical reference. DGED is GeoTIFF,
  float32, pixel-is-point, commonly packaged in 1 by 1 degree geocells.
- CDSE lists releases through `2024_1` (July 2024) and recommends the dataset DOI
  `10.5270/ESA-c5d3d65` for citation.
- General-public users may register and download GLO-30 and GLO-90. Full
  collection bulk access is available through S3-compatible object storage and
  OData using temporary credentials.

### GLO-30 and GLO-90 license, attribution, and redistribution

The current license bundle contains distinct sections titled
`COP-DEM-GLO-30-F Global 30m Full, Free & Open` and
`COP-DEM-GLO-90-F Global 90m Full, Free & Open`. Each grants worldwide,
time-unlimited rights of reproduction, distribution, communication to the
general public, adaptation, modification, and combination, free of charge.
Neither imposes a non-commercial limitation. A provider must nevertheless
retain the licence id matching each resolved tile.

For unmodified public communication/distribution of either product, preserve
this notice exactly:

> © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018
> provided under COPERNICUS by the European Union and ESA; all rights reserved.

For adapted or modified GLO-30 data, preserve this notice:

> produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus
> Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European
> Union and ESA; all rights reserved

For adapted or modified GLO-90 data, preserve this separate notice:

> produced using Copernicus WorldDEM™-90 © DLR e.V. 2010-2014 and © Airbus
> Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European
> Union and ESA; all rights reserved

Distribution or public communication of GLO-30, modified or not, must include
this sentence or a translation:

> The organisations in charge of the Copernicus programme by law or by
> delegation do not incur any liability for any use of the Copernicus
> WorldDEM-30.

The equivalent mandatory sentence for GLO-90 is:

> The organisations in charge of the Copernicus programme by law or by
> delegation do not incur any liability for any use of the Copernicus
> WorldDEM™-90.

Distribution or public communication must also bind downstream distributors to
the same attribution, liability, and non-endorsement obligations. TopoForge
must store the complete license URL, resolved product, and matching notices in
provenance; a generic `open-data` label is insufficient. The GLO-30 AWS STAC
record reports `license: proprietary`, which is another reason to use the
custom licence ids above rather than an SPDX open-content identifier.

Caching is an exercise of reproduction and is permitted under these terms.
Redistribution is permitted only with all source, liability, non-endorsement,
and downstream obligations intact.

### Access and implementation caveats

- CDSE is an account-backed route; the AWS route above is the no-key route.
- Preserve the editing, filling, water-body, and height-error masks when they
  are available. They are material provenance, not optional decorations.
- Do not label the result DTM, and do not claim the nominal 30 m spacing is an
  independent measurement resolution in filled areas.
- CDSE says the collection would be maintained until 2026. The post-2026
  maintenance/versioning plan was not stated on the reviewed page and remains
  unresolved.

Sources:

- [CDSE Copernicus DEM collection](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM)
- [Copernicus DEM license bundle](https://dataspace.copernicus.eu/sites/default/files/media/files/2025-06/copernicus_contributing_mission_data_access_v2_cop_dem_licenses.pdf)
- [CDSE bulk-download FAQ](https://documentation.dataspace.copernicus.eu/FAQ.html#how-can-i-download-copenicus-data-in-bulk-mode)
- [Dataset DOI](https://doi.org/10.5270/ESA-c5d3d65)

## NASADEM

### Verified NASADEM dataset facts

- Current product: **NASADEM Merged DEM Global 1 arc second V001**
  (`NASADEM_HGT.001`), DOI
  `10.5067/MEASURES/NASADEM/NASADEM_HGT.001`.
- Coverage is land between 60 N and 56 S, roughly 80% of Earth's landmass.
- Native HGT distribution is 1 by 1 degree, 3601 by 3601 samples, big-endian
  signed 16-bit integer metres. NetCDF4 companion products are also available.
- The merged HGT layer is void-filled and referenced to the **EGM96 geoid**.
  The official guide distinguishes it from SRTM-only floating-point layers,
  which are WGS84 ellipsoidal heights.
- NASADEM derives from SRTM radar data and uses ASTER, ALOS, ICESat, and other
  information for control and fill. Radar vegetation response and filled
  values mean it must not be represented as a surveyed bare-earth DTM.
- CMR says `AccessConstraints: None`, but the current granule payload URLs are
  in `lp-prod-protected`. A credential endpoint redirects to Earthdata OAuth,
  and an unauthenticated payload fetch ended at HTTP 401. Data rights and
  download authentication are separate concerns.

### License, attribution, caching, and redistribution

The NASADEM CMR record has `AccessConstraints: None`, supplies no
dataset-specific licence, and points to NASA's general ESDIS data-use policy.
That policy says ESDIS material is generally not copyrighted and that unmarked
NASA material may be reproduced and distributed without further NASA
permission, subject to attribution, non-endorsement, and other stated
conditions. It separately assigns CC0 to unmarked data from a NASA-led
mission, and warns that non-NASA material retains the sponsoring
organisation's terms.

NASADEM is a NASA MEaSUREs product assembled with SRTM plus ASTER, ALOS,
ICESat, and other inputs. Neither the CMR record nor the reviewed user guide
explicitly designates the merged product as CC0. TopoForge may rely on the
general ESDIS reproduction/use statement for an unmarked NASADEM asset, but
must record the licence as `NASA-ESDIS-general` (or `unknown` if an asset is
marked), not `CC0`, and must not claim that every constituent source is CC0.
NASA should be acknowledged, the dataset should be cited, and use must not
imply NASA endorsement.

Recommended citation record:

> NASA JPL. (2020). NASADEM Merged DEM Global 1 arc second V001 [Dataset].
> NASA Land Processes Distributed Active Archive Center.
> https://doi.org/10.5067/MEASURES/NASADEM/NASADEM_HGT.001. Accessed DATE.

### NASADEM provider policy

- Require a user Earthdata account and use an OAuth/token-capable library such
  as `earthaccess`; never commit credentials or copy a user's token into
  provenance.
- Cache the protected download only after successful authentication, together
  with the granule id, CMR concept ids, source URL, checksum, and access time.
- Inspect the collection and granule metadata for a use restriction or
  third-party notice at acquisition time. A contrary asset-level marking
  overrides the general ESDIS policy and disables incompatible commercial or
  redistribution modes.
- Preserve the NUM/source layer and water mask when fetched. They distinguish
  measured/reprocessed values from fill sources.
- Treat the old license URL embedded in CMR as a redirectable identifier; cite
  the current data-use page below in user-facing documentation.

Unresolved:

- A NASADEM-specific licence or explicit CC0 designation was not present in the
  reviewed CMR record or user guide. Seek LP DAAC clarification before using a
  `CC0-1.0` identifier in generated provenance.
- Earthdata may revise its login/token flow independently of the collection.
  Keep authentication behind a provider adapter and test it only in opt-in
  integration tests.

Sources:

- [NASADEM Earthdata catalog](https://www.earthdata.nasa.gov/data/catalog/lpcloud-nasadem-hgt-001)
- [Authoritative CMR collection record](https://cmr.earthdata.nasa.gov/search/collections.umm_json_v1_18_2?pretty=true&include_granule_counts=true&concept-id%5B%5D=C2763264762-LPCLOUD)
- [NASADEM User Guide V1.3](https://lpdaac.usgs.gov/documents/2237/NASADEM_User_Guide_V13.pdf)
- [NASA ESDIS data use and citation guidance](https://www.earthdata.nasa.gov/engage/open-data-services-software-policies/data-use-guidance)
- [Earthdata Search collection](https://search.earthdata.nasa.gov/search/granules?p=C2763264762-LPCLOUD)

## USGS 3DEP

### Verified 3DEP dataset and access facts

- Standard 3DEP DEMs represent bare-earth terrain and flatten water surfaces.
- The project-based 1 m DEM has limited but expanding U.S. coverage. The new
  seamless 1 m (S1M) product has been in production since mid-2025 and is
  distributed as 10 km by 10 km Cloud Optimized GeoTIFF tiles.
- The seamless 1/3 arc-second product has full coverage of the conterminous
  states, Alaska, Hawaii, and U.S. territories at about 10 m north/south
  spacing. The seamless 1 arc-second product covers the conterminous U.S. and
  Alaska, plus much of Canada and Mexico, at about 30 m north/south spacing.
- The TNM Access API is a current, no-key discovery interface and returns
  public HTTPS/S3 `downloadURL` values. Its live dataset catalog includes the
  exact tags `Digital Elevation Model (DEM) 1 meter`, `Seamless 1-m DEM (S1M)`,
  and `National Elevation Dataset (NED) 1/3 arc-second`.
- The 3DEP Bare Earth Dynamic ImageServer exposes WMS and WCS. During this
  check its service description said the mosaic reflected data published as of
  2026-07-20.
- The reviewed USGS product pages label these data **Public Domain**. USGS asks
  for proper credit, for example `Credit: U.S. Geological Survey` or
  `Source of [data name]: U.S. Geological Survey`.

### Cache and redistribution

USGS-authored/produced data are in the U.S. public domain. TopoForge may cache,
adapt, redistribute, and use them commercially. Preserve the concrete product
title, publication/update date, ScienceBase metadata URL, asset URL, checksum,
and requested USGS credit. Do not use the trademarked USGS identifier/logo as
if it endorsed TopoForge.

### 3DEP provider policy

- Prefer TNM pre-staged GeoTIFF/COG assets for reproducible builds; use WCS for
  bounded extracts only when its exact request and returned metadata are saved.
- Query with the exact dataset tag and AOI, paginate, choose current rather than
  historical assets explicitly, and retain every returned product id.
- Read the horizontal and vertical CRS from each product and its metadata.
  3DEP spans different acquisition projects and datum epochs; a provider must
  not assign one vertical datum to every asset by convention.
- Use 1 m/S1M only when coverage, print scale, and triangle budget justify it;
  otherwise prefer the seamless 1/3 arc-second product.

Unresolved:

- The TNM API documentation reviewed here states pagination behavior but no
  fixed public rate quota. Implement polite concurrency, exponential backoff,
  and caching rather than treating the absence of a published quota as
  unlimited capacity.
- Vertical datum is an asset-level fact until verified from embedded/sidecar
  metadata.

Sources:

- [3DEP products and services](https://www.usgs.gov/3d-elevation-program/about-3dep-products-services)
- [TNM Access API documentation](https://tnmaccess.nationalmap.gov/api/v1/docs)
- [TNM Access dataset catalog](https://tnmaccess.nationalmap.gov/api/v1/datasets?outputFormat=JSON)
- [3DEP Bare Earth Dynamic ImageServer](https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer?f=pjson)
- [3DEP WCS capabilities](https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/WCSServer?request=GetCapabilities&service=WCS)
- [USGS copyright and credit policy](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits)
- [USGS acknowledgment examples](https://www.usgs.gov/information-policies-and-instructions/acknowledging-or-crediting-usgs)

## OpenTopography

### Verified service facts

- Every Global Datasets API request requires a personal OpenTopography API key.
  The Global API includes NASADEM, COP30, COP90, SRTM, ALOS, SRTM15+, GEBCO
  variants, and other datasets.
- Output formats are `GTiff` (default), `AAIGrid`, and `HFA`.
- Maximum request area is 450,000 km2 for 30 m datasets, 4,050,000 km2 for
  SRTM GL3/COP90, 125,000,000 km2 for SRTM15+/GEBCO variants, and
  500,000,000 km2 for GEDI L3.
- Free keys are limited to 200 calls per 24 hours for academic users and 50 for
  non-academic users. The USGS API has the same daily call limits, with 1 m
  access currently restricted to academic users.
- Keys may not be shared, made public, or embedded so third parties bypass the
  one-user/one-key rule.
- For-profit integration of OpenTopography API keys into a product or service
  requires an Enterprise API key. Higher-volume use is also an Enterprise use
  case.
- OpenTopography says API data are available for non-commercial and commercial
  use, but also makes users responsible for each underlying dataset's license,
  citation, and acknowledgment. Dataset-specific terms therefore remain
  authoritative.

Required service acknowledgment for API use:

> This work is based on API services provided by the OpenTopography Facility
> with support from the National Science Foundation under NSF Award Numbers
> 2410799, 2410800 & 2410801.

### TopoForge policy

- This is an **optional user-configured provider**, not the no-key default.
- Open-source distribution may let an individual supply their own personal key,
  but TopoForge must not ship, proxy, log, or expose that key. A hosted or
  for-profit TopoForge service needs appropriate Enterprise terms.
- Record both the OpenTopography service acknowledgment and the selected
  dataset's own citation/license. `COP30 through OpenTopography` is still
  Copernicus data; `NASADEM through OpenTopography` is still NASADEM.
- Respect the area limit before sending a request and fail with a calculated
  suggested split rather than repeatedly submitting oversized requests.

Cache/redistribution assessment:

- The reviewed terms do not prohibit caching of returned raster data, and they
  describe the data as freely reusable subject to dataset licenses. No explicit
  cache lifetime or local-retention policy was found. Cache returned data only
  under the underlying dataset's rules, preserve full provenance, and do not
  build a bulk mirror of the service without written clarification.

Sources:

- [OpenTopography developer/API overview](https://opentopography.org/developers)
- [OpenAPI schema](https://portal.opentopography.org/apidocs/openapi.json)
- [OpenTopography Terms of Use, updated 2025-10-08](https://opentopography.org/usageterms)
- [Citation and acknowledgment policy](https://opentopography.org/citations)

## GEBCO

### Verified GEBCO dataset facts

- Current release: **GEBCO_2026 Grid**, published 2026-04-23.
- It is a continuous global model of ocean bathymetry and land topography in
  metres on a 15 arc-second grid. A companion Type Identifier (TID) Grid
  describes the kind of source underlying each grid cell.
- It is available as global netCDF, eight 90 by 90 degree GeoTIFF or Esri ASCII
  tiles, user-defined subsets, and through CEDA/OPeNDAP. No user key is stated
  for these download routes, and anonymous HEAD requests succeeded in this
  review.
- GEBCO assumes source data are referred to mean sea level, but explicitly warns
  that some shallow-water inputs use a different vertical datum. The vertical
  reference must therefore be recorded as a documented heterogeneous/assumed
  MSL model, not as a rigorously uniform datum.
- Nominal grid spacing is not the measurement resolution. GEBCO warns that the
  interpolated grid can differ significantly from the resolution of underlying
  observations.

### Terms, attribution, caching, and redistribution

GEBCO places the Grid in the public domain and expressly permits copying,
publishing, distribution, transmission, adaptation, and commercial
exploitation. Users must acknowledge GEBCO, must not imply endorsement, and
must not misrepresent the Grid or source. It is not for navigation or other
safety-at-sea uses.

Use the release-specific attribution:

> GEBCO Bathymetric Compilation Group 2026 (2026). The GEBCO_2026 Grid - a
> continuous terrain model for oceans and land at 15 arc-second intervals.
> doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa

Caching and redistribution are permitted with that acknowledgment and the
disclaimer/non-endorsement conditions. Preserve the TID grid alongside cached
elevation subsets whenever practical.

Sources:

- [GEBCO gridded bathymetry data](https://www.gebco.net/data-products/gridded-bathymetry-data)
- [GEBCO Grid terms of use](https://www.gebco.net/data-products/gridded-bathymetry/terms-of-use)
- [GEBCO_2026 DOI](https://doi.org/10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa)
- [GEBCO subset application](https://download.gebco.net/)
- [CEDA GEBCO_2026 directory](https://data.ceda.ac.uk/bodc/gebco/global/gebco_2026)

## FABDEM

### Verified FABDEM dataset facts

- Current repository record: **FABDEM V1-2**, released January 2023, DOI
  `10.5523/bris.s5hqmjcdj8yo2ibzi9b4ew3sn`.
- FABDEM removes estimated forest and building height bias from Copernicus
  GLO-30 using machine-learning corrections and post-processing. It is closer
  to a DTM than the source DSM, but it is not a surveyed bare-earth surface and
  retains residual artifacts and model error.
- Coverage is 60 S to 80 N at one-arc-second spacing. The repository tile index
  contained 19,011 one-degree land tiles over that bounding range.
- V1-2 uses Copernicus DEM `2021_1`, aligns high-latitude grids, stores Cloud
  Optimized GeoTIFF with DEFLATE/PREDICTOR=2, and correctly labels samples as
  pixel-is-point.
- The publication says reference data were transformed to Copernicus vertical
  coordinates, **EGM2008**.
- Direct repository assets and the tile GeoJSON are accessible without a key.
  The complete archive is about 296.7 GiB; a provider should select regional
  ZIP groups from the tile index rather than download the full archive.

### License and commercial status

The dataset abstract and bundled `license.txt` state
**CC BY-NC-SA 4.0**. That permits sharing and adaptation only for
non-commercial purposes, requires attribution, and requires adaptations to use
the same or a compatible license. Commercial-use requests are directed to
`fabdem@fathom.global`.

The repository's separate structured `Licence` field displays
`Non-Commercial Government Licence for public sector information`, while the
dataset abstract and actual bundled license file say CC BY-NC-SA 4.0. This is a
verified metadata inconsistency. TopoForge must apply the stricter combined
interpretation, expose the exact source notices, and keep the provider disabled
for commercial/default selection unless the publisher supplies clarifying or
commercial terms.

Recommended citation:

> Jeffrey Neal, Laurence Hawker (2023): FABDEM V1-2.
> https://doi.org/10.5523/bris.s5hqmjcdj8yo2ibzi9b4ew3sn

Cache/redistribution assessment:

- Local caching is allowed only for an allowed non-commercial use.
- Redistribution must remain non-commercial, provide attribution and license
  link, indicate modifications, and apply ShareAlike to adapted material.
- Preserve Copernicus as the upstream data source in provenance. Whether every
  downstream FABDEM artifact must also reproduce the Copernicus adapted-data
  notice is not clarified by the FABDEM landing page; TopoForge should preserve
  both FABDEM and Copernicus notices until the publisher clarifies this point.

Sources:

- [University of Bristol FABDEM V1-2 record](https://doi.org/10.5523/bris.s5hqmjcdj8yo2ibzi9b4ew3sn)
- [Bundled FABDEM license](https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn/license.txt)
- [FABDEM V1-2 tile index](https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn/FABDEM_v1-2_tiles.geojson)
- [FABDEM V1-2 changelog](https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn/FABDEM-V1-2%20Changelog.pdf)
- [FABDEM paper](https://doi.org/10.1088/1748-9326/ac4d4f)
- [CC BY-NC-SA 4.0 deed](https://creativecommons.org/licenses/by-nc-sa/4.0/)

## Provider-selection consequences

1. License suitability is a hard filter, not a ranking weight. A build marked
   commercial-compatible cannot select FABDEM under the public terms.
2. Terrain semantics are a hard filter unless the user explicitly accepts a
   different surface. GLO-30 and NASADEM must not silently satisfy a DTM-only
   request; GEBCO must not silently satisfy land-only DTM semantics.
3. A nominal pixel spacing is not measurement resolution. Preserve fill/source
   masks and dataset quality metadata.
4. The vertical datum is per resolved dataset/asset. Mixed or uncertain sources
   remain `unknown` or an explicit heterogeneous classification; they are never
   inferred from location.
5. Provider fallback must record every attempted provider, failure class,
   resolved fallback, release, and license change. A fallback from 3DEP DTM to
   Copernicus DSM is a semantic downgrade and needs explicit policy approval.
6. Caches hold third-party data under third-party terms. Cache manifests must
   include license URL, attribution text, access time, immutable source
   identifiers, ETag/Last-Modified where available, and SHA-256.

## Live checks executed

Representative evidence from the 2026-07-31 review:

```text
GET https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/?list-type=2&max-keys=20
HTTP 200; anonymous XML bucket listing returned objects.

HEAD https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/
  Copernicus_DSM_COG_10_N27_00_E101_00_DEM/
  Copernicus_DSM_COG_10_N27_00_E101_00_DEM.tif
HTTP 200; Content-Type image/tiff; Content-Length 41787273.

GET https://tnmaccess.nationalmap.gov/api/v1/products
  ?datasets=National%20Elevation%20Dataset%20(NED)%201/3%20arc-second
  &bbox=-105,39,-104,40&max=1&outputFormat=JSON
HTTP 200; total 47; response included a public prd-tnm.s3.amazonaws.com GeoTIFF URL.

GET https://elevation.nationalmap.gov/arcgis/rest/services/
  3DEPElevation/ImageServer?f=pjson
HTTP 200; service reported source data published through 2026-07-20.

GET https://data.lpdaac.earthdatacloud.nasa.gov/s3credentials
HTTP 307 to Earthdata OAuth.

GET an lp-prod-protected NASADEM HGT granule without credentials
Final HTTP 401.

HEAD GEBCO_2026 netCDF global ZIP at dap.ceda.ac.uk
HTTP 200; Content-Type application/zip.

GET FABDEM V1-2 license.txt and tile GeoJSON
HTTP 200; bundled license is CC BY-NC-SA 4.0; tile index has 19,011 features.
```

## Open items to recheck before Phase 3 release

- AWS Copernicus mirror release/coverage freshness versus CDSE.
- CDSE's post-2026 Copernicus DEM maintenance and access plan.
- OpenTopography's cache-retention policy for a local application cache.
- Asset-level vertical CRS/datum parsing across all selected 3DEP products.
- FABDEM's conflicting repository license field and upstream Copernicus notice.
- Actual provider integration tests for one land, coastal, antimeridian, and
  high-latitude AOI; online tests must remain opt-in.
