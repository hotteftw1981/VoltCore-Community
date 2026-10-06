"""One-shot, hash-guarded repair for the Community cleanup branch only."""
import ast
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BLOB = "f48cea4d08b081722368b2fcce1bcad6efda5e10"
FUNCTION = '''def _update_tx_cost_conn(conn, tx_id):
    """Price metered energy using this transaction's tariff snapshot only.

    A monthly kWh limit controls authorization and warnings, not free credit.
    Missing/invalid energy or tariff data is unknown, never implicitly free.
    Only this transaction is updated; historical rows are not recalculated.
    """
    from decimal import InvalidOperation

    row = conn.execute(
        "SELECT energy_kwh,price_cents_per_kwh FROM transactions WHERE id=?",
        (tx_id,),
    ).fetchone()
    if row is None:
        return

    cost_cents = None
    try:
        energy = Decimal(str(row["energy_kwh"]))
        price = Decimal(str(row["price_cents_per_kwh"]))
        if (energy.is_finite() and price.is_finite()
                and energy >= 0 and price >= 0
                and price == price.to_integral_value()):
            amount = (energy * price).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            # SQLite INTEGER is signed 64-bit; reject unusable meter/tariff data.
            if amount <= 9223372036854775807:
                cost_cents = int(amount)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        pass
    conn.execute("UPDATE transactions SET cost_cents=? WHERE id=?", (cost_cents, tx_id))
'''


def replace_function(source):
    tree = ast.parse(source)
    found = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name == "_update_tx_cost_conn"]
    if len(found) != 1:
        raise ValueError("Expected exactly one billing function; refusing to patch")
    node = found[0]
    lines = source.splitlines(keepends=True)
    old = "".join(lines[node.lineno - 1:node.end_lineno])
    if old.rstrip() == FUNCTION.rstrip():
        return source
    if "monthly_kwh_limit" not in old or "chargeable_energy" not in old:
        raise ValueError("Billing implementation differs from reviewed code")
    result = "".join(lines[:node.lineno - 1]) + FUNCTION + "".join(lines[node.end_lineno:])
    ast.parse(result)
    return result


def main():
    path = ROOT / "app" / "db.py"
    raw = path.read_bytes()
    source = raw.decode("utf-8")
    updated = replace_function(source)
    if updated == source:
        print("Billing correction already present")
        return
    blob_sha = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    if blob_sha != EXPECTED_BLOB:
        raise SystemExit("db.py changed since review; refusing to overwrite")
    path.write_text(updated, encoding="utf-8")
    print("Replaced only _update_tx_cost_conn; all other source remains unchanged")


if __name__ == "__main__":
    main()
