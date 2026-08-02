import { fireEvent, render, screen } from "@testing-library/react";
import * as THREE from "three";
import { describe, expect, it, vi } from "vitest";

import type { AssemblyTile, JobAssemblyOverview } from "../types";
import {
  AssemblyPanel,
  assemblyDisplayBounds,
  tileExplosionOffset,
  withTileVisibility,
} from "./AssemblyPanel";
import { cameraFrameForBounds } from "./TerrainPreview";

const westTile: AssemblyTile = {
  tile_id: "tile-r0000-c0000",
  row: 0,
  column: 0,
  physical_bounds_mm: [0, 0, 32, 24],
  global_bounds_mm: [0, 0, 0, 32, 24, 20],
  triangle_count: 1200,
  volume_mm3: 2000,
  male_connector_ids: ["connector-west"],
  female_connector_ids: [],
  glb_url: "/tile-west.glb",
  glb_sha256: "a".repeat(64),
};

const eastTile: AssemblyTile = {
  ...westTile,
  tile_id: "tile-r0000-c0001",
  column: 1,
  physical_bounds_mm: [32, 0, 64, 24],
  global_bounds_mm: [32, 0, 0, 64, 24, 18],
  male_connector_ids: [],
  female_connector_ids: ["connector-west"],
  glb_url: "/tile-east.glb",
  glb_sha256: "b".repeat(64),
};

const assembly: JobAssemblyOverview = {
  schema_version: "topoforge-web-assembly-v1",
  job_id: "job-phase10",
  layout_id: "layout-phase10",
  model_size_mm: [64, 24],
  tile_grid_shape: [1, 2],
  tile_count: 2,
  seam_count: 1,
  connector_count: 1,
  row_origin: "north",
  column_origin: "west",
  east_axis: "+X",
  north_axis: "+Y",
  up_axis: "+Z",
  aggregate_glb_url: "/assembly.glb",
  connector_map_url: "/connector-map.png",
  tiles: [westTile, eastTile],
  connectors: [
    {
      connector_id: "connector-west",
      seam_id: "seam-west-east",
      direction: "east",
      male_tile_id: westTile.tile_id,
      female_tile_id: eastTile.tile_id,
      seam_coordinate_mm: 32,
      center_along_seam_mm: 12,
      insertion_axis: "+X",
    },
  ],
  required_checks_passed: true,
};

describe("assembly display state", () => {
  it("explodes tiles away from the assembly center without changing Z", () => {
    expect(tileExplosionOffset(westTile, assembly.model_size_mm, 1)).toEqual([
      -6.08,
      0,
      0,
    ]);
    expect(tileExplosionOffset(eastTile, assembly.model_size_mm, 1)).toEqual([
      6.08,
      0,
      0,
    ]);
    expect(tileExplosionOffset(eastTile, assembly.model_size_mm, 0)).toEqual([
      0,
      0,
      0,
    ]);
  });

  it("updates visibility immutably", () => {
    const current = { [westTile.tile_id]: true, [eastTile.tile_id]: true };
    const next = withTileVisibility(current, eastTile.tile_id, false);
    expect(next).not.toBe(current);
    expect(current[eastTile.tile_id]).toBe(true);
    expect(next[eastTile.tile_id]).toBe(false);
  });

  it("expands deterministic display bounds for multi-column exploded layouts", () => {
    const fourColumn = {
      ...assembly,
      model_size_mm: [128, 24] as [number, number],
      tile_grid_shape: [1, 4] as [number, number],
      tile_count: 4,
      tiles: Array.from({ length: 4 }, (_, column) => ({
        ...westTile,
        tile_id: `tile-r0000-c000${column}`,
        column,
        physical_bounds_mm: [column * 32, 0, (column + 1) * 32, 24] as [
          number,
          number,
          number,
          number,
        ],
        global_bounds_mm: [column * 32, 0, 0, (column + 1) * 32, 24, 20] as [
          number,
          number,
          number,
          number,
          number,
          number,
        ],
      })),
    };
    const bounds = assemblyDisplayBounds(fourColumn, {}, 1);
    expect(bounds.min.x).toBeLessThan(0);
    expect(bounds.max.x).toBeGreaterThan(128);
    expect(bounds.max.x - bounds.min.x).toBeCloseTo(164.48);

    const camera = new THREE.PerspectiveCamera(35, 0.55, 0.01, 10000);
    camera.up.set(0, 0, 1);
    const frame = cameraFrameForBounds(bounds, camera.aspect, camera.fov);
    camera.position.copy(frame.position);
    camera.near = frame.near;
    camera.far = frame.far;
    camera.lookAt(frame.center);
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld();
    for (const x of [bounds.min.x, bounds.max.x]) {
      for (const y of [bounds.min.y, bounds.max.y]) {
        for (const z of [bounds.min.z, bounds.max.z]) {
          const projected = new THREE.Vector3(x, y, z).project(camera);
          expect(Math.abs(projected.x)).toBeLessThan(1);
          expect(Math.abs(projected.y)).toBeLessThan(1);
        }
      }
    }
  });

  it("selects 2D tiles and exposes per-tile visibility controls", () => {
    const onSelect = vi.fn();
    render(
      <AssemblyPanel
        language="en"
        assembly={assembly}
        selectedTileId={westTile.tile_id}
        loading={false}
        error={null}
        onSelectedTileChange={onSelect}
      />,
    );

    fireEvent.click(screen.getByText("R1 C2"));
    expect(onSelect).toHaveBeenCalledWith(eastTile.tile_id);
    expect(screen.getByText("2/2")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: `Hide tile ${eastTile.tile_id}`,
      }),
    );
    expect(screen.getByText("1/2")).toBeInTheDocument();
  });
});
