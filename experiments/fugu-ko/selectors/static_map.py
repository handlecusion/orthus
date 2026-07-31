"""규칙 선택기 (b) — 논문 Fugu의 정적 라우팅. 태스크 → 최적 모델.

env(ORTHUS_LLM)가 아니라 **코드 레이어**다. orthus는 태스크를 이미 안다(엔드포인트/`classify`가
structured|wiki|graph를 판정) → 태스크-라벨 기반 라우팅은 실현 가능(치팅 아님).

잎(leaf) 작업 배정 근거(D2/D3 실측, `analysis/d2-results.md`, `analysis/d3-orchestration.md`):
- structured(NL→SQL): Solar/EXAONE 94% > A.X 78% → **solar**
- routing(라우팅 분류): A.X 20/21 최강(=현행 baseline) → **ax**
- wiki_qa(/ask): Solar 86% 승률 최강 > EXAONE 78% > A.X 38% → **solar**

오케스트레이터(agent-work) 계층 배정 근거(D4 실측, `analysis/d4-orchestrator-layer.md`):
- intent(classify_intent 7-way): 4모델 전부 LLM 도달분 6/6 동률 → 모델 무관, 종합 최강 **solar**
- decompose(should_decompose 게이트): Solar/baseline 90% > EXAONE 80% ≫ **A.X 20%(붕괴)** → **solar**
  (A.X는 복합질문을 단일로 오판 — 오케스트레이터에 절대 배정 불가)
- synthesize(조각 답 통합): Solar/EXAONE 무패(승률 100%) vs **A.X 0%(0승2패, 토큰 누출/잘림)** → **solar**
"""
from __future__ import annotations

# 태스크 → 워커 slug (pool.py의 slug)
# 잎 계층(T2/T3/T5) + 오케스트레이터 계층(T6/T7) 통합 배정.
TASK_MODEL: dict[str, str] = {
    # 잎(leaf) 작업 — /ask 라우터가 분기하는 실행 단위
    "structured": "solar",
    "routing": "ax",
    "wiki_qa": "solar",
    # 오케스트레이터(agent-work) 결정 — /agent-work/orchestrate 내부 LLM 판단
    "intent": "solar",
    "decompose": "solar",
    "synthesize": "solar",
}
_DEFAULT = "solar"


def select(task: str) -> str:
    """태스크 라벨 → 워커 slug. 미지 태스크는 종합 최강(solar)."""
    return TASK_MODEL.get(task, _DEFAULT)
