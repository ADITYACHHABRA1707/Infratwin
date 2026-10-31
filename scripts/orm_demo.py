"""
Show the ORM at work: each operation, the SQL SQLAlchemy emitted, and the result.
Everything is rolled back at the end, so the data is unchanged.

    python scripts/orm_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import event, select  # noqa: E402

import services  # noqa: E402
from auth import Principal  # noqa: E402
from db import SessionLocal, engine  # noqa: E402
from models import Lot, Product  # noqa: E402

MANAGER = Principal(2, "manager@chaintwin.local", "Mohan Manager", "MANAGER")


@event.listens_for(engine, "before_cursor_execute")
def show_sql(conn, cursor, statement, parameters, context, executemany):
    flat = " ".join(statement.split())
    print(f"    SQL> {flat[:230]}{'…' if len(flat) > 230 else ''}")
    if parameters:
        print(f"    params> {parameters}")


def step(title: str) -> None:
    print(f"\n--- {title}")


def main() -> int:
    s = SessionLocal()
    try:
        step("CREATE  services.create_product(...)  [ORM INSERT]")
        p = services.create_product(s, MANAGER, sku="ORM-DEMO", name="ORM demo yoghurt", category="FINISHED",
                                    temp_min=2, temp_max=6, shelf_life_days=21)
        print(f"  -> product_id={p.product_id} sku={p.sku}")

        step("READ    services.list_products(search='orm', category='FINISHED', page=1, page_size=5)  [SELECT + LIMIT/OFFSET]")
        page = services.list_products(s, search="orm", category="FINISHED", page=1, page_size=5)
        print(f"  -> {page.total} match, page {page.page}/{page.pages}: {[x.sku for x in page.items]}")

        step("UPDATE  services.update_product(...)")
        services.update_product(s, MANAGER, p.product_id, name="ORM demo yoghurt 500 g", temp_min=1, temp_max=5,
                                shelf_life_days=25)
        s.flush()
        print(f"  -> name={p.name!r} shelf_life_days={p.shelf_life_days}")

        step("DELETE  services.delete_product(...)")
        services.delete_product(s, MANAGER, p.product_id)
        print(f"  -> exists afterwards: {s.get(Product, p.product_id) is not None}")

        step("JOIN    services.list_lots(search='yoghurt', lot_type='STOCK', page_size=3)  [lots JOIN products JOIN parties]")
        lots = services.list_lots(s, search="yoghurt", lot_type="STOCK", page_size=3)
        print(f"  -> {lots.total:,} lots match; first: {[(l.lot_code, l.product.name, l.party.name) for l in lots.items][:2]}")

        step("AGGREGATE  services.inventory_by_party(limit=3)  [LEFT JOIN + GROUP BY + conditional count]")
        for row in services.inventory_by_party(s, limit=3):
            print(f"  -> {row[0]:28} lots={row[2]:<4} qty={float(row[3]):>10,.0f} on_hold={row[4]}")

        step("SQL INJECTION attempt  services.list_lots(search=\"'; DROP TABLE lots; --\")  [value is a bound parameter]")
        hostile = services.list_lots(s, search="'; DROP TABLE lots; --")
        print(f"  -> matches: {hostile.total}; lots table still has {s.scalar(select(Lot.lot_id).limit(1)) is not None and 'rows' or 'NO ROWS'}")

        step("CONSTRAINT  services.create_product(sku='RAW-MILK')  [duplicate -> friendly error]")
        try:
            services.create_product(s, MANAGER, sku="RAW-MILK", name="dup", category="RAW", temp_min=0, temp_max=4,
                                    shelf_life_days=3)
        except services.ServiceError as exc:
            print(f"  -> ServiceError({exc.status}): {exc}")
    finally:
        s.rollback()
        s.close()
        print("\n(rolled back: no data changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
