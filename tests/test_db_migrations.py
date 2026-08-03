# Copyright (C) 2025 Travis Abendshien (CyanVoxel).
# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio


import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tagstudio.core.constants import TS_FOLDER_NAME
from tagstudio.core.library.alchemy.constants import (
    SQL_FILENAME,
)
from tagstudio.core.library.alchemy.db import Base
from tagstudio.core.library.alchemy.library import Library
from tagstudio.core.library.alchemy.migrations import Migration, MigrationError, MigrationRunner
from tagstudio.core.library.alchemy.models import SchemaMigration

CWD = Path(__file__)
FIXTURES = "fixtures"
EMPTY_LIBRARIES = "empty_libraries"


@pytest.mark.parametrize(
    "path",
    [
        str(Path(CWD.parent / FIXTURES / EMPTY_LIBRARIES / "DB_VERSION_6")),
        str(Path(CWD.parent / FIXTURES / EMPTY_LIBRARIES / "DB_VERSION_7")),
        str(Path(CWD.parent / FIXTURES / EMPTY_LIBRARIES / "DB_VERSION_8")),
        str(Path(CWD.parent / FIXTURES / EMPTY_LIBRARIES / "DB_VERSION_9")),
        str(Path(CWD.parent / FIXTURES / EMPTY_LIBRARIES / "DB_VERSION_100")),
    ],
)
def test_library_migrations(path: str):
    library = Library()

    # Copy libraries to temp dir so modifications don't show up in version control
    original_path = Path(path)
    temp_path = Path(CWD.parent / FIXTURES / EMPTY_LIBRARIES / "DB_VERSION_TEMP")
    temp_path.mkdir(exist_ok=True)
    temp_path_ts = temp_path / TS_FOLDER_NAME
    temp_path_ts.mkdir(exist_ok=True)
    shutil.copy(
        original_path / TS_FOLDER_NAME / SQL_FILENAME,
        temp_path / TS_FOLDER_NAME / SQL_FILENAME,
    )

    try:
        status = library.open_library(library_dir=temp_path)
        library.close()
        with closing(sqlite3.connect(temp_path / TS_FOLDER_NAME / SQL_FILENAME)) as connection:
            revisions = connection.execute(
                "SELECT revision FROM schema_migrations ORDER BY revision"
            ).fetchall()
        assert revisions == [
            (7,),
            (8,),
            (9,),
            (100,),
            (101,),
            (102,),
            (103,),
            (104,),
            (105,),
            (106,),
        ]
        shutil.rmtree(temp_path)
        assert status.success
    except Exception as e:
        library.close()
        shutil.rmtree(temp_path)
        raise (e)


def test_migration_runner_upgrades_and_seeds_history():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    applied: list[str] = []

    migrations = (
        Migration(
            1,
            "first",
            lambda _session: applied.append("up-1"),
            lambda _session: applied.append("down-1"),
        ),
        Migration(
            2,
            "second",
            lambda _session: applied.append("up-2"),
            lambda _session: applied.append("down-2"),
        ),
    )

    with Session(engine) as session:
        runner = MigrationRunner(session, migrations)
        runner.upgrade(0, 2)
        runner.upgrade(2, 2)

        assert applied == ["up-1", "up-2"]
        assert runner.applied_revisions() == (1, 2)
        assert session.query(SchemaMigration).count() == 2


def test_migration_runner_downgrades_reversible_steps():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    applied: list[str] = []
    migrations = (
        Migration(
            1,
            "first",
            lambda _session: applied.append("up-1"),
            lambda _session: applied.append("down-1"),
        ),
        Migration(
            2,
            "second",
            lambda _session: applied.append("up-2"),
            lambda _session: applied.append("down-2"),
        ),
    )

    with Session(engine) as session:
        runner = MigrationRunner(session, migrations)
        runner.upgrade(0, 2)
        runner.downgrade(2, 0)

        assert applied == ["up-1", "up-2", "down-2", "down-1"]
        assert runner.applied_revisions() == ()


def test_migration_runner_refuses_irreversible_downgrade_without_mutation():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    applied: list[str] = []
    migrations = (Migration(1, "irreversible", lambda _session: applied.append("up-1")),)

    with Session(engine) as session:
        runner = MigrationRunner(session, migrations)
        runner.upgrade(0, 1)

        with pytest.raises(MigrationError, match="cannot be downgraded"):
            runner.downgrade(1, 0)

        assert applied == ["up-1"]
        assert runner.applied_revisions() == (1,)
