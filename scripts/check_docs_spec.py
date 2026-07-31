"""문서 무결성 체크 (대회 빌드).

유지하기로 한 핵심 설계/실험 문서가 존재하는지, 그리고 공개 빌드에서 제거한
내부 식별자가 문서에 다시 들어오지 않았는지 확인한다. CI의 backend lane에서
실행된다 (`make docs-check`).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    "README.md",
    "AGENTS.md",
    "docs/architecture-v2.md",
    "docs/data-model.md",
    "docs/operations.md",
    "docs/llm-wiki.md",
    "docs/kg-model.md",
    "docs/kg-implementation-spec.md",
    "docs/inline-agentic-ask.md",
    "docs/company-agent-orchestration.md",
    "docs/model-orchestration.md",
    "experiments/fugu-ko/RESULTS.md",
]

# 공개 빌드에서 제거된 내부 식별자 — 문서/설정 예시에 다시 들어오면 실패한다.
# 공개 빌드 가드 — 특정 식별자를 나열하지 않는 일반형 패턴만 쓴다(구체 식별자를
# 여기 적으면 그 목록 자체가 유출이다). 실기기 힌트가 문서에 다시 들어오면 실패.
BANNED_PATTERNS = [
    re.compile(r"010-\d{4}-\d{4}"),  # 휴대전화 번호 형태 (문서에는 픽스처도 금지)
    re.compile(r"(?<!example\.)ts\.net"),  # tailscale 실호스트 (example.ts.net 제외)
    re.compile(r"100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.(?!0\.1\b)\d+\.\d+"),  # CGNAT 실IP (100.64.0.1 예시 제외)
    re.compile(r"/Users/[a-z]+/|/home/[a-z]+/"),  # 개인 로컬 절대경로
]

SCAN_GLOBS = ["docs/**/*.md", "README.md", "AGENTS.md", "CLAUDE.md", ".env.example"]


def main() -> int:
    errors: list[str] = []
    for rel in REQUIRED_FILES:
        if not (ROOT / rel).is_file():
            errors.append(f"missing required doc: {rel}")

    for pattern in SCAN_GLOBS:
        for path in ROOT.glob(pattern):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for banned in BANNED_PATTERNS:
                if banned.search(text):
                    errors.append(
                        f"banned identifier {banned.pattern!r} in {path.relative_to(ROOT)}"
                    )

    if errors:
        print("docs-check FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("docs-check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
