"""KG 그래프 엔진 수동 테스트 — 타입드 템플릿 1개를 게이트로 실행해 결과를 출력한다.

K4 읽기 게이트(`orthus/kg/gate.py`)를 그대로 통과한다(플래그→레지스트리→파라미터
검증→slug 선확인→가용성→실행→매핑→로그). HTTP로 노출된 건 `neighbors`(=/graph)
뿐이라, conflicts_of / provenance_chain / path_between / entity_mentions 를
커맨드라인에서 눈으로 확인하려고 둔 데모용 러너다.

플래그가 켜진 프로세스에서 실행해야 한다:
    ORTHUS_KG_ENABLED=true ORTHUS_KG_OWNER_SCOPE_ENABLED=true ORTHUS_OWNER_SCOPE_ENABLED=true \\
        uv run python -m scripts.demo.kg_query neighbors v2-0-0-2026-04-20 2
    ... provenance_chain 아틀라스-정책사항-공개용-7ae32742
    ... conflicts_of <claim-slug>
    ... path_between <slug-a> <slug-b>
"""

from __future__ import annotations

import sys

from orthus.kg.gate import run_kg_template

USAGE = (
    "usage: python -m scripts.demo.kg_query <template> <arg1> [arg2]\n"
    "  neighbors <slug> [depth=1|2]\n"
    "  conflicts_of <claim-slug>\n"
    "  provenance_chain <claim-slug>\n"
    "  path_between <slug-a> <slug-b>\n"
    "  entity_mentions <name_norm>"
)


def _params(template: str, args: list[str]) -> dict:
    if template == "neighbors":
        depth = int(args[1]) if len(args) > 1 else 1
        return {"slug": args[0], "depth": depth}
    if template in ("conflicts_of", "provenance_chain"):
        return {"slug": args[0]}
    if template == "path_between":
        return {"slug_a": args[0], "slug_b": args[1], "max_hops": 4}
    if template == "entity_mentions":
        return {"name_norm": args[0]}
    raise SystemExit(f"unknown template: {template}\n{USAGE}")


def main() -> int:
    if len(sys.argv) < 3:
        print(USAGE, file=sys.stderr)
        return 2
    template, args = sys.argv[1], sys.argv[2:]
    result = run_kg_template(user_id=None, template_name=template, params=_params(template, args))

    print(f"template = {template}  params = {args}")
    print(f"status   = {result.status.value}", end="")
    if result.reject_reason:
        print(f"  reject_reason = {result.reject_reason}", end="")
    print(f"  duration_ms = {result.duration_ms}  truncated = {result.truncated}")
    print(f"nodes = {len(result.nodes)}  edges = {len(result.edges)}")

    if result.nodes:
        print("\n-- nodes --")
        for n in result.nodes[:20]:
            mark = "" if n.materialized else "  (placeholder)"
            print(f"  [{n.label}] {n.slug or n.id}{mark}")
    if result.edges:
        print("\n-- edges --")
        for e in result.edges[:30]:
            extra = f"  {e.properties}" if e.properties else ""
            print(f"  {e.src}  -[{e.rel}]->  {e.dst}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
