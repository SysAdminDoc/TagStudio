# Licensed under the GPL-3.0 License.
# Created for TagStudio: https://github.com/CyanVoxel/TagStudio

"""Small, explicit migration runner for the SQLite library schema.

The project intentionally keeps its library format independent of Alembic's
runtime.  This module provides the useful part of an Alembic-style workflow:
ordered revisions, an on-disk ledger, idempotent history seeding for existing
databases, and an explicit refusal for irreversible downgrades.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tagstudio.core.library.alchemy.models import SchemaMigration

MigrationCallback = Callable[[Session], None]


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    """Describe one ordered schema/data migration."""

    revision: int
    name: str
    upgrade: MigrationCallback
    downgrade: MigrationCallback | None = None


class MigrationRunner:
    """Apply a validated sequence of migrations and record its history."""

    def __init__(self, session: Session, migrations: Iterable[Migration]) -> None:
        self.session = session
        self.migrations = tuple(sorted(migrations, key=lambda migration: migration.revision))
        revisions = [migration.revision for migration in self.migrations]
        if len(revisions) != len(set(revisions)):
            raise ValueError("Migration revisions must be unique")

    def seed(self, revision: int) -> None:
        """Record a database that already contains the requested revision."""
        self._validate_revision(revision)
        for migration in self.migrations:
            if migration.revision <= revision:
                self._record(migration)
        self.session.commit()

    def upgrade(self, from_revision: int, to_revision: int) -> None:
        """Apply all migrations after ``from_revision`` through ``to_revision``."""
        self._validate_range(from_revision, to_revision)
        for migration in self.migrations:
            if migration.revision <= from_revision:
                self._record(migration)
                self.session.commit()
                continue
            if migration.revision > to_revision:
                break

            try:
                migration.upgrade(self.session)
                self._record(migration)
                self.session.commit()
            except Exception as error:
                self.session.rollback()
                raise MigrationError(
                    f"Could not apply migration {migration.revision} ({migration.name})"
                ) from error

        self.session.commit()

    def downgrade(self, from_revision: int, to_revision: int) -> None:
        """Downgrade a revision, refusing before mutation if any step is irreversible."""
        self._validate_range(to_revision, from_revision)
        pending = [
            migration
            for migration in reversed(self.migrations)
            if to_revision < migration.revision <= from_revision
        ]
        irreversible = next((migration for migration in pending if not migration.downgrade), None)
        if irreversible:
            raise MigrationError(
                f"Migration {irreversible.revision} ({irreversible.name}) cannot be downgraded"
            )

        for migration in pending:
            assert migration.downgrade is not None
            try:
                migration.downgrade(self.session)
                self.session.execute(
                    delete(SchemaMigration).where(SchemaMigration.revision == migration.revision)
                )
                self.session.commit()
            except Exception as error:
                self.session.rollback()
                raise MigrationError(
                    f"Could not downgrade migration {migration.revision} ({migration.name})"
                ) from error

    def applied_revisions(self) -> tuple[int, ...]:
        """Return recorded revisions in ascending order."""
        revisions = self.session.scalars(
            select(SchemaMigration.revision).order_by(SchemaMigration.revision)
        )
        return tuple(revisions)

    def _record(self, migration: Migration) -> None:
        if self.session.get(SchemaMigration, migration.revision) is None:
            self.session.add(SchemaMigration(revision=migration.revision, name=migration.name))
            self.session.flush()

    def _validate_revision(self, revision: int) -> None:
        known_revisions = {migration.revision for migration in self.migrations}
        if revision < 0 or any(item > revision for item in known_revisions):
            raise MigrationError(f"Unknown migration revision: {revision}")

    def _validate_range(self, lower: int, upper: int) -> None:
        if lower < 0 or upper < 0 or lower > upper:
            raise MigrationError(f"Invalid migration range: {lower} -> {upper}")
        known_revisions = {migration.revision for migration in self.migrations}
        has_pending = any(lower < revision <= upper for revision in known_revisions)
        if has_pending and upper not in known_revisions:
            raise MigrationError(f"Unknown migration revision: {upper}")
