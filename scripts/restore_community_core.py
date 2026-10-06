"""One-shot recovery of reviewed shared functions and the first-run audit name.

Uses immutable source and explicit allowlists. Only app/db.py and one audited
expression in app/main.py may change. Commit only after real HTTP validation.
"""
import ast
import hashlib
from pathlib import Path
import re
import subprocess

BASE = "d6bb35c65b01bceaae9c95246f8d6fc5ad009aa6"
SOURCE_BLOB = "af5c31e8a8a2990ec5a68115ad5483a37458389b"
TARGET_BLOB = "6180df6f8498cf00428382e8a8e23995669deddf"
MAIN_BLOB = "ada22f06a76f88268a52a839f11907c50b819a5d"
NAMES = {
    "active_system_session_counts", "delete_other_system_sessions", "security_event_summary",
    "add_activity", "recent_activity", "_severity_rank", "_upsert_notification_conn",
    "create_notification", "_record_diagnostic_event_conn", "_sync_diagnostic_state_conn",
    "_resolve_missing_diagnostic_states_conn", "active_diagnostic_states", "_event_diagnostic_row",
    "diagnostic_history", "charge_point_health", "sync_notifications", "_notification_role_conn",
    "notifications_for_user", "mark_notification_read", "dismiss_notification", "dismiss_read_notifications",
    "_normalize_push_severity", "save_web_push_subscription", "update_web_push_preference",
    "remove_web_push_subscription", "remove_web_push_subscription_by_id", "web_push_subscriptions_for_user",
    "web_push_subscription_for_user_endpoint", "pending_web_push_deliveries", "mark_web_push_delivery",
    "mark_web_push_success", "mark_web_push_error", "mark_all_notifications_read",
}


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def main():
    target = Path("app/db.py")
    before = target.read_bytes()
    assert blob_sha(before) == TARGET_BLOB, "Target moved; review before retrying"
    main_path = Path("app/main.py")
    main_before = main_path.read_bytes()
    assert blob_sha(main_before) == MAIN_BLOB, "Main source moved; review before retrying"
    source = subprocess.check_output(["git", "show", BASE + ":app/db.py"])
    assert blob_sha(source) == SOURCE_BLOB, "Unexpected baseline source"
    text = source.decode("utf-8")
    old = {n.name: n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)}
    existing = {n.name for n in ast.parse(before.decode()).body if isinstance(n, ast.FunctionDef)}
    assert not NAMES & existing, "Refusing to overwrite existing functions"
    assert NAMES <= old.keys(), "Baseline does not contain every reviewed function"
    pieces = []
    for node in sorted((old[name] for name in NAMES), key=lambda n: n.lineno):
        piece = ast.get_source_segment(text, node)
        if node.name == "sync_notifications":
            legacy = '''                bonus=_bonus_wallet_conn(conn,user["id"])
                if state.get("blocked") and bonus["available_kwh"] > 1e-9:
                    sev,title="warning","Monatsbudget verbraucht – Bonus aktiv"
                elif state.get("blocked"):
'''
            assert piece.count(legacy) == 1, "Budget notification shape changed"
            piece = piece.replace(legacy, '                if state.get("blocked"):\n')
        assert not re.search(r"portal|bonus|achievement|leaderboard|smart_charg|cost_center|fleet_|registration_|rfid_self_enroll|weekly_hours", piece, re.I), node.name
        pieces.append(piece)
    addition = "\n\n# Shared Community audit, notifications, diagnostics, push and session helpers.\n\n" + "\n\n\n".join(pieces) + "\n"
    result = before.decode("utf-8") + addition
    compile(result, str(target), "exec")
    assert result.startswith(before.decode("utf-8"))
    main_text = main_before.decode("utf-8")
    broken = "Monatsguthaben: {'aktiv' if credit_enabled else 'aus'}"
    fixed = "Monatslimit: {'aktiv' if default_limit_enabled else 'aus'}"
    assert main_text.count(broken) == 1, "Unexpected first-run audit expression"
    main_result = main_text.replace(broken, fixed)
    compile(main_result, str(main_path), "exec")
    target.write_text(result, encoding="utf-8")
    main_path.write_text(main_result, encoding="utf-8")
    print(f"Restored {len(pieces)} reviewed shared functions and corrected first-run audit variable")
    for name in sorted(NAMES):
        print("RESTORED", name)


if __name__ == "__main__":
    main()
