"""D0 헬스체크 + json_only 스모크 + VARCO 프로브.

목적: 대회 제공 4종 키가 (1) 살아있는지 (2) orthus의 json_only 계약(response_format
json_object)을 지키는지(R1) 조기 판정. VARCO는 텍스트 chat 엔드포인트 존재 여부 프로브.

보안: 키는 keys.json에서 읽어 Authorization 헤더에만 쓴다. 키·응답 원문은 stdout에
찍지 않는다(상태/지연/JSON 준수 카운트/짧은 스니펫만). 응답 원문 저장은 gitignore.

실행: python experiments/fugu-ko/probe.py [--n 10]
키 파일 경로는 FUGU_KEYS 환경변수로 override (기본 = handoff 폴더).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field

import httpx

KEYS_PATH = os.environ.get(
    "FUGU_KEYS",
    "<로컬 키 저장소>/keys.json",
)

# provider(문자열 부분매칭) → 호출 스펙. D0(2026-07-08) 실측으로 확정된 값.
# 확정: Solar는 /v1(handoff의 v2는 오기), EXAONE는 model 접미사 제거 + enable_thinking=false
# (reasoning 모델, 안 끄면 content 없음 + 70s), A.X 정상. VARCO는 OpenAI-호환 text chat 경로
# 없음 → 드롭.
TARGETS = {
    "solar": {
        "match": "upstage",
        "url": "https://api.upstage.ai/v1/chat/completions",
        "model": "solar-pro",
        "json_mode": True,
        "min_interval": 0.3,
        "timeout": 40,
        "extra_body": {},
    },
    "exaone": {
        "match": "exaone",
        "url": "https://api.friendli.ai/dedicated/v1/chat/completions",
        "model": None,  # keys.json model 필드에서 첫 토큰만 (엔드포인트ID)
        "model_first_token": True,
        "json_mode": True,
        "min_interval": 0.3,
        "timeout": 120,  # 236B dedicated: 30~45s/call 실측
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    },
    "ax": {
        "match": "a.x",
        "url": "https://awf-gw.adot.ai/v1/chat/completions",
        "model": "A.X-K1",
        "json_mode": True,
        "min_interval": 0.4,  # RPS 3 → 최소 간격 0.34s+, 여유 0.4
        "timeout": 40,
        "extra_body": {},
    },
    # varco: D0 프로브 결과 OpenAI-호환 text chat 엔드포인트 없음 → 드롭(핵심 3종으로 진행)
}


@dataclass
class Result:
    slug: str
    reachable: bool = False
    health_status: int | None = None
    health_ms: int | None = None
    json_ok: int = 0
    json_total: int = 0
    notes: list[str] = field(default_factory=list)


def load_keys() -> dict[str, dict]:
    raw = json.load(open(KEYS_PATH, encoding="utf-8"))
    out = {}
    for entry in raw:
        prov = str(entry.get("provider", "")).lower()
        for slug, spec in TARGETS.items():
            if spec["match"] in prov:
                out[slug] = entry
    return out


def _resolve_model(spec: dict, entry: dict) -> str:
    m = spec["model"] or entry.get("model", "")
    if spec.get("model_first_token"):
        m = m.split()[0]  # "depmkuykpfon9lg (Endpoint ID)" → "depmkuykpfon9lg"
    return m


def _post(spec: dict, key: str, model: str, messages: list[dict], json_mode: bool):
    body: dict = {"model": model, "messages": messages, "temperature": 0, "max_tokens": 256}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    body.update(spec.get("extra_body", {}))
    t0 = time.monotonic()
    with httpx.Client(timeout=spec.get("timeout", 40)) as c:
        r = c.post(spec["url"], headers={"Authorization": f"Bearer {key}"}, json=body)
    ms = int((time.monotonic() - t0) * 1000)
    return r, ms


def health_check(slug: str, spec: dict, entry: dict, res: Result) -> None:
    key = entry["key"]
    model = _resolve_model(spec, entry)
    msgs = [{"role": "user", "content": "한국어로 한 문장만 답하라: 오늘 기분 어때?"}]
    try:
        r, ms = _post(spec, key, model, msgs, json_mode=False)
        res.health_status = r.status_code
        res.health_ms = ms
        res.reachable = r.status_code < 500
        if r.status_code == 200:
            try:
                txt = r.json()["choices"][0]["message"]["content"][:60].replace("\n", " ")
                res.notes.append(f"응답 스니펫='{txt}…'")
            except Exception:
                res.notes.append("200이나 응답 구조 비표준(choices/message 파싱 실패)")
        elif r.status_code == 429:
            res.notes.append("429 rate limit (헬스에서 발생 — 간격 늘려야)")
        else:
            res.notes.append(f"HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        res.notes.append(f"연결 실패: {type(e).__name__}: {str(e)[:100]}")


def json_smoke(slug: str, spec: dict, entry: dict, res: Result, n: int) -> None:
    if not res.reachable or res.health_status != 200:
        res.notes.append("health 200 아님 → json_smoke 스킵")
        return
    key = entry["key"]
    model = _resolve_model(spec, entry)
    sys = {"role": "system", "content": "너는 하나의 JSON 객체만 출력한다. 설명·마크다운 금지."}
    for i in range(n):
        a, b = 2 + i, 5 + i
        user = {
            "role": "user",
            "content": f'{a}와 {b}를 더한 값을 {{"sum": <정수>}} 형식 JSON으로만 답하라.',
        }
        res.json_total += 1
        try:
            r, _ = _post(spec, key, model, [sys, user], json_mode=spec["json_mode"])
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"].strip()
                obj = json.loads(content)  # 순수 JSON 파싱 성공 여부 = 핵심 지표
                if "sum" in obj:
                    res.json_ok += 1
            elif r.status_code == 400 and spec["json_mode"]:
                # response_format 미지원 가능 → 프롬프트-온리로 1회 재시도 판정
                res.notes.append("400 on response_format → json_object 미지원 추정")
                spec["json_mode"] = False
        except (json.JSONDecodeError, KeyError, httpx.HTTPError):
            pass  # 준수 실패로 카운트(json_ok 미증가)
        time.sleep(spec["min_interval"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10, help="json_only 스모크 문항 수")
    ap.add_argument("--only", default="", help="특정 slug만 (예: solar)")
    args = ap.parse_args()

    keys = load_keys()
    print(f"[D0 probe] 로드된 provider: {sorted(keys.keys())}  (키 파일: {KEYS_PATH})\n")

    results: list[Result] = []
    for slug, spec in TARGETS.items():
        if args.only and slug != args.only:
            continue
        res = Result(slug=slug)
        if slug not in keys:
            res.notes.append("keys.json에서 매칭 실패")
            results.append(res)
            continue
        health_check(slug, spec, keys[slug], res)
        json_smoke(slug, spec, keys[slug], res, args.n)
        results.append(res)

    print(f"{'모델':8} {'reach':6} {'HTTP':5} {'ms':>6} {'json_ok':>9}  비고")
    print("-" * 78)
    for r in results:
        rate = f"{r.json_ok}/{r.json_total}" if r.json_total else "-"
        print(
            f"{r.slug:8} {str(r.reachable):6} {str(r.health_status or '-'):5} "
            f"{str(r.health_ms or '-'):>6} {rate:>9}  {'; '.join(r.notes)[:120]}"
        )

    # R1 조기 판정 (핵심 풀 3종 기준)
    core = [r for r in results if r.slug in ("solar", "exaone", "ax")]
    healthy = [r for r in core if r.health_status == 200]
    json90 = [r for r in core if r.json_total and r.json_ok / r.json_total >= 0.9]
    print("\n[판정]")
    print(f"  핵심 3종 중 health 200: {len(healthy)}/3 ({[r.slug for r in healthy]})")
    print(f"  json_only 준수율 ≥90%:   {len(json90)}/3 ({[r.slug for r in json90]})")
    if not healthy:
        print("  ⚠️ ABORT 신호: 핵심 3종 전부 health 실패 → 오늘 저녁 일정 재협상 검토")
    elif not json90:
        print("  ⚠️ R1 경고: 핵심 3종 전부 json_only 준수율 <90% → abort 검토 대상")
    else:
        print("  ✅ 최소 1종 통과 — 진행 가능. 통과 모델로 풀 확정")


if __name__ == "__main__":
    main()
