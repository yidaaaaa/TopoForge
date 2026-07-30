# Printing and slicer validation

Manufacturing coordinates are millimetres. STL relies on this coordinate contract; 3MF explicitly stores millimetre units. The chosen elevation baseline maps to the configured base reference above a flat `z=0` bottom. Minimum-baseline builds place the lowest terrain sample exactly there; other datum modes preserve their absolute offset and must still pass the printer-profile minimum-material gate.

TopoForge discovers OrcaSlicer first and falls back to PrusaSlicer. The executed milestone used:

```bash
uv run topoforge slice outputs/milestone-01-synthetic/model.3mf \
  --output artifacts/slicer/milestone-01-final.gcode
```

PrusaSlicer 2.4.0 exited 0, generated 5,030,602 bytes, 140 layers, estimated 7h 11m 41s, and reported 40,552.39 mm / 97.54 cm3 filament with no support, out-of-bed, empty-layer, or floating-region warning. The exact result is in `artifacts/reports/milestone-01-3mf-slice.json`.

Official OrcaSlicer 2.4.2 targets newer Ubuntu runtimes than this host. Official generic OrcaSlicer 2.3.0 and installed PrusaSlicer both completed separate real test slices; see `artifacts/reports/slicer-research.md`.
