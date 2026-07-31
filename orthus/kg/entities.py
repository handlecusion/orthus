"""K6 — entity SoR persist (결정론, 구현 명세 §9.1/§9.2).

LLM은 distill에서 entity 이름만 추출하고(원칙 1), 결정론 코드인 이 모듈이 PG
SoR(`kg_entities`/`kg_entity_mentions`)에 적재한다. KG projection은 이 SoR을
결정론 투영한다 — rebuild 시 LLM 재호출이 없다(rebuildable 계약, kg-model §2
엣지 저작 원칙).

경계/안전:
- **company scope only.** `scope != 'company'`면 no-op(개인 distill 차단,
  owner-scope는 K7). projection SELECT의 `scope='company'` 필터와 이중 방어.
- **PII redaction.** person `display_name`/`name_norm`은 사람 이름(operations
  §8.3 Direct PII)이라 carve-out으로 저장하되, persist 전 `redact_pii_text`로
  이름 속 이메일/전화를 차단한다(kg-model §1, operations §8).
- **충돌 → WikiTask(entity_conflict).** 같은 `name_norm`인데 다른 `entity_kind`
  (person:김대표 vs org:김대표)는 silent split 금지 → 정체성 모순을
  `WikiTask(kind="entity_conflict")`로 가시화한다(결정론 판정, LLM 0회). emit은
  wiki store task writer에 위임한다(B 결정: kg→wiki는 derived→source 방향).

name_norm 정규화(A 결정 2026-06-13 — 보수적): `NFKC → 공백 collapse → casefold`
까지만. 직함/조사 suffix strip은 손상 위험('김대표'→'김')으로 v1에서 제외한다 —
dedup evidence가 필요를 증명하면 후속에서 상수 규칙으로 추가한다.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.audit import audit
from orthus.audit.redact import redact_pii_text
from orthus.db import session
from orthus.kg.schema import KG_SCHEMA_VERSION
from orthus.schemas.canonical import ExtractedEntity, WikiSource, WikiTask
from orthus.kg.client import kg_enabled
from orthus.tables import kg_entities, kg_entity_mentions, wiki_pages
from orthus.wiki import store as wiki_store

_ENTITY_KINDS = frozenset({"person", "org", "project", "system"})
# mention 대상 page kind — projection `_load_mentions` join 필터와 동형(§9.1).
_MENTION_PAGE_KINDS = ("page", "claim")
_WS = re.compile(r"\s+")

# --- Slack/마크업 표기 정준화 (E-N1a, docs/kg-entity-normalization-plan.md) --------
# distill이 Slack 메시지 원문에서 뽑은 이름에는 Slack 특유 표기가 섞인다:
#   <@U012ABCDEF>            user 멘션(실명 없음)
#   <@U012ABCDEF|김철수>      user 멘션(파이프 실명 — 텍스트에 실명 존재)
#   <#C012ABCDEF|채널명>      채널
#   <!subteam^S012|팀명>      subteam 멘션
#   <!here> <!channel>       broadcast
#   <https://x|텍스트>        링크
# 정규화 前(_clean_name 맨 앞, redact 前 — 파이프 실명이 redact 대상이 되게, D1)에
# 이 표기를 사람이 읽는 형태로 정준화한다. 그러면 `<@U…>`와 bare `U…`가 같은 토큰으로
# 수렴해 중복 split이 사라진다(D-c: 이 함수는 `normalize_name_norm`을 통해 persist·
# `/ask` bind·relations 이름 매칭에 동시 반영 — 단일 지점, L5). Slack 델리미터
# (`<@`,`<#`,`<!`,`<http`,`<mailto`)에만 매칭해 일반 텍스트의 `<`/`@`는 건드리지 않는다.
_SLACK_LINK_PIPE = re.compile(r"<(?:https?://|mailto:)[^|>]*\|([^>]+)>")  # <url|text> -> text
_SLACK_MENTION_PIPE = re.compile(
    r"<[@#!][^|>]*\|([^>]+)>"
)  # <@U..|name>,<#C..|ch>,<!subteam^..|t> -> name
_SLACK_LINK_BARE = re.compile(r"<(?:https?://|mailto:)[^>]*>")  # <url> -> 제거
_SLACK_BROADCAST = re.compile(r"<!(?:here|channel|everyone)>", re.I)  # <!here> -> 제거
_SLACK_SUBTEAM_BARE = re.compile(r"<!subteam\^[A-Za-z0-9]+>")  # <!subteam^..> -> 제거
_SLACK_CHANNEL_BARE = re.compile(r"<#C[A-Za-z0-9]+>")  # <#C..> -> 제거
_SLACK_USER_BARE = re.compile(
    r"<@([UW][A-Za-z0-9]+)>"
)  # <@U..> -> U.. (드롭 판정은 _is_opaque_person_id)
# 불투명 Slack user/workspace ID (드롭 판정, D-a). person 한정 — 정준화 후에도
# 실명이 복원되지 않은 ID만 남으면 노이즈이므로 엔티티로 만들지 않는다.
_SLACK_ID = re.compile(r"[UW]0[A-Z0-9]{6,}", re.I)


def _demarkup(name: str) -> str:
    """Slack/링크 표기 정준화(E-N1a). 파이프 실명은 복원, bare 멘션은 텍스트로 환원.

    Slack 델리미터에만 앵커하므로 일반 텍스트(이메일의 `@`, 부등호 등)는 불변이다.
    파이프 변형을 bare 변형보다 먼저 적용해 `<@U..|name>`에서 실명을 살린다."""
    s = name
    s = _SLACK_LINK_PIPE.sub(r"\1", s)
    s = _SLACK_MENTION_PIPE.sub(r"\1", s)  # <@U..|name>,<#C..|name>,<!subteam^..|name>
    s = _SLACK_LINK_BARE.sub(" ", s)
    s = _SLACK_BROADCAST.sub(" ", s)
    s = _SLACK_SUBTEAM_BARE.sub(" ", s)
    s = _SLACK_CHANNEL_BARE.sub(" ", s)
    s = _SLACK_USER_BARE.sub(
        r"\1", s
    )  # 래퍼만 제거, ID 유지 → persist가 _is_opaque_person_id로 드롭
    return s


class EntityPersistResult(BaseModel):
    persisted: int = 0  # upsert된 entity 수(신규+갱신)
    mentions: int = 0  # 신규 삽입된 mention 수
    conflicts: list[str] = Field(default_factory=list)  # emit된 entity_conflict task slug
    skipped: int = 0  # 빈 이름/허용 외 kind로 drop된 수
    dropped_opaque: int = 0  # E-N1a — 불투명 Slack ID(person)로 drop된 수
    noop_scope: bool = False  # scope != company → 전체 no-op
    noop_kg_disabled: bool = False  # ORTHUS_KG_ENABLED=false → 적재 skip(fail-closed)


def _clean_name(name: str) -> str:
    """원문 이름 → 표시용 정규화(Slack 표기 정준화 + redact + NFKC + 공백 collapse, casefold 안 함)."""
    demarked = _demarkup(name or "")
    redacted = redact_pii_text(demarked)
    s = unicodedata.normalize("NFKC", redacted).strip()
    return _WS.sub(" ", s)


def _is_opaque_person_id(kind: str, display: str) -> bool:
    """kind=person이면서 정준화(_clean_name) 후에도 불투명 Slack ID만 남은 경우 True (E-N1a, D-a).

    실명 복원 불가한 노이즈이므로 persist에서 드롭한다. person 한정 — system/org/project은
    ID 꼴이 정상 이름일 수 없어 대상 아니다. `<@U…|실명>`은 _demarkup이 실명을 살리므로
    여기 걸리지 않는다."""
    return kind == "person" and _SLACK_ID.fullmatch(display) is not None


def normalize_name_norm(name: str) -> str:
    """원문 이름 → `name_norm`(저장 키와 동일 정규화: redact + NFKC + 공백 collapse + casefold).

    persist 경로(`name_norm = _clean_name(...).casefold()`)와 `/ask` entity intent
    bind(K9.3a, `orthus.router.graph`)가 **이 단일 함수**를 공유해 정규화 드리프트를
    원천 차단한다(L5 — 한쪽만 바뀌면 같은 이름이 매칭 안 됨). 빈 이름은 `""`를 돌려주고
    호출자가 skip한다."""
    return _clean_name(name).casefold()


def _conflict_task_slug(name_norm: str) -> str:
    h = hashlib.sha256(name_norm.encode("utf-8")).hexdigest()[:12]
    return f"entity-conflict-{h}"


def persist_entities(
    entities: list[ExtractedEntity],
    *,
    page_id: UUID,
    slug: str,
    scope: str,
    user_id: UUID,
    root: Path | None = None,
    now: datetime | None = None,
) -> EntityPersistResult:
    """company wiki page `(page_id, slug)`에 언급된 entity 후보를 PG SoR에 적재.

    같은 page에 함께 들어온 entity들은 같은 `page_id` mention을 공유하므로
    projection이 그 page를 evidence로 `RELATES_TO{co_mention}`을 결정론 유도한다
    (co-mention 계산은 projection이 — 여기서는 entity/mention 적재만).
    """
    result = EntityPersistResult()
    if scope != "company":
        # owner-scope/personal은 K7 — fail-closed no-op. projection도 company만 읽지만
        # 쓰기 경계를 여기서 한 번 더 막는다(unknown/None scope도 deny).
        result.noop_scope = True
        return result

    at = now or datetime.now(UTC)
    seen_norms: dict[str, str] = {}  # name_norm -> display (배치 내 등장)

    with audit("kg.entities.persist") as span:
        with session() as s:
            for e in entities:
                if e.kind not in _ENTITY_KINDS:
                    result.skipped += 1
                    continue
                display = _clean_name(e.name)
                if not display:
                    result.skipped += 1
                    continue
                if _is_opaque_person_id(e.kind, display):
                    # E-N1a — 불투명 Slack ID(person)는 노이즈 → 엔티티 미생성.
                    # name_norm/upsert/seen_norms 진입 전이라 충돌판정에서도 자동 배제.
                    result.dropped_opaque += 1
                    continue
                name_norm = display.casefold()
                entity_key = f"{e.kind}:{name_norm}"
                seen_norms[name_norm] = display

                ins = pg_insert(kg_entities).values(
                    entity_id=uuid4(),
                    entity_key=entity_key,
                    entity_kind=e.kind,
                    name_norm=name_norm,
                    display_name=display,
                    scope="company",
                    owner_id=None,  # K6 — 항상 NULL
                    first_seen=at,
                    last_seen=at,
                    schema_version=KG_SCHEMA_VERSION,
                    created_at=at,
                    updated_at=at,
                )
                upsert = ins.on_conflict_do_update(
                    index_elements=["entity_key"],
                    set_={
                        "display_name": ins.excluded.display_name,
                        "last_seen": func.greatest(kg_entities.c.last_seen, ins.excluded.last_seen),
                        # updated_at은 display_name이 실제로 바뀔 때만 bump한다 —
                        # watermark sync churn 방지(동일 재-distill은 0 changed).
                        # last_seen은 관측값이라 updated_at을 건드리지 않는다.
                        "updated_at": case(
                            (
                                kg_entities.c.display_name != ins.excluded.display_name,
                                ins.excluded.updated_at,
                            ),
                            else_=kg_entities.c.updated_at,
                        ),
                    },
                ).returning(kg_entities.c.entity_id)
                entity_id = s.execute(upsert).scalar_one()
                result.persisted += 1

                mins = (
                    pg_insert(kg_entity_mentions)
                    .values(
                        mention_id=uuid4(),
                        entity_id=entity_id,
                        page_id=page_id,
                        evidence_slug=slug,
                        schema_version=KG_SCHEMA_VERSION,
                        created_at=at,
                    )
                    .on_conflict_do_nothing(index_elements=["entity_id", "page_id"])
                    .returning(kg_entity_mentions.c.mention_id)
                )
                # rowcount는 ON CONFLICT DO NOTHING에서 -1(unknown)일 수 있어
                # 신뢰 못 한다 — RETURNING row 유무로 신규 mention을 센다.
                if s.execute(mins).first() is not None:
                    result.mentions += 1

            # 충돌 판정: 같은 name_norm에 distinct entity_kind가 2개 이상이면
            # 정체성 모순(배치+PG 합산 — upsert 후 조회라 동-배치 쌍도 잡힌다).
            conflicts: list[tuple[str, str, list[str]]] = []
            for name_norm, display in seen_norms.items():
                kinds = sorted(
                    s.execute(
                        select(kg_entities.c.entity_kind)
                        .where(kg_entities.c.name_norm == name_norm)
                        .distinct()
                    ).scalars()
                )
                if len(kinds) > 1:
                    conflicts.append((name_norm, display, kinds))

            s.commit()

        # WikiTask emit은 세션 밖에서(write_task가 자체 세션 사용). 같은 slug라
        # 재-emit은 멱등(_persist upsert). build_conflict_index는 kind=="conflict"만
        # 보므로 entity_conflict는 CONFLICTS_WITH 속성 인덱스를 오염시키지 않는다.
        for name_norm, display, kinds in conflicts:
            task_slug = _conflict_task_slug(name_norm)
            wiki_store.write_task(
                WikiTask(
                    slug=task_slug,
                    kind="entity_conflict",
                    description=(
                        f"Entity '{display}'이(가) 서로 다른 종류로 분류됨: "
                        f"{', '.join(kinds)}. 같은 이름·다른 entity_kind — "
                        "동일 실체인지 검토 필요(silent split 방지)."
                    ),
                    related=[slug],
                    created_at=at,
                ),
                user_id=user_id,
                root=root,
                scope="company",
            )
            result.conflicts.append(task_slug)

        span.add_meta(
            persisted=result.persisted,
            mentions=result.mentions,
            conflicts=len(result.conflicts),
            skipped=result.skipped,
        )
    return result


def persist_distilled_entities(
    entities: list[ExtractedEntity],
    *,
    source: WikiSource,
    user_id: UUID,
    scope: str,
    root: Path | None = None,
    now: datetime | None = None,
) -> EntityPersistResult:
    """distill이 추출한 문서 수준 entity를 그 문서가 기여한 **모든** company concept
    page에 mention으로 적재한다(serial write 단계 — 구현 명세 §9.2, 페이지 바인딩
    결정 2026-06-13: `source.related_pages` 전부).

    distill은 페이지 연결 없는 문서 수준 entity 목록만 주므로, 문서의 claim들이
    매핑된 모든 company concept page에 같은 entity 집합을 mention으로 붙인다 —
    같은 page에 함께 묶인 entity들에서 projection이 `RELATES_TO{co_mention}`을
    결정론 유도한다(여러 page에 걸친 같은 쌍은 (src,rel,dst) dedup으로 1 엣지 수렴).

    company scope 전용. `scope != 'company'`(personal compile 등)이면 page 조회도
    없이 no-op(owner-scope entity는 K7). page는 company + kind in (page|claim)만
    매칭해 mixed-scope 누출을 막는다(projection join 필터와 이중 방어).

    **fail-closed(`ORTHUS_KG_ENABLED`):** live distill→consolidate 경로의 wiring
    지점이므로 K3 outbox enqueue와 같은 기준으로 게이트한다 — KG off면 PG
    `kg_entities`(person 이름 PII 포함) 적재 자체를 skip한다(off 기간 변경 미적재;
    flag on 시 full `kg-rebuild` 1회가 distill 재실행으로 entity를 채운다 —
    full-rebuild-only 계약). 저수준 `persist_entities`는 flag-무관(SoR 원시 연산,
    PR1 단위 테스트가 직접 호출)이고, 게이트는 이 wiring 래퍼에만 둔다."""
    if scope != "company":
        return EntityPersistResult(noop_scope=True)
    if not kg_enabled():
        return EntityPersistResult(noop_kg_disabled=True)
    if not entities or not source.related_pages:
        return EntityPersistResult()

    with session() as s:
        page_rows = s.execute(
            select(wiki_pages.c.page_id, wiki_pages.c.slug).where(
                wiki_pages.c.scope == "company",
                wiki_pages.c.kind.in_(_MENTION_PAGE_KINDS),
                wiki_pages.c.slug.in_(list(dict.fromkeys(source.related_pages))),
            )
        ).all()

    agg = EntityPersistResult()
    seen_conflicts: set[str] = set()
    for page_id, slug in page_rows:
        r = persist_entities(
            entities,
            page_id=page_id,
            slug=slug,
            scope="company",
            user_id=user_id,
            root=root,
            now=now,
        )
        agg.mentions += r.mentions
        # 같은 entity 집합을 page마다 넘기므로 persisted/skipped는 page-불변 —
        # 마지막 호출값으로 distinct entity 수를 반영한다(mentions만 page 합산).
        agg.persisted = r.persisted
        agg.skipped = r.skipped
        for c in r.conflicts:
            if c not in seen_conflicts:
                seen_conflicts.add(c)
                agg.conflicts.append(c)
    return agg
