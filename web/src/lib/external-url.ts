/**
 * 사용자가 입력한 외부 링크(링크드인 · 웹사이트 · 콘솔 URL 등)를
 * "클릭하면 바로 그 사이트로 이동"하는 절대 href 로 정규화한다.
 *
 * 핵심 버그 방지: 사용자가 `linkedin.com/in/foo` 처럼 스킴 없이 입력하면
 * 브라우저는 이를 상대경로로 보고 `현재경로/linkedin.com/in/foo` 로 이동해
 * "클릭해도 해당 사이트로 안 가는" 문제가 생긴다. → 스킴이 없으면 https:// 를 보강한다.
 *
 * 보안: http/https/mailto/tel 외의 스킴(javascript:, data: 등)은 null 로 차단(XSS).
 */
export function toExternalHref(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const v = raw.trim();
  if (!v) return null;

  // 이미 안전한 스킴이면 그대로 사용
  if (/^(https?:|mailto:|tel:)/i.test(v)) return v;

  // 그 외 명시적 스킴(javascript:, data:, vbscript: 등)은 차단
  if (/^[a-z][a-z0-9+.-]*:/i.test(v)) return null;

  // 프로토콜-상대 URL(//host/...) → https 보강
  if (v.startsWith("//")) return `https:${v}`;

  // 스킴 없음 → https:// 보강
  return `https://${v}`;
}

/**
 * 링크를 화면에 보여줄 때 쓰는 짧은 표기.
 * (https://www. 프리픽스와 끝의 / 를 제거한 사람이 읽기 좋은 형태)
 */
export function displayUrl(raw: string | null | undefined): string {
  if (!raw) return "";
  return raw
    .trim()
    .replace(/^[a-z][a-z0-9+.-]*:\/\//i, "")
    .replace(/^www\./i, "")
    .replace(/\/+$/, "");
}
