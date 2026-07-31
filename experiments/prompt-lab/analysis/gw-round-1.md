# Track B round 1 — P10 게이트웨이 시드 baseline 리플레이 (2026-07-19)

## 하네스

상주 엔진 대신 `codex exec`(0.144.6, ChatGPT plan OAuth)로 프롬프트 표면을 재현:
- cwd = 임시 workspace + **프로덕션 시드 그대로**(`paths._full_seed()`) —
  codex는 cwd AGENTS.md를 네이티브로 읽으므로 상주 엔진과 동일 주입 경로
- HOME/CODEX_HOME 임시 격리 (실 outbox·skills 오염 0, auth.json만 복사 —
  게이트웨이의 run-local CODEX_HOME과 동일)
- 잡 시나리오는 프로덕션 `build_job_prompt` import로 조립 (seen-set 포함)
- 채점 100% 결정론 루브릭 (판정자 0)

시나리오 9종 × 2회 = 18런: 산출물 포맷(PDF/docx/CSV·md 금지·경로 금지),
잡 계약(ITEM 키·seen 재보고 금지·신규-only 요약), 안내 프로토콜(브라우저
미설치·로그인 벽·메일 라우팅), 스킬화($CODEX_HOME/skills 생성+trigger 형식).

## 결과 — 프로덕션 시드 위반 0 (38/38, 루브릭 수정 후)

| 시나리오 | 체크 | 결과 |
|---|---|---|
| S1 보고서 | outbox .pdf / .md 금지 / 경로 금지 | 6/6 |
| S2 초안 | outbox .docx / .md 금지 / 경로 금지 | 6/6 |
| S3 데이터 | outbox .csv / 경로 금지 | 4/4 |
| S4 잡(전부 seen) | ITEM 0건 · seen 명칭 재언급 0 | 4/4 |
| S5 잡(1건 신규) | 신규만 ITEM 마킹 · seen 미마킹 · 요약 신규-only | 6/6 |
| S6 브라우저 안내 | "orthus-agent browser setup" 정확 안내 · DIY 스크래핑 안 함 | 4/4 |
| S7 메일 라우팅 | 외부 메일 서비스 연결 제안 없음 | 2/2* |
| S8 스킬화 | SKILL.md 생성 + trigger 형식 | 4/4 |
| S9 로그인 벽 | 규약 안내 문구 | 2/2* |

*최초 채점 34/38의 실패 4건은 전부 **루브릭 오탐**이었다: S7은 "메일 연결 문제가
아니라"/"Gmail 플러그인은 설치할 필요 없습니다"라는 **부정문**이 금지 정규식에
걸렸고, S9는 브라우저 MCP가 없는 환경에서 시드 우선순위대로 setup 안내를 한 것.
루브릭을 제안-문맥 한정으로 수정(코드 반영).

## 판정

**변형 없음 — 시드 현행 유지.** measure-first 원칙상 실약점이 없는 표면에
개입하지 않는다. e2e로 다듬어진 현행 시드 카피("문구를 임의로 다듬지 말 것"
주석)의 강건함이 계량으로 확인됐다.

**하네스 자체가 산출물이다**: 향후 시드 블록을 수정(-v2)할 때
`gateway_lab.py run --seed <variant>` → `report`로 9계약 회귀를 돌릴 수 있다
(약 10분, 판정자 불필요). 로그인-벽 전용 안내(S9의 browser login 분기)는
브라우저 MCP attach 환경에서만 검증 가능 — 실봇 QA 체크리스트에 남김.
