"""`python -m orthus.kg.entity_normalize [--apply]` — E-N1b entity 정규화 backfill.

E-N1a가 `_clean_name`에 Slack 표기 정준화 + 불투명 person ID 드롭을 넣어 **앞으로
distill되는** entity는 수렴한다. 하지만 이미 적재된 `kg_entities` row는 옛 정규화로
쓰였다 — `<@U0ATKMHKZBL>`와 bare `U0ATKMHKZBL`가 별개 엔티티로 갈라져 있고, 불투명
Slack ID가 person 엔티티로 남아 있다(docs/kg-entity-normalization-plan.md §1).

이 명령은 **재distill 없이(LLM 0회)** 기존 row의 `display_name`을 E-N1a 규칙으로
재정규화해 수렴시킨다(§4.3 N3, D4 결정):

  1. 각 company `kg_entities` row → `new_display = _clean_name(display_name)`,
     `new_norm = new_display.casefold()`, `new_key = f"{kind}:{new_norm}"` 재계산.
  2. 드롭 판정: `_is_opaque_person_id`(E-N1a, D-a) 또는 재정규화 후 빈 이름 → 드롭 집합.
  3. 드롭 집합 엔티티 + mention을 **먼저** 삭제(드롭 대상이 쥔 key가 canonical의 new_key와
     겹칠 수 있으므로 canonical update 前에 해제 — 순서 보장).
  4. 잔여를 `new_key`로 그룹핑. 그룹당 canonical 1개(first_seen 최소, 동률은
     entity_id 최소 — 결정론). 나머지 non-canonical의 mention을 canonical로 re-point 후
     삭제(같은 page mention 중복은 UNIQUE(entity_id,page_id) 위반 → 중복 mention 삭제).
     canonical의 `last_seen`은 그룹 관측 max로 union한다.
  5. canonical의 `display_name`/`name_norm`/`entity_key`를 new 값으로 update(변경 시 only).
     entity_key 변경은 그룹 간 key swap에도 UNIQUE 충돌이 없도록 임시 키 park → 최종 값의
     two-phase로 반영한다.
  6. Neo4j 재투영은 별도 단계(E-N1c) — `make node-kg-rebuild NODE=company`
     (full-rebuild-only 계약, §3). 이 명령은 PG SoR만 수렴시킨다.

경계/안전(§7 게이트):
- **company-scope only** + `require_company_node`. `scope != 'company'` row(K7.1
  owner-scope 확장분)는 손대지 않는다(owner-scope 미투영 불변).
- **`ORTHUS_KG_ENABLED` 가드**(fail-closed). entity SoR은 KG on일 때만 채워지므로
  off면 정규화 대상이 없다 — noop로 보고한다.
- **결정론·멱등:** `new_key` **정확 일치**일 때만 병합(서로 다른 사람 병합 불가).
  두 번째 실행은 no-op(이미 정준 키). LLM 미개입.
- **`--dry-run` 기본:** `--apply`만 실제 쓰기. 실행 전/후 카운트 리포트.
- redaction 불변: `_clean_name`이 `redact_pii_text`를 그대로 거친다(정준화는 그 앞).

entity_conflict WikiTask 재평가(§4.3 step 6, §9 오픈 퀘스천 2): backfill이 name_norm을
바꾸거나 엔티티를 드롭하면, 그 name_norm에 걸려 있던 `entity_conflict` WikiTask가 이제
실재하지 않는 모순을 가리킬 수 있다. 이 명령은 **stale conflict task를 탐지해 리포트**
한다(`stale_conflict_tasks`) — 하지만 **자동 resolve하지 않는다.** entity_conflict는
"자동 resolve 금지, 운영자 리뷰 필요"가 기존 invariant이기 때문이다
(`api/routes/wiki.py` `_WIKI_TASK_CLEANUP_KINDS`에서 제외). 새 conflict **emit**도 하지
않는다 — 충돌 태스크는 evidence 페이지 컨텍스트가 필요해 distill/persist가 소유한다.
따라서 "최소 안전(깨진 참조 방지)"은 stale 탐지+리포트로 충족하고, 실제 resolve/재생성은
운영자 수동 정리(`_CLEANUP_KINDS`) 또는 다음 distill 사이클에 맡긴다.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text, update

from orthus.audit.logger import audit
from orthus.db import session
from orthus.kg.client import kg_enabled, require_company_node
from orthus.kg.entities import _clean_name, _conflict_task_slug, _is_opaque_person_id
from orthus.kg.schema import KG_SCHEMA_VERSION
from orthus.tables import kg_entities, kg_entity_mentions
from orthus.wiki import store as wiki_store

# apply 시 backfill 전체를 직렬화하는 트랜잭션-스코프 advisory lock 키(임의 상수).
# 동시 backfill 두 개가 같은 read-modify-write를 겹쳐 UNIQUE 충돌/롤백내는 것을 막는다.
# (라이브 distill/persist 쓰기는 이 lock을 잡지 않으므로 완전 배제는 못 한다 — apply는
# quiescence(메일-sync/distill 정지) 상태에서 실행해야 한다. run_entity_normalize/CLI 경고.)
_ADVISORY_LOCK_KEY = 0x6B67_454E_5F31  # "kgEN_1"

# entity_key가 바뀌는 canonical을 최종 키로 옮기기 전에 잠깐 park하는 임시 키 prefix.
# 그룹 간 key swap(A→B의 옛 키, B→A의 옛 키)이 있어도 non-deferrable UNIQUE(entity_key)
# 충돌 없이 재배치하기 위한 two-phase 업데이트용. real key는 `{kind}:{name_norm}` 꼴이라
# 이 prefix와 절대 겹치지 않고, entity_id를 붙여 park 키끼리도 유니크하다.
_TMP_KEY_PREFIX = "__kg_normalize_tmp__:"


class EntityNormalizeReport(BaseModel):
    dry_run: bool = True
    noop_kg_disabled: bool = False  # ORTHUS_KG_ENABLED=false → 정규화 대상 없음
    considered: int = 0  # 스캔한 company kg_entities row 수
    dropped_opaque: int = 0  # 불투명 person ID/빈 이름으로 드롭된 엔티티 수
    merged: int = 0  # non-canonical로 canonical에 흡수돼 삭제된 엔티티 수
    rerouted_mentions: int = 0  # canonical로 entity_id가 옮겨간 mention 수
    deduped_mentions: int = 0  # canonical이 이미 가진 page라 삭제된 중복 mention 수
    updated_canonical: int = 0  # display/norm/key가 실제로 바뀐 canonical 수
    unchanged: int = 0  # display/norm/key가 안 바뀐 canonical 수(병합 흡수 여부 무관)
    # backfill이 name_norm을 바꾸거나 드롭해 모순이 사라졌는데 아직 남아 있는
    # entity_conflict WikiTask slug들(§9.2). 자동 resolve하지 않고 운영자 리뷰용으로 리포트.
    stale_conflict_tasks: list[str] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.dropped_opaque or self.merged or self.updated_canonical)


def run_entity_normalize(
    *, apply: bool = False, now: datetime | None = None, root: Path | None = None
) -> EntityNormalizeReport:
    """company `kg_entities`를 E-N1a 규칙으로 재정규화·병합·드롭한다(멱등).

    `apply=False`(기본)는 카운트만 계산하고 아무것도 쓰지 않는다. `apply=True`만
    실제 update/delete를 수행하고 단일 트랜잭션으로 commit한다.

    **동시성:** apply는 트랜잭션-스코프 advisory lock으로 동시 backfill 실행을
    직렬화하지만, 라이브 distill/`persist_entities` 쓰기(메일-sync launchd 등)는 이
    lock을 잡지 않는다 — apply는 그 쓰기가 정지된 quiescence 상태에서 실행해야
    한다(동시 쓰기가 UNIQUE 충돌을 유발하면 단일 트랜잭션이 통째로 롤백된다).
    """
    require_company_node()
    report = EntityNormalizeReport(dry_run=not apply)
    if not kg_enabled():
        report.noop_kg_disabled = True
        return report

    at = now or datetime.now(UTC)

    with audit("kg.entities.normalize") as span:
        with session() as s:
            if apply:
                # 동시 backfill 직렬화(read-modify-write 겹침 방지). 트랜잭션 종료 시 해제.
                s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
            rows = s.execute(
                select(
                    kg_entities.c.entity_id,
                    kg_entities.c.entity_key,
                    kg_entities.c.entity_kind,
                    kg_entities.c.name_norm,
                    kg_entities.c.display_name,
                    kg_entities.c.first_seen,
                    kg_entities.c.last_seen,
                ).where(kg_entities.c.scope == "company")
            ).all()
            # NOTE(forward hazard): entity_key는 전역 UNIQUE인데 scope 성분이 없다
            # (`{kind}:{name_norm}`). 현재 엔티티는 company-only(persist가 scope=company,
            # owner_id=NULL 하드코딩)라 아래 canonical key update가 워킹셋 밖 row와 충돌할 수
            # 없다. owner-scope 엔티티가 실제 적재되면(K7 이후) entity_key를 scope/owner로
            # 네임스페이싱하거나 cross-scope 충돌을 여기서 처리해야 한다.
            report.considered = len(rows)

            # 1) 각 row 재정규화 + 드롭 판정. 잔여를 new_key로 그룹핑.
            drop_ids: list = []
            groups: dict[str, list] = defaultdict(list)  # new_key -> [(row, new_display, new_norm)]
            # conflict 재평가(§9.2)용: kind-분포가 바뀔 수 있는 old name_norm 집합
            # (드롭됐거나 다른 name_norm으로 이동한 엔티티의 옛 name_norm).
            affected_norms: set[str] = set()
            for row in rows:
                new_display = _clean_name(row.display_name)
                if not new_display or _is_opaque_person_id(row.entity_kind, new_display):
                    drop_ids.append(row.entity_id)
                    report.dropped_opaque += 1
                    affected_norms.add(row.name_norm)  # 드롭 → 이 norm의 kind 분포 변화
                    continue
                new_norm = new_display.casefold()
                if new_norm != row.name_norm:
                    affected_norms.add(row.name_norm)  # 이동 → 옛 norm 분포 변화
                new_key = f"{row.entity_kind}:{new_norm}"
                groups[new_key].append((row, new_display, new_norm))

            # 2) 드롭 먼저 실행 — 드롭 대상이 쥔 entity_key가 canonical의 new_key와
            #    같을 수 있으므로(§4 리뷰), canonical update 前에 해제해 UNIQUE 충돌을 막는다.
            if apply and drop_ids:
                s.execute(
                    delete(kg_entity_mentions).where(kg_entity_mentions.c.entity_id.in_(drop_ids))
                )
                s.execute(delete(kg_entities).where(kg_entities.c.entity_id.in_(drop_ids)))

            # 3) 그룹별 canonical 선정 + non-canonical 흡수(mention re-point) + canonical
            #    update 계획 수집. entity_key를 바꾸는 update는 두 그룹이 key를 swap해도
            #    UNIQUE 위반이 없도록 아래 4)에서 two-phase로 반영한다.
            canon_updates: list = []  # (entity_id, values_dict, key_changing)
            for new_key, members in groups.items():
                # 결정론 canonical: first_seen 최소 → 동률 entity_id 최소.
                members.sort(key=lambda m: (m[0].first_seen, str(m[0].entity_id)))
                canonical, canon_display, canon_norm = members[0]

                # canonical이 이미 언급하는 page 집합(in-memory 추적 — dry-run/apply
                # 카운트 동일 보장). 멤버를 흡수할 때마다 갱신해 이후 멤버의 중복 판정에
                # 반영한다(3개+ 병합에서 같은 page 공유 시 정확).
                canon_pages = set(
                    s.execute(
                        select(kg_entity_mentions.c.page_id).where(
                            kg_entity_mentions.c.entity_id == canonical.entity_id
                        )
                    ).scalars()
                )
                for row, _d, _n in members[1:]:
                    # non-canonical mention을 canonical로 re-point. 같은 page를
                    # canonical이 이미 언급하면 UNIQUE(entity_id,page_id) 위반 →
                    # 중복 mention 삭제, 아니면 entity_id update.
                    row_pages = list(
                        s.execute(
                            select(kg_entity_mentions.c.page_id).where(
                                kg_entity_mentions.c.entity_id == row.entity_id
                            )
                        ).scalars()
                    )
                    dup_page_ids = {p for p in row_pages if p in canon_pages}
                    moved_page_ids = [p for p in row_pages if p not in canon_pages]
                    report.deduped_mentions += len(dup_page_ids)
                    report.rerouted_mentions += len(moved_page_ids)
                    canon_pages.update(moved_page_ids)

                    if apply:
                        # 중복 mention 삭제(canonical이 이미 가진 page).
                        if dup_page_ids:
                            s.execute(
                                delete(kg_entity_mentions).where(
                                    kg_entity_mentions.c.entity_id == row.entity_id,
                                    kg_entity_mentions.c.page_id.in_(dup_page_ids),
                                )
                            )
                        # 나머지 mention을 canonical로 re-point.
                        s.execute(
                            update(kg_entity_mentions)
                            .where(kg_entity_mentions.c.entity_id == row.entity_id)
                            .values(entity_id=canonical.entity_id)
                        )
                        # non-canonical 엔티티 삭제.
                        s.execute(
                            delete(kg_entities).where(kg_entities.c.entity_id == row.entity_id)
                        )
                    report.merged += 1

                # canonical의 last_seen은 그룹 전체 관측의 max로 union한다 — canonical은
                # first_seen 최소로 뽑히므로, 더 최근 last_seen을 가진 멤버를 흡수하면
                # last_seen이 뒤처진다(recency/staleness 소비자 오작동, §1 리뷰).
                last_seen_target = max(m[0].last_seen for m in members)

                # canonical display/norm/key 값 갱신(값이 바뀔 때만 updated_canonical).
                value_changed = (
                    canon_display != canonical.display_name
                    or canon_norm != canonical.name_norm
                    or new_key != canonical.entity_key
                )
                if value_changed:
                    report.updated_canonical += 1
                    values = {
                        "display_name": canon_display,
                        "name_norm": canon_norm,
                        "entity_key": new_key,
                        "schema_version": KG_SCHEMA_VERSION,
                        "last_seen": last_seen_target,
                    }
                    # persist 규약(entities.py) 준수: updated_at은 display_name이 실제로
                    # 바뀔 때만 bump한다 — name_norm/entity_key만 이동한 정규화 수렴은
                    # 콘텐츠 변경이 아니므로 watermark(kg-sync) churn을 유발하지 않게 한다.
                    if canon_display != canonical.display_name:
                        values["updated_at"] = at
                    canon_updates.append(
                        (canonical.entity_id, values, True)  # entity_key 변경 → two-phase park 대상
                    )
                else:
                    report.unchanged += 1
                    if last_seen_target != canonical.last_seen:
                        # 값은 그대로지만 병합으로 더 최근 관측을 흡수 → last_seen만 최신화.
                        # last_seen은 관측값이라 updated_at은 건드리지 않는다(persist 규약).
                        canon_updates.append(
                            (canonical.entity_id, {"last_seen": last_seen_target}, False)
                        )

            # 4) canonical update 반영 — entity_key를 바꾸는 것부터 two-phase로.
            #    4a) key 변경 대상을 임시 유니크 키로 park → 그룹 간 key swap이 있어도 충돌 없음.
            #    4b) 최종 값 반영. (재사용 backfill이 향후 정규화 델타에서 clean→clean rename을
            #    만들어도 안전 — E-N1a 델타 자체에는 cross-group 충돌이 없다.)
            if apply:
                for cid, _values, key_changing in canon_updates:
                    if key_changing:
                        s.execute(
                            update(kg_entities)
                            .where(kg_entities.c.entity_id == cid)
                            .values(entity_key=f"{_TMP_KEY_PREFIX}{cid}")
                        )
                for cid, values, _key_changing in canon_updates:
                    s.execute(
                        update(kg_entities).where(kg_entities.c.entity_id == cid).values(**values)
                    )
                s.commit()

        # §9.2 — backfill이 kind 분포를 바꾼 name_norm 중, 그 norm에 걸린 entity_conflict
        # WikiTask가 이제 실재하지 않는 모순을 가리키는지(stale) 탐지한다. dry-run/apply
        # 동일: post 상태는 in-memory `groups`에서, task 존재는 read-only load_task로 본다.
        # **자동 resolve/emit은 하지 않는다** — entity_conflict는 운영자 리뷰 invariant
        # (api/routes/wiki.py `_WIKI_TASK_CLEANUP_KINDS` 제외), conflict emit은 evidence
        # 페이지가 필요해 distill/persist 소유. 운영자 수동 정리용 리포트만 남긴다.
        post_kinds_by_norm: dict[str, set[str]] = defaultdict(set)
        for members in groups.values():
            head_row, _hd, head_norm = members[0]  # 그룹 멤버는 (kind, new_norm) 동일
            post_kinds_by_norm[head_norm].add(head_row.entity_kind)
        for old_norm in sorted(affected_norms):
            if len(post_kinds_by_norm.get(old_norm, ())) >= 2:
                continue  # 여전히 2개 이상 kind → 모순 유효, stale 아님
            slug = _conflict_task_slug(old_norm)
            task = wiki_store.load_task(slug, scope="company", root=root)
            if task is not None and not task.resolved and task.kind == "entity_conflict":
                report.stale_conflict_tasks.append(slug)

        span.add_meta(
            dry_run=report.dry_run,
            considered=report.considered,
            dropped_opaque=report.dropped_opaque,
            merged=report.merged,
            rerouted_mentions=report.rerouted_mentions,
            deduped_mentions=report.deduped_mentions,
            updated_canonical=report.updated_canonical,
            unchanged=report.unchanged,
            stale_conflict_tasks=len(report.stale_conflict_tasks),
        )
    return report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    apply = False
    for a in args:
        if a == "--apply":
            apply = True
        elif a == "--dry-run":
            apply = False
        else:
            print(
                "usage: python -m orthus.kg.entity_normalize [--dry-run|--apply]\n"
                "  --dry-run  (default) count only, no writes\n"
                "  --apply    perform merge/drop/normalize + commit",
                file=sys.stderr,
            )
            return 2
    if apply:
        print(
            "kg entity-normalize: --apply writes PG SoR in one transaction. Run during "
            "quiescence (pause live distill / mail-sync); a concurrent write can force a "
            "full rollback.",
            file=sys.stderr,
        )
    try:
        report = run_entity_normalize(apply=apply)
    except Exception as exc:  # noqa: BLE001 — CLI fail-loud (company-node guard 등)
        print(f"kg entity-normalize failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if report.noop_kg_disabled:
        print("kg entity-normalize: ORTHUS_KG_ENABLED=false — no entity SoR to normalize.")
        return 0

    mode = "DRY-RUN" if report.dry_run else "APPLIED"
    print(
        f"kg entity-normalize [{mode}]: considered={report.considered} "
        f"dropped_opaque={report.dropped_opaque} merged={report.merged} "
        f"rerouted_mentions={report.rerouted_mentions} "
        f"deduped_mentions={report.deduped_mentions} "
        f"updated_canonical={report.updated_canonical} unchanged={report.unchanged} "
        f"stale_conflict_tasks={len(report.stale_conflict_tasks)}"
    )
    if report.stale_conflict_tasks:
        # 자동 resolve하지 않음(entity_conflict 리뷰 invariant) — 운영자 수동 정리 안내.
        print(
            "stale entity_conflict tasks (conflict no longer holds; review & resolve "
            "manually via /wiki tasks): " + ", ".join(report.stale_conflict_tasks),
            file=sys.stderr,
        )
    if report.dry_run and report.changed:
        print("re-run with --apply to write these changes.", file=sys.stderr)
    if not report.dry_run and report.changed:
        print(
            "next: `make node-kg-rebuild NODE=company` to re-project into Neo4j "
            "(full-rebuild-only contract, E-N1c).",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
