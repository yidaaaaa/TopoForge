import type { TranslationKey } from "./i18n";
import type { JobArtifact } from "./types";

export function isPrimaryArtifact(artifact: JobArtifact): boolean {
  return artifact.kind === "file" && Boolean(artifact.download_url) && (
    artifact.artifact_id === "overlay_model_3mf" || artifact.artifact_id === "model_stl" || artifact.artifact_id === "model_3mf" ||
    artifact.artifact_id.startsWith("bambu_project_3mf")
  );
}

export function artifactLabel(
  role: string,
  t: (key: TranslationKey) => string,
): string {
  if (role === "overlay_model_3mf") return t("overlay3mfArtifact");
  if (role === "model_stl") return t("stlArtifact");
  if (role === "preview_glb" || role === "overlay_preview_glb") return t("previewArtifact");
  if (role === "model_3mf") {
    return t("core3mfArtifact");
  }
  if (role.startsWith("bambu_project_3mf")) {
    const tile = role.slice("bambu_project_3mf".length).replace(/^_/, "");
    return tile
      ? `${t("bambuProject3mfArtifact")} · ${tile.replaceAll("_", "-")}`
      : t("bambuProject3mfArtifact");
  }
  if (role.startsWith("bambu_project_validation")) {
    const tile = role
      .slice("bambu_project_validation".length)
      .replace(/^_/, "");
    return tile
      ? `${t("bambuProjectValidationArtifact")} · ${tile.replaceAll("_", "-")}`
      : t("bambuProjectValidationArtifact");
  }
  return role.replaceAll("_", " ");
}
