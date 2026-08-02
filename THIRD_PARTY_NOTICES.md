# Third-Party Notices

The locked Python runtime uses permissively licensed libraries including NumPy (BSD and bundled component notices), Rasterio (BSD-3-Clause), PyProj (MIT), Shapely (BSD-3-Clause), Trimesh (MIT), Pillow (MIT-CMU), Pydantic (MIT), Typer (MIT), PyYAML (MIT), SciPy (BSD-3-Clause and wheel component notices), lib3mf 2.5.0 (BSD-2-Clause), FastAPI (MIT), Uvicorn (BSD-3-Clause), and their transitive dependencies. Exact Python versions and hashes are in `uv.lock`; installed distributions carry their full notices.

The local Web application uses React and React DOM (MIT), MapLibre GL JS (BSD-3-Clause), Three.js (MIT), and Lucide React (ISC) at runtime. Its development and verification toolchain uses Vite and Vitest (MIT), TypeScript and Playwright (Apache-2.0), Testing Library packages (MIT), and their transitive dependencies. Exact npm versions and integrity hashes are in `web/package-lock.json`; these dependencies are not copied into the Python wheel. The wheel contains only the compiled Web assets and the checksum manifest produced from them.

OrcaSlicer and PrusaSlicer are separately installed GNU AGPL-3.0 applications. They are executed as independent processes and are not included in TopoForge source or packages.
