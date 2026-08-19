import json
from pathlib import Path
import pytest
from virtizai_core.transactions import TransactionJournal
from virtizai_core.db import Database
from virtizai_core.transactions import StartupTransactionReconciler

def test_transaction_journal_atomic_lifecycle(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path)
    transaction = journal.create("native_rollback", target_version="0.1.17", data_restore_required=True)
    assert transaction["stage"] == "REQUESTED"
    transaction["stage"] = "RESTART_PENDING"
    journal.write(transaction)
    assert journal.read(transaction["transaction_id"])["target_version"] == "0.1.17"
    assert not list(journal.directory.glob("*.tmp"))
    assert journal.pending()[0]["data_restore_required"] is True
    journal.remove(transaction["transaction_id"])
    assert journal.pending() == []

def test_transaction_journal_rejects_invalid_stage_and_path(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path)
    with pytest.raises(ValueError):
        journal.write({"transaction_id": "tx", "stage": "NOT_A_STAGE"})
    with pytest.raises(ValueError):
        journal.path("../escape")

def test_transaction_journal_ignores_partial_or_invalid_records(tmp_path: Path) -> None:
    journal = TransactionJournal(tmp_path)
    (journal.directory / "partial.json").write_text("{")
    (journal.directory / "other.json").write_text(json.dumps({"stage": "REQUESTED"}))
    assert journal.pending() == [{"stage": "REQUESTED"}]

def test_startup_reconciler_persists_and_clears_completed_transaction(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    database.open()
    journal = TransactionJournal(tmp_path)
    transaction = journal.create("native_rollback", target_version="0.1.17", target_schema=10, artifact_path="/var/lib/virtizai/staging/old.deb", backup={"backup_ref": "/var/lib/virtizai/backups/t.tar.gz", "checksum_sha256": "a" * 64, "schema_version": 10}, data_restore_required=True)
    transaction["stage"] = "RESTART_PENDING"
    journal.write(transaction)
    reconciler = StartupTransactionReconciler(database, journal)
    assert reconciler.reconcile("0.1.17", 10) == 1
    row = database.fetch_one("SELECT version, status, metadata_json FROM update_history WHERE id=?", (transaction["transaction_id"],))
    assert row["version"] == "0.1.17"
    assert row["status"] == "known_good"
    assert json.loads(row["metadata_json"])["data_restore_required"] is True
    assert journal.pending() == []
    assert reconciler.reconcile("0.1.17", 10) == 0
    database.close()
