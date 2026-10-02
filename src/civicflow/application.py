"""应用装配。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .audit import AuditLog
from .database import Database
from .idempotency import IdempotencyStore
from .inbox import Inbox
from .jobs import JobQueue
from .ledger import Ledger
from .outbox import Outbox
from .repository import EntityRepository
from .reservations import ReservationBook
from .rosters import RosterService
from .timeutil import Clock


@dataclass(frozen=True)
class CivicFlow:
    database: Database
    clock: Clock
    repository: EntityRepository
    inbox: Inbox
    outbox: Outbox
    ledger: Ledger
    reservations: ReservationBook
    jobs: JobQueue
    rosters: RosterService

    @classmethod
    def open(cls, path: str | Path, *, fixed_now: str | None = None) -> "CivicFlow":
        database = Database(path); database.initialize(); clock = Clock(fixed_now)
        audit = AuditLog(clock); idempotency = IdempotencyStore(clock)
        repository = EntityRepository(database, clock, audit, idempotency)
        inbox = Inbox(database, clock); outbox = Outbox(database, clock); jobs = JobQueue(database, clock)
        reservations = ReservationBook(database)
        rosters = RosterService(database, clock, repository, inbox, outbox, jobs, reservations)
        return cls(database, clock, repository, inbox, outbox, Ledger(database, clock), reservations, jobs, rosters)

    def verify(self) -> dict:
        with self.database.connect() as connection:
            audit_count = AuditLog(self.clock).verify(connection)
            entity_count = connection.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
            conflict_count = connection.execute("SELECT COUNT(*) AS n FROM inbox_conflicts").fetchone()["n"]
        return {"audit_entries": audit_count, "entities": entity_count, "inbox_conflicts": conflict_count}
