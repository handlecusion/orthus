# 모델×태스크 매트릭스 결과


## rewrite — 지시어잔존 (낮을수록 좋음=True)
  solar: 1/58 (1.7%)
  ax: 6/58 (10.3%)
  exaone: 20/58 (34.5%)
    solar vs ax: 0:5 p=0.0625 → solar 우세
    solar vs exaone: 1:20 p=0.0000 → solar 우세
    ax vs exaone: 3:17 p=0.0026 → ax 우세

## synthesize — 인용마커 (낮을수록 좋음=True)
  solar: 4/29 (13.8%)
  ax: 1/29 (3.4%)
  exaone: 1/29 (3.4%)
    solar vs ax: 3:0 p=0.2500 → ax 우세
    solar vs exaone: 3:0 p=0.2500 → exaone 우세
    ax vs exaone: 0:0 p=1.0000 → = 우세

## synthesize — action누설 (낮을수록 좋음=True)
  solar: 3/29 (10.3%)
  ax: 0/29 (0.0%)
  exaone: 0/29 (0.0%)
    solar vs ax: 3:0 p=0.2500 → ax 우세
    solar vs exaone: 3:0 p=0.2500 → exaone 우세
    ax vs exaone: 0:0 p=1.0000 → = 우세

## decompose — 게이트오답 (낮을수록 좋음=True)
  solar: 6/38 (15.8%)
  ax: 20/38 (52.6%)
  exaone: 10/38 (26.3%)
    solar vs ax: 1:15 p=0.0005 → solar 우세
    solar vs exaone: 1:5 p=0.2188 → solar 우세
    ax vs exaone: 10:0 p=0.0020 → exaone 우세

## delegation — 오탐(함정fire) (낮을수록 좋음=True)
  solar: 7/54 (13.0%)
  ax: 14/54 (25.9%)
  exaone: 6/54 (11.1%)
    solar vs ax: 1:8 p=0.0391 → solar 우세
    solar vs exaone: 3:2 p=1.0000 → exaone 우세
    ax vs exaone: 8:0 p=0.0078 → exaone 우세

## distill — 오염(문서단위) (낮을수록 좋음=True)
  solar: 0/48 (0.0%)
  ax: 0/47 (0.0%)
  exaone: 0/48 (0.0%)
    solar vs ax: 0:0 p=1.0000 → = 우세
    solar vs exaone: 0:0 p=1.0000 → = 우세
    ax vs exaone: 0:0 p=1.0000 → = 우세

## wiki_qa — 라운드로빈 쌍대 (판정자, 높을수록 좋음=승)
  solar: 승 37 패 69 (Copeland -32)
  ax: 승 47 패 36 (Copeland +11)
  exaone: 승 51 패 30 (Copeland +21)
    solar vs ax: 21:33 tie=5 p=0.1337 → ax
    solar vs exaone: 16:36 tie=7 p=0.0078 → exaone
    ax vs exaone: 14:15 tie=30 p=1.0000 → exaone

## 신뢰성 (결정론)
  solar: qa오류 0/59 · 지연 p50=1140ms p95=1823ms
  ax: qa오류 0/59 · 지연 p50=1553ms p95=4777ms
  exaone: qa오류 0/59 · 지연 p50=945ms p95=3363ms

## 다중비교 보정 (Holm, α=0.05)
  rewrite/지시어잔존 solarvexaone: p=0.0000 < 0.0024 ★유의 → solar
  decompose/게이트오답 solarvax: p=0.0005 < 0.0025 ★유의 → solar
  decompose/게이트오답 axvexaone: p=0.0020 < 0.0026 ★유의 → exaone
  rewrite/지시어잔존 axvexaone: p=0.0026 < 0.0028 ★유의 → ax