# 미니위키 — 저작 모델 × downstream 답변

## granularity + 거부율 (결정론)
  solar: pages=339 claims=387 claims/doc=5.7 저작=197.0s | 답변거부 0/70 오류 0
  ax: pages=197 claims=238 claims/doc=4.2 저작=2116.0s | 답변거부 0/70 오류 0
  exaone: pages=272 claims=447 claims/doc=6.7 저작=2073.4s | 답변거부 0/70 오류 0

## 거부율 paired (검색 실패 proxy, 낮을수록 좋음)
    solar vs ax: 0:0 p=1.0000 → =
    solar vs exaone: 0:0 p=1.0000 → =
    ax vs exaone: 0:0 p=1.0000 → =

## 답변 품질 라운드로빈 (codex, 높을수록 좋음)
  solar: 승 33 패 61 (Copeland -28)
  ax: 승 58 패 34 (Copeland +24)
  exaone: 승 50 패 46 (Copeland +4)
    solar vs ax: 14:31 tie=25 p=0.0161 → ax
    solar vs exaone: 19:30 tie=21 p=0.1524 → exaone
    ax vs exaone: 27:20 tie=23 p=0.3817 → ax

## Holm 보정 (α=0.05)
  보정 후 유의 없음 (총 6 비교, 최소 p=0.0161) → 저작 모델은 downstream을 유의하게 바꾸지 않음.