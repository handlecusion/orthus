-- K7.3 §4.6 — cross-scope edge inventory (머지 전 게이트 아티팩트, M4 / T3)
--
-- "마퀴 약속"(개인 노트가 회사 지식에 실제 graph 엣지로 연결)이 실재하는지
-- PG-side로 측정한다. `resolve_slug`가 PG라 이 집계는 graph 투영과 동형이다.
-- D2(entity company-only)에서 직관적 entity 다리는 엣지를 만들지 않으므로, 오늘
-- 실재하는 cross-scope 다리는 명시적 backlink/derived_from/supports 엣지뿐이다.
--
-- T3 제약(증거 출력 규칙):
--   * 절대 카운트 + rel-type만. personal slug/title/owner_id 미노출.
--   * owner-scoped(:owner 지정) 또는 회사-집계만. cross-owner 존재 oracle 금지
--     — owner_id를 행별로 내보내지 않고 count(DISTINCT owner_id)로만 집계한다.
--     (§4.6 리터럴 SQL의 `GROUP BY sp.owner_id`/owner_id 출력은 같은 문서 T3와
--      충돌하므로, owner_id를 출력에서 제거하고 distinct-owner 집계로 화해.)
--
-- 사용: `make kg-cross-scope-inventory NODE=company [OWNER=<uuid>]`
--   * OWNER 미지정 → 회사-집계(distinct-owner 카운트, 정체성 미노출)
--   * OWNER 지정   → 그 owner 한 명의 투영만(게이트 권위 측정, 단일 분모)
--
-- 0이면 패널 헤드라인이 "내 노트가 어떻게 연결되는지"를 주장하면 안 되고
-- BACKLINK-real/"coming" framing으로 떨어지며, 운영자 필수 escalation(§7,
-- personal-entity vs P7.3 시퀀싱)이 수용 트리거다.

SELECT
    wl.rel                            AS rel,
    count(DISTINCT sp.owner_id)       AS owners_with_edge,
    count(DISTINCT sp.page_id)        AS personal_pages_with_edge,
    count(*)                          AS edges
FROM wiki_links wl
JOIN wiki_pages sp
    ON sp.page_id = wl.src_page_id
   AND sp.scope = 'personal'
JOIN wiki_pages dp
    ON dp.slug = wl.dst_slug
   AND dp.scope = 'company'
   AND dp.owner_id IS NULL  -- company canonical 페이지는 owner NULL; (slug,scope,owner_id)
                            -- NULLS-NOT-DISTINCT 유니크라 owner 있는 company 행이 같은
                            -- slug로 다중 매칭해 edges 과대집계되는 것을 방어.
WHERE wl.rel IN ('backlink', 'derived_from', 'supports')
  AND (CAST(:owner AS uuid) IS NULL OR sp.owner_id = CAST(:owner AS uuid))
GROUP BY wl.rel
ORDER BY wl.rel;
