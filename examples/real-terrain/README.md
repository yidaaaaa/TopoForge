# Real terrain example models

These examples use peak-centred, 13 km square Copernicus DEM GLO-30 DSM crops.
They preserve the source grid, map east to `+X` and north to `+Y`, and fit the
complete terrain relief into a `180 mm` footprint with a `48 mm` total-height
limit and a `3 mm` printable base.

```bash
uv run topoforge build --config examples/real-terrain/great-trango-tower.yaml
uv run topoforge build --config examples/real-terrain/mount-thor.yaml
```

The source rasters and acquisition manifests are generated with:

```bash
uv run topoforge fetch-dem \
  --center 76.2014796 35.7574728 --radius-m 6500 \
  --output downloads/examples/great-trango-tower-glo30.tif

uv run topoforge fetch-dem \
  --center -65.3203097 66.5380298 --radius-m 6500 \
  --output downloads/examples/mount-thor-glo30.tif
```

The resulting bundles contain STL and 3MF manufacturing models, a GLB preview,
a rendered PNG, the processed DEM, source NoData mask, provenance, resolved
configuration, checksum manifest, and geometry validation reports.

