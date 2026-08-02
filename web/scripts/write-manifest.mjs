import { createHash } from "node:crypto";
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const root = resolve(scriptDir, "../../src/topoforge/web/static");

async function collect(directory) {
  const paths = [];
  for (const name of (await readdir(directory)).sort()) {
    const path = join(directory, name);
    const info = await stat(path);
    if (info.isDirectory()) {
      paths.push(...(await collect(path)));
    } else if (name !== "asset-manifest.json") {
      paths.push(path);
    }
  }
  return paths;
}

const files = await collect(root);
const assets = [];
const sha256 = {};
const sizes = {};
for (const path of files) {
  const key = relative(root, path).split("\\").join("/");
  const payload = await readFile(path);
  assets.push(key);
  sha256[key] = createHash("sha256").update(payload).digest("hex");
  sizes[key] = payload.byteLength;
}

const manifest = {
  schema_version: "topoforge-web-assets-v1",
  languages: ["zh-CN", "en"],
  frameworks: ["React", "MapLibre", "Three.js"],
  assets,
  sha256,
  sizes,
};
await writeFile(
  join(root, "asset-manifest.json"),
  JSON.stringify(manifest, null, 2) + "\n",
  "utf8",
);
