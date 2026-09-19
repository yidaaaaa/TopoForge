# Terrain semantics

TopoForge distinguishes DTM, DSM, bathymetry, mixed surface, and unknown. A missing label remains unknown. A horizontal CRS does not establish a vertical CRS or datum. NoData interpolation is reported separately from source validity and never described as measured terrain.

Elevation samples are decoded as `(raw * band_scale + band_offset) * unit_to_m` before reprojection and manufacturing scaling. Metres, international feet, and US survey feet are supported. Conflicting or unsupported declarations are rejected with a corrective error. An absent elevation unit retains the local-input assumption of metres and is explicitly recorded as `assumed-metres`; supply correct band metadata before building data stored in another unit. This unit conversion does not identify or transform the vertical datum.

`provenance.json` records the source scale, offset, unit declaration, conversion factor and formula in `dataset.elevation_conversion`. Processed elevation rasters declare metres with identity scale/offset. Original NoData masks remain separate from converted valid elevations.
