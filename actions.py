"""
Write-side operations the dashboard performs: change a lot's status and record the
change on the ledger in the same transaction, so the two can never disagree.
"""
from __future__ import annotations

import time

import chain


def set_status(conn, lots: list[tuple[int, str, str]], new_status: str, reason: str,
               party: str = chain.PLANT_NAME) -> int:
    """lots: (lot_id, lot_code, current_status). Returns how many lots changed.

    RECALLED is terminal here: a recalled lot is never silently downgraded to HOLD.
    The database update and the ledger block commit together or not at all.
    """
    if new_status not in ("OK", "HOLD", "RECALLED"):
        raise ValueError(new_status)
    now_ms = int(time.time() * 1000)
    changed = 0
    with conn.transaction():
        cur = conn.cursor()
        txs = []
        for lot_id, code, old in lots:
            if old == new_status or (old == "RECALLED" and new_status != "RECALLED"):
                continue
            cur.execute("UPDATE lots SET status = %s WHERE lot_id = %s", (new_status, lot_id))
            payload, signers = chain.status_payload(code, old, new_status, reason, party)
            txs.append(chain.make_tx("STATUS_CHANGE", code, now_ms, payload, signers))
            changed += 1
        if txs:
            chain.append_txs(cur, txs, live=True)
    if changed:
        chain.save_checkpoint(conn.cursor())
    return changed
