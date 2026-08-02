import { RotateCcw } from "lucide-react";
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

export interface CameraFrame {
  center: THREE.Vector3;
  position: THREE.Vector3;
  near: number;
  far: number;
}

const DEFAULT_CAMERA_DIRECTION = new THREE.Vector3(0, -1.35, 1.45).normalize();
const TERRAIN_COLOR_STOPS = [
  { position: 0, color: new THREE.Color(0x365e54) },
  { position: 0.34, color: new THREE.Color(0x6f8668) },
  { position: 0.66, color: new THREE.Color(0xb09a70) },
  { position: 0.86, color: new THREE.Color(0xcfc5aa) },
  { position: 1, color: new THREE.Color(0xf1f0e8) },
] as const;

export function terrainColorForNormalizedHeight(
  value: number,
  target = new THREE.Color(),
): THREE.Color {
  const normalized = THREE.MathUtils.clamp(value, 0, 1);
  for (let index = 1; index < TERRAIN_COLOR_STOPS.length; index += 1) {
    const upper = TERRAIN_COLOR_STOPS[index]!;
    const lower = TERRAIN_COLOR_STOPS[index - 1]!;
    if (normalized <= upper.position) {
      const span = upper.position - lower.position;
      const amount = span > 0 ? (normalized - lower.position) / span : 0;
      return target.copy(lower.color).lerp(upper.color, amount);
    }
  }
  return target.copy(TERRAIN_COLOR_STOPS.at(-1)!.color);
}

export function cameraFrameForBounds(
  bounds: THREE.Box3,
  aspect: number,
  verticalFovDegrees: number,
): CameraFrame {
  const sphere = bounds.getBoundingSphere(new THREE.Sphere());
  const radius = Math.max(sphere.radius, 0.5);
  const verticalFov = THREE.MathUtils.degToRad(verticalFovDegrees);
  const horizontalFov =
    2 * Math.atan(Math.tan(verticalFov / 2) * Math.max(aspect, 0.1));
  const limitingFov = Math.max(Math.min(verticalFov, horizontalFov), 0.05);
  const distance = (radius / Math.sin(limitingFov / 2)) * 1.18;
  return {
    center: sphere.center.clone(),
    position: sphere.center
      .clone()
      .addScaledVector(DEFAULT_CAMERA_DIRECTION, distance),
    near: Math.max(distance - radius * 2.5, radius / 1000, 0.01),
    far: distance + radius * 8,
  };
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

function applyTerrainPresentation(object: THREE.Object3D, bounds: THREE.Box3) {
  const height = Math.max(bounds.max.z - bounds.min.z, 0.001);
  object.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) {
      return;
    }
    const positions = child.geometry.getAttribute("position");
    const colors = new Float32Array(positions.count * 3);
    const color = new THREE.Color();
    for (let index = 0; index < positions.count; index += 1) {
      const normalized = (positions.getZ(index) - bounds.min.z) / height;
      terrainColorForNormalizedHeight(normalized, color);
      colors[index * 3] = color.r;
      colors[index * 3 + 1] = color.g;
      colors[index * 3 + 2] = color.b;
    }
    child.geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    const previous = Array.isArray(child.material)
      ? child.material
      : [child.material];
    child.material = new THREE.MeshStandardMaterial({
      vertexColors: true,
      roughness: 0.9,
      metalness: 0,
      flatShading: child.geometry.getAttribute("normal") === undefined,
      side: THREE.FrontSide,
    });
    previous.forEach((material) => material.dispose());
  });
}

function disposeObject(object: THREE.Object3D) {
  object.traverse((child) => {
    if (child instanceof THREE.Mesh || child instanceof THREE.Line) {
      child.geometry.dispose();
      const materials = Array.isArray(child.material) ? child.material : [child.material];
      materials.forEach((material) => material.dispose());
    } else if (child instanceof THREE.Sprite) {
      child.material.map?.dispose();
      child.material.dispose();
    }
  });
}

function guideLabel(
  label: string,
  color: THREE.ColorRepresentation,
  position: THREE.Vector3,
  size: number,
): THREE.Sprite {
  const canvas = document.createElement("canvas");
  canvas.width = 96;
  canvas.height = 96;
  const context = canvas.getContext("2d");
  if (context) {
    const hexColor = `#${new THREE.Color(color).getHexString()}`;
    context.fillStyle = "rgba(255, 255, 255, 0.94)";
    context.beginPath();
    context.arc(48, 48, 34, 0, Math.PI * 2);
    context.fill();
    context.lineWidth = 5;
    context.strokeStyle = hexColor;
    context.stroke();
    context.fillStyle = hexColor;
    context.font = "700 40px system-ui, sans-serif";
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(label, 48, 50);
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false }),
  );
  sprite.position.copy(position);
  sprite.scale.set(size, size, 1);
  sprite.renderOrder = 10;
  return sprite;
}

function sceneGuides(bounds: THREE.Box3): THREE.Group {
  const center = bounds.getCenter(new THREE.Vector3());
  const size = bounds.getSize(new THREE.Vector3());
  const horizontalExtent = Math.max(size.x, size.y, 1);
  const guide = new THREE.Group();
  const grid = new THREE.GridHelper(
    horizontalExtent * 1.2,
    12,
    0x71847e,
    0xb6c4c0,
  );
  grid.rotation.x = Math.PI / 2;
  grid.position.set(
    center.x,
    center.y,
    bounds.min.z - Math.max(size.z * 0.002, 0.02),
  );
  guide.add(grid);
  const arrowLength = horizontalExtent * 0.13;
  const origin = new THREE.Vector3(
    center.x - horizontalExtent * 0.55,
    center.y - horizontalExtent * 0.55,
    bounds.min.z,
  );
  guide.add(
    new THREE.ArrowHelper(
      new THREE.Vector3(1, 0, 0),
      origin,
      arrowLength,
      0xb54d3f,
      arrowLength * 0.22,
      arrowLength * 0.12,
    ),
    new THREE.ArrowHelper(
      new THREE.Vector3(0, 1, 0),
      origin,
      arrowLength,
      0x146b62,
      arrowLength * 0.22,
      arrowLength * 0.12,
    ),
  );
  const labelSize = horizontalExtent * 0.065;
  const labelOffset = arrowLength * 1.18;
  guide.add(
    guideLabel(
      "E",
      0xb54d3f,
      origin.clone().add(new THREE.Vector3(labelOffset, 0, labelSize * 0.2)),
      labelSize,
    ),
    guideLabel(
      "N",
      0x146b62,
      origin.clone().add(new THREE.Vector3(0, labelOffset, labelSize * 0.2)),
      labelSize,
    ),
  );
  return guide;
}

export function TerrainPreview({
  language,
  modelUrl,
}: TerrainPreviewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const contentRef = useRef<THREE.Object3D | null>(null);
  const boundsRef = useRef<THREE.Box3 | null>(null);
  const guidesRef = useRef<THREE.Group | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const resetViewRef = useRef<(() => void) | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [modelLoaded, setModelLoaded] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xecf1ef);
    const camera = new THREE.PerspectiveCamera(32, 1, 0.01, 10000);
    camera.up.set(0, 0, 1);
    camera.position.set(10, -13, 9);
    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.08;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minPolarAngle = THREE.MathUtils.degToRad(8);
    controls.maxPolarAngle = THREE.MathUtils.degToRad(82);
    controls.target.set(0, 0, 0.5);

    const hemisphere = new THREE.HemisphereLight(0xffffff, 0x5d6863, 1.75);
    scene.add(hemisphere);
    const key = new THREE.DirectionalLight(0xfff9eb, 2.85);
    key.position.set(-8, 10, 16);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0x9fc8c1, 0.85);
    fill.position.set(10, -6, 7);
    scene.add(fill);

    const frameModel = () => {
      if (!boundsRef.current) {
        return;
      }
      const frame = cameraFrameForBounds(
        boundsRef.current,
        camera.aspect,
        camera.fov,
      );
      controls.target.copy(frame.center);
      camera.position.copy(frame.position);
      camera.near = frame.near;
      camera.far = frame.far;
      camera.updateProjectionMatrix();
      controls.update();
    };
    resetViewRef.current = frameModel;

    const resize = () => {
      const width = Math.max(container.clientWidth, 1);
      const height = Math.max(container.clientHeight, 1);
      camera.aspect = width / height;
      renderer.setSize(width, height, false);
      frameModel();
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
    const placeholderBounds = new THREE.Box3().setFromObject(placeholder);
    boundsRef.current = placeholderBounds;
    const placeholderGuides = sceneGuides(placeholderBounds);
    scene.add(placeholderGuides);
    guidesRef.current = placeholderGuides;
    resize();

    return () => {
      window.cancelAnimationFrame(animation);
      observer.disconnect();
      controls.dispose();
      if (contentRef.current) {
        disposeObject(contentRef.current);
      }
      if (guidesRef.current) {
        disposeObject(guidesRef.current);
      }
      renderer.dispose();
      renderer.domElement.remove();
      sceneRef.current = null;
      contentRef.current = null;
      boundsRef.current = null;
      guidesRef.current = null;
      cameraRef.current = null;
      controlsRef.current = null;
      resetViewRef.current = null;
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
    setModelLoaded(false);
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
        const bounds = new THREE.Box3().setFromObject(object);
        applyTerrainPresentation(object, bounds);
        scene.add(object);
        contentRef.current = object;
        boundsRef.current = bounds;
        if (guidesRef.current) {
          scene.remove(guidesRef.current);
          disposeObject(guidesRef.current);
        }
        const guides = sceneGuides(bounds);
        scene.add(guides);
        guidesRef.current = guides;
        resetViewRef.current?.();
        setModelLoaded(true);
      },
      undefined,
      () => {
        if (active) {
          setLoadError(true);
          setModelLoaded(false);
        }
      },
    );
    return () => {
      active = false;
    };
  }, [modelUrl]);

  return (
    <div
      className="preview-shell"
      data-testid="terrain-preview"
      data-model-loaded={modelLoaded}
    >
      <div ref={containerRef} className="preview-canvas" />
      <button
        type="button"
        className="preview-reset"
        title={translate(language, "resetModelView")}
        aria-label={translate(language, "resetModelView")}
        onClick={() => resetViewRef.current?.()}
      >
        <RotateCcw size={16} />
      </button>
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
