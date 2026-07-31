"""D6 Phase E-① — 결정론 피처 주입 (LLM 0회, 서빙 시점 재현 가능).

D5 선택기는 `[task] question` 원문만 봤다. 워커 실패 모드(이모지 db_name 재현 실패, count↔groupby
과분해)는 **구조적**인데, 인코더가 그걸 표면 문자열에서 유추해야 했다. 여기서는 그 축을 **직접 태그로**
준다 — 질문 텍스트 + 알려진 db_name 목록(서빙도 보유)만으로 결정론 계산하므로 누출이 아니다.

피처(문자열 프리픽스로 인코더 입력에 붙임):
  [db_hard=emoji|trail|ascii|none] : 질문이 가리키는 DB의 이름 문자클래스 (baseline 이모지 누락 약점)
  [ambig=0|1]  : count 신호("총 몇 개"/"전체") + 필드-그룹 신호("상태별"/"별로") 공존 (A.X 과분해 약점)
  [gb=0|1]     : groupby 신호
  [flt=0|1]    : 필터 신호(값 비교/조건)
  [len=s|m|l]  : 질문 길이 버킷
서빙에서도 같은 함수로 계산 → 학습/추론 일관.
"""
from __future__ import annotations

import re

# count(단일 집계) 신호 — "총 몇 개", "전체", "모두 몇"
_COUNT_SIG = re.compile(r"총\s*몇|전체|모두\s*몇|다\s*합|통째|총합|몇\s*건|몇\s*개")
# groupby/분류 신호 — "X별", "별로", "분포", "나눠", "각각", "구분", "종류", "마다"
_GB_SIG = re.compile(r"별\s*(로|\s|건수|개수|분포|인원|몇)|별로|분포|나눠|각각|묶|구분|종류|마다")
# ambig 전용 신호 — "그룹 나누지 말고 총합"의 언어적 표지(필드 언급 + 집계 붕괴 요청)
_AMBIG_SIG = re.compile(r"상관없|나누지\s*말|나눠\s*말|구분\s*있|종류\s*상관|별로\s*말|통째|다\s*합쳐|총합")
# filter 신호 — "인 항목", "=", "조건", "이면서", "짜리"
_FLT_SIG = re.compile(r"인\s*(항목|건|게|것)|이면서|그리고|조건|짜리|=|인데")


def db_char_class(name: str) -> str:
    if name != name.strip():
        return "trail"
    if re.search(r"[^가-힣a-zA-Z0-9 ]", name):
        return "emoji"
    return "ascii"


def resolve_db(q: str, db_names: list[str]) -> str | None:
    """질문에 등장하는 알려진 db_name 중 가장 긴 매치(부분일치 포함, 이모지/공백 무시 매치도 시도)."""
    hits = [d for d in db_names if d in q]
    if hits:
        return max(hits, key=len)
    # 이모지/후행공백 무시 정규화 매치(모델이 이모지 빠뜨린 질문도 잡되, 원 db_name 반환)
    def canon(s: str) -> str:
        return re.sub(r"[^가-힣a-zA-Z0-9]", "", s)
    qc = canon(q)
    cand = [d for d in db_names if canon(d) and canon(d) in qc]
    return max(cand, key=len) if cand else None


def extract(q: str, db_names: list[str]) -> dict:
    db = resolve_db(q, db_names)
    hard = db_char_class(db) if db else "none"
    count_sig = bool(_COUNT_SIG.search(q))
    gb = bool(_GB_SIG.search(q))
    flt = bool(_FLT_SIG.search(q))
    # ambig: 집계 붕괴 표지(_AMBIG_SIG)만 = 과분해 유도. count+gb 공존은 groupby 오탐이라 제외.
    ambig = int(bool(_AMBIG_SIG.search(q)))
    n = len(q)
    lb = "s" if n < 18 else ("m" if n < 34 else "l")
    return {"db": db, "db_hard": hard, "ambig": ambig, "gb": int(gb and not ambig),
            "flt": int(flt), "len": lb}


def prefix(q: str, db_names: list[str]) -> str:
    """인코더 입력용 피처 프리픽스 문자열."""
    f = extract(q, db_names)
    return (f"[db_hard={f['db_hard']}][ambig={f['ambig']}][gb={f['gb']}]"
            f"[flt={f['flt']}][len={f['len']}] {q}")


if __name__ == "__main__":  # 자가검증(§12.5: 자를 먼저 의심)
    dbs = ["협업업무표", "🎯 NOVA 영입 후보 리스트", "직원 "]
    cases = [
        ("협업업무표에 항목이 총 몇 개야?", "ascii", 0),
        ("🎯 NOVA 영입 후보 리스트를 Tier별로 개수 알려줘", "emoji", 0),
        ("영입 후보 리스트 상태 구분 있잖아, 다 합쳐서 총 몇 개야?", "emoji", 1),  # 이모지 누락+ambig
        ("직원 총 몇 명이야?", "trail", 0),
    ]
    ok = 0
    for q, exp_hard, exp_ambig in cases:
        f = extract(q, dbs)
        good = f["db_hard"] == exp_hard and f["ambig"] == exp_ambig
        ok += good
        print(f"{'OK ' if good else 'FAIL'} {prefix(q, dbs)}  → hard={f['db_hard']} ambig={f['ambig']}")
    print(f"자가검증 {ok}/{len(cases)}")
