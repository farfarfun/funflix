"""`funflix sync pull/push --job` 的表名解析。

每个 pipeline job 只同步自己需要的表（见 `services/sync/tables.py::JOB_TABLES`），
`--job` 不给时同步全部表——两条路径都在这里覆盖，另外覆盖传错 job 名的报错。
"""

from __future__ import annotations

import pytest
import typer

from funflix.cli import _resolve_sync_tables
from funflix.services.sync import JOB_TABLES


class TestResolveSyncTables:
    def test_no_job_means_all_tables(self) -> None:
        assert _resolve_sync_tables(None) is None

    @pytest.mark.parametrize("job", list(JOB_TABLES))
    def test_known_job_resolves_to_its_tables(self, job: str) -> None:
        assert _resolve_sync_tables(job) == JOB_TABLES[job]

    def test_unknown_job_raises_bad_parameter(self) -> None:
        with pytest.raises(typer.BadParameter):
            _resolve_sync_tables("not_a_real_job")
