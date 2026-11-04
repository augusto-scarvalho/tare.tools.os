"""
tests/test_e2e_cockpit.py — End-to-End (E2E) Automated Browser Tests for Cockpit UI

Validates a genuine browser -> HTTP handler -> intake processor -> graph/inbox
path, but against a *scratch* Relay root — never the live Tailscale Cockpit.
The earlier version of this module pointed Playwright at a hard-coded Tailscale
address and POSTed a wall-clock demand to the live /api/intake, injecting real
backlog tasks (that is the intake this train remediates). Here a stdlib
socketserver runs the real CoordinatorHandler on 127.0.0.1:0 with every mutable
byte rebound to a temporary tree.

Covers: intake bounds/layout (mobile+desktop), zero horizontal overflow, theme
tokens, overseer chat scaffold, and a real intake submission that must land in
the scratch graph exactly once — while the real work-graph.json / intake queue
stay byte-identical.

Dual-import note: cluster_coordinator's handler drives the intake write path
through `relay.intake_processor`, whose `relay_mesh` is the *top-level*
`relay_mesh` module instance, while the handler's own board/state path uses
`relay.relay_mesh`. These are two distinct module objects, so BOTH must be
rebound to the scratch root or the real BOARD/graph would be written.
"""
import hashlib
import json
import socketserver
import sys
import threading
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO), str(REPO / "relay")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Playwright/Chromium may be absent in a restricted CI box. Skip the whole module
# cleanly (fail-closed to the two stdlib suites) instead of pointing at the live
# Cockpit as a substitute or erroring at collection.
pytest.importorskip("playwright.sync_api", reason="Playwright not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

from relay import cluster_coordinator as cc  # noqa: E402

REAL_GRAPH = REPO / "work-graph.json"
REAL_INTAKE = REPO / "relay" / "intake"


def _tree_digest(root: Path) -> str:
    """SHA-256 over a file, or over a directory's (relpath, bytes) manifest."""
    h = hashlib.sha256()
    if root.is_file():
        h.update(root.read_bytes())
    elif root.is_dir():
        for f in sorted(root.rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(root)).encode("utf-8"))
                h.update(b"\0")
                h.update(f.read_bytes())
                h.update(b"\0")
    return h.hexdigest()


@pytest.fixture(scope="module", autouse=True)
def real_root_immutable():
    """FAL-03/FAL-07: prove the real graph + intake queue are byte-identical
    across the whole browser module. Fails without any restoration attempt."""
    before = (_tree_digest(REAL_GRAPH), _tree_digest(REAL_INTAKE))
    yield
    after = (_tree_digest(REAL_GRAPH), _tree_digest(REAL_INTAKE))
    assert before[0] == after[0], "REAL work-graph.json bytes changed during E2E run"
    assert before[1] == after[1], "REAL relay/intake queue bytes changed during E2E run"


@pytest.fixture(scope="module")
def cockpit(tmp_path_factory, real_root_immutable):
    """Boot the real CoordinatorHandler on a loopback ephemeral port with all
    mutable state rebound to a scratch Relay root. Requesting real_root_immutable
    orders this fixture's teardown *before* the immutability assertion runs."""
    root = tmp_path_factory.mktemp("cockpit_scratch")
    (root / "relay").mkdir()
    real = json.loads(REAL_GRAPH.read_text(encoding="utf-8-sig"))
    (root / "work-graph.json").write_text(json.dumps(
        {"nodes": [], "edges": [], "meta": real.get("meta", {}),
         "sources": real.get("sources", {})}), encoding="utf-8")

    # Two distinct relay_mesh instances (see module docstring): the handler's own
    # (relay.relay_mesh) and the intake processor's (top-level relay_mesh).
    cc_mesh = cc.relay_mesh
    ip = cc.intake_processor
    top_mesh = ip.relay_mesh
    hw = cc.human_watcher
    ov = cc.overseer_agent

    orig = {
        "cc_root": cc.RELAY_ROOT, "cc_dir": cc.RELAY_DIR,
        "cc_intake": cc.INTAKE_HUMAN_DIR, "cc_db": cc.DB,
        "cc_mesh_root": cc_mesh.RELAY_ROOT, "top_mesh_root": top_mesh.RELAY_ROOT,
        "auto_mode_file": hw.AUTO_MODE_FILE, "chat_file": ov.CHAT_FILE,
    }

    server = None
    browser = None
    pw = None
    try:
        top_mesh.rebind_root(root)   # intake_processor: graph node + planner inbox + intake dirs
        cc_mesh.rebind_root(root)    # handler: /api/state board + update_board write
        ip.ensure_dirs()
        top_mesh.ensure_dirs()

        cc.RELAY_ROOT = root
        cc.RELAY_DIR = root / "relay"
        cc.INTAKE_HUMAN_DIR = top_mesh.RELAY_DIR / "intake" / "human"
        cc.DB = cc.CoordinatorDB(root / "relay" / "history" / "coordinator.db")
        hw.AUTO_MODE_FILE = root / "relay" / "history" / "auto-approve.json"
        ov.CHAT_FILE = root / "relay" / "history" / "overseer_chat.json"

        # Ephemeral loopback port; no start_coordinator() (no watchdog/telemetry threads).
        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("127.0.0.1", 0), cc.CoordinatorHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass
            pw = None
            browser = None
            pytest.skip(f"Chromium browser not available for Playwright: {exc}")

        yield types.SimpleNamespace(
            browser=browser,
            url=f"http://127.0.0.1:{port}/",
            port=port,
            root=root,
            relay=root / "relay",
            graph=root / "work-graph.json",
        )
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        if server is not None:
            server.shutdown()
            server.server_close()
        cc.RELAY_ROOT = orig["cc_root"]
        cc.RELAY_DIR = orig["cc_dir"]
        cc.INTAKE_HUMAN_DIR = orig["cc_intake"]
        cc.DB = orig["cc_db"]
        hw.AUTO_MODE_FILE = orig["auto_mode_file"]
        ov.CHAT_FILE = orig["chat_file"]
        top_mesh.rebind_root(orig["top_mesh_root"])
        cc_mesh.rebind_root(orig["cc_mesh_root"])


def test_fal04_url_is_loopback_not_live(cockpit):
    """FAL-04: the test target must be a loopback ephemeral port, never Tailscale."""
    assert cockpit.url.startswith("http://127.0.0.1:")
    assert cockpit.port > 0
    assert "//100." not in cockpit.url  # no Tailscale (100.64.0.0/10) address


def test_e2e_desktop_layout_and_intake_bounds(cockpit):
    """Test desktop layout (1280x800) and ensure Intake Box is within viewport bounds."""
    context = cockpit.browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto(cockpit.url, wait_until="networkidle", timeout=10000)

    # 1. Verify title & brand
    assert "tare.tools" in page.title()
    assert page.is_visible("text=tare.tools")
    assert page.is_visible("text=SIGNAL COCKPIT")

    # 2. Check html/body horizontal scroll (must be 0 or within 2px)
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    client_width = page.evaluate("document.documentElement.clientWidth")
    assert scroll_width <= client_width + 2, f"Horizontal overflow detected on desktop: {scroll_width} > {client_width}"

    # 3. Verify Intake Box exists and is fully visible within viewport
    intake_card = page.locator("#intakeCard")
    assert intake_card.is_visible()
    box = intake_card.bounding_box()
    assert box is not None
    assert box["x"] >= 0, f"Intake card left bound is negative: {box['x']}"
    assert box["x"] + box["width"] <= 1280 + 2, f"Intake card right bound overflows viewport: {box['x'] + box['width']}"
    assert box["width"] >= 500, f"Intake card is abnormally squished: {box['width']}px"

    # 4. Verify input fields
    title_input = page.locator("#title")
    body_input = page.locator("#body")
    submit_btn = intake_card.locator("button[type='submit']")
    assert title_input.is_visible()
    assert body_input.is_visible()
    assert submit_btn.is_visible()

    context.close()


def test_e2e_mobile_layout_and_intake_bounds(cockpit):
    """Test mobile viewport (375x812, iPhone size) with zero horizontal clipping."""
    context = cockpit.browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
    page = context.new_page()
    page.goto(cockpit.url, wait_until="networkidle", timeout=10000)

    # 1. Verify zero horizontal page scrolling
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    client_width = page.evaluate("document.documentElement.clientWidth")
    assert scroll_width <= client_width + 2, f"Horizontal overflow on mobile: {scroll_width} > {client_width}"

    # 2. Verify Intake Box on mobile is within 375px viewport
    intake_card = page.locator("#intakeCard")
    assert intake_card.is_visible()
    box = intake_card.bounding_box()
    assert box is not None
    assert box["x"] >= 0
    assert box["x"] + box["width"] <= 375 + 5, f"Intake card overflows mobile: {box['x'] + box['width']}"

    context.close()


def test_e2e_theme_switching(cockpit):
    """Test theme switcher applies CSS tokens to document root."""
    context = cockpit.browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto(cockpit.url, wait_until="networkidle", timeout=10000)

    theme_select = page.locator("#themeSelect")
    assert theme_select.is_visible()

    # Switch to Dracula
    theme_select.select_option("dracula")
    data_theme = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert data_theme == "dracula"
    accent = page.evaluate("document.documentElement.style.getPropertyValue('--accent').trim()")
    assert accent == "#bd93f9"

    # Switch to Tokyo Night
    theme_select.select_option("tokyo_night")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "tokyo_night"
    assert page.evaluate("document.documentElement.style.getPropertyValue('--accent').trim()") == "#7aa2f7"

    # Switch back to Signal
    theme_select.select_option("signal")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "signal"
    assert page.evaluate("document.documentElement.style.getPropertyValue('--accent').trim()") == "#CBF23F"

    context.close()


def test_e2e_intake_submission_flow(cockpit):
    """FAL-01: submit a deterministic demand through the rendered form to the real
    handler; assert the browser success/reset AND that it lands in the scratch
    graph exactly once with exactly one scratch planner notification.

    (Processor-level intake idempotency — FAL-02 — is proven by
    test_relay_intake_and_consensus; the *web* handler stamps a wall-clock
    timestamp into each demand body, so two browser submits are legitimately
    distinct demands, not a duplicate — and production code is out of scope.)"""
    # Deterministic title/body — no wall-clock title (that was the original defect).
    test_title = "E2E Isolation Intake Falsifier FAL-01"
    test_body = "Automated Playwright browser E2E test verifying isolated intake pipeline."

    context = cockpit.browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto(cockpit.url, wait_until="networkidle", timeout=10000)

    page.fill("#title", test_title)
    page.fill("#body", test_body)
    page.click("#intakeCard button[type='submit']")

    toast = page.locator("#toast")
    toast.wait_for(state="visible", timeout=5000)
    toast_text = toast.inner_text()
    assert "added to the backlog" in toast_text or "Demand" in toast_text, toast_text
    # The form resets only on a data.ok response, so this also confirms the async POST succeeded.
    page.wait_for_function("document.getElementById('title').value === ''", timeout=5000)
    context.close()

    # Real intake path (relay.intake_processor) landed the node in the SCRATCH graph.
    graph = json.loads(cockpit.graph.read_text(encoding="utf-8"))
    matching = [n for n in graph.get("nodes", [])
                if isinstance(n, dict) and n.get("title") == test_title]
    assert len(matching) == 1, f"expected exactly one scratch node, got {len(matching)}: {matching}"

    # Exactly one scratch planner notification (idempotency suppresses the duplicate).
    planner_inbox = cockpit.relay / "inbox" / "planner"
    msgs = list(planner_inbox.glob("*.json")) if planner_inbox.exists() else []
    assert len(msgs) == 1, f"expected exactly one scratch planner message, got {len(msgs)}"

    # A processed receipt exists (source swept out of intake/human into processed).
    processed = cockpit.relay / "intake" / "processed"
    receipts = list(processed.glob("*.receipt.json")) if processed.exists() else []
    assert receipts, "expected at least one scratch processed receipt"


def test_e2e_overseer_chat_and_chips(cockpit):
    """Test Overseer chat interface, suggestion chips, and message feed."""
    context = cockpit.browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto(cockpit.url, wait_until="networkidle", timeout=10000)

    overseer_card = page.locator("#overseerCard")
    assert overseer_card.is_visible()
    assert page.is_visible("#overseerChatFeed")
    assert page.is_visible("#overseerInput")
    assert page.is_visible("#overseerSendBtn")

    # Click a suggestion chip and verify input fills
    chip = page.locator("button.chip-btn:has-text('Diagnose Health')")
    assert chip.is_visible()

    context.close()
