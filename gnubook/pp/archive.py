"""Unpack uploaded archives (ZIP, TAR, TAR.GZ/BZ2/XZ) for the PDF import.

Only the documents PP's importers can read are taken (.pdf, .txt); directories, hidden files and macOS
resource forks are ignored. Limits on the number of members and the unpacked size protect against archive
bombs. Archives inside archives are not unpacked.
"""
from __future__ import annotations

import io
import posixpath
import tarfile
import zipfile

from ..i18n import gettext as _

DOC_SUFFIXES = (".pdf", ".txt")
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
ARCHIVE_SUFFIXES = (".zip",) + TAR_SUFFIXES
ACCEPT = ",".join(DOC_SUFFIXES + ARCHIVE_SUFFIXES) + ",application/pdf,application/zip,application/x-tar,application/gzip"

MAX_MEMBERS = 500                   # documents taken from all archives of one upload
MAX_TOTAL = 200 * 1024 * 1024       # unpacked bytes of all archives of one upload


class ArchiveError(Exception):
    pass


def is_archive(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def _wanted(member: str) -> bool:
    base = posixpath.basename(member.replace("\\", "/"))
    if not base or base.startswith(".") or "__MACOSX/" in member.replace("\\", "/"):
        return False
    return base.lower().endswith(DOC_SUFFIXES)


class _Budget:
    def __init__(self):
        self.count = 0
        self.size = 0

    def take(self, size: int):
        self.count += 1
        self.size += size
        if self.count > MAX_MEMBERS:
            raise ArchiveError(_("mehr als {a0} Dokumente", a0=MAX_MEMBERS))
        if self.size > MAX_TOTAL:
            raise ArchiveError(_("entpackt größer als {a0} MB", a0=MAX_TOTAL // (1024 * 1024)))


def _read_limited(fh, budget: _Budget) -> bytes:
    # the declared size may lie, so read at most what is left of the budget
    left = MAX_TOTAL - budget.size
    data = fh.read(left + 1)
    if len(data) > left:
        raise ArchiveError(_("entpackt größer als {a0} MB", a0=MAX_TOTAL // (1024 * 1024)))
    return data


def _unzip(data: bytes, budget: _Budget) -> list[tuple[str, bytes]]:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir() or not _wanted(info.filename):
                continue
            if info.flag_bits & 0x1:
                raise ArchiveError(_("verschlüsselte ZIP-Dateien werden nicht unterstützt"))
            with zf.open(info) as fh:
                content = _read_limited(fh, budget)
            budget.take(len(content))
            if content:
                out.append((info.filename, content))
    return out


def _untar(data: bytes, budget: _Budget) -> list[tuple[str, bytes]]:
    out = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for m in tf:
            if not m.isfile() or not _wanted(m.name):
                continue
            fh = tf.extractfile(m)
            if fh is None:
                continue
            content = _read_limited(fh, budget)
            budget.take(len(content))
            if content:
                out.append((m.name, content))
    return out


def expand(files: list[tuple[str, bytes]]) -> tuple[list[tuple[str, bytes]], list[tuple[str, str]]]:
    """Replace archives in `files` by the documents they contain.

    Returns (documents, problems); problems are (archive name, message) for archives that could not be read
    or held no document. Documents from an archive are named "<archive>/<path in archive>".
    """
    budget = _Budget()
    docs: list[tuple[str, bytes]] = []
    problems: list[tuple[str, str]] = []
    for name, data in files:
        if not is_archive(name):
            docs.append((name, data))
            continue
        try:
            members = _unzip(data, budget) if name.lower().endswith(".zip") else _untar(data, budget)
        except ArchiveError as exc:
            problems.append((name, str(exc)))
            continue
        except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, ValueError) as exc:
            problems.append((name, _("Archiv nicht lesbar ({a0})", a0=exc)))
            continue
        if not members:
            problems.append((name, _("enthält keine PDF- oder TXT-Dateien")))
        docs.extend((f"{name}/{member}", content) for member, content in members)
    return docs, problems
