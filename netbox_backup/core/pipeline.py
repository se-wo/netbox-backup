"""
The streaming pipeline: source -> gpg --encrypt -> SHA-384 + backend checksum -> create-only writer.

Nothing is staged on local disk. Plaintext is read from the source by a feeder thread and written to gpg and to
any taps (e.g. the dump checker); ciphertext is read from gpg in the calling thread, hashed and uploaded. An object
is committed only when the source exited 0, gpg reported END_ENCRYPTION with AES-256 and every tap passed;
otherwise the upload is aborted, so a truncated dump never becomes a valid object.
"""
import contextlib
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import threading
from dataclasses import dataclass, field

CHUNK = 1024 * 1024


class PipelineError(Exception):
    pass


# Sources: open() returns a readable binary stream; close() raises if producing it failed.

class BytesSource:
    def __init__(self, data):
        self.data = data

    def open(self):
        return io.BytesIO(self.data)

    def close(self):
        pass

    def abort(self):
        pass


class CommandSource:
    """
    stdout of a subprocess, e.g. pg_dump. Fails if the command exits non-zero.
    """
    def __init__(self, args, env=None, name=None):
        self.args = args
        self.env = env
        self.name = name or os.path.basename(args[0])
        self.proc = None

    def open(self):
        self.stderr = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(self.args, stdout=subprocess.PIPE, stderr=self.stderr, env=self.env)
        return self.proc.stdout

    def close(self):
        self.proc.stdout.close()
        code = self.proc.wait()
        self.stderr.seek(0)
        message = self.stderr.read().decode(errors='replace').strip()
        self.stderr.close()
        if code != 0:
            raise PipelineError(f"{self.name} exited with status {code}: {message[-2000:]}")
        self.messages = message

    def abort(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


class TarSource:
    """
    A zstd-compressed tarball written by one writer thread into an OS pipe. entries is an iterable of
    (arcname, size, opener) where opener() returns a readable binary file. Members are stored with uid/gid 0 and
    no owner names, as Enterprise does.
    """
    def __init__(self, entries, level=3, on_member=None):
        self.entries = entries
        self.level = level
        self.on_member = on_member
        self.error = None
        self.files = 0
        self.bytes = 0

    def open(self):
        import zstandard
        read_fd, write_fd = os.pipe()
        self.reader = os.fdopen(read_fd, 'rb')
        writer = os.fdopen(write_fd, 'wb')

        def produce():
            try:
                compressor = zstandard.ZstdCompressor(level=self.level, threads=-1)
                with compressor.stream_writer(writer, closefd=True) as zw, \
                        tarfile.open(fileobj=zw, mode='w|', format=tarfile.PAX_FORMAT) as tar:
                    for arcname, size, opener in self.entries:
                        info = tarfile.TarInfo(arcname)
                        info.size = size
                        info.mode = 0o644
                        info.uid = info.gid = 0
                        info.uname = info.gname = ''
                        with opener() as f:
                            tar.addfile(info, _ExactReader(f, size, arcname))
                        self.files += 1
                        self.bytes += size
                        if self.on_member:
                            self.on_member(arcname, size)
            except BaseException as e:  # Reported by close(); the reader sees EOF
                self.error = e
                with contextlib.suppress(OSError):
                    writer.close()

        self.thread = threading.Thread(target=produce, daemon=True, name='nbb-tar')
        self.thread.start()
        return self.reader

    def close(self):
        self.thread.join()
        self.reader.close()
        if self.error:
            raise PipelineError(f"Creating the archive failed: {self.error}") from self.error

    def abort(self):
        with contextlib.suppress(OSError):
            self.reader.close()
        self.thread.join(timeout=30)


class _ExactReader:
    """
    Feeds tarfile exactly `size` bytes; a file that changed size while being read fails the archive instead of
    producing a corrupt member.
    """
    def __init__(self, f, size, name):
        self.f = f
        self.remaining = size
        self.name = name

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.remaining
        data = self.f.read(min(n, self.remaining)) if self.remaining else b''
        self.remaining -= len(data)
        if not data and self.remaining:
            raise PipelineError(f"{self.name} shrank while it was being archived")
        return data


@dataclass
class ObjectRecord:
    key: str
    size: int
    sha384: str
    checksum: dict | None
    plaintext_size: int
    extra: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            'key': self.key, 'size': self.size, 'sha384': self.sha384, 'checksum': self.checksum,
        }


def upload_component(keyring, backend, key, source, size_hint=None, taps=()):
    """
    Stream source through gpg into backend at key. Returns an ObjectRecord. taps receive every plaintext chunk via
    write() and are asked finish() before commit; either may raise to fail the object.
    """
    writer = backend.open_write(key, size_hint=size_hint)
    gpg = keyring.encrypt_process()
    plaintext = {'size': 0, 'error': None}
    stream = None

    def feed():
        try:
            for chunk in iter(lambda: stream.read(CHUNK), b''):
                plaintext['size'] += len(chunk)
                for tap in taps:
                    tap.write(chunk)
                gpg.stdin.write(chunk)
        except BaseException as e:
            plaintext['error'] = e
        finally:
            with contextlib.suppress(OSError):
                gpg.stdin.close()

    digest = hashlib.sha384()
    feeder = None
    try:
        stream = source.open()
        feeder = threading.Thread(target=feed, daemon=True, name='nbb-feed')
        feeder.start()
        for chunk in iter(lambda: gpg.stdout.read(CHUNK), b''):
            digest.update(chunk)
            writer.write(chunk)
        feeder.join()
        source.close()
        if plaintext['error']:
            raise PipelineError(f"Reading the source failed: {plaintext['error']}") from plaintext['error']
        keyring.check_encryption(gpg)
        for tap in taps:
            tap.finish()
        info = writer.commit()
    except BaseException:
        source.abort()
        gpg.kill()
        if feeder:
            feeder.join(timeout=30)
        if not hasattr(gpg, 'returncode'):
            with contextlib.suppress(Exception):
                gpg.wait()
        for tap in taps:
            with contextlib.suppress(Exception):
                tap.abort()
        if not writer.closed:
            writer.abort()
        raise
    finally:
        with contextlib.suppress(OSError):
            gpg.stdout.close()
    checksum = None
    if backend.capabilities.object_checksum and info.checksum:
        checksum = {'type': backend.capabilities.object_checksum, 'value': info.checksum}
    return ObjectRecord(
        key=key, size=info.size, sha384=digest.hexdigest(), checksum=checksum, plaintext_size=plaintext['size'],
    )


def upload_bytes(backend, key, data):
    """
    Plain create-only upload of an unencrypted object (manifest.json, _COMPLETE, instance.json).
    """
    info = backend.put_bytes(key, data)
    checksum = None
    if backend.capabilities.object_checksum and info.checksum:
        checksum = {'type': backend.capabilities.object_checksum, 'value': info.checksum}
    return ObjectRecord(key=key, size=info.size, sha384=hashlib.sha384(data).hexdigest(), checksum=checksum,
                        plaintext_size=len(data))
