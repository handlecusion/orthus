"""X-tier 외부 데이터셋 다운로더 (B4 / `analysis/b4-prereg.md` §3 X1).

받는 것 (전부 public·non-gated, 2026-07-21 확인):
  kudge_human   HAERAE-HUB/KUDGE  `Human Annotations` raw CSV  — 주석자 2인(score1/score2) → κ 천장
  khj           HAERAE-HUB/Korean-Human-Judgements 694 쌍대     — judge-human κ + 위치 스왑 flip rate
  mtbench_human lmsys/mt_bench_human_judgments  human split     — 영어 앵커(동일 프롬프트)
  summeval      mteb/summeval  consistency 1-5                  — factuality 축

하드 제약 (b4-prereg §6):
  - KUDGE·Korean-Human-Judgements는 **라이선스 미표기 → 재배포 금지**.
    저장소에는 이 스크립트 + `MANIFEST.sha256`만 커밋한다. 데이터 행은 커밋하지 않는다.
  - 캐시는 `external/.cache/`(gitignored)로만 떨어진다.

**스키마가 사전선언 가정과 다르면 조용히 적응하지 않고 크게 실패한다** (`SchemaMismatch`).
사전선언이 그 스키마 위에서 주판정을 고정했으므로, 스키마가 바뀌었다면 코드가 아니라
사전선언을 다시 봐야 한다. 대체 데이터셋으로 갈아끼우는 것도 금지다(§5 "사전선언이 잠근 것").

의존성 0 (stdlib만). parquet은 pyarrow가 있으면 직접 읽고, 없으면 HF datasets-server
rows API(JSON)로 같은 revision 내용을 받는다. 어느 경로였는지는 매니페스트에 기록한다.

실행:
    python experiments/fugu-ko/external/download.py            # 전체 받기 + 검증 + 매니페스트
    python experiments/fugu-ko/external/download.py --only khj
    python experiments/fugu-ko/external/download.py --verify   # 재다운로드 없이 해시 대조만
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
RAW = CACHE / "raw"
NORM = CACHE / "normalized"
MANIFEST = HERE / "MANIFEST.sha256"

HF_RESOLVE = "https://huggingface.co/datasets/{hf_id}/resolve/{rev}/{path}"
HF_ROWS = "https://datasets-server.huggingface.co/rows"
_ROWS_PAGE = 100
_TIMEOUT = 90.0
_RETRIES = 4


class SchemaMismatch(RuntimeError):
    """사전선언이 가정한 스키마와 실제 데이터가 다르다 — 적응하지 말고 멈춘다."""


@dataclass(frozen=True)
class DatasetSpec:
    key: str
    hf_id: str
    revision: str  # 고정 커밋 sha. 재현성의 앵커.
    files: tuple[str, ...]  # repo 내 raw 경로 (sha256 대상)
    kind: str  # "csv" | "parquet"
    config: str
    split: str
    prereg_subset: str
    license: str
    redistribution: str
    expected_columns: frozenset[str]
    expected_rows: int
    keep_columns: tuple[str, ...]
    label_field: str | None = None
    label_vocab: frozenset[str] = field(default_factory=frozenset)
    notes: str = ""


SPECS: dict[str, DatasetSpec] = {
    "kudge_human": DatasetSpec(
        key="kudge_human",
        hf_id="HAERAE-HUB/KUDGE",
        revision="7168dc844bb043fe0b0b0ee860da4dd20a67e5a2",
        files=("kudge-human-annotation-raw.csv",),
        kind="csv",
        config="Human Annotations",
        split="train",
        prereg_subset="X1 (human-human κ 천장)",
        license="unstated",
        redistribution="forbidden",
        expected_columns=frozenset(
            {
                "uuid",
                "instruction",
                "response",
                "model",
                "score1",
                "time1",
                "score2",
                "time2",
                "healthy?",
            }
        ),
        expected_rows=2880,
        keep_columns=("uuid", "instruction", "response", "model", "score1", "score2", "healthy?"),
        label_field="score1",
        label_vocab=frozenset({"1", "2", "3", "4", "5"}),
        notes="score2에 센티널 -1(미주석)이 123행 존재 — κ 계산 시 제외하고 제외율을 보고한다.",
    ),
    "khj": DatasetSpec(
        key="khj",
        hf_id="HAERAE-HUB/Korean-Human-Judgements",
        revision="24dae25b0f4c0d3c0d3a10f2997f51cf6eb6cd5f",
        files=("data/train-00000-of-00001.parquet",),
        kind="parquet",
        config="default",
        split="train",
        prereg_subset="X1 (judge-human κ + 위치 스왑 flip rate)",
        license="unstated",
        redistribution="forbidden",
        expected_columns=frozenset(
            {"instruction", "response_a", "response_b", "decision", "source"}
        ),
        expected_rows=694,
        keep_columns=("instruction", "response_a", "response_b", "decision", "source"),
        label_field="decision",
        label_vocab=frozenset({"A", "B", "Tie"}),
    ),
    "mtbench_human": DatasetSpec(
        key="mtbench_human",
        hf_id="lmsys/mt_bench_human_judgments",
        revision="f7d2896d2cc5d80f8b55c2bbc722613555233c25",
        files=("data/human-00000-of-00001-25f4910818759289.parquet",),
        kind="parquet",
        config="default",
        split="human",
        prereg_subset="X1 (영어 앵커 — 한국어 페널티 vs 프롬프트 노이즈 분리)",
        license="cc-by-4.0",
        redistribution="allowed-with-attribution",
        expected_columns=frozenset(
            {
                "question_id",
                "model_a",
                "model_b",
                "winner",
                "judge",
                "conversation_a",
                "conversation_b",
                "turn",
            }
        ),
        expected_rows=3355,
        keep_columns=(
            "question_id",
            "model_a",
            "model_b",
            "winner",
            "judge",
            "conversation_a",
            "conversation_b",
            "turn",
        ),
        label_field="winner",
        label_vocab=frozenset({"model_a", "model_b", "tie"}),
        notes="한 쌍에 여러 주석자 투표가 들어있다 — 소비 측에서 다수결로 접는다.",
    ),
    "summeval": DatasetSpec(
        key="summeval",
        hf_id="mteb/summeval",
        revision="bfc121155064afa2d81b5505682ffc0d96f4334c",
        files=("data/test-00000-of-00001-35901af5f6649399.parquet",),
        kind="parquet",
        config="default",
        split="test",
        prereg_subset="X1 (factuality 축 — consistency 1-5)",
        license="mit",
        redistribution="allowed",
        expected_columns=frozenset(
            {
                "machine_summaries",
                "human_summaries",
                "relevance",
                "coherence",
                "fluency",
                "consistency",
                "text",
                "id",
            }
        ),
        expected_rows=100,
        keep_columns=("id", "text", "machine_summaries", "consistency"),
        notes="문서당 machine_summaries 16개 + 같은 길이의 consistency(1-5) 전문가 평균.",
    ),
}


# --------------------------------------------------------------------------- http


def _get(url: str, *, params: dict[str, object] | None = None) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "orthus-b4-external/1.0"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 401):
                raise SchemaMismatch(
                    f"{url} → HTTP {exc.code}. 게이트가 걸렸거나 접근 권한이 필요하다. "
                    "다른 데이터셋으로 대체하지 말고 사전선언(b4-prereg §7)에 보고하라."
                ) from exc
            if exc.code == 404:
                raise SchemaMismatch(
                    f"{url} → HTTP 404. 고정 revision의 파일이 사라졌다. "
                    "revision 핀을 임의로 옮기지 말고 보고하라."
                ) from exc
            last = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
        if attempt < _RETRIES:
            # 429는 datasets-server 페이지네이션에서 실제로 난다 — 더 길게 물러난다.
            rate_limited = isinstance(last, urllib.error.HTTPError) and last.code == 429
            time.sleep((6.0 if rate_limited else 1.5) * (2**attempt))
    if isinstance(last, urllib.error.HTTPError) and last.code == 429:
        raise RuntimeError(
            f"{url} → HTTP 429 (rate limit). 데이터는 멀쩡하다. 잠시 후 다시 실행하거나 "
            "pyarrow를 설치해 parquet을 직접 읽어라(rows API 페이지네이션을 아예 건너뛴다)."
        ) from last
    raise RuntimeError(f"{url} 다운로드 실패: {last!r}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------------ loaders


def _rows_via_datasets_server(spec: DatasetSpec) -> tuple[list[dict], list[str]]:
    """pyarrow가 없을 때: HF datasets-server rows API로 같은 revision 내용을 JSON으로 받는다."""
    out: list[dict] = []
    columns: list[str] = []
    offset = 0
    while True:
        payload = json.loads(
            _get(
                HF_ROWS,
                params={
                    "dataset": spec.hf_id,
                    "config": spec.config,
                    "split": spec.split,
                    "offset": offset,
                    "length": _ROWS_PAGE,
                },
            )
        )
        if "error" in payload:
            raise SchemaMismatch(
                f"{spec.hf_id} rows API 오류: {payload['error']!r} "
                f"(config={spec.config!r} split={spec.split!r}). 사전선언이 잠근 config/split이다."
            )
        if not columns:
            columns = [f["name"] for f in payload.get("features", [])]
        batch = payload.get("rows", [])
        if not batch:
            break
        out.extend(r["row"] for r in batch)
        offset += len(batch)
        if len(batch) < _ROWS_PAGE:
            break
    return out, columns


def _rows_via_pyarrow(blob: bytes) -> tuple[list[dict], list[str]]:
    import pyarrow.parquet as pq  # noqa: PLC0415  (optional dep — 없으면 rows API로 폴백)

    table = pq.read_table(io.BytesIO(blob))
    return table.to_pylist(), list(table.column_names)


def _rows_via_csv(blob: bytes) -> tuple[list[dict], list[str]]:
    reader = csv.DictReader(io.StringIO(blob.decode("utf-8")))
    rows = [dict(r) for r in reader]
    return rows, list(reader.fieldnames or [])


def _load_rows(spec: DatasetSpec, blobs: dict[str, bytes]) -> tuple[list[dict], list[str], str]:
    """(rows, columns, 경로) — 경로는 매니페스트에 남길 출처 표시."""
    if spec.kind == "csv":
        rows, cols = _rows_via_csv(next(iter(blobs.values())))
        return rows, cols, "raw-csv"
    try:
        rows, cols = _rows_via_pyarrow(next(iter(blobs.values())))
        return rows, cols, "raw-parquet(pyarrow)"
    except ImportError:
        rows, cols = _rows_via_datasets_server(spec)
        return rows, cols, "datasets-server-rows-api"


# ----------------------------------------------------------------------- validate


def _validate(spec: DatasetSpec, rows: list[dict], columns: list[str]) -> None:
    got = frozenset(columns)
    if got != spec.expected_columns:
        raise SchemaMismatch(
            f"[{spec.key}] 컬럼 불일치. 사전선언 가정과 실제가 다르다.\n"
            f"  기대: {sorted(spec.expected_columns)}\n"
            f"  실제: {sorted(got)}\n"
            f"  누락: {sorted(spec.expected_columns - got)}  추가: {sorted(got - spec.expected_columns)}\n"
            "  → 코드를 맞추지 말고 b4-prereg를 다시 열어라. 스키마가 바뀌면 주판정도 바뀐다."
        )
    if len(rows) != spec.expected_rows:
        raise SchemaMismatch(
            f"[{spec.key}] 행 수 불일치: 기대 {spec.expected_rows}, 실제 {len(rows)}. "
            "revision이 고정돼 있는데 행 수가 다르면 받은 게 다른 것이다."
        )
    if spec.label_field:
        vocab = {str(r[spec.label_field]) for r in rows}
        unexpected = vocab - spec.label_vocab
        if spec.key == "kudge_human":
            unexpected -= {"-1"}  # 문서화된 미주석 센티널(score2). score1에는 없어야 한다.
        if unexpected:
            raise SchemaMismatch(
                f"[{spec.key}] 라벨 어휘 불일치 ({spec.label_field}): "
                f"예상 밖 값 {sorted(unexpected)} / 기대 {sorted(spec.label_vocab)}"
            )
    if spec.key == "summeval":
        for r in rows:
            if len(r["machine_summaries"]) != len(r["consistency"]):
                raise SchemaMismatch(
                    f"[summeval] id={r['id']}: machine_summaries "
                    f"{len(r['machine_summaries'])}개 vs consistency {len(r['consistency'])}개 — "
                    "요약과 점수가 1:1이 아니면 factuality 축을 잴 수 없다."
                )
            bad = [c for c in r["consistency"] if not 1.0 <= float(c) <= 5.0]
            if bad:
                raise SchemaMismatch(f"[summeval] consistency가 1-5 범위 밖: {bad[:5]}")


# -------------------------------------------------------------------------- write


def _normalize(spec: DatasetSpec, rows: list[dict]) -> Iterator[str]:
    for r in rows:
        yield json.dumps({k: r[k] for k in spec.keep_columns}, ensure_ascii=False)


def fetch(spec: DatasetSpec) -> dict[str, object]:
    RAW.mkdir(parents=True, exist_ok=True)
    NORM.mkdir(parents=True, exist_ok=True)
    blobs: dict[str, bytes] = {}
    entries: list[dict[str, str]] = []
    for path in spec.files:
        url = HF_RESOLVE.format(hf_id=spec.hf_id, rev=spec.revision, path=path)
        blob = _get(url)
        dest = RAW / spec.key / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        blobs[path] = blob
        entries.append(
            {"path": f"raw/{spec.key}/{path}", "sha256": _sha256(blob), "bytes": str(len(blob))}
        )
        print(f"    raw  {path}  {len(blob):,}B  {_sha256(blob)[:16]}…")

    rows, columns, source = _load_rows(spec, blobs)
    _validate(spec, rows, columns)

    norm_path = NORM / f"{spec.key}.jsonl"
    body = "\n".join(_normalize(spec, rows)) + "\n"
    norm_path.write_text(body, encoding="utf-8")
    entries.append(
        {
            "path": f"normalized/{spec.key}.jsonl",
            "sha256": _sha256(body.encode()),
            "bytes": str(len(body.encode())),
        }
    )
    print(f"    norm {norm_path.name}  rows={len(rows)}  via {source}")

    return {
        "key": spec.key,
        "hf_id": spec.hf_id,
        "revision": spec.revision,
        "config": spec.config,
        "split": spec.split,
        "rows": len(rows),
        "read_path": source,
        "license": spec.license,
        "redistribution": spec.redistribution,
        "prereg_subset": spec.prereg_subset,
        "files": entries,
    }


def write_manifest(results: list[dict[str, object]]) -> None:
    lines = [
        "# X-tier 외부 데이터셋 매니페스트 (B4 / analysis/b4-prereg.md §3 X1)",
        "# 데이터 행은 커밋하지 않는다 — 해시만 남긴다(§6 재배포 금지).",
        "# 경로는 experiments/fugu-ko/external/.cache/ 기준 상대경로.",
        "#",
    ]
    for res in results:
        lines += [
            f"# [{res['key']}] {res['hf_id']}@{res['revision']}",
            f"#   config={res['config']} split={res['split']} rows={res['rows']} "
            f"read={res['read_path']}",
            f"#   license={res['license']} redistribution={res['redistribution']}",
            f"#   prereg={res['prereg_subset']}",
        ]
        for f in res["files"]:  # type: ignore[index]
            lines.append(f"{f['sha256']}  {f['path']}")
        lines.append("#")
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n  매니페스트 → {MANIFEST.relative_to(HERE.parent.parent.parent)}")


def verify_manifest() -> int:
    if not MANIFEST.exists():
        print("MANIFEST.sha256 없음 — 먼저 다운로드하라.", file=sys.stderr)
        return 1
    bad = 0
    for line in MANIFEST.read_text("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        digest, path = line.split("  ", 1)
        target = CACHE / path
        if not target.exists():
            print(f"  MISSING {path}")
            bad += 1
            continue
        actual = _sha256(target.read_bytes())
        ok = actual == digest
        print(f"  {'OK     ' if ok else 'MISMATCH'} {path}")
        bad += 0 if ok else 1
    print("\n  검증 실패 0건" if not bad else f"\n  검증 실패 {bad}건")
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="X-tier 외부 데이터셋 다운로드 + 스키마 검증")
    ap.add_argument("--only", choices=sorted(SPECS), help="한 셋만")
    ap.add_argument("--verify", action="store_true", help="재다운로드 없이 매니페스트 해시만 대조")
    args = ap.parse_args()

    if args.verify:
        return verify_manifest()

    keys = [args.only] if args.only else list(SPECS)
    results: list[dict[str, object]] = []
    for key in keys:
        spec = SPECS[key]
        print(f"\n== {spec.key}  {spec.hf_id}@{spec.revision[:12]}  [{spec.license}] ==")
        results.append(fetch(spec))

    if args.only:
        print("\n  --only 실행이라 매니페스트는 갱신하지 않는다(부분 매니페스트 방지).")
    else:
        write_manifest(results)
    print("\n  캐시는 external/.cache/ (gitignored). 데이터 행을 커밋하지 마라.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
