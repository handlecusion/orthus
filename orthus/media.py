"""대시보드 미디어(이미지/동영상/파일) 로컬 파일 저장.

노션식 본문/카드 미디어를 base64가 아닌 실파일로 보관해 동영상까지 다룬다.
파일명은 unguessable uuid4 hex + 검증된 확장자이고, capability URL
(`/dashboard/media/{filename}`)로 서빙한다 — 파일명 자체가 접근 토큰이라 별도 인증
없이 `<img>`/`<video>`가 바로 로드된다(company-node 내부 미디어 한정). 업로드는
owner/admin만(라우트에서 가드). 경로 traversal은 strict 파일명 정규식 + root 하위
검증으로 막는다.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from orthus.settings import get_settings

# content-type → 확장자 (업로드 시 확장자 추론 보강).
_EXT_BY_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/heic": "heic",
    "image/avif": "avif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/ogg": "ogv",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/ogg": "oga",
    "application/pdf": "pdf",
}

# 확장자 → content-type (서빙 시). image/video/audio/pdf만 inline 렌더, 그 외는 다운로드.
_TYPE_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "heic": "image/heic",
    "avif": "image/avif",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "ogv": "video/ogg",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "oga": "audio/ogg",
    "pdf": "application/pdf",
}

# 저장 파일명: uuid4 hex(32) + 소문자/숫자 확장자(2~6).
_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{2,6}$")


def _safe_ext(filename: str | None, content_type: str | None) -> str:
    """원본 파일명 확장자 우선, 없으면 content-type에서 추론, 그래도 없으면 'bin'."""
    if filename and "." in filename:
        raw = re.sub(r"[^a-z0-9]", "", filename.rsplit(".", 1)[-1].lower())[:6]
        if raw:
            return raw
    if content_type:
        ext = _EXT_BY_TYPE.get(content_type.split(";")[0].strip().lower())
        if ext:
            return ext
    return "bin"


def content_type_for(filename: str) -> str:
    """서빙용 content-type. 알 수 없는 확장자는 octet-stream(다운로드)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _TYPE_BY_EXT.get(ext, "application/octet-stream")


def is_inline_type(content_type: str) -> bool:
    """브라우저에 그대로 렌더할 타입(이미지/동영상/오디오/pdf)인가."""
    return (
        content_type.startswith(("image/", "video/", "audio/")) or content_type == "application/pdf"
    )


def store_upload(data: bytes, filename: str | None, content_type: str | None) -> dict:
    """업로드 바이트를 미디어 루트에 저장하고 메타를 돌려준다.

    raise ValueError: 빈 파일 / 용량 초과(라우트에서 413/422로 변환).
    """
    settings = get_settings()
    if not data:
        raise ValueError("empty file")
    if len(data) > settings.media_max_bytes:
        raise ValueError("file too large")
    ext = _safe_ext(filename, content_type)
    name = f"{uuid.uuid4().hex}.{ext}"
    root = Path(settings.media_store_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(data)
    served_type = content_type_for(name)
    return {
        "filename": name,
        "url": f"/dashboard/media/{name}",
        "name": (filename or name).strip() or name,
        "content_type": served_type,
        "size": len(data),
    }


def resolve_media(filename: str) -> tuple[Path, str]:
    """서빙용: 파일명 검증 + root 하위 실존 확인 → (경로, content-type).

    raise LookupError: 잘못된 파일명 / traversal / 미존재.
    """
    if not _FILENAME_RE.match(filename):
        raise LookupError("invalid media name")
    root = Path(get_settings().media_store_path).resolve()
    path = (root / filename).resolve()
    if path.parent != root:  # traversal 차단
        raise LookupError("invalid media path")
    if not path.is_file():
        raise LookupError("media not found")
    return path, content_type_for(filename)


def delete_media(filename: str) -> bool:
    """저장된 미디어 실파일을 best-effort로 지운다(회의 첨부 삭제 등 명시적 제거용).

    파일명 검증 + traversal 차단 후 root 하위 파일만 unlink한다. 이미 없거나 잘못된
    이름이면 조용히 False. DB 매핑을 지운 뒤(커밋 후) 호출하는 게 안전하다 — 파일만
    남는 orphan은 무해하지만, 반대로 살아 있는 row의 파일을 지우면 안 되므로.
    """
    if not _FILENAME_RE.match(filename):
        return False
    root = Path(get_settings().media_store_path).resolve()
    path = (root / filename).resolve()
    if path.parent != root:  # traversal 차단
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# 청크 업로드 (대용량 동영상)
#
# 단일 요청 업로드는 (1) 요청 바디 전체를 RAM에 올리고 (2) Cloudflare의 요청
# 바디 상한(~100MB)에 걸린다. 클라이언트가 파일을 파트로 쪼개 순차 PUT하면
# 서버는 임시 파일에 append만 하고, complete 시 미디어 루트로 원자 rename한다.
# 파트 단위 진행이 그대로 업로드 진행률/남은시간이 된다.
#
# 상태는 전부 디스크(incomplete/{id}.part + {id}.json)라 프로세스 재시작에도
# 임시 파일이 남지 않도록 init 시 24시간 지난 잔여물을 기회적으로 청소한다.
# --------------------------------------------------------------------------

_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_STALE_UPLOAD_SECONDS = 24 * 3600


def _incomplete_root() -> Path:
    root = Path(get_settings().media_store_path) / "incomplete"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sweep_stale_uploads(root: Path) -> None:
    cutoff = time.time() - _STALE_UPLOAD_SECONDS
    try:
        for p in root.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def _upload_paths(upload_id: str) -> tuple[Path, Path]:
    if not _UPLOAD_ID_RE.match(upload_id):
        raise LookupError("invalid upload id")
    root = _incomplete_root()
    return root / f"{upload_id}.part", root / f"{upload_id}.json"


def chunk_init(filename: str | None, content_type: str | None) -> str:
    """청크 업로드 세션 생성 → upload_id. 잔여 임시파일을 기회적으로 청소한다."""
    root = _incomplete_root()
    _sweep_stale_uploads(root)
    upload_id = uuid.uuid4().hex
    part, meta = _upload_paths(upload_id)
    part.write_bytes(b"")
    meta.write_text(
        json.dumps(
            {
                "filename": (filename or "").strip()[:255],
                "content_type": (content_type or "").strip()[:120],
                "next_index": 0,
            }
        )
    )
    return upload_id


def _read_meta(upload_id: str) -> tuple[Path, Path, dict]:
    part, meta = _upload_paths(upload_id)
    if not part.is_file() or not meta.is_file():
        raise LookupError("upload not found")
    return part, meta, json.loads(meta.read_text())


def chunk_append(upload_id: str, index: int, data: bytes) -> int:
    """파트를 순차 append. 반환값은 지금까지 받은 총 바이트.

    raise LookupError: 미존재 세션 / ValueError: 순서 불일치·빈 파트·용량 초과.
    """
    part, meta, info = _read_meta(upload_id)
    if not data:
        raise ValueError("empty part")
    expected = int(info.get("next_index", 0))
    if index != expected:
        raise ValueError(f"part index mismatch: expected {expected}")
    received = part.stat().st_size
    if received + len(data) > get_settings().media_max_bytes:
        # 초과분은 세션째 정리 — 클라이언트는 413을 받고 새로 시작한다.
        part.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        raise ValueError("file too large")
    with part.open("ab") as f:
        f.write(data)
    info["next_index"] = expected + 1
    meta.write_text(json.dumps(info))
    return received + len(data)


def chunk_complete(upload_id: str) -> dict:
    """조립 완료: 미디어 루트로 원자 rename하고 `store_upload`와 같은 메타 반환."""
    part, meta, info = _read_meta(upload_id)
    size = part.stat().st_size
    if size <= 0:
        part.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        raise ValueError("empty file")
    orig_name = info.get("filename") or None
    ext = _safe_ext(orig_name, info.get("content_type") or None)
    name = f"{uuid.uuid4().hex}.{ext}"
    root = Path(get_settings().media_store_path)
    root.mkdir(parents=True, exist_ok=True)
    part.rename(root / name)
    meta.unlink(missing_ok=True)
    served_type = content_type_for(name)
    return {
        "filename": name,
        "url": f"/dashboard/media/{name}",
        "name": (orig_name or name).strip() or name,
        "content_type": served_type,
        "size": size,
    }


def chunk_abort(upload_id: str) -> None:
    """세션 취소 — 임시 파일 제거(미존재도 조용히 성공: 멱등 취소)."""
    try:
        part, meta = _upload_paths(upload_id)
    except LookupError:
        return
    part.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
