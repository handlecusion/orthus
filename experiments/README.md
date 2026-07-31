# experiments

측정 하네스 모음. 이 시스템의 모델 배정은 전부 여기서 나온 실측이 근거다.

| 디렉터리 | 내용 |
|---|---|
| `fugu-ko/` | LLM 작업 슬롯별 측정 캠페인 — golden 생성기·러너·저지·분석기. **결과 요약: [`fugu-ko/RESULTS.md`](fugu-ko/RESULTS.md)** |
| `fugu-ko/embedding/` | 임베딩 슬롯 교체 실험 (Solar `embedding-passage` 채택 근거, 대칭 배선 결론) |
| `fugu-ko/external/` | 공개 벤치마크(WildBench-ko 포트, MT-Bench/SummEval 픽스처) 하네스 |
| `prompt-lab/` | 프롬프트 변형 실험 (distill claim-cap artefact 발견 등) |

> 공개 빌드 주의: 원본 golden set은 사내 위키/DB에서 역생성한 것이라 이 레포에는
> 없다. 하네스는 전부 남아 있으므로, 자기 노드의 위키로 golden을 다시 만들면 전
> 측정을 재현할 수 있다 (`fugu-ko/RESULTS.md` §6).

## 아카이브 주의

측정 캠페인 원본은 멀티벤더 비교였고, 일부 러너의 **프론티어 저지/비교 arm**은
공개 빌드에서 제거된 어댑터(bedrock/cli/codex_pool)를 참조한다
(`b2_run.py`, `m7_run.py`, `arena_run.py`, `harness_e2e.py`, `e2e/judge_pilot.py`,
`embedding/gen_questions.py` 일부 경로). 해당 arm은 실행 시 명확한 안내와 함께
종료되며, Solar 단일 arm과 분석 스크립트는 그대로 동작한다.
