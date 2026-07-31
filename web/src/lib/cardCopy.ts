/**
 * 드래그/클릭 가능한 카드 안의 "읽기 텍스트"를 복사할 수 있게 해주는 공용 유틸.
 *
 * 칸반 카드(회사 보드·데이터베이스 보드 등)는 카드 전체가
 * 드래그(@dnd-kit 또는 HTML5 draggable) + 클릭(상세 열기) 대상이라, 카드 위에서
 * 마우스를 끌면 텍스트 선택 대신 드래그가 잡혀 Cmd+C 로 복사할 수가 없다.
 *
 * 해결 전략(보드 공통):
 *  1. 복사 대상 텍스트 요소에 `data-card-text` 표식을 단다(+ user-select: text).
 *  2. 그 텍스트 위에서 시작한 포인터다운은 드래그를 시작하지 않게 막아, 브라우저가
 *     기본 텍스트 선택을 하게 둔다. (@dnd-kit 은 sensor activator, HTML5 는
 *     draggable 속성을 그 제스처 동안만 끄는 방식.)
 *  3. 텍스트를 드래그-선택하고 떼는 click 은 카드 상세를 열지 않게 가드한다.
 *
 * 카드를 옮기는 드래그는 텍스트가 아닌 영역(여백·핸들 ⋮⋮·칩 등)에서 그대로 동작한다.
 * 카드 텍스트까지 드래그 시작점이어야 하는 보드는 이 유틸을 쓰지 않고 카드에
 * `user-select: none`을 준다.
 */

import type { MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent } from "react";

/** 복사 가능한 카드 텍스트에 다는 표식 속성. JSX 에서 `data-card-text=""` 로 단다. */
export const CARD_TEXT_ATTR = "data-card-text";

/** 이벤트 타깃이 (복사용) 카드 텍스트 안인지. */
export function isCardTextTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest(`[${CARD_TEXT_ATTR}]`));
}

/** `el` 안에 현재 비어 있지 않은 텍스트 선택이 있는지 — 선택 직후 click 으로 상세가
 *  열리는 것을 막는 데 쓴다. 선택 범위가 이 요소 안에 있을 때만 true. */
export function hasTextSelectionWithin(el: HTMLElement | null | undefined): boolean {
  if (!el || typeof window === "undefined") return false;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.toString().trim()) return false;
  const anchor = sel.anchorNode;
  return anchor != null && el.contains(anchor);
}

/**
 * HTML5 `draggable` 카드용: 포인터다운이 카드 텍스트 위에서 시작하면 그 제스처 동안만
 * draggable 을 꺼서 네이티브 텍스트 선택이 되게 한다. 다음 mouseup(어디서든)에 복구한다.
 * 카드 chrome(여백·칩) 위에서 시작하면 draggable 을 건드리지 않아 드래그가 그대로 된다.
 */
export function suppressHtml5DragOnTextPointerDown(
  e: ReactMouseEvent<HTMLElement> | ReactPointerEvent<HTMLElement>,
): void {
  if (!isCardTextTarget(e.target)) return;
  const el = e.currentTarget;
  el.draggable = false;
  const restore = () => {
    el.draggable = true;
    window.removeEventListener("mouseup", restore, true);
  };
  window.addEventListener("mouseup", restore, true);
}
