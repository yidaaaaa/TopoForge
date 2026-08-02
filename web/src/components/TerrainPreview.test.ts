import { describe, expect, it } from "vitest";
import * as THREE from "three";

import { cameraFrameForBounds } from "./TerrainPreview";

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
});
