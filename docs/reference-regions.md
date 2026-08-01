# Reference regions

`reference_regions/catalog.yaml` freezes seven bounded AOI normalization cases and four
retained real terrain evidence bindings.

| ID | Input | Contract | Retained data |
| --- | --- | --- | --- |
| amazon-low-relief | bbox | equatorial tile-edge AEQD | yes |
| gongga-high-relief | bbox | UTM 47N and fidelity | yes |
| great-trango-center-radius | center + 6.5 km | UTM 43N | yes |
| mount-thor-high-latitude | center + 6.5 km | UTM 20N | yes |
| antimeridian-bbox | bbox | date-line split and AEQD | no |
| polar-center-radius | center + 25 km | above UTM limit and AEQD | no |
| cross-zone-bbox | bbox | spans UTM zones and AEQD | no |

Each definition pins normalized WGS84 bounds, antimeridian classification, metric CRS,
geodesic area, and the canonical normalized-record SHA-256.

Definition-only CI verification needs no retained DEM or network:

```bash
uv run python scripts/verify_reference_regions.py \
  --catalog reference_regions/catalog.yaml \
  --definitions-only \
  --report artifacts/logs/reference-definitions.json
```

On the workstation with existing ignored data:

```bash
uv run python scripts/verify_reference_regions.py \
  --catalog reference_regions/catalog.yaml \
  --report artifacts/logs/reference-regions.json
```

Retained mode hashes but does not rewrite four source rasters, rereads provenance and
validation, requires `+X East` and `+Y North`, and checks grids, horizontal
resolutions, peak loss, peak shift, triangles, and required gates. The script has no
network client and reports `network_attempts: 0`.

The catalog does not bundle or redistribute terrain datasets. Dataset licenses, source
identity, DSM semantics, vertical references, and attribution remain in existing records.
