"""Private files for the cloak dir: the vault, journal, replay cache and audit log are readable by
the agent's user only. Directories are created 0700 and log files 0600, whatever the umask."""
import os


def private_dir(path: str) -> None:
    """Create ``path`` (and parents) and make the leaf 0700 when this user owns it."""
    if not path:
        return
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        st = os.stat(path)
        if hasattr(os, "getuid") and st.st_uid == os.getuid() and st.st_mode & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        pass


def append_private(path: str, text: str) -> None:
    """Append ``text`` to ``path``, creating it 0600 (and tightening an existing looser file)."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        try:
            if os.fstat(fd).st_mode & 0o077:
                os.fchmod(fd, 0o600)
        except (OSError, AttributeError):
            pass
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
