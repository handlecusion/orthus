import { useEffect, type RefObject } from "react";

// Grab empty space and drag to scroll a horizontal board left/right
// (Sunsama-style "grab the canvas"). It only starts on the board/column
// background — never on cards, buttons, inputs, or drag handles — so it never
// fights card drag-and-drop. Mouse only; touch keeps native scrolling.
const DEFAULT_EXCLUDE = [
  "button",
  "a",
  "input",
  "textarea",
  "select",
  "label",
  "[role='button']",
  "[role='dialog']",
  "[draggable='true']",
  "[aria-roledescription='draggable']",
  "[data-card-drag-handle]",
  "[data-no-card-drag]",
].join(", ");

export function useGrabPan(
  ref: RefObject<HTMLElement | null>,
  options?: { exclude?: string; enabled?: boolean },
) {
  const exclude = options?.exclude ?? DEFAULT_EXCLUDE;
  const enabled = options?.enabled ?? true;

  useEffect(() => {
    const el = ref.current;
    if (!el || !enabled) return;
    let panning = false;
    let startX = 0;
    let startScrollLeft = 0;

    const pannable = (target: EventTarget | null) => {
      if (!(target instanceof HTMLElement)) return false;
      return !target.closest(exclude);
    };

    const onPointerDown = (event: PointerEvent) => {
      if (event.button !== 0 || event.pointerType === "touch") return;
      if (!pannable(event.target)) return;
      panning = true;
      startX = event.clientX;
      startScrollLeft = el.scrollLeft;
      el.style.cursor = "grabbing";
      el.style.userSelect = "none";
      // Stop text selection / native drag-image from hijacking the drag.
      event.preventDefault();
    };

    const onPointerMove = (event: PointerEvent) => {
      if (!panning) return;
      el.scrollLeft = startScrollLeft - (event.clientX - startX);
    };

    const endPan = () => {
      if (!panning) return;
      panning = false;
      el.style.cursor = "";
      el.style.userSelect = "";
    };

    el.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", endPan);
    window.addEventListener("pointercancel", endPan);
    return () => {
      el.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", endPan);
      window.removeEventListener("pointercancel", endPan);
      el.style.cursor = "";
      el.style.userSelect = "";
    };
  }, [ref, exclude, enabled]);
}
