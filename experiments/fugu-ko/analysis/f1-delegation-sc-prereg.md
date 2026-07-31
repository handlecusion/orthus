# F1 — 위임추출 슬롯 자기일관성(k=3) 글루 사전선언 (2026-07-23)

> 상태 **등록(측정 전)**. Metric ⑥ Flow Bench 개선 후보 — owner 지시 "오케스트레이터도 높여봐".

## 1. 근거 (측정 완료된 사실)

- 오케스트레이터 flow 72/84의 결손은 전부 위임추출 스테이지: 단일 대비 −1(B-g3-0011, 같은
  EXAONE 재실행 편차), Sonnet(74/84) 대비 −2(B-g3-0011/0013 — 둘 다 delegation 스테이지).
- routing 실패 9건은 Sonnet도 동일하게 실패 — 모델 격차가 아님.
- B1에서 자기일관성 글루는 t3에서 +1.08pp/파손 1건으로 한계효용이 낮았지만, 위임추출은
  "가끔 미검출(flaky miss)"이 유일 결손이라 다수결 구제에 정확히 맞는 모양이다.

## 2. 설계

`extract_delegation_intent`에 k회 호출 + 결정론 다수결(`ORTHUS_DELEGATION_SC_K`, **default 1 =
현행 byte-identical, fail-closed**). is_delegation은 엄격 과반, 필드는 과반 표 내 최빈값
(동률은 호출 순서 첫 값). 안전 방향: 다수결은 1회성 오탐(FP)도 억제하므로 위임 오탐 지표를
악화시키지 않아야 한다(§4에서 검증). LLM은 여전히 추출만, 실행 결정은 결정론 gate 불변.

## 3. 등록 예측

1. 오케스트레이터+SC(k=3) flow가 72/84 → **74/84(Sonnet 동수) 이상**.
2. 깨는 것 0 (기존 pass 플로우 유지).
3. t10 위임 오탐 홀드아웃(24 트랩)에서 오탐 수 악화 없음 (EXAONE 2 유지 이하).

## 4. 측정 계획

flow arm 재실행(orchestrator, 플래그 on, staging DB, tier all) + t10 홀드아웃 오탐 재측정.
prod 활성화는 별도 owner 결정(코드는 default off로 머지 가능).
