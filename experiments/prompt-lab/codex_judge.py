"""codex(ChatGPT 플랜 OAuth) 판정자 — 빌링 소진된 API 키와 별개 계정, 중립 GPT급.

`codex exec`를 판정자로 감싼다. orthus ChatModel Protocol의 `.complete(system, user,
json_only=)` 호환이라 score_distill/matrix_lab의 판정자 슬롯에 그대로 꽂힌다.

콜드스타트가 있어 호출당 ~8s. 판정 볼륨이 크므로 스레드풀로 느슨한 동시성(기본 3)을
쓰되, 호출 자체는 독립 subprocess라 안전하다. OAuth 레이트리밋 방어로 실패 시 재시도.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

_JSON_OBJ = re.compile(r"\{[^{}]*\}")


class CodexJudge:
    def __init__(self, concurrency: int = 3, timeout: int = 90):
        self.model_id = "codex-oauth"
        self._bin = shutil.which("codex") or "codex"
        self.timeout = timeout
        # 동시성은 호출 측(matrix)에서 스레드풀로 관리 — 여기선 단일 호출만 책임.

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        prompt = f"{system}\n\n{user}"
        if json_only:
            prompt += "\n\n(JSON 객체 하나만 출력하라. 다른 텍스트 금지.)"
        last_err = ""
        for attempt in range(3):
            try:
                p = subprocess.run(
                    [self._bin, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "-"],
                    input=prompt, capture_output=True, text=True, timeout=self.timeout,
                )
                out = p.stdout
                if json_only:
                    objs = _JSON_OBJ.findall(out)
                    if objs:
                        return objs[-1]  # 마지막 JSON 객체 (transcript noise 제거)
                    last_err = "no-json"
                    continue
                # 자유텍스트: codex/hook 마커·usage 꼬리 제거 후 본문 추정
                lines = [l for l in out.splitlines() if l and not l.startswith("hook:")
                         and l.strip() not in ("codex", "tokens used")]
                return "\n".join(lines).strip()
            except subprocess.TimeoutExpired:
                last_err = "timeout"
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}"
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"codex judge failed: {last_err}")
