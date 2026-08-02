import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

import { translate } from "../i18n";
import type { Language } from "../types";

interface TerrainPreviewProps {
  language: Language;
  modelUrl: string | null;
}

function placeholderTerrain(): THREE.Mesh {
  const geometry = new THREE.PlaneGeometry(12, 8, 48, 32);
  const positions = geometry.attributes.position;
  for (let index = 0; index < positions.count; index += 1) {
    const x = positions.getX(index);
    const y = positions.getY(index);
    const radius = Math.sqrt(x * x + y * y);
    const z = 0.9 * Math.exp(-0.12 * radius * radius) + 0.12 * Math.sin(x * 1.4);
    positions.setZ(index, z);
  }
  geometry.computeVertexNormals();
  return new THREE.Mesh(
    geometry,
    new THREE.MeshStandardMaterial({
      color: 0x83a99a,
      roughness: 0.82,
      metalness: 0.02,
      side: THREE.DoubleSide,
    }),
  );
}

function disposeObject(object: THREE.Object3D) {
  object.traverse((child) => {
    if (child instanceof THREE.Mesh) {
      child.geometry.dispose();
      const materials = Array.isArray(child.material)
        ? child.material
        : [child.material];
      materials.forEach((material) => material.dispose());
    }
  });
}

export function TerrainPreview({
  language,
  modelUrl,
}: TerrainPreviewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const contentRef = useRef<THREE.Object3D | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xe5ebe9);
    const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 10000);
    camera.up.set(0, 0, 1);
    camera.position.set(10, -13, 9);
    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.target.set(0, 0, 0.5);

    const hemisphere = new THREE.HemisphereLight(0xffffff, 0x64706b, 2.2);
    scene.add(hemisphere);
    const key = new THREE.DirectionalLight(0xffffff, 2.5);
    key.position.set(-6, -8, 14);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xaad8cf, 1.1);
    fill.position.set(10, 6, 5);
    scene.add(fill);

    const grid = new THREE.GridHelper(18, 18, 0x82908b, 0xb7c2be);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = -0.02;
    scene.add(grid);
    const east = new THREE.ArrowHelper(
      new THREE.Vector3(1, 0, 0),
      new THREE.Vector3(-7.5, -5.5, 0),
      2.1,
      0xb54d3f,
      0.45,
      0.24,
    );
    const north = new THREE.ArrowHelper(
      new THREE.Vector3(0, 1, 0),
      new THREE.Vector3(-7.5, -5.5, 0),
      2.1,
      0x146b62,
      0.45,
      0.24,
    );
    scene.add(east, north);

    const resize = () => {
      const width = Math.max(container.clientWidth, 1);
      const height = Math.max(container.clientHeight, 1);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    resize();

    let animation = 0;
    const render = () => {
      controls.update();
      renderer.render(scene, camera);
      animation = window.requestAnimationFrame(render);
    };
    render();

    sceneRef.current = scene;
    cameraRef.current = camera;
    controlsRef.current = controls;
    const placeholder = placeholderTerrain();
    scene.add(placeholder);
    contentRef.current = placeholder;

    return () => {
      window.cancelAnimationFrame(animation);
      observer.disconnect();
      controls.dispose();
      if (contentRef.current) {
        disposeObject(contentRef.current);
      }
      renderer.dispose();
      renderer.domElement.remove();
      sceneRef.current = null;
      contentRef.current = null;
      cameraRef.current = null;
      controlsRef.current = null;
    };
  }, []);

  useEffect(() => {
    const scene = sceneRef.current;
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    if (!scene || !camera || !controls || !modelUrl) {
      return;
    }
    setLoadError(false);
    const loader = new GLTFLoader();
    let active = true;
    loader.load(
      modelUrl,
      (gltf) => {
        if (!active) {
          disposeObject(gltf.scene);
          return;
        }
        if (contentRef.current) {
          scene.remove(contentRef.current);
          disposeObject(contentRef.current);
        }
        const object = gltf.scene;
        scene.add(object);
        contentRef.current = object;
        const bounds = new THREE.Box3().setFromObject(object);
        const center = bounds.getCenter(new THREE.Vector3());
        const size = bounds.getSize(new THREE.Vector3());
        const extent = Math.max(size.x, size.y, size.z, 1);
        controls.target.copy(center);
        camera.near = Math.max(extent / 1000, 0.01);
        camera.far = extent * 100;
        camera.position.copy(
          center.clone().add(new THREE.Vector3(extent, -extent * 1.25, extent * 0.8)),
        );
        camera.updateProjectionMatrix();
        controls.update();
      },
      undefined,
      () => {
        if (active) {
          setLoadError(true);
        }
      },
    );
    return () => {
      active = false;
    };
  }, [modelUrl]);

  return (
    <div className="preview-shell" data-testid="terrain-preview">
      <div ref={containerRef} className="preview-canvas" />
      <div className="axis-legend" aria-label={translate(language, "directionContract")}>
        <span className="axis-east">{translate(language, "eastAxis")}</span>
        <span className="axis-north">{translate(language, "northAxis")}</span>
        <span>{translate(language, "upAxis")}</span>
      </div>
      {!modelUrl && (
        <div className="preview-status">{translate(language, "previewPending")}</div>
      )}
      {loadError && (
        <div className="preview-status error">{translate(language, "glbLoadFailed")}</div>
      )}
    </div>
  );
}
