"""B1 — 글루 사다리 축 (`analysis/b1-prereg.md` §2/§6 구현).

B1은 2요인 완전 교차(모델 6종 × 글루 5단)를 재는 실험이고, 이 파일은 **요인 B(글루)**만
담당한다. 사다리는 누적이며 각 단은 이전 단을 포함한다.

    L0 bare            단일 콜(t0), 후처리 없음      ← 현행 하네스 동작과 동일
    L1 +snap           카탈로그-멤버십 스냅
    L2 +repair         결정론 SQL 수리
    L3 +k=5 SC         자기일관성(t0 + 4×t0.7, 실행결과 null 제외 다수결)
    L4 +probe/폴백     probe-0 + null 폴백(배정워커 → 다른 국내 워커)

**신규 글루 로직을 쓰지 않는다(b1-prereg §6).** 각 단은 이미 동결·측정된 구현을 그대로
호출한다 — 새로 짜면 그 구현 자체가 교란 요인이 되기 때문이다:

| 사다리 단 | 재사용하는 기존 구현 | 위치 |
|---|---|---|
| L1 snap    | `snap_dbname()`            | `train/judge_d8.py:59` (D8 §3 동결 글루) |
| L2 repair  | `propose_repairs()`        | `train/repair.py:101` |
| L3 SC      | null 제외 다수결 + `TempChat` | `train/judge_d8.py::rplus_answer` + `train/sc_pilot.py::TempChat` |
| L4 probe-0 | `probe_zero()`             | `train/judge_d8.py:83` |
| L4 폴백    | 첫 non-null 채택 체인      | `train/judge_d8.py::rplus_answer` |
| 재실행     | `run_sql()`/`execute()`/`gate_ok()` | `train/judge_d8.py:77` / `train/sft_score.py` |

파이프라인 **순서**도 동결 명세(`analysis/d8-prereg.md` §3, `judge_d8.rplus_answer`)를
그대로 따른다: `SC → snap → repair → probe-0 → null 폴백`. 따라서 `level=4`의 t3 파이프라인은
D8 R+ 정의와 동일하다(1차 워커만 배정 모델로 바뀐다).

**적용 범위 = t3 뿐이다.** snap/repair/probe-0는 전부 SQL 텍스트·실행신호에 붙은 글루라
t7(decompose)·t10(delegation)에는 **기존 구현이 존재하지 않는다.** b1-prereg §6이 새 로직
작성을 금지하므로 t7/t10은 전 사다리 단에서 **무변경 통과**시키고, 그 사실을 결과에
명시적으로 기록한다(`applies=False`). 이는 누락이 아니라 사전선언대로의 보고다.

**DSN(§6 두 번째 항목).** Phase 5에서 `ORTHUS_PG_DSN`만 바꾸고 `ORTHUS_PG_DSN_READONLY`를
안 바꿔 t3 18문항이 빈 `orthus` DB에 대해 가짜 실패한 전례가 있다. `override_pg_database()`가
**두 env var를 같이** 다시 쓰고, `check_pg_dsn_pair()`가 두 DSN의 DB 이름이 다르면
fail-closed로 막는다.
"""

from __future__ import annotations

import copy
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
TRAIN = HERE / "train"

# 사다리 이름 (b1-prereg §2 요인 B) — 순서 고정, 결과를 보고 재배열하지 않는다.
LEVEL_NAMES: dict[int, str] = {
    0: "bare",
    1: "snap",
    2: "repair",
    3: "sc_k5",
    4: "probe_fallback",
}
MAX_LEVEL = max(LEVEL_NAMES)

# 사다리가 적용되는 작업 (기존 글루 구현이 있는 작업만).
LADDER_TASKS = ("t3",)
# 기존 글루 구현이 없는 작업 — 전 단계 무변경 통과 + 사유 기록.
NO_GLUE_REASON = "no_existing_glue_implementation_for_task (b1-prereg §6: 새 로직 금지)"

# 국내 워커 폴백 체인 기본값 (b1-prereg §2 L4 "배정워커 → 다른 국내 워커").
DEFAULT_FALLBACK_ORDER = ("solar", "ax", "exaone")

PG_DSN_ENV = "ORTHUS_PG_DSN"
PG_DSN_RO_ENV = "ORTHUS_PG_DSN_READONLY"


# --------------------------------------------------------------------------- #
# DSN — b1-prereg §6: 읽기/쓰기 DSN을 **둘 다** 같은 DB로 고정한다
# --------------------------------------------------------------------------- #
def _settings_default(field_name: str) -> str:
    """`orthus.settings.Settings`의 선언된 default (env 미설정 시의 실제 값)."""
    from orthus.settings import Settings

    return str(Settings.model_fields[field_name].default)


def _swap_database(dsn: str, database: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, "/" + database, parts.query, parts.fragment))


def dsn_database(dsn: str) -> str:
    try:
        return urlsplit(dsn).path.lstrip("/")
    except Exception:  # noqa: BLE001
        return ""


def mask_dsn(dsn: str) -> str:
    """자격증명을 지운 표시용 DSN."""
    try:
        parts = urlsplit(dsn)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        user = f"{parts.username}:***@" if parts.username else ""
        return f"{parts.scheme}://{user}{host}{port}{parts.path}"
    except Exception:  # noqa: BLE001
        return "<unparseable dsn>"


def override_pg_database(database: str) -> dict[str, str]:
    """`ORTHUS_PG_DSN`과 `ORTHUS_PG_DSN_READONLY`의 **DB 이름을 동시에** 교체한다.

    RO DSN을 잊는 것이 Phase 5의 실제 사고였으므로 한쪽만 바꾸는 API를 제공하지 않는다.
    `orthus.settings`가 처음 읽히기 전에 호출해야 하며(하네스는 `main()` 첫머리에서 호출),
    캐시된 settings가 있으면 무효화한다.
    """
    out: dict[str, str] = {}
    for env, field_name in ((PG_DSN_ENV, "pg_dsn"), (PG_DSN_RO_ENV, "pg_dsn_readonly")):
        base = os.environ.get(env) or _settings_default(field_name)
        new = _swap_database(base, database)
        os.environ[env] = new
        out[env] = new
    try:  # 이미 캐시됐다면 새 DSN이 반영되도록 비운다
        from orthus.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return out


def check_pg_dsn_pair(*, strict: bool) -> dict[str, Any]:
    """두 DSN이 같은 DB를 가리키는지 검사한다(fail-closed).

    `strict=True`면 불일치 시 `SystemExit(2)`. B1 실행 경로에서만 strict로 부른다 —
    `--glue-level` 없이 도는 기존 실행의 동작은 건드리지 않는다.
    """
    from orthus.settings import get_settings

    s = get_settings()
    rw, ro = s.pg_dsn, s.pg_dsn_readonly
    info = {
        "pg_dsn": mask_dsn(rw),
        "pg_dsn_readonly": mask_dsn(ro),
        "database": dsn_database(rw),
        "database_readonly": dsn_database(ro),
        "match": dsn_database(rw) == dsn_database(ro),
    }
    if strict and not info["match"]:
        print(
            "REFUSED: ORTHUS_PG_DSN and ORTHUS_PG_DSN_READONLY point at DIFFERENT "
            f"databases ({info['database']!r} vs {info['database_readonly']!r}).\n"
            "b1-prereg §6: both must be overridden together (Phase 5 scored 18 t3 "
            "items against an empty DB because only the RW DSN was swapped).\n"
            "Fix: pass --pg-database <name> (rewrites both) or export both env vars."
        )
        raise SystemExit(2)
    return info


# --------------------------------------------------------------------------- #
# 기존 글루 구현 로딩 (신규 로직 없음 — DSN만 다시 가리킨다)
# --------------------------------------------------------------------------- #
class _D8Glue:
    """`train/judge_d8.py`의 동결 글루를 그대로 노출하는 얇은 핸들.

    `judge_d8`/`sft_score`/`build_sft_data`는 D7·D8 격리 스냅샷 DB
    (`orthus_company_0706`)를 모듈 상수로 박아 두고 있고, `judge_d8`은 import 시점에
    `CATALOG = set(load_db_keys())`로 **즉시 접속**한다. 함수 본문은 손대지 않고
    엔진 바인딩만 B1 실행 DSN으로 교체해 같은 코드를 다른 DB에 태운다.
    """

    def __init__(self, dsn: str):
        import sqlalchemy as sa

        if str(TRAIN) not in sys.path:
            sys.path.append(str(TRAIN))  # append: fugu-ko 최상위 모듈명을 가리지 않게

        import build_sft_data
        import sft_score

        engine = sa.create_engine(dsn)
        build_sft_data._engine = engine  # load_db_keys()가 쓰는 엔진
        sft_score._engine = engine  # execute()가 쓰는 엔진
        sft_score.DSN = dsn

        import judge_d8  # 여기서 CATALOG가 위 엔진으로 만들어진다

        judge_d8._engine = engine  # probe_zero()가 from-import한 별도 바인딩
        self.dsn = dsn
        self.jd = judge_d8
        self.catalog_size = len(judge_d8.CATALOG)

    # 아래는 전부 기존 함수로의 위임(로직 0줄).
    def snap(self, sql: str) -> str:
        return self.jd.snap_dbname(sql)

    def repairs(self, q: str, sql: str) -> list[tuple[str, str]]:
        from repair import propose_repairs

        return propose_repairs(q, sql)

    def run_sql(self, sql: str | None) -> tuple[str, set[int]]:
        return self.jd.run_sql(sql)

    def probe_zero(self, sql: str | None) -> bool:
        return self.jd.probe_zero(sql)

    def is_null(self, nums, status: str = "executed") -> bool:
        return self.jd.is_null(nums, status)


def temperature_variant(chat: Any, temperature: float) -> Any | None:
    """같은 모델의 temperature 변형(자기일관성 샘플용). 없으면 None.

    - `pool.WorkerChat`은 `complete()`가 `temperature=0` 하드코딩이라 기존
      `train/sc_pilot.py::TempChat` 래퍼를 그대로 쓴다(SC 파일럿이 쓴 바로 그 코드).
    - 프로덕션 어댑터(`OpenAIChat`/`BedrockConverseChat`)는 `self._temperature`를
      호출 시점에 읽으므로 얕은 복사 + 필드 교체로 같은 어댑터를 재사용한다
      (새 어댑터를 만들지 않는다).
    """
    target = getattr(chat, "_inner", None) or chat
    if type(target).__name__ == "WorkerChat":
        if str(TRAIN) not in sys.path:
            sys.path.append(str(TRAIN))
        from sc_pilot import TempChat

        return TempChat(target, temperature)
    if hasattr(target, "_temperature"):
        variant = copy.copy(target)
        variant._temperature = temperature
        return variant
    return None


# --------------------------------------------------------------------------- #
# 사다리
# --------------------------------------------------------------------------- #
@dataclass
class GlueLadder:
    """t3 글루 사다리. `level`까지의 단을 **누적** 적용한다."""

    level: int
    dsn: str = ""
    k: int = 5
    temperature: float = 0.7
    scope: str = "company"
    fallback_order: tuple[str, ...] = DEFAULT_FALLBACK_ORDER
    fallback_chat: Callable[[str], Any] | None = None
    _glue: _D8Glue | None = field(default=None, init=False, repr=False)
    n_fallback_used: int = field(default=0, init=False)
    notes: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.level not in LEVEL_NAMES:
            raise ValueError(f"glue level must be one of {sorted(LEVEL_NAMES)} (got {self.level})")

    @property
    def name(self) -> str:
        return LEVEL_NAMES[self.level]

    @property
    def glue(self) -> _D8Glue:
        """L1 이상에서만 필요한 DB 의존 글루를 지연 로딩한다."""
        if self._glue is None:
            self._glue = _D8Glue(self.dsn or _sqlalchemy_dsn())
        return self._glue

    def applies(self, task: str) -> bool:
        return task in LADDER_TASKS

    # -- 샘플링 ------------------------------------------------------------- #
    def _sample(self, uid, q: str, chat: Any) -> dict:
        """프로덕션 `query_structured` 1회 → 사다리가 쓰는 정규화 샘플."""
        from orthus.structured.query import query_structured
        from t3_gold import model_numbers

        try:
            r = query_structured(uid, q, scope=self.scope, chat_model=chat)
        except Exception as e:  # noqa: BLE001 — 샘플 1개 실패는 null 샘플로 취급
            return {
                "status": f"err:{type(e).__name__}",
                "sql": None,
                "nums": [],
                "rows": [],
                "gate_pass": False,
            }
        return {
            "status": r.status,
            "sql": r.compiled.sql if r.compiled else None,
            "nums": sorted(model_numbers(r.rows)) if r.status == "executed" else [],
            "rows": r.rows,
            "gate_pass": bool(r.validation.passed),
        }

    # -- 파이프라인 --------------------------------------------------------- #
    def _pipeline(
        self, q: str, samples: list[dict], level: int, fb: Callable[[], list[dict]]
    ) -> dict:
        """`level`까지의 단을 동결 순서(SC→snap→repair→probe0→폴백)로 적용한다.

        같은 `samples`(이미 뽑은 LLM 출력)를 재사용하므로 하위 단 스냅샷을 만드는 데
        추가 LLM 콜이 들지 않는다 — 단계별 구제/파손 카운트(b1-prereg §4.3)를 위한 것.
        """
        g = self.glue if level >= 1 else None
        ev: dict[str, Any] = {}
        base = samples[0]

        # L3: 자기일관성 (실행결과 null 제외 다수결) — judge_d8.rplus_answer와 동일
        if level >= 3 and len(samples) > 1:
            valid = [s for s in samples if not self.glue.is_null(s["nums"], s["status"])]
            pool = valid or samples
            top = Counter(tuple(s["nums"]) for s in pool).most_common(1)[0][0]
            pick = next(s for s in pool if tuple(s["nums"]) == top)
            ev["sc_valid"] = len(valid)
            if pick is not base:
                ev["sc"] = True
        else:
            pick = base

        sql, nums, status = pick["sql"], list(pick["nums"]), pick["status"]
        rows, gate_pass = pick["rows"], pick["gate_pass"]
        # `rerun` = a stage re-executed SQL (so the numeric set becomes the SoR for
        # `rows`); `fired` = this level changed the answer at all (SC re-pick included).
        rerun = False

        # L1: 카탈로그-멤버십 스냅
        if level >= 1 and sql:
            snapped = g.snap(sql)
            if snapped != sql:
                st, n2 = g.run_sql(snapped)
                if st == "executed":
                    sql, nums, status, gate_pass, rerun = snapped, sorted(n2), st, True, True
                    ev["snap"] = True

        # L2: 결정론 SQL 수리
        if level >= 2 and sql:
            reps = g.repairs(q, sql)
            if reps:
                st, n2 = g.run_sql(reps[-1][1])
                if st == "executed":
                    sql, nums, status, gate_pass, rerun = reps[-1][1], sorted(n2), st, True, True
                    ev["repair"] = reps[-1][0]

        # L4: probe-0 → null 폴백
        if level >= 4 and g.is_null(nums, status):
            if g.probe_zero(sql):
                nums, status, rerun = [0], "executed", True
                ev["probe0"] = True
            else:
                for cand in fb():
                    if not g.is_null(cand["nums"], cand["status"]):
                        sql, nums, status = cand["sql"], list(cand["nums"]), cand["status"]
                        rows, gate_pass, rerun = cand["rows"], cand["gate_pass"], True
                        ev["fallback"] = cand["slug"]
                        break

        if rerun:
            rows = [[n] for n in nums]  # 재실행 결과는 숫자셋이 SoR
        return {
            "output": {"gate_pass": bool(gate_pass), "status": status, "rows": rows},
            "evidence": ev,
            "fired": bool(rerun or ev.get("sc")),
            "sql": sql,
        }

    # -- 진입점 ------------------------------------------------------------- #
    def run_t3(self, uid, q: str, chat: Any, *, scope: str | None = None) -> dict:
        """t3 1문항. 반환은 하네스 `dispatch_l1`의 t3 반환 계약 + `glue` 블록."""
        if scope:
            self.scope = scope
        samples = [self._sample(uid, q, chat)]
        n_calls = 1
        sc_note = None
        if self.level >= 3:
            variant = temperature_variant(chat, self.temperature)
            if variant is None:
                sc_note = "sc_unsupported_adapter (no temperature knob) — k=1 only"
                if sc_note not in self.notes:
                    self.notes.append(sc_note)
            else:
                for _ in range(max(0, self.k - 1)):
                    samples.append(self._sample(uid, q, variant))
                    n_calls += 1

        fb_cache: list[dict] | None = None

        def fb() -> list[dict]:
            nonlocal fb_cache, n_calls
            if fb_cache is not None:
                return fb_cache
            fb_cache = []
            if self.fallback_chat is None:
                return fb_cache
            own = getattr(chat, "model_id", "") or ""
            for slug in self.fallback_order:
                if slug == own or own.startswith(f"{slug}:"):
                    continue
                other = self.fallback_chat(slug)
                if other is None:
                    continue
                s = self._sample(uid, q, other)
                n_calls += 1
                s["slug"] = slug
                fb_cache.append(s)
            return fb_cache

        stages: list[dict] = []
        for lv in range(self.level + 1):
            st = self._pipeline(q, samples, lv, fb)
            stages.append(
                {
                    "level": lv,
                    "name": LEVEL_NAMES[lv],
                    "output": st["output"],
                    "evidence": st["evidence"],
                    "fired": st["fired"],
                }
            )
        final = stages[-1]
        if final["evidence"].get("fallback"):
            self.n_fallback_used += 1
        return {
            "output": final["output"],
            "reached_llm": True,
            "glue": {
                "level": self.level,
                "level_name": self.name,
                "applies": True,
                "stages": stages,
                "n_llm_calls": n_calls,
                "n_samples": len(samples),
                "fallback_used": final["evidence"].get("fallback"),
                "note": sc_note,
            },
        }

    def passthrough(self, task: str) -> dict:
        """사다리 구현이 없는 작업의 기록(무변경 통과)."""
        return {
            "level": self.level,
            "level_name": self.name,
            "applies": False,
            "reason": NO_GLUE_REASON,
            "task": task,
        }


def _sqlalchemy_dsn() -> str:
    from orthus.settings import get_settings

    return get_settings().pg_dsn


# --------------------------------------------------------------------------- #
# 단계별 구제/파손 (b1-prereg §4.3 — 순증만 보고하지 않는다)
# --------------------------------------------------------------------------- #
def annotate_stage_scores(
    item, result: dict, score_fn: Callable[[str, Any, Any], tuple[bool, str]]
) -> None:
    """각 사다리 단의 스냅샷을 같은 채점 함수로 매기고 구제/파손을 기록한다.

    `result["glue"]["stages"][i]`에 `correct`를, 단 전이에는 `rescued`/`broke`를 넣는다.
    """
    glue = result.get("glue")
    if not glue or not glue.get("applies"):
        return
    expected = item.expected.get("value")
    prev: bool | None = None
    for stage in glue["stages"]:
        ok, _detail = score_fn(item.task, stage["output"], expected)
        stage["correct"] = bool(ok)
        stage["rescued"] = bool(prev is False and ok)
        stage["broke"] = bool(prev is True and not ok)
        prev = bool(ok)


def aggregate_stages(rows: list[dict]) -> dict | None:
    """모델 1종의 단계별 정확도 + 구제/파손 카운트 집계."""
    staged = [r for r in rows if (r["result"].get("glue") or {}).get("applies")]
    if not staged:
        return None
    per_level: dict[int, dict[str, int]] = {}
    n_calls = 0
    n_fallback = 0
    for r in staged:
        glue = r["result"]["glue"]
        n_calls += int(glue.get("n_llm_calls") or 0)
        n_fallback += 1 if glue.get("fallback_used") else 0
        for stage in glue["stages"]:
            d = per_level.setdefault(
                stage["level"], {"n": 0, "correct": 0, "rescued": 0, "broke": 0, "fired": 0}
            )
            d["n"] += 1
            d["correct"] += 1 if stage.get("correct") else 0
            d["rescued"] += 1 if stage.get("rescued") else 0
            d["broke"] += 1 if stage.get("broke") else 0
            d["fired"] += 1 if stage.get("fired") else 0
    for lv, d in per_level.items():
        d["accuracy"] = (d["correct"] / d["n"]) if d["n"] else None
        d["name"] = LEVEL_NAMES[lv]
    return {
        "n_items": len(staged),
        "per_level": {str(lv): per_level[lv] for lv in sorted(per_level)},
        "n_llm_calls": n_calls,
        "calls_per_item": n_calls / len(staged) if staged else None,
        "n_fallback_items": n_fallback,
    }
