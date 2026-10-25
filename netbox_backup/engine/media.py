"""
Writing media and scripts back. Media can't be switched like the database, so restore writes the set's files
into the live storage and moves files the set doesn't have to .nbb-quarantine/<restore_id>/. A safety copy of the
current files is taken first, so rollback can put them back.

Every member name is checked before anything is written: no absolute paths, no parent-directory components, no
links or devices (tarfile's data filter rules). Filesystem storages get atomic per-file replaces; other storages
(S3, blob) are written through Django storage.
"""
import contextlib
import os
import shutil
import tarfile
import tempfile

from django.core.files import File
from django.core.files.storage import FileSystemStorage

from . import components

QUARANTINE = '.nbb-quarantine'


class MediaRestoreError(Exception):
    pass


def safe_name(name):
    name = name.replace('\\', '/')
    if not name or name.startswith('/') or any(part in ('..', '') for part in name.split('/')) \
            or name.split('/')[0] == QUARANTINE:
        raise MediaRestoreError(f"Refusing archive member {name!r}")
    return name


def open_archive(path):
    import zstandard
    raw = open(path, 'rb')
    reader = zstandard.ZstdDecompressor().stream_reader(raw, closefd=True)
    return tarfile.open(fileobj=reader, mode='r|')


def archive_members(path):
    names = []
    with open_archive(path) as tar:
        for member in tar:
            tarfile.data_filter(member, '/nonexistent-root')  # raises for links, devices, absolute or escaping paths
            if member.isfile():
                names.append(safe_name(member.name))
    return names


def snapshot_storage(component, dest, keyring):
    """
    The safety copy of the storage's current files: a zstd tarball, gpg-encrypted to the set recipients, so nothing
    sits on the spool in plaintext until finalize. Returns the file count.
    """
    tree = components.StorageTree(component, ())
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as out:
        gpg = keyring.encrypt_process(stdout=out)
        stream = tree.source.open()
        try:
            shutil.copyfileobj(stream, gpg.stdin, 1024 * 1024)
        finally:
            gpg.stdin.close()
        tree.source.close()
        keyring.check_encryption(gpg)
    return tree.source.files


def restore_encrypted_archive(component, path, restore_id, keyring, log=None):
    """
    Decrypt an encrypted safety copy into a temporary file, write it back, and remove the plaintext again.
    """
    from ..core import sets
    plain = sets.decrypt_file(keyring, path, path.removesuffix('.enc'))
    try:
        return restore_archive(component, plain, restore_id, log=log)
    finally:
        os.unlink(plain)


def _write(storage, name, fileobj):
    if isinstance(storage, FileSystemStorage):
        path = storage.path(name)
        root = os.path.realpath(storage.location)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        parent = os.path.realpath(os.path.dirname(path))
        if parent != root and not parent.startswith(root + os.sep):
            raise MediaRestoreError(f"{name} resolves outside {root} (symlinked directory?)")
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.nbb-')
        try:
            with os.fdopen(fd, 'wb') as out:
                shutil.copyfileobj(fileobj, out, 1024 * 1024)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return
    if storage.exists(name):
        storage.delete(name)
    saved = storage.save(name, File(fileobj, name=os.path.basename(name)))
    if saved != name:
        raise MediaRestoreError(f"Storage saved {name} as {saved}")


def _quarantine(storage, name, restore_id):
    target = f'{QUARANTINE}/{restore_id}/{name}'
    if isinstance(storage, FileSystemStorage):
        os.renames(storage.path(name), storage.path(target))
        return
    with storage.open(name, 'rb') as f:
        storage.save(target, File(f, name=os.path.basename(name)))
    storage.delete(name)


def restore_archive(component, archive, restore_id, log=None):
    """
    Write every file of archive into the component's storage, then quarantine files the archive doesn't have.
    Returns {'written': n, 'quarantined': n}.
    """
    storage = components.get_storage(component)
    expected = set(archive_members(archive))  # validates every member before the first write
    written = 0
    with open_archive(archive) as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = safe_name(member.name)
            _write(storage, name, tar.extractfile(member))
            written += 1
    quarantined = 0
    for name in list(components.walk(storage)):
        if name not in expected:
            _quarantine(storage, name, restore_id)
            quarantined += 1
    if log:
        log.info(f"{component}: wrote {written} file(s), quarantined {quarantined} not in the set")
    return {'written': written, 'quarantined': quarantined}
