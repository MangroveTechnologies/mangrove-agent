"""Native macOS ACL regressions; POSIX mode bits alone do not show grants."""
import os
import subprocess
import sys

import pytest
from src.custody.transport import check_ancestors, check_owned_path

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Native macOS extended ACL semantics")


def _grant_acl(path):
    # Disposable nonsecret fixture only. This named-user grant leaves mode
    # bits unchanged on macOS, but permits another OS identity to access it.
    subprocess.run(["/bin/chmod", "+a", "user:nobody allow read,write", str(path)], check=True)


def test_private_file_with_named_user_acl_is_refused(tmp_path):
    path = tmp_path / "synthetic-nonsecret-state"
    path.write_text("nonsecret fixture")
    path.chmod(0o600)
    subprocess.run(["/bin/chmod", "-N", str(path)], check=True)
    check_owned_path(path, owner=os.getuid(), private=True)
    _grant_acl(path)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="ACL"):
        check_owned_path(path, owner=os.getuid(), private=True)


def test_ancestor_acl_is_refused_even_with_private_posix_mode(tmp_path):
    path = tmp_path / "synthetic-protected-parent"
    path.mkdir(mode=0o700)
    _grant_acl(path)
    assert path.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError, match="ACL"):
        check_ancestors(path, allowed_owners={0, os.getuid()})
