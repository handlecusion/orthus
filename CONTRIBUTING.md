# Contributing

이 저장소의 상시 운영 규칙은 `AGENTS.md`(canonical)와 `CLAUDE.md`(Claude Code
호환 shim)에 있다. 기여 전 둘을 읽는다. AI 에이전트(Codex/Claude Code)도 같은
두 파일을 읽고 아래 규칙을 따른다.

## PR 흐름

1. 기능 작업은 `.worktrees/<topic>` feature 브랜치에서 한다
   (`AGENTS.md "Worktree / PR"). main 직접 push 금지 — 전부 PR.
2. PR 제목에 마일스톤 ID를 넣는다 (예: `[P6.7] mail multi-account UI`).
3. **PR body는 `.github/pull_request_template.md` 체크리스트를 채운다.**
   - **웹 UI**로 PR을 열면 템플릿이 자동으로 본문에 들어온다.
   - **CLI/agent**(`gh pr create`, API)로 열면 자동 주입되지 **않는다**.
     `make pr T="<제목>"`로 템플릿을 시드하거나
     `gh pr create --body-file .github/pull_request_template.md` 후 채운다.
4. **Risk** 분류, **Protected Area** 체크, **QA Evidence**(무엇을 바꿨는지 /
   어떻게 테스트했는지 / 실행한 명령 / UI면 스크린샷)를 반드시 채운다.
5. **Protected Area**가 하나라도 해당하면 owner review 전 self-merge 금지.

## 검증

- backend: `make test`. FE/auth/server 변경은 `AGENTS.md` "Worktree / PR"를 따른다.
- lint/format: `make fmt` (ruff), FE는 `pnpm lint` + `pnpm build`.
- 설계가 바뀌면 같은 PR에서 `docs/`와 `AGENTS.md`를 갱신한다.

## 왜 템플릿만으로는 부족한가

`.github/pull_request_template.md`는 GitHub **웹 UI** 전용 자동 주입이다. CLI나
AI 에이전트가 만드는 PR에는 들어오지 않으므로, PR 규칙의 SoR(single source of
truth)는 템플릿 파일이 아니라 **`AGENTS.md` "PR / 커밋"** 섹션이다. 본
`CONTRIBUTING.md`는 사람 기여자용 요약이고, 규칙이 충돌하면 `AGENTS.md`가 우선한다.
