# wiki-store 운영 규칙

> 상세 설계: `docs/llm-wiki.md` (canonical 참조)
> 이 파일은 내부 운영 규칙만 담는다. 사용자 직접 접근 불가.

---

## 핵심 원칙

1. **markdown 파일이 SoR(Source of Record).** Postgres `wiki_pages` / `wiki_chunks` / `wiki_links`는 검색·그래프·감사용 파생 인덱스다. 절대 역방향(DB → markdown)으로 수정하지 않는다.
2. **에이전트 내부 전용.** 사용자는 비서/wiki 답변과 근거만 본다. wiki-store 파일에 직접 접근하지 않는다.
3. **provenance 명시 필수.** 모든 claim은 source_ref(doc_id / query_id 등) + confidence를 가진다. 근거 없는 주장 silent 추가 금지.
4. **충돌 시 WikiTask 생성, silent overwrite 금지.** 모순·중복 발견 시 `tasks/`에 task를 만들고 가시화한다.
5. **distill → consolidate 흐름 준수.** corpus doc / Q&A → WikiSource → WikiClaim[] → WikiPage 순서. 역방향 없음.
6. **redaction 우회 금지.** 직렬화/저장 전 `redact_pii()` 통과. 내부 전용이라도 PII 평문 저장 금지.

---

## 디렉토리 의미

| 경로 | Kind | 내용 |
|---|---|---|
| `sources/` | WikiSource | corpus doc / Q&A 세션 / 대화 단위별 소스 요약 |
| `claims/` | WikiClaim | 원자 주장 (provenance·confidence·conflict) |
| `wiki/` | WikiPage | 개념 중심 정전(canonical) 페이지 |
| `tasks/` | WikiTask | 미해결·모순·stale 감사 |
| `templates/` | — | 각 kind별 파일 작성 템플릿 |

---

## 파일명 규칙

- slug = 파일명(확장자 제외). 영어 소문자 + 하이픈. UUID v4 불필요 — 개념 식별자.
- sources: `{source_type}-{short-desc}-{YYYYMMDD}.md` (예: `corpus-doc-onboarding-flow-20260525.md`)
- claims: `{short-assertion-slug}.md`
- wiki: `{concept-name}.md` (개념 중심)
- tasks: `{kind}-{short-desc}-{YYYYMMDD}.md`

---

## 인덱스 / 로그

- `index.md` — 루트 네비게이션 맵. 의미 있는 변경 후 갱신.
- `log.md` — append-only 오퍼레이션 로그. 사소한 편집은 기록하지 않는다.

---

## Postgres 미러 테이블

`wiki_pages`, `wiki_chunks`, `wiki_links` — `orthus/wiki/store.py`가 markdown SoR을 읽어 동기화. 직접 수정 금지.
