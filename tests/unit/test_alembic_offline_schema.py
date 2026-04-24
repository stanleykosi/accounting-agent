"""
Purpose: Verify the offline Alembic render includes recently added canonical schema.
Scope: Regression coverage for ledger import and close-run binding tables that must
exist in migrated environments before dashboard reads execute.
Dependencies: The repository Alembic config and the current Python environment.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_offline_alembic_sql_includes_ledger_import_schema() -> None:
    """The migration chain should render the ledger import tables used by close-run reads."""

    repo_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.setdefault(
        "DATABASE_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/accounting_agent",
    )

    completed = subprocess.run(
        [
            "uv",
            "run",
            "alembic",
            "-c",
            "infra/alembic.ini",
            "upgrade",
            "head",
            "--sql",
        ],
        check=False,
        capture_output=True,
        cwd=repo_root,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr

    rendered_sql = completed.stdout.lower()

    assert "create table general_ledger_import_batches" in rendered_sql
    assert "create table general_ledger_import_lines" in rendered_sql
    assert "create table trial_balance_import_batches" in rendered_sql
    assert "create table trial_balance_import_lines" in rendered_sql
    assert "create table close_run_ledger_bindings" in rendered_sql
    assert "transaction_group_key" in rendered_sql
    assert "message_order" in rendered_sql
    assert "uq_chat_messages_thread_message_order" in rendered_sql
    assert "drop constraint fk_chat_messages_linked_action_id_recommendations" in rendered_sql
    assert "fk_chat_messages_linked_action_id_chat_action_plans" in rendered_sql
