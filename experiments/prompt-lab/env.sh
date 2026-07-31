#!/usr/bin/env bash
# prompt-lab 실행 환경 — `source experiments/prompt-lab/env.sh` 로 로드한다.
# 레포 루트에서 실행할 것. (키·원자료는 커밋 대상이 아니다 — .gitignore 참조)

# 레포 루트 — zsh/bash 무관하게 git으로 해석한다 ($BASH_SOURCE는 bash 전용이라
# zsh에서 source하면 빈 값이 되어 경로가 어긋난다).
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/experiments/fugu-ko:$REPO_ROOT/experiments/fugu-ko/embedding:$REPO_ROOT/experiments/prompt-lab"

# 국내 모델 키 (레포 밖 보관, 0600). Solar·A.X·EXAONE 3종.
export FUGU_KEYS="${FUGU_KEYS:-$HOME/.orthus/fugu-keys.json}"

# 검색 임베딩 = Solar (국내 임베딩 API는 Solar뿐). 키는 keys.json의 upstage 항목 재사용.
export ORTHUS_EMBEDDING=solar
if [ -f "$FUGU_KEYS" ]; then
  _SOLAR_KEY=$(python3 -c "import json,sys;print(next(e['key'] for e in json.load(open('$FUGU_KEYS')) if e['provider']=='upstage'))" 2>/dev/null)
  export ORTHUS_LLM_SOLAR_API_KEY="$_SOLAR_KEY"
  export ORTHUS_EMBEDDING_SOLAR_API_KEY="$_SOLAR_KEY"
  unset _SOLAR_KEY
fi

# 로컬 실험 DB (docker orthus_pg). 기본은 회사 위키 재구축본(orthus).
# 미니위키 실험은 아래를 orthus_miniwiki 로 바꾼다.
export ORTHUS_PG_DSN="${ORTHUS_PG_DSN:-postgresql+psycopg://orthus:orthus@localhost:5433/orthus}"

# 판정자: codex(ChatGPT OAuth) — API 빌링과 별개. `codex login` 상태여야 한다.
# 국내 모델 판정으로 대체하려면 각 하네스의 --judge 를 solar|ax|exaone 로 준다.

echo "prompt-lab env loaded"
echo "  repo    : $REPO_ROOT"
echo "  keys    : $FUGU_KEYS $([ -f "$FUGU_KEYS" ] && echo '✓' || echo '✗ 없음')"
echo "  DB      : $ORTHUS_PG_DSN"
echo "  judge   : codex $(command -v codex >/dev/null && echo '✓' || echo '✗ 없음')"
