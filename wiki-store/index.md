# wiki-store — 지식 맵

> 이 파일은 에이전트가 wiki-store 탐색을 시작하는 루트 네비게이션 레이어다.
> 상세 설계: `docs/llm-wiki.md` (canonical 참조)
> updated: 2026-05-25

---

## 레이어 구조

| 레이어 | 경로 | 설명 | 현재 수 |
|---|---|---|---|
| sources | `sources/` | corpus doc / Q&A 세션 / 대화 단위별 WikiSource 요약 | 0 |
| claims | `claims/` | 원자 주장 (provenance·confidence·conflict) | 0 |
| wiki | `wiki/` | 개념 중심 정전(canonical) 페이지 | 0 |
| tasks | `tasks/` | 미해결 질문·모순·stale 감사 | 0 |

---

## 운영 파일

- `log.md` — append-only 오퍼레이션 로그
- `README.md` — 운영 규칙 (markdown=SoR, 충돌 정책 등)
- `templates/` — 각 kind별 파일 템플릿

---

## 최근 변경

- 2026-05-25: wiki-store 초기화 (P1.W1) — 빈 스캐폴드 생성

---

## 미해결 질문

_(초기화 단계 — 추가 예정)_

---

## 설계 참조

- `docs/llm-wiki.md` §2 레이어 매핑, §3 저장 레이아웃, §5 canonical 스키마
- `AGENTS.md` §비서 검증 게이트
