"""One-time, guarded publication closeout. Removed from the final source tree."""
import ast
from pathlib import Path


def replace_function(path, name, replacement):
    file = Path(path)
    text = file.read_text(encoding='utf-8')
    matches = [n for n in ast.walk(ast.parse(text)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(matches) == 1, (path, name)
    node = matches[0]
    lines = text.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = [replacement.strip('\n') + '\n']
    updated = ''.join(lines)
    ast.parse(updated)
    file.write_text(updated, encoding='utf-8')


def edit(path, old, new):
    file = Path(path)
    text = file.read_text(encoding='utf-8')
    assert text.count(old) == 1, (path, old[:100], text.count(old))
    file.write_text(text.replace(old, new, 1), encoding='utf-8')


assert 'APP_VERSION = "0.9.7.96"' in Path('app/main.py').read_text()
replace_function('app/db.py', 'update_transaction_from_meter', '''
def update_transaction_from_meter(tx, meter_kwh=None, power_kw=None, measured_at=None):
    """Accept only finite, non-regressing measurements for an active session."""
    with _lock, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx,)).fetchone()
        if not row:
            return None
        if row['status'] != 'Active' or row['ended_at'] is not None:
            return row['energy_kwh']
        previous_at = _parse_iso_utc(row['last_meter_at'])
        incoming_at = _parse_iso_utc(measured_at)
        if measured_at is not None and incoming_at is None:
            return row['energy_kwh']
        if previous_at and incoming_at and incoming_at < previous_at:
            return row['energy_kwh']
        try:
            current = float(meter_kwh) if meter_kwh is not None else None
            power = float(power_kw) if power_kw is not None else None
        except (TypeError, ValueError):
            return row['energy_kwh']
        if any(value is not None and (not math.isfinite(value) or value < 0) for value in (current, power)):
            return row['energy_kwh']
        last = row['last_meter_kwh']
        if current is not None and last is not None and current < float(last):
            return row['energy_kwh']
        start = row['meter_start_kwh']
        energy = row['energy_kwh']
        if current is not None:
            if start is None:
                start = current
            energy = max(float(energy or 0), max(0.0, current - float(start)))
        peak = max(float(row['max_power_kw'] or 0), power or 0)
        conn.execute("UPDATE transactions SET meter_start_kwh=?,energy_kwh=?,max_power_kw=?,last_meter_kwh=COALESCE(?,last_meter_kwh),last_meter_at=COALESCE(?,last_meter_at) WHERE id=?", (start, energy, peak, current, measured_at, tx))
        conn.execute("UPDATE connectors SET meter_current_kwh=COALESCE(?,meter_current_kwh) WHERE transaction_id=?", (current, tx))
        conn.commit()
        return energy
''')
edit('app/db.py', '''        row=conn.execute("SELECT charge_point_id,connector_id,meter_start_kwh,last_meter_kwh,user_id FROM transactions WHERE id=?",(tx,)).fetchone()
        if not row:
            return None
        start = row[2]''', '''        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT charge_point_id,connector_id,meter_start_kwh,last_meter_kwh,user_id,status,ended_at,energy_kwh FROM transactions WHERE id=?",(tx,)).fetchone()
        if not row:
            return None
        if row[5] != "Active" or row[6] is not None:
            return row[7]
        start = row[2]''')
edit('app/db.py', '''        if stop is None or (start is not None and stop < float(start)):''', '''        if stop is None or not math.isfinite(stop) or (start is not None and stop < float(start)) or (last is not None and stop < float(last)):''')
edit('app/db.py', '''    reason="Autorisierung gültig"
    if budget''', '''    with _lock, _connect() as conn:
        card = conn.execute("SELECT id,max_concurrent_sessions FROM rfid_cards WHERE uid=? LIMIT 1", (rfid,)).fetchone()
        card_limit = max(1, int(card[1] or 1)) if card else 1
        if card:
            count = conn.execute("SELECT COUNT(*) FROM transactions WHERE status='Active' AND ended_at IS NULL AND (rfid_card_id=? OR (rfid_card_id IS NULL AND id_tag=?))", (card[0], rfid)).fetchone()[0]
        else:
            count = conn.execute("SELECT COUNT(*) FROM transactions WHERE status='Active' AND ended_at IS NULL AND id_tag=?", (rfid,)).fetchone()[0]
        user_limit = conn.execute("SELECT max_concurrent_sessions FROM users WHERE id=?", (user.get('id'),)).fetchone()
        user_count = conn.execute("SELECT COUNT(*) FROM transactions WHERE status='Active' AND ended_at IS NULL AND user_id=?", (user.get('id'),)).fetchone()[0]
        full = count >= card_limit or (user_limit and user_count >= max(1, int(user_limit[0] or 1)))
    if full:
        return {"accepted":False,"ocpp_status":"ConcurrentTx","reason":"Limit gleichzeitiger Ladevorgaenge erreicht","user":user,"budget":budget}
    reason="Autorisierung gültig"
    if budget''')
edit('app/ocpp_server.py', '''    ACTIVE_CONNECTIONS[cp_id] = {"connected_at": connected_at, "remote": remote, "subprotocol": subprotocol, "transport":"WSS" if secure else "WS"}''', '''    connection_info = {"connected_at": connected_at, "remote": remote, "subprotocol": subprotocol, "transport":"WSS" if secure else "WS"}
    ACTIVE_CONNECTIONS[cp_id] = connection_info''')
edit('app/ocpp_server.py', '''    ACTIVE_CONNECTIONS[cp_id]["charge_point"] = cp''', '''    connection_info["charge_point"] = cp''')
edit('app/ocpp_server.py', '''        ACTIVE_CONNECTIONS.pop(cp_id, None)
        db.upsert_charge_point(cp_id, status="Offline", power_kw=0)
        db.add_event(cp_id, "Disconnected")
        log.info("Charge point disconnected: %s", cp_id)''', '''        if ACTIVE_CONNECTIONS.get(cp_id) is connection_info:
            ACTIVE_CONNECTIONS.pop(cp_id, None)
            db.upsert_charge_point(cp_id, status="Offline", power_kw=0)
            db.add_event(cp_id, "Disconnected")
            log.info("Charge point disconnected: %s", cp_id)''')
edit('app/ocpp_server.py', '''        if tx is None and cp.get("transaction_id"):
            tx = int(cp["transaction_id"])

        parsed_entries''', '''        reported_tx = kwargs.get("transaction_id")
        if reported_tx is not None:
            try:
                candidate = db.get_transaction(int(reported_tx)) or {}
            except (TypeError, ValueError):
                candidate = {}
            tx = int(candidate['id']) if (candidate.get('charge_point_id') == self.id and int(candidate.get('connector_id') or 0) == cid and cid > 0 and candidate.get('status') == 'Active' and candidate.get('ended_at') is None) else None
        elif tx is not None:
            candidate = db.get_transaction(tx) or {}
            if candidate.get('charge_point_id') != self.id or int(candidate.get('connector_id') or 0) != cid or candidate.get('status') != 'Active' or candidate.get('ended_at') is not None:
                tx = None

        parsed_entries''')
edit('app/ocpp_server.py', '''        tx_before=db.get_transaction(tx) or {}
        try:
            stop_meter_kwh''', '''        tx_before=db.get_transaction(tx) or {}
        if tx_before.get('charge_point_id') != self.id:
            db.add_event(self.id, 'StopTransactionRejected', 'Transaction does not belong to this charge point')
            return call_result.StopTransaction(id_tag_info={"status": "Invalid"})
        try:
            stop_meter_kwh''')
edit('app/ocpp_server.py', '''        db.upsert_charge_point(self.id, transaction_id=None, power_kw=0)
        db.mark_message(self.id, "StopTransaction")''', '''        # stop_transaction preserves references to other active connectors.
        db.mark_message(self.id, "StopTransaction")''')
replace_function('app/backup.py', 'restore_backup', '''
def restore_backup(path: Path):
    """Stage files first and restore both the database and applied files on failure."""
    validate_restore(path)
    safety = create_backup(label="pre-restore", keep_local=True)
    root = db.DATA_DIR.resolve()
    with tempfile.TemporaryDirectory(prefix=".voltcore-restore-", dir=root) as td:
        stage = Path(td)
        unpacked = stage / "unpacked"
        with zipfile.ZipFile(path, "r") as archive:
            archive.extractall(unpacked)
        source_db = unpacked / "database" / "ocpp.sqlite3"
        source = sqlite3.connect(source_db)
        try:
            result = source.execute("PRAGMA integrity_check").fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise ValueError("Datenbank im Backup ist beschaedigt.")
        finally:
            source.close()
        excluded = {CREDENTIAL_FILE.resolve(), SMTP_CREDENTIAL_FILE.resolve(), (root / '.update_github_token').resolve(), db.DB_PATH.resolve()}
        plan = []
        data_stage = unpacked / 'data'
        for item in sorted(data_stage.rglob('*')) if data_stage.exists() else []:
            if not item.is_file():
                continue
            relative = item.relative_to(data_stage)
            target = root / relative
            resolved = target.resolve()
            if root not in resolved.parents:
                raise ValueError('Restore target escapes the data directory.')
            if resolved in excluded or resolved == BACKUP_DIR.resolve() or BACKUP_DIR.resolve() in resolved.parents or target.name in {'ocpp.sqlite3-wal', 'ocpp.sqlite3-shm'}:
                continue
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError('Unsupported restore target.')
            prepared = stage / ('new-' + str(len(plan)))
            original = stage / ('old-' + str(len(plan))) if target.exists() else None
            shutil.copy2(item, prepared)
            if original is not None:
                shutil.copy2(target, original)
            plan.append((target, prepared, original))
        rollback_db = stage / 'rollback.sqlite3'
        applied = []
        database_changed = False
        def copy_database(source_path, target_path):
            source = sqlite3.connect(source_path, timeout=30)
            target = sqlite3.connect(target_path, timeout=30)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
        try:
            with db._lock:
                copy_database(db.DB_PATH, rollback_db)
                database_changed = True
                copy_database(source_db, db.DB_PATH)
                for target, prepared, original in plan:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(prepared, target)
                    applied.append((target, original))
            db.init_db()
        except Exception:
            with db._lock:
                if database_changed:
                    copy_database(rollback_db, db.DB_PATH)
                for target, original in reversed(applied):
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        os.replace(original, target)
            raise
    return {"ok": True, "safety_backup": safety}
''')
edit('app/backup.py', 'import os\n', 'import os\n')
# The prior race test treated the intended atomic rejection as a failure.
# Catch only that exact admission result; all other exceptions still fail.
edit('tests/test_p01_parallel_transactions.py', '''                return db.start_transaction(CP_ID, connector_id=connector, id_tag=uid)
            return None''', '''                try:
                    return db.start_transaction(CP_ID, connector_id=connector, id_tag=uid)
                except ValueError as exc:
                    if str(exc) not in {"RFID_CONCURRENT_SESSION_LIMIT", "USER_CONCURRENT_SESSION_LIMIT"}:
                        raise
            return None''')
removed = 0
for path in Path('tests').glob('test_*.py'):
    text = path.read_text(encoding='utf-8')
    removed += text.count('    @unittest.expectedFailure\n')
    path.write_text(text.replace('    @unittest.expectedFailure\n', ''), encoding='utf-8')
assert removed == 8, removed
edit('app/main.py', 'APP_VERSION = "0.9.7.96"', 'APP_VERSION = "0.9.7.97"')
# Keep normal release automation, remove obsolete one-shot mutation workflows.
for name in ('community-cleanup-release.yml', 'community-final-check.yml', 'publication-audit.yml', 'publication-closure.yml'):
    Path('.github/workflows', name).unlink(missing_ok=True)
# Include the HTTP integration-test dependency in ordinary release runs too.
for name in ('ci.yml', 'release.yml', 'community-qa.yml'):
    path = Path('.github/workflows', name)
    if path.exists():
        text = path.read_text().replace('pip install -r requirements.txt\n', 'pip install -r requirements.txt -r tests/requirements.txt\n')
        path.write_text(text)
print('Publication closeout patch applied; all 8 cases are now required to pass.')
