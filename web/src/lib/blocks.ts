import type { Block } from "@/lib/api";

/**
 * BlockNote 문서(blocks)가 실질적으로 비었는지 판정한다.
 *
 * 과거에는 lossy markdown 길이(`(await getMarkdown()).length > 0`)로 판정해, 이미지·
 * 체크박스·표·구분선처럼 텍스트가 없는 블록만 있는 본문을 "비었다"고 보고 저장 시 `null`로
 * 지워버렸다(내용 유실). 이 헬퍼는 블록 구조로 판정한다 — 빈 문단만 있을 때만 비었다고 본다.
 */
export function blocksAreEmpty(blocks: Block[] | null | undefined): boolean {
  if (!blocks || blocks.length === 0) return true;
  return blocks.every((b) => {
    const anyB = b as unknown as { type?: string; content?: unknown };
    // 문단이 아닌 블록(이미지/헤딩/체크박스/표/구분선 등)은 텍스트가 없어도 내용으로 본다.
    if (anyB.type && anyB.type !== "paragraph") return false;
    const content = anyB.content;
    if (Array.isArray(content)) {
      // 모든 inline이 공백 텍스트일 때만 빈 문단(링크 등 비-텍스트 inline은 내용으로 본다).
      return content.every((c) => {
        const inline = c as { text?: unknown };
        return typeof inline.text === "string"
          ? inline.text.trim().length === 0
          : false;
      });
    }
    return true;
  });
}
