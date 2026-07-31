"""O4/E3 — should_decompose 프리필터 신호 확장 티어 (docs/decompose-prefilter-ext.md).

고정하는 계약:
1. **flag-off byte-identical** (tier 0 default): 확장 토큰만 든 질문은 여전히 프리필터에서
   LLM 0회로 걸러진다 — 기존 t7 골든/단위 테스트 판정 불변.
2. **모집단 격리(최대 리스크)**: 티어를 3까지 올려도 `command_split_signal`과
   `mail_has_orchestration_signal`의 판정은 바뀌지 않는다. 확장은 `should_decompose`
   경로에만 걸린다.
3. **티어 누적 + clamp**: tier=2는 T1+T2, tier=3은 T1+T2+T3. 범위를 벗어난 설정값은
   [0, MAX]로 clamp된다(오설정이 프리필터를 전부 통과시키지 않는다).
"""

from __future__ import annotations

import pytest

from orthus.router import event_orchestration as eo
from orthus.router.decompose import (
    _has_connective_or_enum,
    _has_relational_term,
    _has_t4_go_signal,
    _PREFILTER_EXT_MAX_TIER,
    command_split_signal,
    prefilter_ext_tier,
    should_decompose,
)

# O4가 지목한 누락형: 기존 공유 토큰셋에는 신호가 없어 LLM 게이트에 도달조차 못 하던 복합질문.
T1_MISSED = "환불 정책과 배송 정책 각각 알려줘"
T1_MISSED_2 = "nova와 orbit 프로젝트 상태를 비교해줘"
T2_MISSED = "환불정책이랑 배송정책 알려줘"
T2_MISSED_3 = "온보딩 절차는 뭐고 리텐션 정책은 어때?"
T3_MISSED = "아틀라스 프로젝트 상태와 아크메 직원 수 알려줘"

# 기존 신호(접속어)로 이미 통과하던 복합질문 — 확장과 무관하게 항상 True.
LEGACY_PASS = "A는 뭐야 그리고 B는 뭐야"
# 신호가 전혀 없는 단일 질문 — 어떤 티어에서도 프리필터 False.
PLAIN_SINGLE = "환불정책이 뭐야"


class TestFlagOffByteIdentical:
    """tier 0 (default) — 확장 토큰이 있어도 기존과 동일하게 False."""

    @pytest.mark.parametrize(
        "question", [T1_MISSED, T1_MISSED_2, T2_MISSED, T2_MISSED_3, T3_MISSED, PLAIN_SINGLE]
    )
    def test_ext_tokens_do_not_fire_at_tier_0(self, question):
        assert _has_connective_or_enum(question) is False
        assert _has_connective_or_enum(question, ext_tier=0) is False

    def test_legacy_connective_still_fires(self):
        assert _has_connective_or_enum(LEGACY_PASS) is True

    def test_should_decompose_prefilter_false_without_llm(self):
        """LLM을 부르면 실패하는 chat_model — 프리필터에서 끊겼음을 증명한다(LLM 0회)."""

        class ExplodingChat:
            model_id = "must-not-be-called"

            def complete(self, *a, **kw):  # noqa: ANN002, ANN003
                raise AssertionError("prefilter가 통과시켜 LLM 게이트를 호출했다")

        assert should_decompose(T2_MISSED, chat_model=ExplodingChat(), ext_tier=0) is False

    def test_default_tier_is_off(self):
        assert prefilter_ext_tier() == 0


class TestTierCumulative:
    @pytest.mark.parametrize(
        "question, first_firing_tier",
        [
            (T1_MISSED, 1),  # "각각"
            (T1_MISSED_2, 1),  # "비교"
            (T2_MISSED, 2),  # "이랑"
            (T2_MISSED_3, 2),  # "는 뭐고"
            (T3_MISSED, 3),  # 병렬 조사 "와 "
        ],
    )
    def test_signal_fires_from_its_tier_up(self, question, first_firing_tier):
        for tier in range(0, _PREFILTER_EXT_MAX_TIER + 1):
            expected = tier >= first_firing_tier
            assert _has_connective_or_enum(question, ext_tier=tier) is expected, (
                f"tier={tier} {question!r}"
            )

    @pytest.mark.parametrize("tier", [0, 1, 2, 3])
    def test_plain_single_never_fires(self, tier):
        assert _has_connective_or_enum(PLAIN_SINGLE, ext_tier=tier) is False

    @pytest.mark.parametrize("tier", [0, 1, 2, 3])
    def test_legacy_signal_unaffected_by_tier(self, tier):
        assert _has_connective_or_enum(LEGACY_PASS, ext_tier=tier) is True

    def test_out_of_range_tier_is_clamped(self):
        # 과대 설정이 프리필터를 "전부 통과"로 만들지 않는다 — MAX와 동일하게 동작.
        assert _has_connective_or_enum(PLAIN_SINGLE, ext_tier=99) is False
        assert _has_connective_or_enum(T3_MISSED, ext_tier=99) is True
        # 음수는 off와 동일.
        assert should_decompose(T3_MISSED, ext_tier=-1) is False


class TestPopulationIsolation:
    """확장 티어는 command-split / event-orch 모집단을 오염시키지 않는다(§7 최대 리스크)."""

    @pytest.mark.parametrize("question", [T1_MISSED, T2_MISSED, T3_MISSED])
    def test_command_split_signal_ignores_ext_tiers(self, question, monkeypatch):
        # 설정을 최대 티어로 올려도 command_split_signal은 default(0)로 호출되므로 불변.
        monkeypatch.setattr(
            "orthus.router.decompose.prefilter_ext_tier", lambda: _PREFILTER_EXT_MAX_TIER
        )
        assert command_split_signal(question) is False

    @pytest.mark.parametrize("question", [T1_MISSED, T2_MISSED, T3_MISSED])
    def test_mail_orchestration_signal_ignores_ext_tiers(self, question, monkeypatch):
        monkeypatch.setattr(
            "orthus.router.decompose.prefilter_ext_tier", lambda: _PREFILTER_EXT_MAX_TIER
        )
        assert eo.mail_has_orchestration_signal(question) is False

    def test_command_split_signal_keeps_legacy_true(self):
        assert command_split_signal(LEGACY_PASS) is True


class TestRelationalSuppression:
    """병렬 조사 + 관계어 = 관계형 단일 질문 → 발화하지 않는다 (실측 기반, §7).

    실측: 이 억제가 없으면 자연 트래픽 오통과가 +22.2%p 늘고, 늘어난 것은 전부 KG graph
    라우트의 관계형 질문이었다(게이트는 전부 no로 막았다 = 순수 지연 낭비).
    """

    @pytest.mark.parametrize(
        "question",
        [
            "Nova와 아크메는 무슨 관계야?",
            "박기획이랑 김철수는 무슨 관계야?",
            "nova와 연결된 문서들 사이의 관계가 어떻게 돼?",
            "이 프로젝트랑 연결된 사람들이 누구야?",
            "그 이슈와 연관된 티켓이 몇 개야?",
        ],
    )
    def test_relational_single_question_suppressed(self, question):
        assert _has_connective_or_enum(question, ext_tier=3) is False

    @pytest.mark.parametrize(
        "question",
        [
            # 병렬 나열 — 관계어 없음 → 발화한다.
            "환불 정책과 배송 정책 알려줘",
            "아크메 직원 수랑 아틀라스 프로젝트 상태 알려줘",
            # 고유명사 "AI관련툴": 억제어에서 "관련"을 뺀 이유(실측: 이 문항을 잃었다).
            "AI관련툴 목록이랑 팀원 목록 보여줘",
        ],
    )
    def test_parallel_listing_still_fires(self, question):
        assert _has_connective_or_enum(question, ext_tier=3) is True

    def test_lexical_signal_not_suppressed_by_relational_term(self):
        """억제는 병렬 조사에만 걸린다 — 어휘 신호(각각/비교…)는 관계어가 있어도 발화한다."""
        assert _has_connective_or_enum("관계된 두 정책을 각각 알려줘", ext_tier=1) is True

    def test_legacy_connective_not_suppressed(self):
        """기존 공유 토큰셋(그리고 …)은 억제 대상이 아니다 — tier와 무관하게 발화."""
        assert _has_connective_or_enum("A와 B의 관계 그리고 C는 뭐야", ext_tier=3) is True


class TestShouldDecomposeReachesGateWhenTierOn:
    """티어를 켜면 누락형이 LLM 게이트에 도달한다 — 게이트 판정 자체는 LLM 몫."""

    class YesChat:
        model_id = "stub-yes"

        def __init__(self):
            self.calls = 0

        def complete(self, system, user, *, json_only=False):  # noqa: ANN001, ARG002
            self.calls += 1
            return '{"decompose": "yes"}'

    def test_tier_2_reaches_llm_gate(self):
        chat = self.YesChat()
        assert should_decompose(T2_MISSED, chat_model=chat, ext_tier=2) is True
        assert chat.calls == 1

    def test_tier_0_does_not_reach_llm_gate(self):
        chat = self.YesChat()
        assert should_decompose(T2_MISSED, chat_model=chat, ext_tier=0) is False
        assert chat.calls == 0


class TestSuppressionBoundaryBugfix:
    """관계어 억제는 경계 인식으로만 발화한다(진단 §3-E, c34).

    이전 구현은 compact 부분일치라 "디아더사이드"의 "사이"까지 관계어로 잡아 병렬 조사
    신호를 무효화했다. 이제 단어-내부 부분일치는 관계어로 보지 않는다."""

    def test_proper_noun_substring_not_relational(self):
        # "디아더사이드"의 "사이"는 회사 고유명사 내부 부분일치 — 관계어 아님.
        assert _has_relational_term("디아더사이드") is False

    def test_genuine_relational_term_still_detected(self):
        assert _has_relational_term("무슨 관계야") is True
        assert _has_relational_term("문서들 사이의 관계") is True
        assert _has_relational_term("nova와 연결된 문서") is True

    def test_c34_parallel_particle_no_longer_suppressed(self):
        # 병렬 조사 "랑 "이 있는 복합질문이 고유명사 "디아더사이드"의 "사이" 오탐으로
        # 억제되던 회귀 — tier 2부터 정상 발화한다.
        q = "재벌형사2 취소 스케줄 복구랑 디아더사이드 작업 내용 뭐야?"
        assert _has_connective_or_enum(q, ext_tier=2) is True

    def test_relational_single_question_still_suppressed_at_tier3(self):
        # 진짜 관계형 단일 질문은 여전히 억제(경계가 있어 정상 발화 → 억제).
        assert _has_connective_or_enum("Nova와 아크메는 무슨 관계야?", ext_tier=3) is False


class TestT4GoConnective:
    """T4 "-고" 접속어미 신호 — 모드 A(병렬) / 모드 C(순차 지시) 양성, 흔한 단일 "-고" 음성."""

    @pytest.mark.parametrize(
        "question",
        [
            # 모드 A: 절경계 "-고" + 후행 독립 의문
            "파트너 분야별 분포는 어떻게 되고 진행상태별 분포는 어때?",
            "이번 달 목표 매출이 얼마고 지난주 회고 블로커는 뭐였어?",
            "촬영관리 오류는 뭐였고 촬영 좋아요 싫어요 기능은 어떻게 동작해?",
            "신규회사 등록은 어떻게 하고 지부 필터링 추가는 뭐야?",
            # 모드 C: 절경계 "-고," + 순차 지시어 "그"
            "협업업무표에서 티켓이 가장 많은 담당자가 누군지 찾고, 그 사람이 맡은 항목 목록을 보여줘",
            "가장 최근 회사 미팅이 언제였는지 찾고, 그 미팅에 참석한 직원이 누구였는지 알려주세요",
        ],
    )
    def test_compound_go_fires_at_tier4(self, question):
        assert _has_t4_go_signal(question.lower()) is True
        assert _has_connective_or_enum(question, ext_tier=4) is True
        # tier 3에서는 (조사/어휘 신호가 없으면) 도달하지 못했다.
        assert _has_connective_or_enum(question, ext_tier=3) is False

    @pytest.mark.parametrize(
        "question",
        [
            # 보조용언 활용 — 단일 절 ("-고 싶다"/"-고 있다"/"-고 나서")
            "매출을 높이고 싶은데 방법이 뭐야?",
            "진행하고 있는 프로젝트가 뭐야?",
            "검토하고 싶은 자료가 어디 있어?",
            "회의하고 나서 뭐 했어?",
            # 고-종결 명사 — "-고" 접속 아님
            "이번 사고 원인이 뭐야?",
            "참고 문서 링크가 뭐야?",
            "광고 예산이 얼마야?",
            "신고 절차가 어떻게 돼?",
            # 절경계 "-고"가 아예 없는 단일 질문
            "환불정책이 뭐야",
            "온보딩은 어떤 순서로 진행돼?",
        ],
    )
    def test_t4_predicate_rejects_single_go_or_noun(self, question):
        # T4 predicate 자체는 이 단일 질문들을 발화하지 않는다(precision).
        assert _has_t4_go_signal(question.lower()) is False

    @pytest.mark.parametrize(
        "question",
        [
            # 위 목록 중 하위 티어 토큰이 없는 것만 — tier 4 게이트 전체가 False여야 한다.
            # ("매출을 높이고 …"는 "높이고"의 "이고"가 T2 토큰이라 tier 2부터 발화한다 —
            #  T4와 무관한 기존 동작이므로 여기서 제외한다.)
            "진행하고 있는 프로젝트가 뭐야?",
            "검토하고 싶은 자료가 어디 있어?",
            "회의하고 나서 뭐 했어?",
            "이번 사고 원인이 뭐야?",
            "참고 문서 링크가 뭐야?",
            "광고 예산이 얼마야?",
            "신고 절차가 어떻게 돼?",
            "환불정책이 뭐야",
            "온보딩은 어떤 순서로 진행돼?",
        ],
    )
    def test_single_go_or_noun_never_reaches_gate_at_tier4(self, question):
        assert _has_connective_or_enum(question, ext_tier=4) is False

    def test_t4_is_cumulative_over_tier3(self):
        # tier 4는 T1+T2+T3 신호도 그대로 통과시킨다.
        assert _has_connective_or_enum("환불 정책과 배송 정책 알려줘", ext_tier=4) is True
        assert _has_connective_or_enum("환불정책이랑 배송정책 알려줘", ext_tier=4) is True

    def test_t4_off_at_tier3(self):
        # "-고" 신호는 tier 4에서만 켜진다.
        q = "파트너 분야별 분포는 어떻게 되고 진행상태별 분포는 어때?"
        for tier in range(0, 4):
            assert _has_connective_or_enum(q, ext_tier=tier) is False

    def test_t4_max_tier_is_4(self):
        assert _PREFILTER_EXT_MAX_TIER == 4


class TestT4NounExceptionPrecision:
    """실트래픽 검증(prefilter-t4-realtraffic.md §3)에서 misfire 90%가 "고"-종결 명사
    예외 공백("회고" 63.0%·"로고" 7.4%·"공고" 3.7%) 때문이었다. `_T4_NOUN_GO`에
    회고/로고/공고/충고/재고를 보강한다 — 단 이 명사들 다수는 "-하다" 동사 어간이기도 하므로
    (보고하다/참고하다/신고하다/재고하다/충고하다), 그 동사 활용형("…하고")은 여전히 진짜
    복합 신호로 발화해야 한다."""

    @pytest.mark.parametrize(
        "question",
        [
            # 단독 명사로 절경계 "-고" 위치에 등장 — 억제돼야 한다(단일 질문).
            # 억제가 없다면 후행 절의 의문사/종결어미 때문에 오발화했을 것들.
            "지난주 회고 내용이 뭐야?",
            "새 로고 시안이 어디 있어?",
            "이번 채용 공고 링크가 뭐야?",
            "그 충고 내용이 뭐였어요?",
            "재고 수량이 몇 개야?",
        ],
    )
    def test_new_noun_exceptions_suppressed(self, question):
        assert _has_t4_go_signal(question.lower()) is False
        assert _has_connective_or_enum(question, ext_tier=4) is False

    @pytest.mark.parametrize(
        "question",
        [
            # 같은 명사가 "-하다" 동사 어간으로 활용된 진짜 복합 신호 — 여전히 발화해야 한다.
            # 매칭 위치가 노트 끝(절경계 "고" 바로 앞)인데, "…하고"는 그 앞 글자가 "하"라
            # 명사 문자열과 불일치하므로 억제되지 않는다.
            "지난달 실적을 보고하고 다음 분기 목표는 뭐야?",
            "새 로고를 만들고 다음 작업은 뭐야?",
            "채용 공고를 올리고 지원자는 몇 명이야?",
            "선배가 충고하고 후배는 뭐라고 답했어?",
            "예산안을 재고하고 다음 회의는 언제야?",
        ],
    )
    def test_verb_activation_of_same_nouns_still_fires(self, question):
        assert _has_t4_go_signal(question.lower()) is True
        assert _has_connective_or_enum(question, ext_tier=4) is True
