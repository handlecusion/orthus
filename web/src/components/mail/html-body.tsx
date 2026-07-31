"use client";

/**
 * 수신 메일 HTML 본문을 원본 서식 그대로 렌더하는 샌드박스 iframe.
 *
 * 보안 모델: sandbox에 allow-scripts를 주지 않으므로 메일 안 스크립트/폼은
 * 실행되지 않는다. allow-same-origin은 부모에서 contentDocument 높이 측정용이며
 * 스크립트가 실행되지 않는 한 안전한 조합이다. 링크는 <base target="_blank"> +
 * allow-popups(-to-escape-sandbox)로 사용자 클릭 시 새 탭에서만 열린다.
 * 인라인 cid: 이미지는 서버(`_inline_cid_images`)가 data: URI로 치환해 온다.
 */

import { useEffect, useRef } from "react";

import { API_BASE } from "@/lib/api";

const MAX_HEIGHT_PX = 20000;
// 본문을 감싸는 측정용 루트. 높이는 이 div의 content 높이로만 잰다 —
// `documentElement`/`body`는 엔진(WebKit/WKWebView 등)에 따라 iframe 뷰포트
// 높이로 "채워져" 실제 내용보다 크게 측정되고, 그 결과 iframe이 내용보다 길어져
// 본문 아래에 큰 흰 여백이 남는다("펼치면 백색"). block+flow-root div는 모든
// 엔진에서 자식 margin까지 포함한 정확한 content 높이를 준다.
const MAIL_ROOT_ID = "__orthus_mail_root";

/**
 * 본문 <img>의 원격(http/https) src를 orthus 이미지 프록시로 치환한다.
 * 핫링크 차단(Google mail-sig, claude.ai 로고 등)·만료 presigned S3 URL·
 * http 혼합콘텐츠로 깨지던 이미지가 서버 경유로 렌더된다. data:/cid:는 그대로.
 */
export function proxyMailImages(
  html: string,
  ctx?: { backend?: string; accountId?: string | null },
): string {
  if (!html) return html;
  return html.replace(
    /(<img\b[^>]*?\ssrc=)(["'])(https?:\/\/[^"']+)\2/gi,
    (_match, prefix: string, quote: string, src: string) => {
      // 속성값 안의 &amp;는 실제 &로 되돌려 원본 URL을 프록시에 넘긴다.
      const params = new URLSearchParams({ url: src.replace(/&amp;/g, "&") });
      if (ctx?.backend) params.set("backend", ctx.backend);
      if (ctx?.accountId) params.set("account_id", ctx.accountId);
      return `${prefix}${quote}${API_BASE}/mail/image?${params.toString()}${quote}`;
    },
  );
}

export function MailHtmlBody({ html, className }: { html: string; className?: string }) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);

  useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    let observer: ResizeObserver | null = null;
    let raf = 0;
    let pollRaf = 0;
    let tracking = false;
    let polls = 0;
    const timers: number[] = [];

    function measure() {
      const doc = iframe?.contentDocument;
      const root = doc?.getElementById(MAIL_ROOT_ID);
      if (!iframe || !root) return;
      // 측정 루트 div의 content 높이만 본다. 이 값은 iframe 높이에 의존하지 않아
      // (뷰포트 채움/래칫 없음) grow/shrink 모두 정확하고 엔진 독립적이다.
      const next = Math.max(root.scrollHeight, root.offsetHeight);
      if (next <= 0) return;
      const target = Math.min(next + 8, MAX_HEIGHT_PX);
      // 서브픽셀 churn로 ResizeObserver가 무한 재측정하지 않도록 1px 이하는 무시.
      if (Math.abs((parseFloat(iframe.style.height) || 0) - target) > 1) {
        iframe.style.height = `${target}px`;
      }
    }

    function track() {
      const doc = iframe?.contentDocument;
      const root = doc?.getElementById(MAIL_ROOT_ID);
      if (!doc || !root || tracking) return;
      tracking = true;
      measure();
      observer?.disconnect();
      // 루트 div를 관찰한다. div 높이는 iframe 리사이즈에 반응하지 않으므로
      // (자기 참조 피드백 루프 없음) 이미지/폰트 지연 로드 등 실제 content
      // 변화에만 재측정이 돈다.
      observer = new ResizeObserver(() => {
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(measure);
      });
      observer.observe(root);
      // 마케팅 메일처럼 이미지가 늦게 로드되며 높이가 변하는 경우: 각 <img>가
      // 끝날 때마다 재측정한다. ResizeObserver가 iframe 안에서 신뢰도가 낮은
      // 엔진(WKWebView 등)에서도 이 per-image 신호로 확실히 따라잡는다.
      for (const img of Array.from(doc.querySelectorAll("img"))) {
        if (img.complete) continue;
        img.addEventListener("load", measure, { once: true });
        img.addEventListener("error", measure, { once: true });
      }
      // 지연 로드/리플로우 보강 재측정.
      for (const delay of [250, 1000, 3000]) timers.push(window.setTimeout(measure, delay));
    }

    // srcDoc 문서는 파싱되는 즉시(readyState "interactive") 루트 div가 존재한다.
    // iframe `load` 이벤트는 본문 안 **모든 이미지**가 끝나야 발화하므로(이미지가
    // 많거나 무거운 뉴스레터는 수 초, 프록시 이미지가 느리면 더 오래) 측정을 load에
    // 매달면 그때까지 iframe이 초기 200px로 클리핑돼 "원본이 안 뜬다"처럼 보인다.
    // load를 기다리지 않고 루트가 나타나는 즉시 추적을 시작한다(rAF 폴링, 상한 있음).
    function poll() {
      if (tracking) return;
      track();
      if (!tracking && polls++ < 120) pollRaf = requestAnimationFrame(poll);
    }
    poll();
    // 모든 이미지 완료 후 최종 레이아웃 정착값 한 번 더 보정.
    const onLoad = () => {
      track();
      measure();
    };
    iframe.addEventListener("load", onLoad);
    return () => {
      iframe.removeEventListener("load", onLoad);
      observer?.disconnect();
      cancelAnimationFrame(raf);
      cancelAnimationFrame(pollRaf);
      timers.forEach((t) => window.clearTimeout(t));
    };
  }, [html]);

  const srcDoc = `<!doctype html><html><head><meta charset="utf-8"><base target="_blank"><style>
html,body{margin:0;padding:0}
body{font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Apple SD Gothic Neo","Noto Sans KR",sans-serif;color:#1f2937;background:#fff;word-break:break-word;overflow-wrap:anywhere;overflow-x:auto}
#${MAIL_ROOT_ID}{display:flow-root}
img{max-width:100%;height:auto}
table{max-width:100% !important}
a{color:#2563eb}
blockquote{margin:8px 0;padding-left:12px;border-left:3px solid #e5e7eb;color:#6b7280}
</style></head><body><div id="${MAIL_ROOT_ID}">${html}</div></body></html>`;

  return (
    <iframe
      ref={iframeRef}
      title="메일 본문"
      sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
      srcDoc={srcDoc}
      className={className}
      style={{
        width: "100%",
        border: 0,
        display: "block",
        height: 200,
        background: "#fff",
        colorScheme: "light",
      }}
    />
  );
}
