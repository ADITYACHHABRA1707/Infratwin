"""
Streamlit pages for the Registry (lots, products, parties, analytics, audit trail) and
Users administration. All data access goes through services.py (ORM); each write form is
shown only to roles that hold the permission, and the service re-checks it regardless.
"""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

import auth
import services
from auth import Forbidden, Principal
from db import session_scope
from services import ServiceError

log = logging.getLogger("chaintwin.ui")


def guarded(action, success: str | None = None):
    """Run a write. Expected problems become a clear message; anything else is logged
    and reported without leaking internals."""
    try:
        result = action()
    except ServiceError as exc:
        st.error(str(exc))
    except Forbidden as exc:
        st.error(f"Not allowed: {exc}")
    except Exception:           # noqa: BLE001 - last line of defence for the UI
        log.exception("unexpected error in UI action")
        st.error("Something went wrong. The change was not saved.")
    else:
        if success:
            st.success(success)
        return result
    return None


def pager(key: str, pages: int) -> int:
    cur = min(max(1, st.session_state.get(key, 1)), pages)
    st.session_state[key] = cur
    a, b, c = st.columns([1, 1, 6])
    if a.button("◀ Prev", key=f"{key}_prev", disabled=cur <= 1):
        st.session_state[key] = cur - 1
        st.rerun()
    if b.button("Next ▶", key=f"{key}_next", disabled=cur >= pages):
        st.session_state[key] = cur + 1
        st.rerun()
    c.caption(f"Page {cur} of {pages}")
    return cur


def _opt(label: str, options: list[str]) -> str | None:
    v = st.selectbox(label, ["All"] + options)
    return None if v == "All" else v


# ===========================================================================
def registry_page(who: Principal) -> None:
    st.subheader("Registry")
    st.caption(f"Signed in as **{who.full_name}** · role **{who.role}**"
               + ("" if who.can("registry:write") else " · read-only access"))
    t_lots, t_prod, t_part, t_stats, t_audit = st.tabs(
        ["Lots", "Products", "Parties", "Analytics", "Audit trail"])
    with t_lots:
        _lots(who)
    with t_prod:
        _products(who)
    with t_part:
        _parties(who)
    with t_stats:
        _analytics()
    with t_audit:
        _audit()


def _lots(who: Principal) -> None:
    with session_scope() as s:
        parties = services.list_parties(s, page_size=200).items
        products = services.list_products(s, page_size=200).items
    f1, f2, f3, f4 = st.columns([3, 2, 2, 2])
    search = f1.text_input("Search lot code, product or holder", key="lot_search")
    lot_type = f2.selectbox("Type", ["All"] + list(services.LOT_TYPES), key="lot_type")
    status = f3.selectbox("Status", ["All"] + list(services.LOT_STATUSES), key="lot_status")
    size = f4.selectbox("Rows per page", [10, 20, 50], index=1, key="lot_size")
    key = f"lots_page_{hash((search, lot_type, status, size))}"
    with session_scope() as s:
        page = services.list_lots(s, search=search, lot_type=None if lot_type == "All" else lot_type,
                                  status=None if status == "All" else status,
                                  page=st.session_state.get(key, 1), page_size=size)
    st.caption(f"{page.total:,} lots match")
    st.dataframe(pd.DataFrame([{"lot_code": l.lot_code, "type": l.lot_type, "product": l.product.name,
                                "holder": l.party.name, "quantity": float(l.quantity),
                                "created": l.created_at.strftime("%d %b %H:%M"), "status": l.status}
                               for l in page.items]), hide_index=True, use_container_width=True)
    pager(key, page.pages)

    st.markdown("##### Receive a raw lot")
    if not who.can("lots:write"):
        st.info("Your role (AUDITOR) can view lots but not create them.")
    else:
        raw_products = [p for p in products if p.category == "RAW"]
        suppliers = [p for p in parties if p.party_type == "Supplier"]
        with st.form("raw_lot", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            prod = c1.selectbox("Product", raw_products, format_func=lambda p: f"{p.name} ({p.sku})")
            sup = c2.selectbox("Supplier", suppliers, format_func=lambda p: p.name)
            qty = c3.number_input("Quantity", min_value=0.0, value=1000.0, step=50.0)
            if st.form_submit_button("Register lot", type="primary"):
                def go():
                    with session_scope() as s:
                        lot = services.create_raw_lot(s, who, product_id=prod.product_id,
                                                      party_id=sup.party_id, quantity=qty)
                        code = lot.lot_code
                    services.save_ledger_checkpoint()
                    return code
                code = guarded(go)
                if code:
                    st.success(f"Lot {code} registered and anchored on the ledger.")

    st.markdown("##### Change a lot's status")
    if not who.can("status:change"):
        st.info("Your role cannot change lot status.")
    else:
        with st.form("lot_status", clear_on_submit=True):
            c1, c2, c3 = st.columns([2, 1, 3])
            code = c1.text_input("Lot code", placeholder="e.g. RM-261030-01")
            new = c2.selectbox("New status", list(services.LOT_STATUSES))
            why = c3.text_input("Reason")
            if st.form_submit_button("Apply and sign"):
                def go():
                    with session_scope() as s:
                        found = services.list_lots(s, search=code.strip(), page_size=5).items
                        exact = [l for l in found if l.lot_code == code.strip()]
                        if not exact:
                            raise ServiceError("No lot with that code.", status=404)
                        n = services.set_lot_status(s, who, [exact[0].lot_id], new, why)
                    if n:
                        services.save_ledger_checkpoint()
                    return n
                n = guarded(go)
                if n is not None:
                    st.success("Status updated and recorded on the ledger." if n
                               else "No change: the lot already has that status (or is RECALLED).")
    st.caption("Lots are never edited or deleted here: the ledger has signed their quantity and "
               "genealogy, so only their status can change, and every change is signed.")


def _products(who: Principal) -> None:
    c1, c2 = st.columns([3, 2])
    search = c1.text_input("Search SKU or name", key="prod_search")
    cat = c2.selectbox("Category", ["All", "RAW", "FINISHED"], key="prod_cat")
    key = f"prod_page_{hash((search, cat))}"
    with session_scope() as s:
        page = services.list_products(s, search=search, category=None if cat == "All" else cat,
                                      page=st.session_state.get(key, 1), page_size=10)
    st.dataframe(pd.DataFrame([{"id": p.product_id, "sku": p.sku, "name": p.name, "category": p.category,
                                "temp_min": p.temp_min, "temp_max": p.temp_max,
                                "shelf_life_days": p.shelf_life_days} for p in page.items]),
                 hide_index=True, use_container_width=True)
    pager(key, page.pages)
    if not who.can("registry:write"):
        st.info("Read-only for your role.")
        return

    st.markdown("##### Add a product")
    with st.form("new_product", clear_on_submit=True):
        a, b, c = st.columns(3)
        sku = a.text_input("SKU (capitals, digits, hyphen)")
        name = b.text_input("Name")
        category = c.selectbox("Category", ["RAW", "FINISHED"])
        d, e, f = st.columns(3)
        tmin = d.number_input("Min temp °C", value=0.0)
        tmax = e.number_input("Max temp °C", value=6.0)
        shelf = f.number_input("Shelf life (days)", min_value=0, value=7, step=1)
        if st.form_submit_button("Add product", type="primary"):
            def go():
                with session_scope() as s:
                    services.create_product(s, who, sku=sku, name=name, category=category,
                                            temp_min=tmin, temp_max=tmax, shelf_life_days=int(shelf))
            guarded(go, f"Product {sku.strip().upper()} added.")

    st.markdown("##### Edit or delete a product")
    with session_scope() as s:
        all_products = services.list_products(s, page_size=200).items
    chosen = st.selectbox("Product", all_products, format_func=lambda p: f"{p.sku} · {p.name}", key="prod_edit")
    if chosen:
        with st.form("edit_product"):
            a, b, c, d = st.columns(4)
            n = a.text_input("Name", chosen.name)
            tmin = b.number_input("Min temp °C", value=float(chosen.temp_min or 0.0))
            tmax = c.number_input("Max temp °C", value=float(chosen.temp_max or 0.0))
            shelf = d.number_input("Shelf life (days)", min_value=0, value=int(chosen.shelf_life_days), step=1)
            save, drop = st.columns(2)
            do_save = save.form_submit_button("Save changes")
            do_drop = drop.form_submit_button("Delete product")
        if do_save:
            def go():
                with session_scope() as s:
                    services.update_product(s, who, chosen.product_id, name=n, temp_min=tmin,
                                            temp_max=tmax, shelf_life_days=int(shelf))
            guarded(go, "Product updated.")
        if do_drop:
            def go():
                with session_scope() as s:
                    services.delete_product(s, who, chosen.product_id)
            guarded(go, "Product deleted.")


def _parties(who: Principal) -> None:
    c1, c2 = st.columns([3, 2])
    search = c1.text_input("Search name or city", key="party_search")
    ptype = c2.selectbox("Type", ["All"] + list(services.PARTY_TYPES), key="party_type")
    key = f"party_page_{hash((search, ptype))}"
    with session_scope() as s:
        page = services.list_parties(s, search=search, party_type=None if ptype == "All" else ptype,
                                     page=st.session_state.get(key, 1), page_size=10)
    st.dataframe(pd.DataFrame([{"id": p.party_id, "name": p.name, "type": p.party_type, "city": p.city}
                               for p in page.items]), hide_index=True, use_container_width=True)
    pager(key, page.pages)
    if not who.can("registry:write"):
        st.info("Read-only for your role.")
        return
    st.markdown("##### Add a party")
    with st.form("new_party", clear_on_submit=True):
        a, b, c = st.columns(3)
        name = a.text_input("Name")
        kind = b.selectbox("Type", list(services.PARTY_TYPES))
        city = c.text_input("City")
        if st.form_submit_button("Add party", type="primary"):
            def go():
                with session_scope() as s:
                    services.create_party(s, who, name=name, party_type=kind, city=city)
            guarded(go, f"{name.strip()} added.")
    st.markdown("##### Update city or delete")
    with session_scope() as s:
        every = services.list_parties(s, page_size=200).items
    chosen = st.selectbox("Party", every, format_func=lambda p: f"{p.name} ({p.party_type})", key="party_edit")
    if chosen:
        with st.form("edit_party"):
            new_city = st.text_input("City", chosen.city or "")
            save, drop = st.columns(2)
            do_save = save.form_submit_button("Save city")
            do_drop = drop.form_submit_button("Delete party")
        if do_save:
            def go():
                with session_scope() as s:
                    services.update_party_city(s, who, chosen.party_id, new_city)
            guarded(go, "City updated.")
        if do_drop:
            def go():
                with session_scope() as s:
                    services.delete_party(s, who, chosen.party_id)
            guarded(go, "Party deleted.")
    st.caption("A party's name is the identity it signs ledger records with, so it cannot be renamed. "
               "A party that holds lots, units or shipments cannot be deleted.")


def _analytics() -> None:
    with session_scope() as s:
        by_status = services.lot_counts_by_status(s)
        by_party = services.inventory_by_party(s, limit=12)
    df = pd.DataFrame(by_status, columns=["lot_type", "status", "lots", "quantity"])
    a, b = st.columns(2)
    a.markdown("**Lots by type and status** (ORM `GROUP BY`)")
    a.dataframe(df, hide_index=True, use_container_width=True)
    pivot = df.pivot_table(index="lot_type", columns="status", values="lots", aggfunc="sum", fill_value=0)
    b.markdown("**Status mix per lot type**")
    b.bar_chart(pivot)
    st.markdown("**Inventory per organisation** (ORM `LEFT JOIN` + conditional aggregate)")
    st.dataframe(pd.DataFrame(by_party, columns=["organisation", "type", "lots", "quantity", "on_hold"])
                 .assign(quantity=lambda d: d["quantity"].astype(float)),
                 hide_index=True, use_container_width=True)


def _audit() -> None:
    with session_scope() as s:
        rows = services.recent_audit(s, 25)
    st.caption("Written by database triggers, never by the application: lot inserts and status "
               "changes, and user role / active-flag changes.")
    st.dataframe(pd.DataFrame([{"id": r.log_id, "when": r.log_time.strftime("%d %b %H:%M:%S"),
                                "entity": r.entity, "action": r.action, "row": r.row_pk,
                                "details": r.details} for r in rows]),
                 hide_index=True, use_container_width=True)


# ===========================================================================
def users_page(who: Principal) -> None:
    st.subheader("Users and roles")
    st.caption("Every account lives in one `users` table; the `role_id` column decides what a person "
               "may do. Only ADMIN sees this page.")
    c1, c2 = st.columns([3, 2])
    search = c1.text_input("Search name or email", key="user_search")
    role = c2.selectbox("Role", ["All"] + list(auth.ROLES), key="user_role")
    key = f"user_page_{hash((search, role))}"

    def load():
        with session_scope() as s:
            return services.list_users(s, who, search=search, role=None if role == "All" else role,
                                       page=st.session_state.get(key, 1), page_size=10)
    page = guarded(load)
    if page is None:
        return
    st.dataframe(pd.DataFrame([{"id": u.user_id, "email": u.email, "name": u.full_name,
                                "role": u.role.role_name, "active": u.is_active,
                                "last sign-in": u.last_login_at.strftime("%d %b %H:%M") if u.last_login_at else "—"}
                               for u in page.items]), hide_index=True, use_container_width=True)
    pager(key, page.pages)

    st.markdown("##### Add a user")
    with st.form("new_user", clear_on_submit=True):
        a, b = st.columns(2)
        email = a.text_input("Email")
        name = b.text_input("Full name")
        c, d = st.columns(2)
        pw = c.text_input("Initial password", type="password", help="8+ chars with upper, lower and a digit")
        r = d.selectbox("Role", list(auth.ROLES), index=2)
        if st.form_submit_button("Create user", type="primary"):
            def go():
                with session_scope() as s:
                    services.add_user(s, who, email=email, full_name=name, password=pw, role_name=r)
            guarded(go, f"{email.strip().lower()} created as {r}.")

    st.markdown("##### Change role / disable")
    with session_scope() as s:
        everyone = services.list_users(s, who, page_size=200).items
    target = st.selectbox("User", everyone, format_func=lambda u: f"{u.email} · {u.role.role_name}"
                          + ("" if u.is_active else " · disabled"), key="user_edit")
    if target:
        a, b = st.columns(2)
        new_role = a.selectbox("New role", list(auth.ROLES), index=list(auth.ROLES).index(target.role.role_name))
        if a.button("Change role (stored procedure)"):
            def go():
                with session_scope() as s:
                    services.change_user_role(s, who, target.user_id, new_role)
            guarded(go, "Role changed.")
        label = "Disable account" if target.is_active else "Enable account"
        if b.button(label):
            def go():
                with session_scope() as s:
                    services.set_user_active(s, who, target.user_id, not target.is_active)
            guarded(go, "Account updated.")
        st.caption("The database refuses to demote or disable the last active ADMIN (trigger).")
