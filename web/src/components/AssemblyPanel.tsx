import { Boxes, Eye, EyeOff, RotateCcw, SquareStack } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

import { translate } from "../i18n";
import type {
  AssemblyConnector,
  AssemblyMode,
  AssemblyTile,
  JobAssemblyOverview,
  Language,
} from "../types";
import { cameraFrameForBounds } from "./TerrainPreview";

interface AssemblyPanelProps {
  language: Language;
  assembly: JobAssemblyOverview | null;
  selectedTileId: string | null;
  loading: boolean;
  error: string | null;
  onSelectedTileChange: (tileId: string) => void;
}

interface AssemblySceneProps {
  assembly: JobAssemblyOverview;
  selectedTileId: string | null;
  visibility: Record<string, boolean>;
  explosion: number;
  resetToken: number;
  onSelectedTileChange: (tileId: string) => void;
  onLoadError: () => void;
}

const TILE_COLORS = [0x497f73, 0xb28a4b, 0x7b8790, 0xa75c45, 0x537c96, 0x768b51];

export function tileExplosionOffset(
  tile: AssemblyTile,
  modelSizeMm: [number, number],
  amount: number,
): [number, number, number] {
  const [xMin, yMin, xMax, yMax] = tile.physical_bounds_mm;
  const centerX = (xMin + xMax) / 2;
  const centerY = (yMin + yMax) / 2;
  const scale = Math.min(Math.max(amount, 0), 1) * 0.38;
  return [
    (centerX - modelSizeMm[0] / 2) * scale,
    (centerY - modelSizeMm[1] / 2) * scale,
    0,
  ];
}

export function withTileVisibility(
  current: Record<string, boolean>,
  tileId: string,
  visible: boolean,
): Record<string, boolean> {
  return { ...current, [tileId]: visible };
}

export function assemblyDisplayBounds(
  assembly: JobAssemblyOverview,
  visibility: Record<string, boolean>,
  explosion: number,
): THREE.Box3 {
  const bounds = new THREE.Box3();
  for (const tile of assembly.tiles) {
    if (visibility[tile.tile_id] === false) {
      continue;
    }
    const [xMin, yMin, zMin, xMax, yMax, zMax] = tile.global_bounds_mm;
    const [offsetX, offsetY, offsetZ] = tileExplosionOffset(
      tile,
      assembly.model_size_mm,
      explosion,
    );
    bounds.expandByPoint(
      new THREE.Vector3(xMin + offsetX, yMin + offsetY, zMin + offsetZ),
    );
    bounds.expandByPoint(
      new THREE.Vector3(xMax + offsetX, yMax + offsetY, zMax + offsetZ),
    );
  }
  if (bounds.isEmpty()) {
    const [widthMm, depthMm] = assembly.model_size_mm;
    const maxZ = Math.max(...assembly.tiles.map((tile) => tile.global_bounds_mm[5]));
    bounds.set(new THREE.Vector3(0, 0, 0), new THREE.Vector3(widthMm, depthMm, maxZ));
  }
  return bounds;
}

function connectorPoint(
  connector: AssemblyConnector,
  modelDepthMm: number,
): [number, number] {
  const xAxis = connector.insertion_axis.endsWith("X");
  const x = xAxis ? connector.seam_coordinate_mm : connector.center_along_seam_mm;
  const northing = xAxis
    ? connector.center_along_seam_mm
    : connector.seam_coordinate_mm;
  return [x, modelDepthMm - northing];
}

function AssemblyDiagram({
  assembly,
  selectedTileId,
  visibility,
  onSelectedTileChange,
}: {
  assembly: JobAssemblyOverview;
  selectedTileId: string | null;
  visibility: Record<string, boolean>;
  onSelectedTileChange: (tileId: string) => void;
}) {
  const [widthMm, depthMm] = assembly.model_size_mm;
  const padding = Math.max(widthMm, depthMm) * 0.12;
  return (
    <svg
      className="assembly-diagram"
      data-testid="assembly-diagram"
      viewBox={`${-padding} ${-padding} ${widthMm + padding * 2} ${depthMm + padding * 2}`}
      role="img"
      aria-label="TopoForge physical tile assembly"
    >
      <rect
        className="assembly-bed"
        x={0}
        y={0}
        width={widthMm}
        height={depthMm}
      />
      {assembly.tiles.map((tile, index) => {
        const [xMin, yMin, xMax, yMax] = tile.physical_bounds_mm;
        const selected = tile.tile_id === selectedTileId;
        const visible = visibility[tile.tile_id] !== false;
        const screenY = depthMm - yMax;
        return (
          <g
            key={tile.tile_id}
            className={`assembly-diagram-tile${selected ? " selected" : ""}${
              visible ? "" : " hidden-tile"
            }`}
            role="button"
            tabIndex={0}
            onClick={() => onSelectedTileChange(tile.tile_id)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSelectedTileChange(tile.tile_id);
              }
            }}
          >
            <rect
              x={xMin}
              y={screenY}
              width={xMax - xMin}
              height={yMax - yMin}
              fill={`#${TILE_COLORS[index % TILE_COLORS.length].toString(16).padStart(6, "0")}`}
            />
            <text x={(xMin + xMax) / 2} y={screenY + (yMax - yMin) / 2}>
              R{tile.row + 1} C{tile.column + 1}
            </text>
            <title>{`${tile.tile_id} - R${tile.row + 1} C${tile.column + 1}`}</title>
          </g>
        );
      })}
      {assembly.connectors.map((connector) => {
        const [cx, cy] = connectorPoint(connector, depthMm);
        return (
          <g key={connector.connector_id} className="assembly-connector">
            <circle cx={cx} cy={cy} r={Math.max(widthMm, depthMm) * 0.012} />
            <title>{connector.connector_id}</title>
          </g>
        );
      })}
      <g className="assembly-north">
        <line
          x1={widthMm + padding * 0.42}
          y1={depthMm * 0.72}
          x2={widthMm + padding * 0.42}
          y2={depthMm * 0.2}
        />
        <path
          d={`M ${widthMm + padding * 0.42} ${depthMm * 0.13} l ${-padding * 0.08} ${padding * 0.17} h ${padding * 0.16} z`}
        />
        <text x={widthMm + padding * 0.42} y={depthMm * 0.08}>
          N
        </text>
      </g>
    </svg>
  );
}

function disposeObject(object: THREE.Object3D) {
  object.traverse((child) => {
    if (child instanceof THREE.Mesh || child instanceof THREE.Line) {
      child.geometry.dispose();
      const materials = Array.isArray(child.material) ? child.material : [child.material];
      materials.forEach((material) => material.dispose());
    }
  });
}

function applyTileState(
  object: THREE.Object3D,
  tile: AssemblyTile,
  assembly: JobAssemblyOverview,
  selectedTileId: string | null,
  visibility: Record<string, boolean>,
  explosion: number,
) {
  const offset = tileExplosionOffset(tile, assembly.model_size_mm, explosion);
  object.position.set(...offset);
  object.visible = visibility[tile.tile_id] !== false;
  object.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) {
      return;
    }
    const materials = Array.isArray(child.material) ? child.material : [child.material];
    materials.forEach((material) => {
      if (material instanceof THREE.MeshStandardMaterial) {
        material.emissive.set(tile.tile_id === selectedTileId ? 0x6f2d16 : 0x000000);
        material.emissiveIntensity = tile.tile_id === selectedTileId ? 0.52 : 0;
      }
    });
  });
}

function AssemblyScene({
  assembly,
  selectedTileId,
  visibility,
  explosion,
  resetToken,
  onSelectedTileChange,
  onLoadError,
}: AssemblySceneProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const objectsRef = useRef(new Map<string, THREE.Object3D>());
  const displayRef = useRef({ selectedTileId, visibility, explosion });
  const selectRef = useRef(onSelectedTileChange);
  const errorRef = useRef(onLoadError);
  const frameRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    displayRef.current = { selectedTileId, visibility, explosion };
    for (const tile of assembly.tiles) {
      const object = objectsRef.current.get(tile.tile_id);
      if (object) {
        applyTileState(
          object,
          tile,
          assembly,
          selectedTileId,
          visibility,
          explosion,
        );
      }
    }
  }, [assembly, selectedTileId, visibility, explosion]);

  useEffect(() => {
    frameRef.current?.();
  }, [assembly, explosion, visibility]);

  useEffect(() => {
    frameRef.current?.();
  }, [resetToken]);

  useEffect(() => {
    selectRef.current = onSelectedTileChange;
    errorRef.current = onLoadError;
  }, [onLoadError, onSelectedTileChange]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xe5ebe9);
    const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 10000);
    camera.up.set(0, 0, 1);
    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      preserveDrawingBuffer: true,
    });
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.08;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;

    scene.add(new THREE.HemisphereLight(0xffffff, 0x5f6965, 2.15));
    const key = new THREE.DirectionalLight(0xffffff, 2.55);
    key.position.set(-6, -8, 14);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xaddbd2, 1.05);
    fill.position.set(10, 8, 6);
    scene.add(fill);

    const [widthMm, depthMm] = assembly.model_size_mm;
    const grid = new THREE.GridHelper(
      Math.max(widthMm, depthMm) * 1.18,
      16,
      0x687d77,
      0xb6c4c0,
    );
    grid.rotation.x = Math.PI / 2;
    grid.position.set(widthMm / 2, depthMm / 2, -0.03);
    scene.add(grid);
    const arrowLength = Math.max(widthMm, depthMm) * 0.12;
    const arrowOrigin = new THREE.Vector3(
      -arrowLength * 0.4,
      -arrowLength * 0.4,
      0,
    );
    scene.add(
      new THREE.ArrowHelper(
        new THREE.Vector3(1, 0, 0),
        arrowOrigin,
        arrowLength,
        0xb54d3f,
      ),
      new THREE.ArrowHelper(
        new THREE.Vector3(0, 1, 0),
        arrowOrigin,
        arrowLength,
        0x146b62,
      ),
    );

    const frameScene = () => {
      const bounds = assemblyDisplayBounds(
        assembly,
        displayRef.current.visibility,
        displayRef.current.explosion,
      );
      const frame = cameraFrameForBounds(bounds, camera.aspect, camera.fov);
      controls.target.copy(frame.center);
      camera.position.copy(frame.position);
      camera.near = frame.near;
      camera.far = frame.far;
      camera.updateProjectionMatrix();
      controls.update();
    };
    frameRef.current = frameScene;

    const resize = () => {
      const width = Math.max(container.clientWidth, 1);
      const height = Math.max(container.clientHeight, 1);
      camera.aspect = width / height;
      renderer.setSize(width, height, false);
      frameScene();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    resize();

    const loader = new GLTFLoader();
    let active = true;
    for (const [index, tile] of assembly.tiles.entries()) {
      loader.load(
        tile.glb_url,
        (gltf) => {
          if (!active) {
            disposeObject(gltf.scene);
            return;
          }
          const object = gltf.scene;
          object.traverse((child) => {
            child.userData.tileId = tile.tile_id;
            if (child instanceof THREE.Mesh) {
              const materials = Array.isArray(child.material)
                ? child.material
                : [child.material];
              const cloned = materials.map((material) => {
                const next = material.clone();
                if (next instanceof THREE.MeshStandardMaterial) {
                  next.color.setHex(TILE_COLORS[index % TILE_COLORS.length]);
                  next.roughness = 0.8;
                  next.metalness = 0.02;
                }
                return next;
              });
              child.material = Array.isArray(child.material) ? cloned : cloned[0]!;
            }
          });
          objectsRef.current.set(tile.tile_id, object);
          scene.add(object);
          applyTileState(
            object,
            tile,
            assembly,
            displayRef.current.selectedTileId,
            displayRef.current.visibility,
            displayRef.current.explosion,
          );
          if (objectsRef.current.size === assembly.tiles.length) {
            frameScene();
          }
        },
        undefined,
        () => {
          if (active) {
            errorRef.current();
          }
        },
      );
    }

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const select = (event: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hits = raycaster.intersectObjects([...objectsRef.current.values()], true);
      const tileId = hits[0]?.object.userData.tileId;
      if (typeof tileId === "string") {
        selectRef.current(tileId);
      }
    };
    renderer.domElement.addEventListener("pointerup", select);

    let animation = 0;
    const render = () => {
      controls.update();
      renderer.render(scene, camera);
      animation = window.requestAnimationFrame(render);
    };
    render();

    return () => {
      active = false;
      window.cancelAnimationFrame(animation);
      observer.disconnect();
      renderer.domElement.removeEventListener("pointerup", select);
      controls.dispose();
      objectsRef.current.forEach((object) => {
        scene.remove(object);
        disposeObject(object);
      });
      objectsRef.current.clear();
      frameRef.current = null;
      disposeObject(grid);
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [assembly]);

  return (
    <div
      ref={containerRef}
      className="assembly-3d-canvas"
      data-testid="assembly-3d-canvas"
    />
  );
}

export function AssemblyPanel({
  language,
  assembly,
  selectedTileId,
  loading,
  error,
  onSelectedTileChange,
}: AssemblyPanelProps) {
  const [mode, setMode] = useState<AssemblyMode>("2d");
  const [explosion, setExplosion] = useState(0);
  const [visibility, setVisibility] = useState<Record<string, boolean>>({});
  const [loadError, setLoadError] = useState(false);
  const [resetToken, setResetToken] = useState(0);

  useEffect(() => {
    setExplosion(0);
    setLoadError(false);
    setVisibility(
      Object.fromEntries((assembly?.tiles ?? []).map((tile) => [tile.tile_id, true])),
    );
  }, [assembly?.job_id]);

  const visibleCount = useMemo(
    () =>
      assembly?.tiles.filter((tile) => visibility[tile.tile_id] !== false).length ?? 0,
    [assembly, visibility],
  );

  const reset = () => {
    if (!assembly) {
      return;
    }
    setExplosion(0);
    setVisibility(Object.fromEntries(assembly.tiles.map((tile) => [tile.tile_id, true])));
    setResetToken((current) => current + 1);
    const firstTile = assembly.tiles[0];
    if (firstTile) {
      onSelectedTileChange(firstTile.tile_id);
    }
  };

  return (
    <div
      className="assembly-shell"
      data-testid="assembly-panel"
      data-selected-tile={selectedTileId ?? ""}
      data-explosion={explosion.toFixed(2)}
      data-reset-token={resetToken}
    >
      <div className="assembly-toolbar">
        <div className="segmented two" role="group" aria-label={translate(language, "assemblyView")}>
          <button
            type="button"
            className={mode === "2d" ? "active" : ""}
            onClick={() => setMode("2d")}
          >
            <SquareStack size={15} />
            {translate(language, "assembly2d")}
          </button>
          <button
            type="button"
            className={mode === "3d" ? "active" : ""}
            onClick={() => setMode("3d")}
          >
            <Boxes size={15} />
            {translate(language, "assembly3d")}
          </button>
        </div>
        {mode === "3d" && assembly && (
          <label className="explosion-control">
            <span>{translate(language, "explosion")}</span>
            <input
              aria-label={translate(language, "explosion")}
              type="range"
              min="0"
              max="1"
              step="0.05"
              value={explosion}
              onChange={(event) => setExplosion(Number(event.target.value))}
            />
          </label>
        )}
        <button
          type="button"
          className="icon-button"
          title={translate(language, "resetView")}
          aria-label={translate(language, "resetView")}
          onClick={reset}
          disabled={!assembly}
        >
          <RotateCcw size={15} />
        </button>
      </div>

      <div className="assembly-content">
        <div className="assembly-viewport">
          {loading && (
            <div className="assembly-status">{translate(language, "visualizationLoading")}</div>
          )}
          {!loading && error && <div className="assembly-status error">{error}</div>}
          {!loading && !error && !assembly && (
            <div className="assembly-status">{translate(language, "assemblyPending")}</div>
          )}
          {assembly && mode === "2d" && (
            <AssemblyDiagram
              assembly={assembly}
              selectedTileId={selectedTileId}
              visibility={visibility}
              onSelectedTileChange={onSelectedTileChange}
            />
          )}
          {assembly && mode === "3d" && (
            <AssemblyScene
              assembly={assembly}
              selectedTileId={selectedTileId}
              visibility={visibility}
              explosion={explosion}
              resetToken={resetToken}
              onSelectedTileChange={onSelectedTileChange}
              onLoadError={() => setLoadError(true)}
            />
          )}
          {loadError && (
            <div className="assembly-status error">
              {translate(language, "assemblyLoadFailed")}
            </div>
          )}
          {assembly && (
            <div className="assembly-axis-legend">
              <span className="axis-east">{translate(language, "eastAxis")}</span>
              <span className="axis-north">{translate(language, "northAxis")}</span>
              <span>{translate(language, "upAxis")}</span>
            </div>
          )}
        </div>

        {assembly && (
          <aside className="assembly-roster">
            <div className="assembly-roster-heading">
              <strong>{translate(language, "tiles")}</strong>
              <span>
                {visibleCount}/{assembly.tile_count}
              </span>
            </div>
            <div className="assembly-roster-list">
              {assembly.tiles.map((tile) => {
                const visible = visibility[tile.tile_id] !== false;
                return (
                  <div
                    key={tile.tile_id}
                    className={`assembly-roster-row${
                      tile.tile_id === selectedTileId ? " selected" : ""
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => onSelectedTileChange(tile.tile_id)}
                    >
                      <strong>{tile.tile_id}</strong>
                      <small>
                        R{tile.row + 1} C{tile.column + 1} - {tile.triangle_count.toLocaleString()}
                      </small>
                    </button>
                    <label
                      className="visibility-toggle"
                      title={translate(language, visible ? "hideTile" : "showTile")}
                    >
                      <input
                        aria-label={`${translate(language, visible ? "hideTile" : "showTile")} ${tile.tile_id}`}
                        type="checkbox"
                        checked={visible}
                        onChange={(event) =>
                          setVisibility((current) =>
                            withTileVisibility(
                              current,
                              tile.tile_id,
                              event.target.checked,
                            ),
                          )
                        }
                      />
                      {visible ? <Eye size={15} /> : <EyeOff size={15} />}
                    </label>
                  </div>
                );
              })}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}
