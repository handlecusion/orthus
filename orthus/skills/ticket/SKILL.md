---
name: ticket
description: How an agent tracks issues on orthus — `orthus ticket ls|add|set|rm` CLI and the ticket_* MCP tools. Read this before creating or updating tickets.
---

# Orthus ticket skill

티켓 명령은 unix 동사 4개다: **`ls` / `add` / `set` / `rm`** (+ `show`/`comment`/`projects`).
기본 대상은 **내 보드**(개인 이슈 트래킹), `--project <회사 프로젝트>`를 주면 **그
프로젝트의 칸반(업무 보드)** 이 대상이 된다. MCP tools(`ticket_*`)도 같은 데이터를
다룬다. 토큰은 owner-bound라 내 보드는 토큰 소유자의 것이다.

## 치트시트

```bash
orthus ticket ls                                  # 내 보드 열린 티켓
orthus ticket ls --project "탕수육 레시피"          # 프로젝트 칸반 (컬럼별)
orthus ticket add "로그인 500 조사" --priority u --channel nova      # 내 보드
orthus ticket add "경쟁사 분석" --project "탕수육 레시피" \
  --assignee Jaden --prop 우선순위=높음 --due +6d --body "## 상세…"   # 칸반 카드
orthus ticket set 85c4 --status 진행중             # id만으로 저장소 자동 판별
orthus ticket set 3fa8 --status done              # 내 보드도 같은 명령
orthus ticket rm 3fa8                             # soft: archived (복구 가능)
orthus ticket show 85c4                           # 상세 + 댓글/본문
orthus ticket comment 3fa8 "원인: 쿠키 만료"       # 내 보드 전용
orthus ticket projects                            # 채널 + 버킷 키 목록
```

## 핵심 규칙

1. **id는 저장소를 몰라도 된다.** `set/rm/show/comment <id>`는 내 보드 → 담당
   프로젝트 칸반 순으로 prefix(≥4자) 검색한다. 겹치면 후보를 나열하니 더 긴 id나
   `--project`로 좁혀라. `--project`를 주면 칸반으로 한정되고 **제목 일부**로도
   카드를 지정할 수 있다.
2. **`--status`/`--priority`는 어디서든 같은 단어가 통한다 (자동 매핑).**
   - 칸반에서 `--status open|todo` → 첫 to_do 컬럼, `in_progress|doing` → 진행 그룹,
     `done|complete` → 완료 그룹. 컬럼 옵션 이름(공백 무시: 시작전=시작 전)도 그대로.
   - 칸반에서 `--priority u|urgent|p|n|l` → 그 보드의 우선순위 select에서 비슷한
     옵션(긴급/높음/보통/낮음…)으로 자동 매핑. 실제 옵션 이름(높음, 핫픽스…)도 그대로.
   - 나머지 플래그는 저장소별이다 — 잘못 쓰면 오류의 NOTE 행이 이 저장소에서의
     대응 수단을 알려준다:
   - 내 보드 전용: `--date`|`--bucket`(배치는 정확히 하나, add 기본 today) ·
     `--channel 이름|none`(회사 채널이면 팀 공개) · `--note`(''=삭제) · `--id`(멱등 키)
   - 칸반 전용: `--assignee <팀 멤버 이름>` · `--prop 이름=값`(임의 속성) ·
     `--body`(markdown, add 전용)
3. **rm은 soft다.** 내 보드 티켓 → `archived`(복구:
   `set <id> --status open`). 칸반 카드는 삭제 불가(웹 전용) — 보류/완료 컬럼으로
   `set`. 실삭제 경로는 어디에도 없다.
4. **회사 채널/프로젝트는 담당 배정된 것만** 지정할 수 있다. 목록은
   `orthus ticket projects`.
5. **프로젝트에 보드(DB)가 여러 개일 수 있다.** `ls --project`는 전부 보여주고,
   `set/show`의 제목·id 검색도 전 보드를 뒤진다. `add`는 기본 '업무 보드'
   (여럿인데 그 이름이 없으면 후보 나열) — `--board 이름`으로 대상 지정. 보드마다
   컬럼 이름이 달라도 open/doing/done 영어 별칭은 어느 보드든 통한다.

## 재시도 안전 (멱등 add)

내 보드 add 재시도 시 중복을 막으려면 클라이언트 UUID를 `--id`(MCP:
`idempotency_key`)로 넘겨라 — 같은 키 재전송은 기존 티켓을 돌려준다.

## 실시간 반영

내 보드(`/board`)와 프로젝트 칸반 둘 다 SSE 실시간이다 — CLI로 만들거나 옮긴
티켓/카드는 열려 있는 화면에 새로고침 없이 뜬다. 칸반 row 저장은 전체 교체
계약이라 `set`이 서버 최신 props를 읽어 병합해 보낸다(직접 raw API 금지).

## 오류를 읽는 법

모든 오류는 self-healing 형식: 텍스트 `FAIL`(원인)/`VALID`(유효값)/`TRY`(고친 예시)/
`NOTE`(규칙 배경·이 저장소의 대응 수단), `--json`은
`{"ok": false, "error": {"message", "valid", "example", "note"}}`. `TRY`를 그대로
따라 하면 대부분 한 번에 복구되고, `NOTE`를 읽으면 같은 실수를 반복하지 않는다.
쓰기는 토큰당 5분 30회 rate limit — 429면 기다렸다 재시도.

### 흔한 오류 → 해결

| 오류 | 해결 |
|---|---|
| `project 'X' not found on your board` | 담당 배정된 회사 프로젝트만 가능 — `orthus ticket projects`로 확인 |
| `'상태' 옵션 'Y' 없음` | VALID의 실제 컬럼 이름 또는 open/done 영어 사용 |
| `--date은(는) 내 보드 티켓 전용` | 칸반 카드엔 `--due`(마감)와 `--status`(컬럼)만 |
| `'X'가 N건과 일치` | 더 긴 id prefix 또는 `--project`로 좁히기 |
| `칸반 카드 삭제는 웹(세션) 전용` | `set <id> --status done`으로 완료 처리 |
| 429 rate limit | 5분 창 — 기다렸다 재시도 |
