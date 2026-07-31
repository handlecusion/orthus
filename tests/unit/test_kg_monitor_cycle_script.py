"""K7.5 (W3) — kg_monitor_cycle.sh exit→sentinel 매핑 (Linux-runnable, node env 불필요).

스케줄 monitor cycle은 `set -e` 금지(monitor 비-0 exit를 직접 분류) + `|| echo` swallow 금지 +
exit code별 distinct sentinel 파일을 남긴다. WSL/CI에서 ORTHUS_KG_MONITOR_SELFTEST 훅으로
node env 없이 매핑만 검증한다. 실제 launchd install은 Mac mini operator 단계(plist 존재 =
활성화 acceptance artifact)다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "node" / "kg_monitor_cycle.sh"
_INSTALLER = _REPO / "scripts" / "node" / "install_kg_monitor_scheduler.sh"

_EXPECTED = {
    0: "state-ok.sentinel",
    1: "state-dead_events.sentinel",
    2: "state-backlog_stale.sentinel",
    3: "state-boundary_violation.sentinel",
    4: "state-could_not_verify.sentinel",
    5: "state-rebuild_in_progress.sentinel",
    7: "state-unknown_7.sentinel",
}


@pytest.mark.parametrize("code,sentinel", list(_EXPECTED.items()))
def test_k7_monitor_cycle_exit_to_sentinel(tmp_path, code, sentinel):
    """monitor exit code별 distinct sentinel + 스크립트가 그 코드를 그대로 전파한다."""
    proc = subprocess.run(
        ["bash", str(_SCRIPT), "selftest"],
        env={
            "ORTHUS_KG_MONITOR_SELFTEST": "1",
            "ORTHUS_KG_MONITOR_CMD": f"exit {code}",
            "ORTHUS_KG_MONITOR_SENTINEL_DIR": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
        capture_output=True,
        text=True,
    )
    assert proc.returncode == code  # 스크립트가 monitor exit code를 그대로 전파
    files = sorted(p.name for p in tmp_path.glob("state-*.sentinel"))
    # 현 상태 = 단일 sentinel(이전 state 파일은 지운다).
    assert files == [sentinel]
    assert f"exit={code}" in (tmp_path / sentinel).read_text()


def _code_lines(path: Path) -> list[str]:
    """주석(#로 시작)과 빈 줄을 제거한 실행 라인만 — 설명 주석의 리터럴이 가드를 오염시키지 않게."""
    out = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def test_k7_monitor_cycle_sets_scheduled_flag():
    """cycle 스크립트는 monitor 실행에 ORTHUS_KG_MONITOR_SCHEDULED=1을 붙인다 — 스케줄 경로만
    boundary-health audit를 기록해 수동 점검이 staleness를 가리지 못하게 한다(M-r3)."""
    code = _code_lines(_SCRIPT)
    assert any(
        "ORTHUS_KG_MONITOR_SCHEDULED=1" in line and "orthus.kg.monitor" in line for line in code
    )


def test_k7_monitor_cycle_no_set_e_present():
    """`set -e` 금지 가드 — common.sh가 켠 set -e를 다시 끄는 `set +e`가 있어야 한다
    (없으면 monitor 비-0 exit에서 sentinel 쓰기 전에 죽는다). 실행 라인만 검사."""
    code = _code_lines(_SCRIPT)
    assert any("set -uo pipefail" in line for line in code)
    assert any(line == "set +e" for line in code)  # common.sh set -e를 무력화
    # `|| echo` swallow 금지(exit code 손실) — 실행 라인엔 없어야 한다.
    assert not any("|| echo" in line for line in code)


def test_k7_monitor_scheduler_installer_uses_ai_orthus_label():
    """generator 패턴 — label prefix는 ai.orthus.* 계열(com.example.* standalone 아님),
    신규 launchd 디렉터리 컨벤션을 만들지 않고 kg_monitor_cycle.sh를 호출한다(실행 라인만)."""
    code = _code_lines(_INSTALLER)
    joined = "\n".join(code)
    assert "ai.orthus." in joined
    assert "kg-monitor" in joined
    assert "kg_monitor_cycle.sh" in joined
    # 신규 launchd 디렉터리/템플릿 컨벤션을 만들지 않는다(실행 라인에 그 경로 mkdir 없음).
    assert not any("scripts/node/launchd/" in line for line in code)
