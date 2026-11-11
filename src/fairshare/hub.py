"""The hub: one folder that makes several accounts one assistant. Provider-neutral; a provider
says only which items are shared (`Provider.shared`, a name ending in "/" is a folder).

    <hub>/shared/          the shared items
    <hub>/accounts/<id>/   one config folder per account: its own login and state, and a RELATIVE
                           link for every shared item (projects -> ../../shared/projects), so the
                           whole hub can be moved
    <hub>/backups/         whatever fairshare moves aside. fairshare never deletes.

Nothing here writes outside the hub except `seed`, which only reads the user's folder."""
from __future__ import annotations

import difflib
import filecmp
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import store


def shared_dir(hub: Path) -> Path:
    return hub / "shared"


def account_dir(hub: Path, account_id: str) -> Path:
    return hub / "accounts" / account_id


def lock(hub: Path):
    """Held while the hub's links or shared items change: two launches must not repair at once."""
    return store.locked(hub / ".lock")


def backup_dir(hub: Path, label: str) -> Path:
    """A new folder name under backups/ for what one run moves aside (created when used)."""
    return store.free_name(hub / "backups" / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}")


def _items(prov) -> list[tuple[str, bool]]:
    return [(i.rstrip("/"), i.endswith("/")) for i in prov.shared]


def _same(a: Path, b: Path) -> bool:
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    return a.is_file() and b.is_file() and filecmp.cmp(a, b, shallow=False)


def _suffixed(p: Path, label: str, n: int) -> Path:
    tag = f"from-{label}" + (f"-{n}" if n > 1 else "")
    return p.with_name(f"{p.stem}.{tag}{p.suffix}")


def _place(src: Path, dest: Path, label: str, move: bool) -> bool:
    """Put the file (or link) `src` at `dest` in shared/. A clash keeps both: the incoming one
    takes a name naming `label`. An identical file is not placed twice. True when placed."""
    target, n = dest, 1
    while os.path.lexists(target):
        if _same(src, target):
            return False
        target, n = _suffixed(dest, label, n), n + 1
    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        os.rename(src, target)
    elif src.is_symlink():
        os.symlink(os.readlink(src), target)
    else:                                    # a copy keeps the times: sessions sort by them
        fd, tmp = tempfile.mkstemp(dir=target.parent)
        os.close(fd)
        shutil.copy2(src, tmp)
        os.replace(tmp, target)
    return True


def _merge_tree(src: Path, dest: Path, label: str, move: bool) -> int:
    """Every file under `src` into `dest` by `_place`; returns how many were placed."""
    placed = 0
    for root, dirs, files in os.walk(src):
        here = Path(root)
        links = [d for d in dirs if (here / d).is_symlink()]       # a linked folder is an entry
        dirs[:] = [d for d in dirs if d not in links]
        for name in sorted(files + links):
            placed += _place(here / name, dest / here.relative_to(src) / name, label, move)
    return placed


def _append_missing(src: Path, dest: Path) -> int:
    """Append to `dest` the lines of `src` it lacks, in their order; returns how many."""
    have = dest.read_bytes() if dest.exists() else b""
    seen, add = set(have.splitlines()), []
    for line in src.read_bytes().splitlines():
        if line and line not in seen:
            seen.add(line)
            add.append(line)
    if add:
        with open(dest, "ab") as f:
            f.write((b"\n" if have and not have.endswith(b"\n") else b"") + b"\n".join(add) + b"\n")
    return len(add)


def _write_bytes(dest: Path, data: bytes, mode_from: Path | None = None) -> None:
    """Write a new file atomically (through a symlink, never over it)."""
    if dest.is_symlink():
        dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    if mode_from is not None:
        os.chmod(tmp, mode_from.stat().st_mode & 0o777)
    os.replace(tmp, dest)


def _diff(prov, item: str, ours: bytes, theirs: bytes, theirs_name: str) -> str:
    """The differences the user is shown, values that may be secrets hidden by the provider."""
    try:
        a = prov.masked(item, ours.decode("utf-8"))
        b = prov.masked(item, theirs.decode("utf-8"))
    except UnicodeDecodeError:
        return "    (not text: no diff shown)\n"
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                        f"shared/{item}", theirs_name))


def check(hub: Path, account_id: str, prov) -> list[str]:
    """The start check, run before every launch: make the account's links right, never losing
    content. Returns one line per item it changed (a diff follows the line where one is due).
    - missing link: created (all new links of one run are reported in one line);
    - a real folder: its files are merged into shared/ (a clash keeps both, the incoming one
      named after the account), then the emptied folder goes to backups/ and the link is made;
    - a real `.jsonl` file: shared/ gets the lines it lacks, in order; the file goes to backups/;
    - another real file: moved into shared/ when shared/ has none; else, identical or not, it
      goes to backups/ and shared/'s is kept (a difference is shown);
    - a link pointing anywhere else (absolute links included): the link goes to backups/.
    Raises OSError when the hub cannot be changed; nothing is lost when it does."""
    shared, acct = shared_dir(hub), account_dir(hub, account_id)
    notes, linked, aside = [], [], None

    def put_aside(p: Path) -> Path:
        nonlocal aside
        aside = aside or backup_dir(hub, account_id)
        aside.mkdir(parents=True, exist_ok=True)
        dest = store.free_name(aside / p.name)
        os.rename(p, dest)
        return dest

    with lock(hub):
        acct.mkdir(parents=True, exist_ok=True)
        for name, is_dir in _items(prov):
            if is_dir:
                (shared / name).mkdir(parents=True, exist_ok=True)
        for name, is_dir in _items(prov):
            link, want, ours = acct / name, f"../../shared/{name}", shared / name
            what = f"{account_id}: {name}{'/' if is_dir else ''}"
            if link.is_symlink():
                if os.readlink(link) == want:
                    continue
                target = os.readlink(link)
                notes.append(f"{what} pointed to {target}; that link is now in "
                             f"{put_aside(link)}")
            elif not os.path.lexists(link):
                linked.append(name)
            elif is_dir != link.is_dir():
                notes.append(f"{what} was a {'file' if is_dir else 'folder'}; moved to "
                             f"{put_aside(link)}")
            elif is_dir:
                n = _merge_tree(link, ours, account_id, move=True)
                notes.append(f"{what} was a real folder: {n} file{'s' * (n != 1)} merged into "
                             f"the shared one; the rest is in {put_aside(link)}")
            elif name.endswith(".jsonl"):
                n = _append_missing(link, ours)
                notes.append(f"{what} was a real file: {n} line{'s' * (n != 1)} added to the "
                             f"shared one; the file is in {put_aside(link)}")
            elif not os.path.lexists(ours):
                os.rename(link, ours)
                notes.append(f"{what} was a real file and the hub had none: it is now the "
                             "shared one")
            elif _same(link, ours):
                notes.append(f"{what} was a real copy of the shared one; moved to "
                             f"{put_aside(link)}")
            else:
                diff = _diff(prov, name, ours.read_bytes(), link.read_bytes(),
                             f"{account_id}'s {name} (set aside)")
                notes.append(f"{what} differed from the shared one, which is kept; this "
                             f"account's copy is in {put_aside(link)}:\n{diff}".rstrip("\n"))
            os.symlink(want, link)
    if linked:
        notes.insert(0, f"{account_id}: linked {', '.join(linked)}")
    return notes


def seed(hub: Path, source: Path, account_id: str, prov,
         prepare: dict[str, object] | None = None) -> list[str]:
    """Copy the shared items of the user's folder `source` into shared/, never writing `source`.
    Folders merge as in `check` (a clash keeps both); `.jsonl` files add the lines shared/ lacks;
    another file is copied when shared/ has none (`prepare[name]`, when given, turns its text into
    what is stored and raises ValueError for one it cannot use; a shared file equal to what it
    makes of an empty text holds nothing of the user's and counts as none), and otherwise the
    first seed wins and a difference is shown, not applied. Returns one line per item."""
    shared, notes, prepare = shared_dir(hub), [], prepare or {}
    skip = {i.rstrip("/") for i in prov.not_copied}
    with lock(hub):
        for name, is_dir in _items(prov):
            src, ours = source / name, shared / name
            if name in skip or not src.exists():
                continue
            if is_dir != src.is_dir():
                notes.append(f"{name}: not a {'folder' if is_dir else 'file'} in {source}; "
                             "not copied")
            elif is_dir:
                if n := _merge_tree(src, ours, account_id, move=False):
                    notes.append(f"{name}/: copied {n} file{'s' * (n != 1)}")
            elif name.endswith(".jsonl"):
                if n := _append_missing(src, ours):
                    notes.append(f"{name}: added {n} line{'s' * (n != 1)}")
            else:
                data = src.read_bytes()
                if name in prepare:
                    try:
                        data = prepare[name](data.decode("utf-8")).encode("utf-8")
                    except (UnicodeDecodeError, ValueError) as e:
                        notes.append(f"{name}: cannot be read in {source} ({e}); not copied")
                        continue
                if not ours.exists():
                    _write_bytes(ours, data, src)
                    notes.append(f"{name}: copied")
                elif name in prepare and ours.read_bytes() == prepare[name]("").encode("utf-8"):
                    store.write_with_backup(ours, data.decode("utf-8"),
                                            backup_dir(hub, f"seed-{account_id}"))
                    notes.append(f"{name}: copied (the hub's held only what fairshare adds)")
                elif ours.read_bytes() != data:
                    diff = _diff(prov, name, ours.read_bytes(), data, f"{source}/{name}")
                    notes.append(f"{name}: differs from the hub's, which is kept (the first "
                                 f"folder copied wins); not applied:\n{diff}".rstrip("\n"))
    return notes


def edit_shared(hub: Path, path: Path, change, label: str) -> str | None:
    """Apply `change` (current text or None -> new text or None for no change; may raise
    ValueError) to the shared file `path`, under the hub's lock: read, change, back up into
    backups/, write atomically, read back. Returns what happened, or None when nothing changed.
    A file that cannot be read or decoded is refused, never taken for absent."""
    with lock(hub):
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else None
        except (OSError, UnicodeDecodeError) as e:
            return f"{path} cannot be read ({e}); left as it is"
        try:
            new = change(text)
        except ValueError as e:
            return f"{path}: {e}; left as it is"
        if new is None:
            return None
        backup = store.write_with_backup(path, new, backup_dir(hub, label))
        return f"updated {path}" + (f" (backup: {backup})" if backup else "")
