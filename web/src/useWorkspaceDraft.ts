import { useEffect, useRef, useState } from "react";

import { freshForm, readDraft, saveDraft } from "./workspaceStorage";

export type DraftStatus = "new" | "restored" | "saved" | "invalid" | "unavailable";

export function useWorkspaceDraft() {
  const [initial] = useState(readDraft);
  const [form, setForm] = useState(() => initial.value ?? freshForm());
  const [draftStatus, setDraftStatus] = useState<DraftStatus>(
    initial.status === "ready" ? "restored" : initial.status === "empty" ? "new" : initial.status,
  );
  const lastForm = useRef(JSON.stringify(form));
  useEffect(() => {
    const serialized = JSON.stringify(form);
    if (serialized === lastForm.current) return;
    try {
      saveDraft(form);
      lastForm.current = serialized;
      setDraftStatus("saved");
    } catch {
      setDraftStatus("unavailable");
    }
  }, [form]);
  return { form, setForm, draftStatus };
}
