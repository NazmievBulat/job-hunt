"""Check every ZIP member before a parser can read any of them.

Only stored and deflate members have bounded reads in Python's zipfile. The byte
budgets are also counted while reading, including entries a caller will ignore.
Nothing is extracted to disk or retained after checking.
"""

from collections.abc import Generator
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass
from io import BytesIO
from typing import Literal
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo


@dataclass(frozen=True)
class ZipLimits:
    member_bytes: int
    total_bytes: int
    members: int = 1_000


DOCX_LIMITS = ZipLimits(member_bytes=20 * 1024 * 1024, total_bytes=50 * 1024 * 1024)
_CHUNK_BYTES = 64 * 1024


class ZipLimitExceeded(ValueError):
    """The ZIP exceeds a member, total byte, or member count budget."""

    def __init__(self, limit: Literal["member", "total", "count"]) -> None:
        self.limit = limit
        super().__init__(f"ZIP {limit} limit exceeded")


def _check_member(
    archive: ZipFile, info: ZipInfo, budget: int, limit: Literal["member", "total"]
) -> int:
    # ZipExtFile normally truncates at the claimed file_size. Give a copy our
    # budget plus read-ahead room instead, so a forged small size cannot hide the
    # real output. Ending at budget + 1 would make zipfile check the CRC of that
    # truncated prefix before we could report overflow. Reads remain bounded,
    # with one extra byte to detect overflow; a true EOF still checks the CRC.
    bounded = copy(info)
    bounded.file_size = max(info.file_size, budget + _CHUNK_BYTES + 1)
    size = 0
    with archive.open(bounded) as member:
        while chunk := member.read(min(_CHUNK_BYTES, budget - size + 1)):
            size += len(chunk)
            if size > budget:
                raise ZipLimitExceeded(limit)
    if size != info.file_size:
        raise BadZipFile("ZIP member size does not match its contents")
    return size


@contextmanager
def checked_zip(data: bytes, *, limits: ZipLimits | None = None) -> Generator[ZipFile]:
    """Yield an open ZIP only after all its members pass the bounded checks.

    Headers allow early refusal but never grant permission: output bytes are counted
    and CRCs checked before handing the unchanged archive to the caller.
    """
    caps = DOCX_LIMITS if limits is None else limits
    with ZipFile(BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > caps.members:
            raise ZipLimitExceeded("count")
        if len({info.header_offset for info in infos}) != len(infos):
            # Reject aliases before opening any member: zero-output deflate streams could
            # be decompressed repeatedly without consuming any output budget.
            raise BadZipFile("Overlapping ZIP members")
        for info in infos:
            if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
                raise ValueError("Unsupported ZIP compression")
        if sum(info.file_size for info in infos) > caps.total_bytes:
            raise ZipLimitExceeded("total")
        for info in infos:
            if info.file_size > caps.member_bytes:
                raise ZipLimitExceeded("member")
        total = 0
        for info in infos:
            remaining = caps.total_bytes - total
            limit = "total" if remaining <= caps.member_bytes else "member"
            total += _check_member(archive, info, min(caps.member_bytes, remaining), limit)
        yield archive
