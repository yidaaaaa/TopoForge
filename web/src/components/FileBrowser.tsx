import { ChevronLeft, File, Folder, FolderOpen, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { listFiles } from "../api";
import { translate, type TranslationKey } from "../i18n";
import type { FileListing, Language } from "../types";
import { formatBytes } from "../config";

interface FileBrowserProps {
  open: boolean;
  language: Language;
  onClose: () => void;
  onSelect: (path: string) => void;
}

export function FileBrowser({
  open,
  language,
  onClose,
  onSelect,
}: FileBrowserProps) {
  const t = useCallback(
    (key: TranslationKey) => translate(language, key),
    [language],
  );
  const [listing, setListing] = useState<FileListing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (path?: string) => {
    setLoading(true);
    setError("");
    try {
      setListing(await listFiles(path));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      void load();
    }
  }, [load, open]);

  if (!open) {
    return null;
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="file-browser"
        role="dialog"
        aria-modal="true"
        aria-label={t("chooseFile")}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog-header">
          <div>
            <span className="section-icon">
              <FolderOpen size={18} aria-hidden="true" />
            </span>
            <h2>{t("chooseFile")}</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            onClick={onClose}
            title={t("close")}
            aria-label={t("close")}
          >
            <X size={18} />
          </button>
        </header>
        <div className="browser-path">
          {listing?.parent ? (
            <button
              className="icon-button"
              type="button"
              onClick={() => void load(listing.parent ?? undefined)}
              title={t("parentDirectory")}
              aria-label={t("parentDirectory")}
            >
              <ChevronLeft size={18} />
            </button>
          ) : (
            <span className="icon-spacer" />
          )}
          <code>{listing?.path ?? t("localInputRoots")}</code>
        </div>
        <div className="browser-list" aria-busy={loading}>
          {loading && <div className="empty-state">{t("loading")}</div>}
          {!loading &&
            listing?.entries.map((entry) => (
              <button
                className="browser-entry"
                type="button"
                key={entry.path}
                onClick={() => {
                  if (entry.kind === "directory") {
                    void load(entry.path);
                  } else if (entry.selectable) {
                    onSelect(entry.path);
                    onClose();
                  }
                }}
              >
                {entry.kind === "directory" ? (
                  <Folder size={18} aria-hidden="true" />
                ) : (
                  <File size={18} aria-hidden="true" />
                )}
                <span>{entry.name}</span>
                <small>{formatBytes(entry.size_bytes)}</small>
              </button>
            ))}
          {!loading && listing?.entries.length === 0 && (
            <div className="empty-state">{t("fileEmpty")}</div>
          )}
          {error && <div className="inline-error">{error}</div>}
        </div>
      </section>
    </div>
  );
}
