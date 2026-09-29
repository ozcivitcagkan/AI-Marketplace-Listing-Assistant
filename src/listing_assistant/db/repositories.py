"""The only place that talks SQL. Everything else works with Pydantic models.

Every query about a listing's data filters by `listing_id`, so one listing can never
read or change another listing's rows through these methods.
"""

import json
import sqlite3

from listing_assistant.models import (
    AgentRunRecord,
    Approval,
    ApprovalDecision,
    ApprovalGate,
    AuditEntry,
    Clarification,
    ClarificationStatus,
    CopywriterOutput,
    Draft,
    Fact,
    FactStatus,
    Listing,
    ListingPhoto,
    ListingStatus,
    SafetyVerdict,
    ToolCallRecord,
    utc_now,
)


class NotFoundError(LookupError):
    pass


class ListingRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, listing: Listing) -> Listing:
        with self._conn:
            self._conn.execute(
                "INSERT INTO listings (id, category, status, created_at)"
                " VALUES (:id, :category, :status, :created_at)",
                listing.model_dump(mode="json"),
            )
        return listing

    def get(self, listing_id: str) -> Listing | None:
        row = self._conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        return Listing.model_validate(dict(row)) if row else None

    def list_all(self) -> list[Listing]:
        rows = self._conn.execute("SELECT * FROM listings ORDER BY created_at DESC, id")
        return [Listing.model_validate(dict(row)) for row in rows]

    def compare_and_set_status(
        self, listing_id: str, expected: ListingStatus, new: ListingStatus
    ) -> bool:
        """Low-level write used ONLY by state_machine.transition()."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE listings SET status = ? WHERE id = ? AND status = ?",
                (new.value, listing_id, expected.value),
            )
        return cursor.rowcount == 1


class FactRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, fact: Fact) -> Fact:
        with self._conn:
            self._conn.execute(
                "INSERT INTO listing_facts (id, listing_id, field_key, value, source, status,"
                " confidence, evidence_photo_id, created_at)"
                " VALUES (:id, :listing_id, :field_key, :value, :source, :status,"
                " :confidence, :evidence_photo_id, :created_at)",
                fact.model_dump(mode="json"),
            )
        return fact

    def get(self, listing_id: str, fact_id: str) -> Fact | None:
        row = self._conn.execute(
            "SELECT * FROM listing_facts WHERE listing_id = ? AND id = ?", (listing_id, fact_id)
        ).fetchone()
        return Fact.model_validate(dict(row)) if row else None

    def list_for_listing(self, listing_id: str, status: FactStatus | None = None) -> list[Fact]:
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM listing_facts WHERE listing_id = ? ORDER BY created_at, id",
                (listing_id,),
            )
        else:
            rows = self._conn.execute(
                "SELECT * FROM listing_facts WHERE listing_id = ? AND status = ?"
                " ORDER BY created_at, id",
                (listing_id, status.value),
            )
        return [Fact.model_validate(dict(row)) for row in rows]

    def approve(self, listing_id: str, fact_id: str) -> Fact:
        """Approve one value and reject any other approved value of the same field, atomically."""
        fact = self.get(listing_id, fact_id)
        if fact is None:
            raise NotFoundError(f"fact {fact_id} not found in listing {listing_id}")
        with self._conn:
            self._conn.execute(
                "UPDATE listing_facts SET status = 'rejected'"
                " WHERE listing_id = ? AND field_key = ? AND status = 'approved' AND id != ?",
                (listing_id, fact.field_key, fact_id),
            )
            self._conn.execute(
                "UPDATE listing_facts SET status = 'approved' WHERE listing_id = ? AND id = ?",
                (listing_id, fact_id),
            )
        return self.get(listing_id, fact_id)

    def add_approved(self, fact: Fact) -> Fact:
        """Insert a seller-confirmed value, replacing any approved value of the same field."""
        if fact.status is not FactStatus.APPROVED:
            raise ValueError("add_approved expects an approved fact")
        with self._conn:
            self._conn.execute(
                "UPDATE listing_facts SET status = 'rejected'"
                " WHERE listing_id = ? AND field_key = ? AND status = 'approved'",
                (fact.listing_id, fact.field_key),
            )
            self._conn.execute(
                "INSERT INTO listing_facts (id, listing_id, field_key, value, source, status,"
                " confidence, evidence_photo_id, created_at)"
                " VALUES (:id, :listing_id, :field_key, :value, :source, :status,"
                " :confidence, :evidence_photo_id, :created_at)",
                fact.model_dump(mode="json"),
            )
        return fact

    def set_status(self, listing_id: str, fact_id: str, status: FactStatus) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE listing_facts SET status = ? WHERE listing_id = ? AND id = ?",
                (status.value, listing_id, fact_id),
            )
        if cursor.rowcount == 0:
            raise NotFoundError(f"fact {fact_id} not found in listing {listing_id}")


class DraftRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def next_version(self, listing_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM drafts WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row[0]

    def add(self, draft: Draft) -> Draft:
        with self._conn:
            self._conn.execute(
                "INSERT INTO drafts (id, listing_id, version, title, description, content_json,"
                " model, prompt_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft.id,
                    draft.listing_id,
                    draft.version,
                    draft.title,
                    draft.description,
                    draft.content.model_dump_json(),
                    draft.model,
                    draft.prompt_version,
                    draft.created_at.isoformat(),
                ),
            )
        return draft

    def get(self, listing_id: str, version: int) -> Draft | None:
        row = self._conn.execute(
            "SELECT * FROM drafts WHERE listing_id = ? AND version = ?", (listing_id, version)
        ).fetchone()
        return self._to_draft(row) if row else None

    def latest(self, listing_id: str) -> Draft | None:
        row = self._conn.execute(
            "SELECT * FROM drafts WHERE listing_id = ? ORDER BY version DESC LIMIT 1",
            (listing_id,),
        ).fetchone()
        return self._to_draft(row) if row else None

    @staticmethod
    def _to_draft(row: sqlite3.Row) -> Draft:
        # title/description columns are for human reading; content_json is the source.
        return Draft(
            id=row["id"],
            listing_id=row["listing_id"],
            version=row["version"],
            content=CopywriterOutput.model_validate_json(row["content_json"]),
            model=row["model"],
            prompt_version=row["prompt_version"],
            created_at=row["created_at"],
        )


class SafetyReviewRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, verdict: SafetyVerdict) -> None:
        issues = [issue.model_dump(mode="json") for issue in verdict.issues]
        with self._conn:
            self._conn.execute(
                "INSERT INTO safety_reviews"
                " (draft_id, reviewer_type, decision, issues_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    verdict.draft_id,
                    verdict.reviewer_type.value,
                    verdict.decision.value,
                    json.dumps(issues, ensure_ascii=False),
                    utc_now().isoformat(),
                ),
            )

    def list_for_draft(self, draft_id: str) -> list[SafetyVerdict]:
        rows = self._conn.execute(
            "SELECT * FROM safety_reviews WHERE draft_id = ? ORDER BY id", (draft_id,)
        )
        return [
            SafetyVerdict(
                draft_id=row["draft_id"],
                reviewer_type=row["reviewer_type"],
                issues=json.loads(row["issues_json"]),
            )
            for row in rows
        ]


class AuditLogRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, entry: AuditEntry) -> AuditEntry:
        data = entry.model_dump(mode="json")
        data["details_json"] = json.dumps(data.pop("details"), ensure_ascii=False)
        with self._conn:
            self._conn.execute(
                "INSERT INTO audit_logs (id, created_at, actor_type, actor_name, action,"
                " resource_type, resource_id, details_json)"
                " VALUES (:id, :created_at, :actor_type, :actor_name, :action,"
                " :resource_type, :resource_id, :details_json)",
                data,
            )
        return entry

    def list_for_resource(self, resource_id: str) -> list[AuditEntry]:
        rows = self._conn.execute(
            "SELECT * FROM audit_logs WHERE resource_id = ? ORDER BY created_at, id",
            (resource_id,),
        )
        entries = []
        for row in rows:
            data = dict(row)
            data["details"] = json.loads(data.pop("details_json"))
            entries.append(AuditEntry.model_validate(data))
        return entries


class PhotoRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, photo: ListingPhoto) -> ListingPhoto:
        data = photo.model_dump(mode="json")
        data["quality_warnings_json"] = json.dumps(data.pop("quality_warnings"))
        data["privacy_flags_json"] = json.dumps(data.pop("privacy_flags"))
        data["is_cover"] = int(data["is_cover"])
        with self._conn:
            self._conn.execute(
                "INSERT INTO listing_photos (id, listing_id, storage_key, sha256, perceptual_hash,"
                " width, height, blur_score, brightness, quality_warnings_json,"
                " privacy_flags_json, order_index, is_cover, created_at)"
                " VALUES (:id, :listing_id, :storage_key, :sha256, :perceptual_hash,"
                " :width, :height, :blur_score, :brightness, :quality_warnings_json,"
                " :privacy_flags_json, :order_index, :is_cover, :created_at)",
                data,
            )
        return photo

    def get(self, listing_id: str, photo_id: str) -> ListingPhoto | None:
        row = self._conn.execute(
            "SELECT * FROM listing_photos WHERE listing_id = ? AND id = ?", (listing_id, photo_id)
        ).fetchone()
        return self._to_photo(row) if row else None

    def list_for_listing(self, listing_id: str) -> list[ListingPhoto]:
        rows = self._conn.execute(
            "SELECT * FROM listing_photos WHERE listing_id = ? ORDER BY order_index, created_at",
            (listing_id,),
        )
        return [self._to_photo(row) for row in rows]

    def count_for_listing(self, listing_id: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM listing_photos WHERE listing_id = ?", (listing_id,)
        ).fetchone()[0]

    def exists_with_sha256(self, listing_id: str, sha256: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM listing_photos WHERE listing_id = ? AND sha256 = ?", (listing_id, sha256)
        ).fetchone()
        return row is not None

    @staticmethod
    def _to_photo(row: sqlite3.Row) -> ListingPhoto:
        data = dict(row)
        data["quality_warnings"] = json.loads(data.pop("quality_warnings_json"))
        data["privacy_flags"] = json.loads(data.pop("privacy_flags_json"))
        return ListingPhoto.model_validate(data)


class PhotoPresentationRepository:
    """The only photo columns that may change: order, cover and privacy flags."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def update(
        self,
        listing_id: str,
        photo_id: str,
        *,
        order_index: int,
        is_cover: bool,
        privacy_flags: list[str],
    ) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE listing_photos SET order_index = ?, is_cover = ?, privacy_flags_json = ?"
                " WHERE listing_id = ? AND id = ?",
                (
                    order_index,
                    int(is_cover),
                    json.dumps(sorted(privacy_flags)),
                    listing_id,
                    photo_id,
                ),
            )
        if cursor.rowcount == 0:
            raise NotFoundError(f"photo {photo_id} not found in listing {listing_id}")


class AgentRunRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, run: AgentRunRecord, tool_calls: list[ToolCallRecord]) -> None:
        """Run row and its tool calls are written together, in one transaction."""
        data = run.model_dump(mode="json")
        with self._conn:
            self._conn.execute(
                "INSERT INTO agent_runs (id, listing_id, agent_name, status, model,"
                " prompt_version, llm_calls, input_tokens, output_tokens, error,"
                " started_at, finished_at)"
                " VALUES (:id, :listing_id, :agent_name, :status, :model, :prompt_version,"
                " :llm_calls, :input_tokens, :output_tokens, :error, :started_at, :finished_at)",
                data,
            )
            self._conn.executemany(
                "INSERT INTO tool_calls"
                " (agent_run_id, tool_name, allowed, args_summary, error, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        run.id,
                        call.tool_name,
                        int(call.allowed),
                        call.args_summary,
                        call.error,
                        call.created_at.isoformat(),
                    )
                    for call in tool_calls
                ],
            )

    def list_for_listing(self, listing_id: str) -> list[AgentRunRecord]:
        rows = self._conn.execute(
            "SELECT * FROM agent_runs WHERE listing_id = ? ORDER BY started_at, id", (listing_id,)
        )
        return [AgentRunRecord.model_validate(dict(row)) for row in rows]

    def tool_calls_for_run(self, run_id: str) -> list[ToolCallRecord]:
        rows = self._conn.execute(
            "SELECT tool_name, allowed, args_summary, error, created_at FROM tool_calls"
            " WHERE agent_run_id = ? ORDER BY id",
            (run_id,),
        )
        return [ToolCallRecord.model_validate(dict(row)) for row in rows]

    def total_llm_calls(self, listing_id: str) -> int:
        return self._conn.execute(
            "SELECT COALESCE(SUM(llm_calls), 0) FROM agent_runs WHERE listing_id = ?",
            (listing_id,),
        ).fetchone()[0]


class NoteRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, listing_id: str, notes: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO listing_notes (listing_id, notes, created_at) VALUES (?, ?, ?)",
                (listing_id, notes, utc_now().isoformat()),
            )

    def get(self, listing_id: str) -> str:
        row = self._conn.execute(
            "SELECT notes FROM listing_notes WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row["notes"] if row else ""


class ClarificationRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, clarification: Clarification) -> Clarification:
        with self._conn:
            self._conn.execute(
                "INSERT INTO clarifications (id, listing_id, field_key, question, status,"
                " answer_fact_id, created_at)"
                " VALUES (:id, :listing_id, :field_key, :question, :status,"
                " :answer_fact_id, :created_at)",
                clarification.model_dump(mode="json"),
            )
        return clarification

    def get(self, listing_id: str, clarification_id: str) -> Clarification | None:
        row = self._conn.execute(
            "SELECT * FROM clarifications WHERE listing_id = ? AND id = ?",
            (listing_id, clarification_id),
        ).fetchone()
        return Clarification.model_validate(dict(row)) if row else None

    def list_for_listing(
        self, listing_id: str, status: ClarificationStatus | None = None
    ) -> list[Clarification]:
        query = "SELECT * FROM clarifications WHERE listing_id = ?"
        params: tuple = (listing_id,)
        if status is not None:
            query += " AND status = ?"
            params = (listing_id, status.value)
        rows = self._conn.execute(query + " ORDER BY created_at, id", params)
        return [Clarification.model_validate(dict(row)) for row in rows]

    def close(
        self,
        listing_id: str,
        clarification_id: str,
        status: ClarificationStatus,
        answer_fact_id: str | None = None,
    ) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE clarifications SET status = ?, answer_fact_id = ?"
                " WHERE listing_id = ? AND id = ? AND status = 'open'",
                (status.value, answer_fact_id, listing_id, clarification_id),
            )
        if cursor.rowcount == 0:
            raise NotFoundError(f"open clarification {clarification_id} not found")


class ComparableRepository:
    """Synthetic comparables only. Queries use plain SQL filters, no vectors (doc §9.1)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM comparable_listings").fetchone()[0]

    def add_many(self, rows: list) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT INTO comparable_listings (make, model, model_year, mileage_km, city,"
                " price_try, description, is_synthetic) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                [
                    (
                        r.make,
                        r.model,
                        r.model_year,
                        r.mileage_km,
                        r.city,
                        r.price_try,
                        r.description,
                    )
                    for r in rows
                ],
            )

    def search(self, make: str, model: str, year_min: int, year_max: int) -> list[tuple[str, int]]:
        """Return (city, price) pairs; the city filter is applied by the caller in Python
        because SQLite's lower() does not know Turkish letters."""
        rows = self._conn.execute(
            "SELECT city, price_try FROM comparable_listings"
            " WHERE lower(make) = lower(?) AND lower(model) = lower(?)"
            " AND model_year BETWEEN ? AND ?",
            (make, model, year_min, year_max),
        )
        return [(row["city"], row["price_try"]) for row in rows]


class ApprovalRepository:
    """Append-only record of human decisions at the two approval gates."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(self, approval: Approval) -> Approval:
        with self._conn:
            self._conn.execute(
                "INSERT INTO approvals (id, listing_id, gate, decision, draft_id, comment,"
                " actor_name, created_at)"
                " VALUES (:id, :listing_id, :gate, :decision, :draft_id, :comment,"
                " :actor_name, :created_at)",
                approval.model_dump(mode="json"),
            )
        return approval

    def list_for_listing(self, listing_id: str) -> list[Approval]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE listing_id = ? ORDER BY created_at, id", (listing_id,)
        )
        return [Approval.model_validate(dict(row)) for row in rows]

    def final_approval_for(self, listing_id: str, draft_id: str) -> Approval | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE listing_id = ? AND draft_id = ? AND gate = ?"
            " AND decision = ? ORDER BY created_at DESC LIMIT 1",
            (listing_id, draft_id, ApprovalGate.FINAL.value, ApprovalDecision.APPROVED.value),
        ).fetchone()
        return Approval.model_validate(dict(row)) if row else None
