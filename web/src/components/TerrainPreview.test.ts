import { describe, expect, it } from "vitest";
import * as THREE from "three";

import {
  applyTerrainPresentation,
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


describe("applyTerrainPresentation", () => {
  function sourceGeometry() {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute([0, 0, 0, 10, 0, 5, 0, 10, 10], 3));
    geometry.setIndex([0, 1, 2]);
    geometry.computeVertexNormals();
    return geometry;
  }

  it("recolors terrain while preserving the exported overlay material and vertex colors", () => {
    const terrainGeometry = sourceGeometry();
    const terrainMaterial = new THREE.MeshStandardMaterial();
    const terrain = new THREE.Mesh(terrainGeometry, terrainMaterial);
    terrain.name = "terrain";
    const routeGeometry = sourceGeometry();
    const routeColor = new THREE.Uint8BufferAttribute([209, 73, 91, 255, 209, 73, 91, 255, 209, 73, 91, 255], 4, true);
    routeGeometry.setAttribute("color", routeColor);
    const routeMaterial = new THREE.MeshStandardMaterial({ vertexColors: true });
    const route = new THREE.Mesh(routeGeometry, routeMaterial);
    route.name = "overlay-gpx-route";
    const scene = new THREE.Group();
    scene.add(terrain, route);
    const routeBytes = Array.from(routeColor.array);
    const terrainPositions = Array.from(terrainGeometry.getAttribute("position").array);
    const routePositions = Array.from(routeGeometry.getAttribute("position").array);
    let oldTerrainDisposed = false;
    let routeDisposed = false;
    terrainMaterial.addEventListener("dispose", () => { oldTerrainDisposed = true; });
    routeMaterial.addEventListener("dispose", () => { routeDisposed = true; });

    applyTerrainPresentation(scene, new THREE.Box3().setFromObject(scene));

    expect(terrain.material).not.toBe(terrainMaterial);
    expect(terrain.material.vertexColors).toBe(true);
    expect(oldTerrainDisposed).toBe(true);
    const low = terrainColorForNormalizedHeight(0);
    const high = terrainColorForNormalizedHeight(1);
    const terrainColors = terrain.geometry.getAttribute("color");
    expect(terrainColors.getX(0)).toBeCloseTo(low.r);
    expect(terrainColors.getZ(2)).toBeCloseTo(high.b);
    expect(terrainColors.getX(0)).not.toBe(terrainColors.getX(2));
    expect(Array.from(terrain.geometry.getAttribute("position").array)).toEqual(terrainPositions);
    expect(Array.from(terrain.geometry.index!.array)).toEqual([0, 1, 2]);
    expect(route.material).toBe(routeMaterial);
    expect(route.material.vertexColors).toBe(true);
    expect(routeDisposed).toBe(false);
    expect(route.geometry).toBe(routeGeometry);
    expect(route.geometry.getAttribute("color")).toBe(routeColor);
    expect(Array.from(route.geometry.getAttribute("color").array)).toEqual(routeBytes);
    expect(Array.from(route.geometry.getAttribute("position").array)).toEqual(routePositions);
  });

  it("protects source resources shared with unnamed meshes under an overlay GLTF node", () => {
    const geometry = sourceGeometry();
    const color = new THREE.Float32BufferAttribute([0.8, 0.1, 0.2, 0.8, 0.1, 0.2, 0.8, 0.1, 0.2], 3);
    geometry.setAttribute("color", color);
    const material = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.3 });
    const terrain = new THREE.Mesh(geometry, material);
    terrain.name = "terrain";
    const overlayNode = new THREE.Group();
    overlayNode.name = "overlay-gpx-route";
    const route = new THREE.Mesh(geometry, material);
    overlayNode.add(route);
    const scene = new THREE.Group();
    scene.add(terrain, overlayNode);
    let sharedDisposed = false;
    material.addEventListener("dispose", () => { sharedDisposed = true; });

    applyTerrainPresentation(scene, new THREE.Box3().setFromObject(scene));

    expect(sharedDisposed).toBe(false);
    expect(route.material).toBe(material);
    expect(route.material.roughness).toBe(0.3);
    expect(route.geometry).toBe(geometry);
    expect(route.geometry.getAttribute("color")).toBe(color);
    expect(terrain.material).not.toBe(material);
    expect(terrain.geometry).not.toBe(geometry);
    expect(terrain.geometry.getAttribute("color")).not.toBe(color);
    expect(Array.from(terrain.geometry.getAttribute("position").array)).toEqual(Array.from(geometry.getAttribute("position").array));
    expect(Array.from(terrain.geometry.index!.array)).toEqual(Array.from(geometry.index!.array));
  });
});
