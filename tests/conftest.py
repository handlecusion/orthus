"""Shared fixtures and CI classification for pure, PostgreSQL, and KG tests."""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from sqlalchemy import insert, text

from orthus.auth import _bootstrap_done
from orthus.db import session
from orthus.kg.client import close_kg_driver, reset_kg_force_disabled
from orthus.settings import get_settings
from orthus.tables import metadata, users

# --- 작업 DB 보호 가드 (fail-closed) -------------------------------------
# 이 스위트의 fixture는 앱 테이블 전체를 TRUNCATE한다. `make test`는
# ORTHUS_PG_DSN을 orthus_test로 바꿔 격리하지만, 맨 `uv run pytest`는 루트 .env의
# 작업 DB(orthus)를 그대로 물어 실데이터(프로젝트/팀/allowlist/유저)를 지워버린다
# (2026-07-01, 2026-07-02 두 차례 실사고). 그래서 DB 이름에 'test'가 없으면
# 수집 단계에서 즉시 중단한다. 의도적으로 다른 DB를 쓰려면
# ORTHUS_TEST_ALLOW_UNSAFE_DB=1로만 우회할 수 있다.
_dsn = os.environ.get("ORTHUS_PG_DSN", "") or getattr(get_settings(), "pg_dsn", "")
_dbname = _dsn.rsplit("/", 1)[-1].split("?", 1)[0] if _dsn else ""
if "test" not in _dbname and os.environ.get("ORTHUS_TEST_ALLOW_UNSAFE_DB") != "1":
    raise pytest.UsageError(
        f"pytest가 테스트 DB가 아닌 '{_dbname}'를 가리키고 있습니다. "
        "fixture가 데이터 테이블을 비우므로 작업 DB에서는 실행하지 않습니다. "
        "`make test`를 사용하거나 ORTHUS_PG_DSN을 orthus_test로 지정하세요 "
        "(강제 실행: ORTHUS_TEST_ALLOW_UNSAFE_DB=1)."
    )

# neo4j-test 컨테이너 (docker-compose profile test — `make kg-test-up`).
# 고정 password는 테스트 전용 값이며 secret이 아니다.
# ORTHUS_KG_TEST_URI override는 병렬 worktree 로컬 개발용이다 — 두 스위트가
# 같은 :7688 그래프를 동시에 wipe하면 서로의 테스트를 깨뜨린다(PG TEST_DB
# 분리와 동일 이유, K4에서 추가). CI/단일 세션은 기본값 그대로.
_KG_TEST_URI = os.environ.get("ORTHUS_KG_TEST_URI", "bolt://127.0.0.1:7688")
_KG_TEST_PASSWORD = "orthus-kg-test"

# SQLAlchemy metadata intentionally mirrors every application table.  Building
# this list from metadata prevents a new FK child table from making TRUNCATE fail
# (or, with the old handwritten DELETE list, leaking rows between tests).
_DATA_TABLES = tuple(metadata.tables.values())


def _truncate_data_tables() -> None:
    """Empty every application table in one round-trip, preserving sequences."""
    table_names = ", ".join(f'"{table.name}"' for table in _DATA_TABLES)
    with session() as s:
        s.execute(text(f"TRUNCATE TABLE {table_names}"))
        s.commit()


# These modules exercise code paths that emit audit/query records through their
# own sessions even though the tests do not request the shared DB fixtures.
# Keeping the exception list explicit makes a new hidden DB dependency fail in
# Backend Fast instead of silently turning that lane into an integration suite.
_PG_UNIT_MODULES = {
    "test_agentic_ask.py",
    "test_agentic_ask_user_tool.py",
    "test_agentic_mail_tools.py",
    "test_agentic_meta_tools.py",
    "test_ask_cli_agent.py",
    "test_ask_decompose.py",
    "test_author_qa_headline.py",
    "test_backfill_claim_headline_parallel.py",
    "test_classify_intent.py",
    "test_command_split_readact.py",
    "test_company_directory_tools.py",
    # resolve_followup / answer_from_hits 는 audit() span을 열어 자체 세션으로 쓴다.
    "test_followup_rewrite.py",
    "test_qa_learn_gate.py",
    "test_k7_4_framings.py",
    "test_kg_relations.py",
    "test_model_orchestration.py",
    "test_security_fixes.py",
    "test_validate.py",
}

_SERIAL_MODULES = {
    # Creates and drops a fixed-name scratch database inside its isolated PG lane.
    "test_p8_migration.py",
}


def pytest_addoption(parser):
    """CI-only deterministic sharding without making local pytest depend on a plugin."""
    group = parser.getgroup("orthus-ci")
    group.addoption("--ci-shard-index", type=int, default=0)
    group.addoption("--ci-shard-count", type=int, default=1)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Classify tests by infrastructure and optionally select a stable CI shard.

    Integration tests are conservatively PostgreSQL-backed.  A few tests under
    tests/unit use the shared DB fixtures too, so fixture closure (not directory
    naming alone) is the source of truth for those.  KG tests are a strict subset
    of PG tests because every graph integration also uses the PostgreSQL SoR.
    """
    pg_fixtures = {"pg", "clean", "user_id", "kg", "kg_clean"}
    kg_fixtures = {"kg", "kg_clean"}

    for item in items:
        fixtures = set(item.fixturenames)
        is_integration = "integration" in item.path.parts
        if is_integration or item.path.name in _PG_UNIT_MODULES or fixtures & pg_fixtures:
            item.add_marker("pg")
        if fixtures & kg_fixtures:
            item.add_marker("kg")
        if item.path.name in _SERIAL_MODULES:
            item.add_marker("serial")

    shard_count = config.getoption("--ci-shard-count")
    shard_index = config.getoption("--ci-shard-index")
    if shard_count < 1:
        raise pytest.UsageError("--ci-shard-count must be at least 1")
    if not 0 <= shard_index < shard_count:
        raise pytest.UsageError("--ci-shard-index must be in [0, shard-count)")
    if shard_count == 1:
        return

    selected = []
    deselected = []
    for item in items:
        # Keep a module together: several legacy tests intentionally share
        # module-level caches/settings, while files are the isolation boundary.
        relative_path = item.path.relative_to(config.rootpath)
        digest = hashlib.sha256(str(relative_path).encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") % shard_count
        (selected if bucket == shard_index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


@pytest.fixture(scope="session", autouse=True)
def _isolate_wiki_store(tmp_path_factory):
    """Redirect the LLM-wiki markdown SoR root to a tmp dir for the whole session.

    The editor-save T1 trigger authors into `wiki_store_path` (default = repo
    `wiki-store/`); pointing it at a tmp dir keeps test runs from polluting the
    repo store. Tests that pass an explicit `root=` are unaffected.

    격리는 env로 건다 — 캐시된 Settings 인스턴스의 필드를 갈아끼우면
    `get_settings.cache_clear()`(예: tests/unit/test_model_orchestration.py) 한 번에
    격리가 죽고, 새로 만들어진 Settings가 기본값(= 저장소 루트 `wiki-store/`)을
    들고 온다. 그 뒤 모든 wiki 쓰기가 개발자 작업 트리로 간다. `wiki-store/company/`와
    `wiki-store/personal/`은 .gitignore 대상이라 `git status`에는 보이지 않지만,
    WikiTask는 markdown이 유일한 SoR이라 DB truncate로도 지워지지 않고 **다음
    실행까지 남아** 스위트를 오염시킨다(빈 저장소를 가정하는 테스트가 깨진다).
    env를 세팅해 두면 누가 어디서 캐시를 비우든 재생성된 Settings가 tmp 경로를 읽는다.
    회귀: tests/unit/test_wiki_store_isolation.py
    """
    root = tmp_path_factory.mktemp("wiki-store")
    mp = pytest.MonkeyPatch()  # 함수 스코프 monkeypatch 픽스처는 session에서 못 쓴다
    mp.setenv("ORTHUS_WIKI_STORE", str(root))
    # 위 작업 DB 가드가 임포트 시점에 이미 기본 경로 Settings를 캐시에 넣었다.
    get_settings.cache_clear()
    yield
    mp.undo()
    get_settings.cache_clear()


def _pg_available() -> bool:
    try:
        with session() as s:
            s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def pg():
    if not _pg_available():
        pytest.skip("Postgres not available on localhost:5433 (run `make up && make migrate`)")
    return True


@pytest.fixture
def clean(pg):
    """Empty all data tables before the test (children first for FKs)."""
    settings = get_settings()
    settings.auth_mode = "demo"
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.owner_scope_enabled = False
    settings.auth_cookie_name = ""
    settings.auth_dev_login_enabled = False
    settings.auth_magic_link_domain = "acme.example"
    settings.auth_magic_link_ttl_minutes = 15
    settings.auth_magic_link_delivery = "none"
    settings.auth_magic_link_from_email = "Orthus <login@auth.acme.example>"
    settings.resend_api_key = ""
    settings.resend_base_url = "https://api.resend.com"
    settings.resend_timeout_seconds = 10
    settings.central_admin_emails = ""
    settings.personal_owner_email = ""
    settings.central_read_base_url = ""
    settings.central_read_token = ""
    settings.central_read_timeout_ms = 6000
    settings.email_sender = "none"
    settings.mail_nova_base_url = "https://api.nova.example"
    settings.mail_nova_api_key_secret_ref = ""
    settings.mail_nova_api_key = ""
    settings.mail_nova_owner = ""
    settings.mail_acme_base_url = "https://mail-api.acme.example"
    settings.mail_acme_api_token_secret_ref = ""
    settings.mail_acme_api_token = ""
    settings.mail_acme_session_secret_ref = ""
    settings.mail_acme_session = ""
    settings.mail_acme_owner = ""
    settings.mail_nova_kind = "individual"
    settings.mail_acme_kind = "individual"
    settings.mail_send_enabled = False
    settings.mail_acme_send_token_secret_ref = ""
    settings.mail_acme_send_token = ""
    settings.mail_timeout_seconds = 10.0
    settings.mail_inbox_default_limit = 50
    settings.mail_ingest_enabled = False
    settings.mail_ingest_secret_ref = ""
    settings.mail_ingest_secret = ""
    settings.mail_ingest_hmac_secret_ref = ""
    settings.mail_ingest_hmac_secret = ""
    settings.mail_ingest_replay_window_seconds = 300
    settings.mail_ingest_service_user_id = "00000000-0000-4000-8000-000000000001"
    settings.mail_pull_ingest_enabled = False
    settings.mail_multi_account_enabled = False
    settings.collector_api_enabled = False
    # 위임 루프 flag 2종은 2026-07-17 owner 결정으로 default ON이다. 일부 테스트가
    # monkeypatch 없이 singleton을 직접 False로 바꾸므로(예: kill-switch 회귀),
    # 여기서 새 default로 명시 복원해 순서 의존 오염을 막는다.
    settings.agent_task_enabled = True
    settings.agent_gateway_actions_enabled = True
    settings.federation_service_token = ""
    settings.federation_service_user_id = "00000000-0000-4000-8000-000000000001"
    settings.embedding = "mock"
    # 테스트는 memory secret backend만 쓴다(docs/operations.md §3.1) — auto로
    # 두면 운영자 Mac에서 keychain의 실제 secret(orthus/kg/password 등)이
    # settings override보다 우선해 테스트가 prod credential로 연결을 시도한다.
    settings.secret_backend = "memory"
    # KG는 테스트 기본 상태가 off다 — KG 테스트는 kg_clean fixture로 명시 opt-in
    # (docs/kg-implementation-spec.md §2.2/§3.4).
    settings.kg_enabled = False
    settings.kg_uri = "bolt://127.0.0.1:7687"
    settings.kg_user = "neo4j"
    settings.kg_password = ""
    settings.kg_owner_scope_enabled = False
    settings.kg_query_timeout_ms = 2000
    settings.kg_query_limit = 50
    # decompose/command-split은 테스트 기본 상태가 off(fail-closed default)다 —
    # prod 활성화(2026-07-05)로 로컬 .env/node.env가 true를 실어도 스위트 기대가
    # 흔들리지 않게 여기서 고정한다. flag-on 테스트는 개별 fixture/monkeypatch로 opt-in.
    settings.ask_decompose_enabled = False
    settings.ask_command_split_enabled = False
    settings.ask_decompose_command_guard = True
    close_kg_driver()
    reset_kg_force_disabled()  # 테스트 격리 — L3 강제-off 래치는 close와 분리(코드리뷰)
    # One TRUNCATE statement replaces ~70 DELETE round-trips for every DB test.
    # Do not use CASCADE: metadata must explicitly account for every application
    # table.  Likewise, omit RESTART IDENTITY to preserve the previous DELETE
    # fixture's sequence behavior.
    _truncate_data_tables()
    # Wiping the allowlist invalidates the process-level bootstrap cache.
    _bootstrap_done.clear()
    yield


@pytest.fixture(scope="session")
def kg():
    """neo4j-test(:7688) driver — 미기동이면 skip (`pg()` 패턴 동형).

    CI 등에서 컨테이너를 필수로 강제하려면 ORTHUS_KG_TEST_REQUIRED=1로 skip을
    fail로 바꾼다(K2에서 CI service container와 함께 사용 예정)."""
    import os

    import neo4j

    driver = neo4j.GraphDatabase.driver(_KG_TEST_URI, auth=("neo4j", _KG_TEST_PASSWORD))
    try:
        driver.verify_connectivity()
    except Exception:
        driver.close()
        if os.environ.get("ORTHUS_KG_TEST_REQUIRED") == "1":
            raise
        pytest.skip("neo4j-test not running on 127.0.0.1:7688 (run `make kg-test-up`)")
    yield driver
    with driver.session(database="neo4j") as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
    driver.close()


@pytest.fixture
def kg_clean(kg):
    """테스트 전 그래프 전체 wipe + KG 설정을 test 컨테이너로 고정.

    설정 override 덕에 테스트 코드가 실수로 운영 포트(7687)를 보는 일이
    구조적으로 불가능하다. teardown에서 fail-closed 기본값으로 되돌린다."""
    with kg.session(database="neo4j") as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
    # DETACH DELETE는 노드/관계만 지우고 constraint(스키마)는 남지만, fresh 컨테이너
    # 첫 실행에는 :OutboxApplied UNIQUE 같은 constraint가 아직 없다. apply가 IF NOT EXISTS라
    # 멱등이므로 매 테스트 부트해 DB-level 마커 idempotency를 보장한다(app-level 가드 단독 아님).
    from orthus.kg.bootstrap import apply_schema

    apply_schema(kg)
    settings = get_settings()
    close_kg_driver()
    settings.kg_enabled = True
    settings.kg_uri = _KG_TEST_URI
    settings.kg_user = "neo4j"
    settings.kg_password = _KG_TEST_PASSWORD
    yield kg
    close_kg_driver()
    settings.kg_enabled = False
    settings.kg_uri = "bolt://127.0.0.1:7687"
    settings.kg_password = ""


@pytest.fixture
def user_id(clean) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Demo User"))
        s.commit()
    return uid
