"""Exercise installed Red Config; replace only its storage driver."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from copy import deepcopy
from pathlib import Path


async def _real_config_contract() -> None:
    from redbot.core import Config

    from TidalPlayerExp.providers.tokens import TokenRepository, TokenSnapshot

    class Driver:
        def __init__(self):
            self.data = {"_schema_version": 3, "unrelated": {"keep": True}}
            self.writes = 0
            self.fail_read = False
            self.fail_write = False

        async def get(self, identifiers):
            assert not identifiers.identifiers
            if self.fail_read:
                raise OSError("storage read failed")
            return deepcopy(self.data)

        async def set(self, identifiers, *, value):
            # Fail if token persistence ever regresses to separate field writes.
            assert not identifiers.identifiers
            if self.fail_write:
                raise OSError("storage write failed")
            self.data = deepcopy(value)
            self.writes += 1

    driver = Driver()
    config = Config("TidalPlayerTokenContract", "1234567", driver, force_registration=True)
    config.register_global(token_type=None, access_token=None, refresh_token=None, expiry_time=None)
    repository = TokenRepository(config)
    snapshot = TokenSnapshot("Bearer", "test-access", "test-refresh", 123456789)
    await repository.replace(snapshot)
    assert await repository.load() == snapshot
    assert driver.writes == 1
    assert driver.data["_schema_version"] == 3
    assert driver.data["unrelated"] == {"keep": True}

    # Keep a strong reference, as another waiting Config user would do.
    group_lock = config.get_lock()
    for failure in ("fail_write", "fail_read"):
        setattr(driver, failure, True)
        try:
            await repository.clear()
        except OSError:
            pass
        else:
            raise AssertionError("The storage failure was swallowed")
        setattr(driver, failure, False)
        assert not group_lock.locked(), "Failed storage access retained Red's group lock"
        assert await repository.load() == snapshot

    await asyncio.wait_for(repository.clear(), timeout=1)
    assert driver.writes == 2
    assert await repository.load() is None
    assert driver.data["_schema_version"] == 3
    assert driver.data["unrelated"] == {"keep": True}


def test_installed_red_group_write_and_failure_contract() -> None:
    result = subprocess.run(
        [
            sys.executable, "-c",
            (
                "import asyncio; from TidalPlayerExp.tests.test_red_token_contract "
                "import _real_config_contract; asyncio.run(_real_config_contract())"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
