"""
GnuPG integration. The plugin contains no cryptographic code of its own: encryption and signatures are delegated
to the gpg binary, hashing to hashlib.

Every job gets an isolated GNUPGHOME holding only the keys pinned in static config. gpg always runs with --batch,
--status-fd and without key retrieval, and every result is checked from the machine-readable status lines rather
than the exit code alone:

* encryption must report END_ENCRYPTION with AES-256;
* decryption must report DECRYPTION_OKAY with AES-256 and an integrity check (MDC or AEAD);
* a signature counts only if VALIDSIG names a pinned signer's primary key and SHA-384.

The algorithm profile ("bsi", the default) admits only BSI TR-02102-1 key types: ECDSA/ECDH on brainpoolP256r1,
P384r1 or P512r1, and RSA of 3072 bits or more. With compliance="de-vs", gpg additionally runs in
--compliance=de-vs --require-compliance mode; that needs a GnuPG/libgcrypt build whose RNG is de-vs compliant
(stock distribution packages usually are not, and refuse every operation in that mode).
"""
import contextlib
import os
import shutil
import subprocess
import tempfile
import threading

from .config import read_reference_bytes, resolve_secret

__all__ = ('GPGError', 'Keyring', 'NoIdentity', 'generate_key')

AES256 = '9'
SHA384 = '9'
BRAINPOOL_CURVES = {'brainpoolP256r1', 'brainpoolP384r1', 'brainpoolP512r1'}
ALGO_RSA = {'1', '2', '3'}
ALGO_ECC = {'18', '19'}  # ECDH, ECDSA
SIGNATURE_ALGOS = {'19', '1', '3'}  # ECDSA, RSA (key sizes are held to the profile at import)


class GPGError(Exception):
    pass


class NoIdentity(GPGError):
    """
    None of the mounted identities is a recipient of the object (e.g. the config component without the recovery key).
    """


class StatusReader(threading.Thread):
    """
    Collects gpg's --status-fd lines from a pipe.
    """
    def __init__(self, fd):
        super().__init__(daemon=True, name='nbb-gpg-status')
        self.fd = fd
        self.lines = []

    def run(self):
        with os.fdopen(self.fd, 'rb') as f:
            for line in f:
                if line.startswith(b'[GNUPG:] '):
                    self.lines.append(line[9:].decode(errors='replace').rstrip('\n').split(' '))

    def has(self, keyword):
        return any(parts[0] == keyword for parts in self.lines)

    def get(self, keyword):
        return [parts[1:] for parts in self.lines if parts[0] == keyword]


class Process:
    """
    A running gpg with its status channel. wait() returns the status reader once gpg has exited and every status
    line has been read.
    """
    def __init__(self, popen, status, stderr_file, args):
        self.popen = popen
        self.status = status
        self.stderr_file = stderr_file
        self.args = args

    @property
    def stdin(self):
        return self.popen.stdin

    @property
    def stdout(self):
        return self.popen.stdout

    def wait(self):
        returncode = self.popen.wait()
        self.status.join()
        self.stderr_file.seek(0)
        self.stderr = self.stderr_file.read().decode(errors='replace').strip()
        self.stderr_file.close()
        self.returncode = returncode
        return self.status

    def kill(self):
        with contextlib.suppress(ProcessLookupError):
            self.popen.kill()

    def error(self, what):
        detail = self.stderr.splitlines()[-1] if getattr(self, 'stderr', '') else f'exit status {self.returncode}'
        return GPGError(f"gpg {what} failed: {detail}")


class Keyring:
    """
    An isolated GnuPG home for one job. Use as a context manager; the directory and its agent are removed on exit.

    signing=True imports the worker's signing key; identities=True imports the decryption identities (mounted
    only for restores and QA restores).
    """

    def __init__(self, crypto, signing=False, identities=False, extra_signers=()):
        if not crypto.configured:
            raise GPGError("crypto is not configured: public_keys, recipients, recovery_key and signing_key are "
                           "required")
        self.crypto = crypto
        self.binary = shutil.which(crypto.gpg_binary)
        if not self.binary:
            raise GPGError(f"gpg binary {crypto.gpg_binary!r} not found")
        self.home = tempfile.mkdtemp(prefix='nbb-gpg-')
        os.chmod(self.home, 0o700)
        self.signers = {crypto.signing_key, *crypto.trusted_signers, *extra_signers}
        self.passphrase_file = None
        self.last_decryption_key = None
        try:
            self._setup(signing, identities)
        except BaseException:
            self.close()
            raise

    # Lifecycle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.home and os.path.isdir(self.home):
            gpgconf = shutil.which('gpgconf')
            if gpgconf:
                subprocess.run([gpgconf, '--homedir', self.home, '--kill', 'all'], capture_output=True, check=False)
            shutil.rmtree(self.home, ignore_errors=True)
        self.home = None

    # Plumbing

    def base_args(self):
        args = [
            self.binary, '--homedir', self.home, '--batch', '--no-tty', '--no-options', '--no-auto-key-retrieve',
            '--auto-key-locate', 'local', '--trust-model', 'always', '--no-greeting', '--with-colons',
        ]
        if self.crypto.compliance:
            args += [f'--compliance={self.crypto.compliance}', '--require-compliance']
        return args

    def run(self, args, input=None):
        result = subprocess.run(self.base_args() + list(args), input=input, capture_output=True, check=False)
        if result.returncode != 0:
            message = result.stderr.decode(errors='replace').strip().splitlines()
            raise GPGError(f"gpg {' '.join(args[:1])} failed: {message[-1] if message else result.returncode}")
        return result.stdout

    def spawn(self, args, stdin=subprocess.PIPE, stdout=subprocess.PIPE):
        """
        Start gpg with a status pipe. The caller streams through stdin/stdout and then calls wait().
        """
        read_fd, write_fd = os.pipe()
        stderr_file = tempfile.TemporaryFile()
        full = self.base_args() + ['--status-fd', str(write_fd)] + list(args)
        try:
            popen = subprocess.Popen(full, stdin=stdin, stdout=stdout, stderr=stderr_file, pass_fds=(write_fd,))
        except BaseException:
            os.close(read_fd)
            stderr_file.close()
            raise
        finally:
            os.close(write_fd)
        status = StatusReader(read_fd)
        status.start()
        return Process(popen, status, stderr_file, args)

    # Keys

    def _setup(self, signing, identities):
        material = read_reference_bytes(self.crypto.public_keys)
        self.run(['--import', '--import-options', 'import-minimal'], input=material)
        if signing:
            if not self.crypto.signing_secret_key:
                raise GPGError("crypto.signing_secret_key is required to sign sets")
            self._import_secret(self.crypto.signing_secret_key, self.crypto.signing_passphrase)
        if identities:
            if not self.crypto.identities:
                raise GPGError("No decryption identity is mounted (crypto.identities)")
            for ref in self.crypto.identities:
                self._import_secret(ref, self.crypto.identity_passphrase)
        pinned = set(self.crypto.all_recipients) | self.signers
        keys = self.list_keys()
        # Keep only pinned keys, so a stray key in the public key file can never be used
        for fpr in set(keys) - pinned:
            self.run(['--delete-secret-and-public-key', '--yes', fpr])
            keys.pop(fpr)
        missing = sorted(f for f in pinned if f not in keys)
        if missing:
            raise GPGError(f"Pinned key(s) not found in crypto.public_keys: {', '.join(missing)}")
        for fpr in pinned:
            problems = self.profile_problems(keys[fpr])
            if problems:
                raise GPGError(f"Key {fpr} does not meet the '{self.profile}' algorithm profile: {problems}")
        for fpr in self.crypto.all_recipients:
            if not any('e' in sub['caps'] for sub in keys[fpr]['subkeys']) and 'e' not in keys[fpr]['caps']:
                raise GPGError(f"Recipient key {fpr} has no encryption-capable (sub)key")
        if signing and not keys[self.crypto.signing_key]['secret']:
            raise GPGError("crypto.signing_secret_key does not contain the secret key of crypto.signing_key")
        self.keys = keys

    @property
    def profile(self):
        return 'de-vs' if self.crypto.compliance else 'bsi'

    def _import_secret(self, ref, passphrase_ref):
        material = read_reference_bytes(ref)
        self.run(['--import', '--import-options', 'import-minimal'], input=material)
        if passphrase_ref:
            self.passphrase_file = os.path.join(self.home, 'passphrase')
            fd = os.open(self.passphrase_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(resolve_secret(passphrase_ref))

    def secret_args(self):
        args = ['--pinentry-mode', 'loopback']
        if self.passphrase_file:
            args += ['--passphrase-file', self.passphrase_file]
        else:
            args += ['--passphrase', '']
        return args

    def list_keys(self):
        """
        Parse --list-keys into {primary fingerprint: {algo, length, curve, caps, secret, subkeys: [...]}}.
        """
        secret = set()
        output = self.run(['--list-secret-keys', '--with-fingerprint']).decode()
        current = None
        for line in output.splitlines():
            fields = line.split(':')
            if fields[0] == 'sec':
                current = 'sec'
            elif fields[0] == 'fpr' and current == 'sec':
                secret.add(fields[9])
                current = None
        keys = {}
        primary = None
        pending = None
        for line in self.run(['--list-keys', '--with-fingerprint', '--with-subkey-fingerprint']).decode().splitlines():
            fields = line.split(':')
            if fields[0] in ('pub', 'sub'):
                pending = {
                    'type': fields[0], 'validity': fields[1], 'length': int(fields[2] or 0), 'algo': fields[3],
                    'caps': fields[11], 'curve': fields[16] if len(fields) > 16 else '',
                }
            elif fields[0] == 'fpr' and pending:
                pending['fpr'] = fields[9]
                if pending['type'] == 'pub':
                    primary = pending
                    primary['subkeys'] = []
                    primary['secret'] = fields[9] in secret
                    keys[fields[9]] = primary
                elif primary is not None:
                    primary['subkeys'].append(pending)
                pending = None
        return keys

    def profile_problems(self, key):
        problems = []
        # Every (sub)key counts, revoked and expired ones too: gpg picks the newest usable encryption subkey on
        # its own, so a key passes only if no subkey it could ever pick is outside the profile
        for k in [key, *key['subkeys']]:
            if k['algo'] in ALGO_ECC and k['curve'] in BRAINPOOL_CURVES:
                continue
            if k['algo'] in ALGO_RSA and k['length'] >= 3072:
                continue
            problems.append(f"{k['fpr'][-16:]} uses algorithm {k['algo']} {k['curve'] or k['length']}")
        return '; '.join(problems)

    # Operations

    def encrypt_args(self, recipients=None):
        args = ['--encrypt', '--compress-algo', 'none', '--cipher-algo', 'AES256', '--digest-algo', 'SHA384',
                '--no-encrypt-to', '--output', '-']
        for fpr in recipients or self.crypto.all_recipients:
            args += ['--recipient', fpr]
        return args

    def encrypt_process(self, stdin=subprocess.PIPE, stdout=subprocess.PIPE, recipients=None):
        """
        recipients defaults to the operational keys plus the recovery key; the config component passes the recovery
        key alone.
        """
        return self.spawn(self.encrypt_args(recipients), stdin=stdin, stdout=stdout)

    @staticmethod
    def check_encryption(proc):
        status = proc.wait()
        if proc.returncode != 0 or not status.has('END_ENCRYPTION'):
            raise proc.error('encryption')
        begin = status.get('BEGIN_ENCRYPTION')
        if not begin or begin[0][1] != AES256:
            raise GPGError(f"gpg did not encrypt with AES-256 (BEGIN_ENCRYPTION {begin})")
        return status

    def encrypt_bytes(self, data, recipients=None):
        proc = self.encrypt_process(recipients=recipients)
        output, _ = proc.popen.communicate(data)
        self.check_encryption(proc)
        return output

    def decrypt_process(self, stdin=subprocess.PIPE, stdout=subprocess.PIPE):
        return self.spawn(self.secret_args() + ['--decrypt', '--output', '-'], stdin=stdin, stdout=stdout)

    def check_decryption(self, proc):
        """
        gpg emits plaintext before its integrity check finishes; only after this returns may the output be used.
        Records the primary fingerprint of the key that decrypted in last_decryption_key.
        """
        status = proc.wait()
        if proc.returncode != 0 or not status.has('DECRYPTION_OKAY') or status.has('DECRYPTION_FAILED'):
            if status.has('NO_SECKEY') and not status.has('DECRYPTION_OKAY'):
                recipients = ', '.join(parts[0] for parts in status.get('ENC_TO'))
                raise NoIdentity(f"No mounted identity matches a recipient of this object (encrypted to {recipients})")
            raise proc.error('decryption')
        info = status.get('DECRYPTION_INFO')
        if not info or info[0][1] != AES256:
            raise GPGError(f"Object was not encrypted with AES-256 (DECRYPTION_INFO {info})")
        aead = len(info[0]) > 2 and info[0][2] not in ('', '0')
        if not aead and not status.has('GOODMDC'):
            raise GPGError("Object has no integrity protection (no MDC/AEAD)")
        used = status.get('DECRYPTION_KEY')
        self.last_decryption_key = used[0][1] if used and len(used[0]) > 1 else None
        return status

    def decrypt_bytes(self, data):
        proc = self.decrypt_process()
        output, _ = proc.popen.communicate(data)
        self.check_decryption(proc)
        return output

    def sign_detached(self, data):
        """
        Armored detached signature over data with the pinned signing key, ECDSA with SHA-384.
        """
        args = self.secret_args() + [
            '--local-user', f'{self.crypto.signing_key}!', '--digest-algo', 'SHA384', '--armor', '--detach-sign',
            '--output', '-',
        ]
        proc = self.spawn(args)
        output, _ = proc.popen.communicate(data)
        status = proc.wait()
        if proc.returncode != 0 or not status.has('SIG_CREATED'):
            raise proc.error('signing')
        return output

    def verify_detached(self, data, signature, signers=None):
        """
        Verify an armored detached signature. Returns the signer's primary fingerprint. Only pinned signers count
        (the equivalent of --assert-signer, which older GnuPG releases lack), and only SHA-384.
        """
        allowed = set(signers or self.signers)
        sig_fd, sig_path = tempfile.mkstemp(dir=self.home, suffix='.asc')
        with os.fdopen(sig_fd, 'wb') as f:
            f.write(signature)
        try:
            proc = self.spawn(['--verify', sig_path, '-'], stdout=subprocess.DEVNULL)
            proc.popen.communicate(data)
            status = proc.wait()
        finally:
            os.unlink(sig_path)
        detail = f": {proc.stderr.splitlines()[-1]}" if proc.stderr else ''
        # Exactly one signature; VALIDSIG alone is also emitted for expired and revoked keys, so GOODSIG must
        # vouch for the same signature, and no expired/revoked/bad variant may appear
        refused = [k for k in ('BADSIG', 'ERRSIG', 'EXPSIG', 'EXPKEYSIG', 'REVKEYSIG') if status.has(k)]
        newsig, good, valid = status.get('NEWSIG'), status.get('GOODSIG'), status.get('VALIDSIG')
        if proc.returncode != 0 or refused or len(newsig) != 1 or len(good) != 1 or len(valid) != 1:
            reason = f" ({', '.join(refused)})" if refused else ''
            raise GPGError(f"Signature verification failed{reason}{detail}")
        fields = valid[0]
        signing_key, primary = fields[0], (fields[9] if len(fields) > 9 and fields[9] else fields[0])
        if not signing_key.endswith(good[0][0]):
            raise GPGError("GOODSIG and VALIDSIG name different keys")
        if primary not in allowed:
            raise GPGError(f"Signed by {primary}, which is not a pinned signer")
        if fields[6] not in SIGNATURE_ALGOS:
            raise GPGError(f"Signature uses public-key algorithm {fields[6]}, not ECDSA or RSA")
        if fields[7] != SHA384:
            raise GPGError(f"Signature uses hash algorithm {fields[7]}, not SHA-384")
        return primary


def generate_key(name, gpg_binary='gpg', passphrase='', curve='brainpoolP384r1'):
    """
    Generate a key pair for the BSI profile: an ECDSA primary key (sign, certify) with an ECDH encryption subkey on
    the given brainpool curve. Returns (fingerprint, public key armor, secret key armor).
    """
    binary = shutil.which(gpg_binary)
    if not binary:
        raise GPGError(f"gpg binary {gpg_binary!r} not found")
    home = tempfile.mkdtemp(prefix='nbb-gpg-keygen-')
    os.chmod(home, 0o700)
    base = [binary, '--homedir', home, '--batch', '--no-tty', '--pinentry-mode', 'loopback', '--passphrase',
            passphrase]
    try:
        def run(*args):
            result = subprocess.run(base + list(args), capture_output=True, check=False)
            if result.returncode != 0:
                raise GPGError(f"gpg {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
            return result.stdout
        run('--quick-gen-key', name, curve, 'sign,cert', 'never')
        listing = run('--list-keys', '--with-colons').decode().splitlines()
        fpr = next(line.split(':')[9] for line in listing if line.startswith('fpr:'))
        run('--quick-add-key', fpr, curve, 'encr', 'never')
        public = run('--export', '--armor', fpr)
        secret = run('--export-secret-keys', '--armor', fpr)
        return fpr, public, secret
    finally:
        gpgconf = shutil.which('gpgconf')
        if gpgconf:
            subprocess.run([gpgconf, '--homedir', home, '--kill', 'all'], capture_output=True, check=False)
        shutil.rmtree(home, ignore_errors=True)
