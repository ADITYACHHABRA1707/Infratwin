"""
Capture the report's UI screenshots from the RUNNING app with a real browser.

    python docs/report/capture.py          (dashboard on :8501, API on :8010, database seeded)

Credentials come from the environment (SEED_PASSWORD in .env); nothing is hard-coded.
Output: docs/report/shots/*.png
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent.parent
SHOTS = Path(__file__).resolve().parent / "shots"
SHOTS.mkdir(exist_ok=True)
load_dotenv(ROOT / ".env")
PASSWORD = os.environ["SEED_PASSWORD"]
APP = "http://localhost:8501"
API = "http://localhost:8010"
W = 1440


def settle(page: Page, ms: int = 1500) -> None:
    page.wait_for_timeout(ms)
    try:
        page.wait_for_selector('[data-testid="stStatusWidget"]', state="detached", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(400)


def shot(page: Page, name: str, height: int = 1000) -> None:
    page.set_viewport_size({"width": W, "height": height})
    settle(page, 600)
    page.screenshot(path=str(SHOTS / f"{name}.png"))
    print("  shot", name)


def login(page: Page, email: str, password: str = PASSWORD) -> None:
    page.goto(APP)
    page.get_by_label("Email", exact=True).fill(email)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()


def tab(page: Page, name: str) -> None:
    page.get_by_role("tab", name=name, exact=True).first.click()
    settle(page)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)

        # ---------------------------------------------------------------- login
        ctx = browser.new_context(viewport={"width": W, "height": 800})
        page = ctx.new_page()
        page.goto(APP)
        page.get_by_label("Email", exact=True).wait_for(timeout=60000)
        shot(page, "01_login", 700)
        page.get_by_label("Email", exact=True).fill("admin@chaintwin.local")
        page.get_by_label("Password", exact=True).fill("wrong-password")
        page.get_by_role("button", name="Sign in").click()
        page.get_by_text("Incorrect email or password.").wait_for(timeout=20000)
        shot(page, "02_login_failed", 700)
        page.get_by_label("Email", exact=True).fill("")
        page.get_by_label("Password", exact=True).fill("")
        page.get_by_role("button", name="Sign in").click()
        page.get_by_text("Enter both email and password.").wait_for(timeout=20000)
        shot(page, "03_login_validation", 700)
        ctx.close()

        # ---------------------------------------------------------------- admin
        ctx = browser.new_context(viewport={"width": W, "height": 1000})
        page = ctx.new_page()
        login(page, "admin@chaintwin.local")
        page.get_by_text("Units behaving abnormally right now").wait_for(timeout=90000)
        settle(page, 3000)
        shot(page, "04_dashboard_admin", 1250)

        # Expand the session panel in the sidebar
        page.get_by_text("Session (authenticated user info)").click()
        settle(page, 800)
        shot(page, "05_session_info", 1250)
        page.get_by_text("Session (authenticated user info)").click()

        # Control tower, deeper: scroll to investigation
        page.set_viewport_size({"width": W, "height": 2600})
        settle(page, 2500)
        page.screenshot(path=str(SHOTS / "06_control_tower_full.png"))
        print("  shot 06_control_tower_full")

        tab(page, "Recall simulator")
        page.set_viewport_size({"width": W, "height": 2100})
        settle(page, 2500)
        page.screenshot(path=str(SHOTS / "07_recall_simulator.png"))
        print("  shot 07_recall_simulator")

        tab(page, "Provenance")
        page.set_viewport_size({"width": W, "height": 1500})
        settle(page, 2500)
        page.screenshot(path=str(SHOTS / "08_provenance.png"))
        print("  shot 08_provenance")

        tab(page, "Ledger")
        page.set_viewport_size({"width": W, "height": 1500})
        page.get_by_role("button", name="Verify the chain (fast)").click()
        page.get_by_text(re.compile("^Intact")).wait_for(timeout=60000)
        page.get_by_role("button", name="Run the attack and verify").click()
        page.get_by_text(re.compile("CAUGHT")).wait_for(timeout=120000)
        settle(page, 1500)
        page.screenshot(path=str(SHOTS / "09_ledger_verify_tamper.png"))
        print("  shot 09_ledger_verify_tamper")

        # ---------------------------------------------------------------- registry
        tab(page, "Registry")
        page.set_viewport_size({"width": W, "height": 1900})
        settle(page, 2000)
        page.screenshot(path=str(SHOTS / "10_registry_lots.png"))
        print("  shot 10_registry_lots")

        # search + filter
        page.get_by_label("Search lot code, product or holder").fill("RM-")
        page.keyboard.press("Enter")
        settle(page, 1500)
        page.get_by_label("Type").first.click()
        page.get_by_role("option", name="RAW").click()
        settle(page, 2000)
        page.screenshot(path=str(SHOTS / "11_registry_search_filter.png"))
        print("  shot 11_registry_search_filter")

        # pagination
        page.get_by_role("button", name="Next ▶").first.click()
        settle(page, 2000)
        page.screenshot(path=str(SHOTS / "12_registry_pagination.png"))
        print("  shot 12_registry_pagination")

        # create a raw lot (CREATE)
        form = page.locator('[data-testid="stForm"]').first
        form.get_by_role("button", name="Register lot").click()
        page.get_by_text(re.compile("registered and anchored on the ledger")).wait_for(timeout=60000)
        settle(page, 1000)
        page.screenshot(path=str(SHOTS / "13_create_raw_lot.png"))
        print("  shot 13_create_raw_lot")

        # products tab: validation error, then success
        page.get_by_role("tab", name="Products", exact=True).click()
        settle(page, 1500)
        page.set_viewport_size({"width": W, "height": 2000})
        def prod_form():
            return page.locator('[data-testid="stForm"]').filter(has=page.get_by_role("button", name="Add product"))

        f = prod_form()
        f.get_by_label("SKU (capitals, digits, hyphen)").fill("bad sku!")
        f.get_by_label("Name", exact=True).fill("Demo product")
        f.get_by_role("button", name="Add product").click()
        page.get_by_text(re.compile("SKU must be")).wait_for(timeout=20000)
        settle(page, 800)
        page.screenshot(path=str(SHOTS / "14_validation_error.png"))
        print("  shot 14_validation_error")

        f = prod_form()
        f.get_by_label("SKU (capitals, digits, hyphen)").fill("DEMO-01")
        f.get_by_label("Name", exact=True).fill("Demo product")
        f.get_by_role("button", name="Add product").click()
        page.get_by_text("Product DEMO-01 added.").wait_for(timeout=20000)
        settle(page, 800)
        page.screenshot(path=str(SHOTS / "15_product_created.png"))
        print("  shot 15_product_created")

        # duplicate -> friendly database error
        f = prod_form()
        f.get_by_label("SKU (capitals, digits, hyphen)").fill("DEMO-01")
        f.get_by_label("Name", exact=True).fill("Demo product again")
        f.get_by_role("button", name="Add product").click()
        page.get_by_text(re.compile("Already exists")).wait_for(timeout=20000)
        settle(page, 800)
        page.screenshot(path=str(SHOTS / "16_duplicate_error.png"))
        print("  shot 16_duplicate_error")

        # delete it again (DELETE): select product then delete
        page.locator('[data-testid="stSelectbox"]').filter(has_text=re.compile("^Product")).last.click()
        page.get_by_role("option", name=re.compile("DEMO-01")).click()
        settle(page, 1200)
        page.get_by_role("button", name="Delete product").click()
        page.get_by_text("Product deleted.").wait_for(timeout=20000)
        settle(page, 800)
        page.screenshot(path=str(SHOTS / "17_product_deleted.png"))
        print("  shot 17_product_deleted")

        page.get_by_role("tab", name="Parties", exact=True).click()
        settle(page, 1500)
        page.screenshot(path=str(SHOTS / "18_parties.png"))
        print("  shot 18_parties")
        page.get_by_role("tab", name="Analytics", exact=True).click()
        settle(page, 2000)
        page.screenshot(path=str(SHOTS / "19_analytics.png"))
        print("  shot 19_analytics")
        page.get_by_role("tab", name="Audit trail", exact=True).click()
        settle(page, 1500)
        page.screenshot(path=str(SHOTS / "20_audit_trail.png"))
        print("  shot 20_audit_trail")

        # ---------------------------------------------------------------- users page
        tab(page, "Users")
        page.set_viewport_size({"width": W, "height": 1700})
        settle(page, 2000)
        page.screenshot(path=str(SHOTS / "21_users_admin.png"))
        print("  shot 21_users_admin")

        # weak password rejected
        uf = page.locator('[data-testid="stForm"]').filter(has=page.get_by_role("button", name="Create user"))
        uf.get_by_label("Email", exact=True).fill("new.user@chaintwin.local")
        uf.get_by_label("Full name").fill("New User")
        uf.get_by_label("Initial password").fill("weak")
        uf.get_by_role("button", name="Create user").click()
        page.get_by_text(re.compile("Password needs")).wait_for(timeout=20000)
        settle(page, 800)
        page.screenshot(path=str(SHOTS / "22_weak_password.png"))
        print("  shot 22_weak_password")

        # last-admin protection: try to demote the only admin through the stored procedure
        page.locator('[data-testid="stSelectbox"]').filter(has_text="New role").click()
        page.get_by_role("option", name="AUDITOR").click()
        settle(page, 800)
        page.get_by_role("button", name="Change role (stored procedure)").click()
        page.get_by_text(re.compile("Rejected by a database rule")).wait_for(timeout=20000)
        settle(page, 800)
        page.screenshot(path=str(SHOTS / "23_last_admin_protected.png"))
        print("  shot 23_last_admin_protected")

        tab(page, "Benchmark")
        settle(page, 1500)
        shot(page, "24_benchmark", 1100)
        ctx.close()

        # ---------------------------------------------------------------- manager
        ctx = browser.new_context(viewport={"width": W, "height": 1000})
        page = ctx.new_page()
        login(page, "manager@chaintwin.local")
        page.get_by_text("Units behaving abnormally right now").wait_for(timeout=90000)
        settle(page, 2500)
        shot(page, "25_dashboard_manager", 1250)
        ctx.close()

        # ---------------------------------------------------------------- auditor
        ctx = browser.new_context(viewport={"width": W, "height": 1000})
        page = ctx.new_page()
        login(page, "auditor@chaintwin.local")
        page.get_by_text("Units behaving abnormally right now").wait_for(timeout=90000)
        settle(page, 2500)
        shot(page, "26_dashboard_auditor", 1250)
        tab(page, "Registry")
        page.set_viewport_size({"width": W, "height": 1700})
        settle(page, 2000)
        page.screenshot(path=str(SHOTS / "27_registry_auditor_readonly.png"))
        print("  shot 27_registry_auditor_readonly")
        tab(page, "Ledger")
        page.set_viewport_size({"width": W, "height": 1500})
        settle(page, 1500)
        page.screenshot(path=str(SHOTS / "28_ledger_auditor_no_attacks.png"))
        print("  shot 28_ledger_auditor_no_attacks")
        ctx.close()

        # ---------------------------------------------------------------- API docs
        ctx = browser.new_context(viewport={"width": W, "height": 1500})
        page = ctx.new_page()
        page.goto(f"{API}/docs")
        page.wait_for_selector(".opblock", timeout=30000)
        settle(page, 1500)
        page.screenshot(path=str(SHOTS / "29_api_docs.png"))
        print("  shot 29_api_docs")
        ctx.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
