"""rewrite/synthesize 프롬프트 변형 레지스트리 (baseline 측정 후 약점 기반으로 추가)."""

from __future__ import annotations

REWRITE_VARIANTS: dict[str, dict] = {
    "baseline": {"system": None, "note": "프로덕션 현행"},
    "strict-v1": {
        # baseline 실측: 지시어 잔존 8/40 (20%). 잔존 금지를 명시 + 자기검검.
        "system": None,
        "system_append": (
            " The rewritten question must contain NO deictic reference words "
            "('그거', '그것', '해당', '아까', '방금', '위에서', '그때', '거기') — "
            "replace every one of them with the concrete thing from the prior "
            "conversation. Re-check before answering: if any such word remains, "
            "rewrite again."
        ),
        "note": "지시어 잔존 금지 명시 — 결정론 잔존율이 1차 지표",
    },
}

SYN_VARIANTS: dict[str, dict] = {
    "baseline": {"system": None, "note": "프로덕션 현행"},
    "v2": {
        # baseline 실측: 인용마커 25/30(83%) — rule 3의 "source markers must
        # survive" 문구가 조장 (cite-v2 채택 방향과 충돌). action 누설 7/10(70%)
        # — rule 5가 중간에 묻힘. 두 지표는 독립 결정론 계측이라 한 변형에 담되
        # 지표별로 귀속한다.
        "system": (
            "You are a company wiki assistant synthesizing answers to a compound question. "
            "You are given multiple grounded sub-answers, one per sub-question. Combine them "
            "into a single coherent answer. Rules:\n"
            "1. Only use facts from the provided sub-answers — do NOT generate new facts.\n"
            "2. Address EVERY sub-question. Never silently drop one. If a sub-answer states the "
            "information is missing, say so explicitly for that sub-question rather than omitting it.\n"
            "3. Preserve concrete facts verbatim — dates, numbers, names, IDs, and status values "
            "in the sub-answers must survive into the combined answer; do not abstract them away. "
            "However, DROP citation markers and reference notes ('[1]', '문서 [2] 참조', "
            "'(참고: [1])', '출처: [3]') — they are interface artifacts, not facts; the combined "
            "answer must contain none of them.\n"
            "4. Re-arrange and excerpt for clarity; do not invent or merge two distinct facts into one.\n"
            "5. Answer in the question's language (Korean if the question is Korean).\n"
            "FINAL RULE — if the original question also requests an ACTION (sending an email, "
            "scheduling, creating a task, messaging someone, etc.), IGNORE that action entirely: "
            "do not mention it, do not promise it ('보내드리겠습니다' is forbidden), do not say "
            "you cannot do it, and do not address the recipient. The action is handled separately "
            "by the system. Output ONLY the factual answer to the knowledge sub-questions. "
            "Re-check before answering: your output must contain no promise or mention of any action."
        ),
        "note": "rule3 마커 drop 명시 + rule5 말미 승격/예시 — 마커율·action누설률 각각 귀속",
    },
}
