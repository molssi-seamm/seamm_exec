"""An ini that still asks for a Docker installation gets a clear message."""

import pytest

from seamm_exec.local import Local


def test_installation_docker_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="Docker support was removed"):
        Local().exec({"installation": "docker", "code": "true"}, ["{code}"], tmp_path)
