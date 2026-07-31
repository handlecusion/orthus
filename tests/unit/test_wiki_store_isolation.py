"""wiki-store 격리가 Settings 캐시 무효화에 살아남는지 고정한다.

conftest의 `_isolate_wiki_store`는 세션 내내 wiki markdown SoR 루트를 tmp로 돌린다.
과거에는 캐시된 Settings 인스턴스의 필드를 갈아끼웠는데, 어느 테스트든
`get_settings.cache_clear()`를 부르면(예: tests/unit/test_model_orchestration.py)
격리가 죽고 새 Settings가 **기본값 = 저장소 루트 `wiki-store/`** 를 들고 왔다.
그 뒤 모든 wiki 쓰기가 개발자 작업 트리로 갔다. 잔재는 .gitignore 때문에
`git status`에 보이지도 않고, WikiTask는 markdown이 유일한 SoR이라 DB truncate로도
지워지지 않아 **다음 실행까지 남아** 빈 저장소를 가정하는 테스트를 깨뜨렸다.

이 테스트가 깨지면 그 격리가 다시 죽은 것이다.
"""

from __future__ import annotations

from pathlib import Path

from orthus.settings import _REPO_ROOT, get_settings


def test_wiki_store_survives_settings_cache_clear() -> None:
    get_settings.cache_clear()  # 최악의 경우(임의 지점 캐시 무효화)를 재현한다

    root = Path(get_settings().wiki_store_path).resolve()

    assert "wiki-store" in root.name
    assert not root.is_relative_to(Path(_REPO_ROOT).resolve())  # 저장소 밖이어야 한다
