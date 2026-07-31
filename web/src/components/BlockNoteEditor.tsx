"use client";

import { useEffect, useImperativeHandle, useMemo, useRef } from "react";
import { BlockNoteView } from "@blocknote/mantine";
import { useCreateBlockNote } from "@blocknote/react";
import type { PartialBlock } from "@blocknote/core";
import type { Block } from "@/lib/api";
import { useTheme } from "@/lib/use-theme";

// Editor styles are scoped to this client-only module so the global stylesheet
// keeps valid @import ordering. (BlockNote's sheet nests its own @imports.)
import "@blocknote/core/fonts/inter.css";
import "@blocknote/mantine/style.css";

export interface EditorHandle {
  /** Current document as the opaque block JSON the backend stores. */
  getBlocks: () => Block[];
  /** Lossy markdown rendering for indexing / RAG. */
  getMarkdown: () => Promise<string>;
}

/** Fired on any editor content change. Lets callers track dirty state and lift
 *  content so it survives non-explicit exits (nav away, tab hide, unmount). */
export type EditorChangeHandler = () => void;

/**
 * Thin wrapper around BlockNote. It owns ONE editor instance and re-seeds its
 * content whenever `docId` changes, so switching documents in the list never
 * leaves stale blocks behind. Touches `window` → must be loaded ssr:false.
 */
export function BlockNoteEditor({
  docId,
  initialBlocks,
  initialMarkdown,
  handleRef,
  uploadFile,
  onChange,
  className = "min-h-[60vh]",
}: {
  /** identity of the loaded doc; "" for an unsaved new doc */
  docId: string;
  initialBlocks: Block[] | null;
  /**
   * Fallback body when `initialBlocks` is empty. Notion DB-row documents import
   * with `block_json = []` but carry their rendered text in `markdown`; we parse
   * it into blocks on open so the body is visible and editable instead of blank.
   */
  initialMarkdown?: string | null;
  handleRef: React.RefObject<EditorHandle | null>;
  /**
   * Optional image/file upload handler. When provided, BlockNote's image block
   * can accept files (drag/drop, paste, file picker) and the returned URL is
   * stored on the block. Meeting notes pass a base64 data-URL uploader so
   * photos are embedded inline without any upload backend; /editor omits it.
   */
  uploadFile?: (file: File) => Promise<string>;
  /** Called on every content change (for dirty-tracking / live content lift). */
  onChange?: EditorChangeHandler;
  /**
   * BlockNote root className. Defaults to the editor's tall `min-h-[60vh]`;
   * surfaces like the corpus browser pass a denser value so short docs don't
   * render as a large empty box.
   */
  className?: string;
}) {
  // The seed data and brand-new docs can be empty; an empty array is not a
  // valid BlockNote initial document, so seed a single empty paragraph.
  const initialContent = useMemo<PartialBlock[]>(() => {
    if (initialBlocks && initialBlocks.length > 0) {
      return initialBlocks as unknown as PartialBlock[];
    }
    return [{ type: "paragraph" }];
  }, [initialBlocks]);

  const editor = useCreateBlockNote({ initialContent, uploadFile }, [docId]);
  const { resolvedTheme } = useTheme();

  // Hydrate from markdown when the doc has no block_json (Notion DB-row imports).
  // Runs once per loaded doc; guarded so we never clobber real block content or
  // overwrite edits already in the editor.
  const hydratedFor = useRef<string | null>(null);
  useEffect(() => {
    const hasBlocks = initialBlocks && initialBlocks.length > 0;
    const md = initialMarkdown?.trim();
    if (hasBlocks || !md || hydratedFor.current === docId) return;
    const parsed = editor.tryParseMarkdownToBlocks(md);
    if (parsed.length > 0) {
      editor.replaceBlocks(editor.document, parsed as PartialBlock[]);
      hydratedFor.current = docId;
    }
  }, [docId, editor, initialBlocks, initialMarkdown]);

  useImperativeHandle(
    handleRef,
    () => ({
      getBlocks: () => editor.document as unknown as Block[],
      // 0.51 returns markdown synchronously; wrap so callers can `await` it.
      getMarkdown: async () => editor.blocksToMarkdownLossy(editor.document),
    }),
    [editor],
  );

  // Keyboard users land in the editor body when a fresh doc opens.
  useEffect(() => {
    if (docId === "") editor.focus();
  }, [docId, editor]);

  return (
    <BlockNoteView
      editor={editor}
      theme={resolvedTheme === "dark" ? "dark" : "light"}
      className={className}
      onChange={onChange}
    />
  );
}
