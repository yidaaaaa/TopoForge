# Benchmarks

The first milestone evidence uses a 64 x 80 analytic GeoTIFF (5,120 samples), producing 10,240 vertices and 20,476 triangles. The final model is 180 x 144 x 42.0 mm. Strict lib3mf reread reports one named object, one build item, and zero warnings. PrusaSlicer completed the 3MF slice in about 2 seconds of wall-clock CLI time on this host; slicer-estimated print time is 7h 11m 41s.

The engine currently protects large inputs with `max_grid_cells` and average downsampling. The formal 256/1024/4096 raster benchmark matrix and printer-aware triangle policy are Phase 4 work and remain tracked in `.agent/PLANS.md`.
