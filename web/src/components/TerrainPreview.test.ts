import { describe, expect, it } from "vitest";
import * as THREE from "three";

import {
  cameraFrameForBounds,
  terrainColorForNormalizedHeight,
} from "./TerrainPreview";

describe("cameraFrameForBounds", () => {
  it.each([0.72, 1, 1.8])("keeps every corner visible at aspect %s", (aspect) => {
    const bounds = new THREE.Box3(
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(180, 125, 45),
    );
    const camera = new THREE.PerspectiveCamera(35, aspect, 0.01, 10000);
    camera.up.set(0, 0, 1);
    const frame = cameraFrameForBounds(bounds, aspect, camera.fov);
    camera.position.copy(frame.position);
    camera.near = frame.near;
    camera.far = frame.far;
    camera.lookAt(frame.center);
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld(true);

    for (const x of [bounds.min.x, bounds.max.x]) {
      for (const y of [bounds.min.y, bounds.max.y]) {
        for (const z of [bounds.min.z, bounds.max.z]) {
          const projected = new THREE.Vector3(x, y, z).project(camera);
          expect(Math.abs(projected.x)).toBeLessThan(0.92);
          expect(Math.abs(projected.y)).toBeLessThan(0.92);
          expect(projected.z).toBeGreaterThan(-1);
          expect(projected.z).toBeLessThan(1);
        }
      }
    }
  });

  it("keeps east on screen-right and north toward screen-top", () => {
    const bounds = new THREE.Box3(
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(180, 180, 45),
    );
    const camera = new THREE.PerspectiveCamera(32, 1, 0.01, 10000);
    camera.up.set(0, 0, 1);
    const frame = cameraFrameForBounds(bounds, 1, camera.fov);
    camera.position.copy(frame.position);
    camera.lookAt(frame.center);
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld(true);

    const center = frame.center.clone().project(camera);
    const east = frame.center
      .clone()
      .add(new THREE.Vector3(20, 0, 0))
      .project(camera);
    const north = frame.center
      .clone()
      .add(new THREE.Vector3(0, 20, 0))
      .project(camera);
    const direction = frame.position.clone().sub(frame.center).normalize();

    expect(east.x).toBeGreaterThan(center.x);
    expect(north.y).toBeGreaterThan(center.y);
    expect(Math.abs(direction.x)).toBeLessThan(1e-12);
    expect(direction.y).toBeLessThan(-0.6);
    expect(direction.z).toBeGreaterThan(0.6);
  });
});

describe("terrainColorForNormalizedHeight", () => {
  it("clamps heights and produces distinct low, middle, and summit colors", () => {
    const below = terrainColorForNormalizedHeight(-1);
    const low = terrainColorForNormalizedHeight(0);
    const middle = terrainColorForNormalizedHeight(0.5);
    const summit = terrainColorForNormalizedHeight(1);
    const above = terrainColorForNormalizedHeight(2);

    expect(below.getHex()).toBe(low.getHex());
    expect(above.getHex()).toBe(summit.getHex());
    expect(new Set([low.getHex(), middle.getHex(), summit.getHex()]).size).toBe(3);
    expect(summit.r + summit.g + summit.b).toBeGreaterThan(
      low.r + low.g + low.b,
    );
  });
});
