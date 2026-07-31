# 테스트 격리 결함 — `get_settings.cache_clear()`가 wiki-store 격리를 무력화한다

> 상태: **원인 확정, 미수정.** 워크트리 `.worktrees/test-isolation`,
> 브랜치 `fix/test-isolation-wiki-store` (origin/main 기준).
> 작성 2026-07-13. 이 문서는 인수인계용이며, 수정 PR과 함께 정리하거나 삭제해도 된다.

## 1. 한 줄 요약

`tests/unit/test_model_orchestration.py`의 autouse 픽스처가 **`get_settings.cache_clear()`**
를 호출하는데, 이것이 `tests/conftest.py`의 **세션 스코프 wiki-store 격리 픽스처를
무력화**한다. 그 결과 테스트가 **개발자의 실제 저장소 `wiki-store/` 디렉터리에 파일을 쓰고**,
그 잔재가 이후 테스트와 **다음 실행까지** 오염시킨다.

## 2. 증상

로컬에서 전체 스위트를 돌리면 두 통합 테스트가 깨진다. **같은 워크트리에서 반복 실행할수록
잘 깨진다**(잔재가 쌓이므로).

```
FAILED tests/integration/test_wiki_tasks_api.py::test_wiki_tasks_list_and_resolve
FAILED tests/integration/test_project_override.py::test_set_project_override_retags_traceable_wiki
```

```
tests/integration/test_wiki_tasks_api.py:33: in test_wiki_tasks_list_and_resolve
    assert listed.json()[0]["slug"] == "conflict-nova-status"
E   AssertionError: assert 'open-question' == 'conflict-nova-status'
```

**두 테스트 모두 단독 실행하면 통과한다.** 통합 스위트만(`pytest tests/integration`) 돌려도
통과한다. **유닛 + 통합을 함께** 돌릴 때만 깨진다.

## 3. 원인 (확정)

### 3.1 conftest의 격리 픽스처

`tests/conftest.py:203`

```python
@pytest.fixture(scope="session", autouse=True)
def _isolate_wiki_store(tmp_path_factory):
    settings = get_settings()                                   # ← 캐시된 Settings 인스턴스
    settings.wiki_store_path = tmp_path_factory.mktemp("wiki-store")   # ← 그 인스턴스를 변조
    yield
```

`get_settings`는 `@lru_cache`다. 이 픽스처는 **캐시된 객체의 필드를 갈아끼우는** 방식으로
격리한다 — 새 객체를 만들어 캐시에 넣는 게 아니다.

### 3.2 그것을 깨는 쪽

`tests/unit/test_model_orchestration.py:49` (PR #669에서 들어옴)

```python
@pytest.fixture(autouse=True)
def _pin_slot(monkeypatch):
    monkeypatch.setenv("ORTHUS_LLM", "mock")
    ...
    get_settings.cache_clear()     # ★ 여기서 격리가 죽는다
    yield
    get_settings.cache_clear()     # ★ 여기서도
```

`cache_clear()` 하는 순간 **변조된 Settings 인스턴스가 버려진다.** 다음 `get_settings()`는
**새 인스턴스**를 만들고, `wiki_store_path`는 **기본값 = 저장소 루트의 `wiki-store/`** 로
돌아간다.

### 3.3 그 뒤에 벌어지는 일

`orthus/wiki/store.py:569`

```python
def _store_root(root: Path | None) -> Path:
    return Path(root) if root is not None else get_settings().wiki_store_path
```

이 시점부터 세션의 **모든** wiki 읽기/쓰기가 **저장소 `wiki-store/`** 를 본다.

- **WikiTask에는 DB 테이블이 없다** — markdown 파일이 유일한 SoR이다(`wiki_pages`,
  `wiki_chunks`, `wiki_links`만 Postgres에 있다). 그래서 conftest의 테이블 truncate로는
  **절대 지워지지 않는다.**
- `test_wiki_tasks_api`는 태스크를 하나 쓴 뒤 `/wiki/tasks` 목록의 **첫 항목이 자기 것**이라
  단언한다 — 즉 **저장소가 비어 있다고 가정**한다. 앞선 테스트가 남긴 `open-question` 태스크가
  하나라도 있으면 깨진다.
- 파일은 **실행이 끝나도 남는다.** 다음 실행에서 또 깨진다.

### 3.4 증거

| 관찰 | 의미 |
|---|---|
| `pytest tests/integration` 단독 → **전부 통과** | 유닛이 없으면 `cache_clear`도 없다 |
| `pytest tests/unit` + 문제의 2건 → **재현** | 유닛이 격리를 깬다 |
| **clean origin/main에서 동일 재현** | A·B와 무관하다(#669는 이미 main) |
| 전체 스위트 후 저장소에 `wiki-store/company`, `wiki-store/personal`이 **재생성됨** | 쓰기가 tmp가 아니라 저장소로 갔다 |
| `rm -rf wiki-store` 후 전체 스위트 → **통과**(기존 `test_orthus_cli` 5건 외 0) | 잔재가 원인임을 확인 |

> **CI가 초록불인 이유:** 매번 새 체크아웃이라 잔재가 없다. 한 번의 실행 안에서 유령이
> 쌓이는 타이밍은 순서에 따라 갈리는데, CI 순서에서는 우연히 걸리지 않는다.
> **로컬에서만, 그것도 반복 실행할수록 잘 터진다.** 그래서 발견이 늦었다.

## 4. 왜 고쳐야 하나 (단순 flake가 아니다)

1. **테스트가 개발자의 작업 트리를 오염시킨다.** `wiki-store/`는 **git 추적 대상**이다
   (`git ls-files wiki-store` → 11개). 테스트를 돌리면 추적되지 않는 쓰레기 파일이 그 안에
   쌓인다. `deploy_public_api.sh`/`deploy_public_web.sh`는 deploy repo에 **uncommitted tracked
   변경**이 있으면 abort한다 — 같은 계열의 사고가 이미 v0.1.30에서 릴리스를 막은 적이 있다
   (AGENTS.md "Deploy directory" 참조).
2. **"로컬에서 두 번 돌리면 빨간불"은 사람을 오진하게 만든다.** 실제로 이번 작업에서 이
   현상을 **두 번 회귀로 오해**했다(무관한 PR을 의심했다).
3. **격리 픽스처가 있는데 안 듣는다**는 게 최악이다 — 있으니 믿게 되고, 실제로는 안 지킨다.

## 5. 수정 방향 (택일 — 판단 필요)

### 옵션 A — 격리 픽스처를 캐시 무효화에 견디게 만든다 (권장)

`_isolate_wiki_store`가 **캐시된 인스턴스를 변조**하는 대신, **환경변수**를 세팅한다.
그러면 `cache_clear()` 후 새로 만들어지는 Settings도 tmp 경로를 갖는다.

```python
@pytest.fixture(scope="session", autouse=True)
def _isolate_wiki_store(tmp_path_factory):
    root = tmp_path_factory.mktemp("wiki-store")
    os.environ["ORTHUS_WIKI_STORE_PATH"] = str(root)   # ← 새 Settings도 이걸 읽는다
    get_settings.cache_clear()
    yield
```

- **장점:** 근본 원인을 없앤다. 누가 어디서 `cache_clear()`를 하든 격리가 살아남는다.
- **확인됨:** `orthus/settings.py:81`
  ```python
  wiki_store_path: Path = Field(
      default=_REPO_ROOT / "wiki-store",                                    # ← 저장소 루트!
      validation_alias=AliasChoices("ORTHUS_WIKI_STORE", "ORTHUS_WIKI_STORE_PATH"),
  )
  ```
  둘 중 아무 env 이름이나 쓰면 된다. **기본값이 저장소 루트**라는 게 이 결함의 위험도를
  결정한다 — 격리가 죽는 순간 곧바로 작업 트리에 쓴다.
- **주의:** `monkeypatch`는 session 스코프에서 못 쓴다. `os.environ` 직접 조작 + teardown
  복원이 필요하다.

### 옵션 B — `test_model_orchestration`이 격리를 복원하게 한다

`cache_clear()` 후 `wiki_store_path`를 다시 tmp로 세팅한다.

- **장점:** diff가 작다.
- **단점:** **다음 사람이 또 밟는다.** `cache_clear()`를 쓰는 새 테스트가 생길 때마다
  같은 버그가 재발한다. 근본 원인이 아니라 증상을 막는 것이다.

### 옵션 C — 테스트가 빈 저장소를 가정하지 않게 한다

`test_wiki_tasks_api`가 `listed[0]`이 아니라 **자기 slug를 찾아서** 단언한다.

- **장점:** 테스트가 더 견고해진다(이건 A와 별개로 해도 좋다).
- **단점:** **오염 자체는 그대로다.** 저장소에 쓰레기 파일이 계속 쌓인다. 근본 해결 아님.

> **권장: A + C.** A가 근본 원인을 없애고, C는 "목록의 첫 항목"에 기대는 취약한 단언을
> 고친다. B는 하지 말 것.

## 6. 검증 방법 (수정 후 반드시)

```bash
# 0) 잔재부터 제거 — 안 그러면 수정해도 계속 깨진다
git status --short wiki-store          # 추적 파일 외에 뭐가 있는지 확인
rm -rf wiki-store && git checkout -- wiki-store

# 1) 격리가 실제로 듣는지 — 전체 스위트 후 저장소가 더럽혀지지 않아야 한다
ORTHUS_PG_DSN=postgresql+psycopg://orthus:orthus@localhost:5433/orthus_test_iso \
ORTHUS_PG_DSN_READONLY=postgresql+psycopg://orthus_ro:orthus_ro@localhost:5433/orthus_test_iso \
ORTHUS_EMBEDDING=mock ORTHUS_LLM=mock \
pytest tests/unit tests/integration -q

git status --short wiki-store           # ★ 반드시 비어 있어야 한다 (이게 진짜 회귀 테스트다)

# 2) 두 번 연속 돌려도 초록불이어야 한다 (잔재 누적 확인)
pytest tests/unit tests/integration -q  # 1회차
pytest tests/unit tests/integration -q  # 2회차 — 여기서 깨지면 못 고친 것이다
```

**기존 실패로 알려진 것 (이 결함과 무관):** `tests/unit/test_orthus_cli.py` 5건은
clean main에서도 실패한다(환경 의존). 이 작업 범위가 아니다.

## 7. 회귀 테스트 제안

수정과 함께 **"테스트가 저장소를 더럽히지 않는다"** 를 고정하는 테스트를 넣으면 좋다.
가장 단순한 형태:

```python
def test_wiki_store_is_isolated_from_the_repo(...):
    """격리가 죽으면 여기서 잡힌다 — cache_clear()를 하는 새 테스트가 생겨도."""
    from orthus.settings import get_settings
    get_settings.cache_clear()          # 최악의 경우를 재현한다
    root = get_settings().wiki_store_path
    assert "wiki-store" in str(root)
    assert not str(root.resolve()).startswith(str(REPO_ROOT.resolve()))  # 저장소 밖이어야 한다
```

## 8. 참고 (같은 계열의 다른 함정)

수정하는 김에 알아두면 좋은, **이 결함과 무관하지만 사람을 오진하게 만드는** 것들:

- **`ORTHUS_PG_DSN_READONLY`를 빼먹으면** structured 실행기가 **작업 DB(`orthus`)** 를 읽어
  0행을 낸다 → `test_structured` / `test_router`가 "회귀처럼" 깨진다. 코드 문제가 아니다.
  `Settings.pg_dsn_readonly`의 기본값이 `.../orthus`(작업 DB)를 가리킨다. `make test`는 둘 다
  세팅한다 — **개별 실행 시에만 함정.**
- 새 마이그레이션이 들어온 뒤에는 격리 테스트 DB를 올려야 한다:
  `TEST_DB=<db> bash scripts/setup_test_db.sh`. 안 하면 대량 ERROR가 난다.
