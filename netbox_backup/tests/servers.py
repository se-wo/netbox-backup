"""
Throwaway servers for the storage contract suite: an OpenSSH sshd on a free local port that accepts one generated
key for the user running the tests.
"""
import getpass
import os
import shutil
import socket
import subprocess
import tempfile
import time

SSHD = shutil.which('sshd') or ('/usr/sbin/sshd' if os.path.exists('/usr/sbin/sshd') else None)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class SSHServer:
    """
    sshd -D with a private config. Run as root it logs in root; as any other user it can only log that user in,
    which is all the tests need.
    """

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='nbb-sshd-')
        self.root = os.path.join(self.dir, 'root')
        os.makedirs(self.root)
        self.port = free_port()
        self.user = getpass.getuser()
        for name in ('host', 'client'):
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', os.path.join(self.dir, name)],
                           check=True)
        shutil.copy(os.path.join(self.dir, 'client.pub'), os.path.join(self.dir, 'authorized_keys'))
        os.chmod(os.path.join(self.dir, 'authorized_keys'), 0o600)
        with open(os.path.join(self.dir, 'sshd_config'), 'w') as f:
            f.write(f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {self.dir}/host\n"
                    f"AuthorizedKeysFile {self.dir}/authorized_keys\nPasswordAuthentication no\n"
                    f"PermitRootLogin prohibit-password\nStrictModes no\nUsePAM no\nPidFile {self.dir}/pid\n"
                    f"Subsystem sftp internal-sftp\n")
        if os.geteuid() == 0:
            os.makedirs('/run/sshd', exist_ok=True)  # the privilege separation directory
        self.proc = subprocess.Popen([SSHD, '-D', '-e', '-f', os.path.join(self.dir, 'sshd_config')],
                                     stderr=open(os.path.join(self.dir, 'log'), 'wb'))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as s:
                if s.connect_ex(('127.0.0.1', self.port)) == 0:
                    break
            time.sleep(0.1)
        else:
            self.stop()
            raise RuntimeError(f"sshd did not start: {open(os.path.join(self.dir, 'log')).read()}")

    @property
    def known_host(self):
        with open(os.path.join(self.dir, 'host.pub')) as f:
            return f.read().strip()

    @property
    def client_key(self):
        return os.path.join(self.dir, 'client')

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        shutil.rmtree(self.dir, ignore_errors=True)
