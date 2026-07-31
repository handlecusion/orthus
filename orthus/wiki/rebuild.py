"""`python -m orthus.wiki.rebuild` — re-author the LLM wiki from all corpus
documents (backs `make wiki-rebuild`).

Distill/compile uses the configured chat model (`ORTHUS_LLM`). Wiki embedding is
only the retrieval index slot (`ORTHUS_EMBEDDING`) and remains independently
configured.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from sqlalchemy import delete

from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import embeddings, wiki_chunks, wiki_links, wiki_pages
from orthus.wiki.author import rebuild_all, rebuild_all_parallel


def _clean_wiki_layer() -> None:
    settings = get_settings()
    root = settings.wiki_store_path.resolve()
    if root == (Path.cwd() / "wiki-store").resolve():
        raise RuntimeError(f"refuse to clean repo wiki-store: {root}")

    with session() as s:
        s.execute(delete(wiki_links))
        s.execute(delete(wiki_chunks))
        s.execute(delete(embeddings).where(embeddings.c.kind == "wiki_chunk"))
        s.execute(delete(wiki_pages))
        s.commit()

    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    print(f"wiki clean complete: {root}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-author the LLM wiki from documents.")
    parser.add_argument("--clean", action="store_true", help="clear wiki DB mirror and wiki-store")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("ORTHUS_WIKI_REBUILD_CONCURRENCY", "1")),
        help="parallel LLM distill workers; writes remain serial",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="skip failed distill docs and rebuild the rest",
    )
    parser.add_argument(
        "--skip-authored",
        action="store_true",
        help="resume by skipping docs that already have wiki source files",
    )
    parser.add_argument(
        "--source-prefix",
        default=None,
        help="only author documents whose source starts with this prefix (e.g. 'mail')",
    )
    args = parser.parse_args()

    if args.clean:
        _clean_wiki_layer()

    # --skip-authored / --continue-on-error are only honored by the parallel
    # path, so route there whenever either is set even at concurrency=1
    # (otherwise the flags were silently ignored and a full rebuild ran).
    if args.concurrency > 1 or args.skip_authored or args.continue_on_error:
        totals = rebuild_all_parallel(
            concurrency=max(1, args.concurrency),
            continue_on_error=args.continue_on_error,
            skip_authored=args.skip_authored,
            source_prefix=args.source_prefix,
        )
    else:
        totals = rebuild_all(source_prefix=args.source_prefix)
    print("wiki rebuild complete: " + ", ".join(f"{k}={v}" for k, v in totals.items()))


if __name__ == "__main__":
    main()
