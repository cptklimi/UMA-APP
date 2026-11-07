#!/usr/bin/env python3
"""
Browser smoke test for UMA (index.html).

Static checks can't see runtime failures. This loads the real page in a
real browser, drives it through offline mode, and fails on any uncaught
error. Offline mode is used deliberately: it needs no network, so the
test result reflects the code and not whether Supabase happens to be up.

Usage:  python3 smoke-test.py [path/to/index.html]
Exit code 0 = safe to push, 1 = problems found.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
OFF = "\033[0m"

SHOTS = Path("/tmp/claude-0/-home-claude/2f89ddc1-5a85-528d-a526-49c9d43b4ad9/scratchpad/smoke")


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "index.html").resolve()
    if not path.exists():
        print(f"{RED}cannot find {path}{OFF}")
        return 1

    SHOTS.mkdir(parents=True, exist_ok=True)
    errors, warnings, steps, network = [], [], [], []

    print(f"\n{BOLD}Smoke testing {path.name}{OFF}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            viewport={"width": 390, "height": 844},  # iPhone-ish
            device_scale_factor=1,
        )

        def on_console(msg):
            if msg.type == "warning":
                warnings.append(msg.text)
                return
            if msg.type != "error":
                return
            # A blocked CDN is an environment fact, not a code defect: this
            # sandbox's proxy refuses jsdelivr and unpkg. Don't fail on it.
            if "Failed to load resource" in msg.text or "net::ERR_" in msg.text:
                network.append(msg.text)
                return
            errors.append(f"console.error: {msg.text}")

        page.on("pageerror", lambda e: errors.append(f"uncaught: {e}"))
        page.on("console", on_console)

        def step(label, fn):
            try:
                fn()
                steps.append((True, label, ""))
            except Exception as exc:  # noqa: BLE001
                steps.append((False, label, str(exc).split("\n")[0][:110]))

        page.goto(f"file://{path}")
        page.wait_for_timeout(1200)

        # The landing screen must be visible and offer a way in.
        step(
            "landing screen renders",
            lambda: page.wait_for_selector("text=Use offline", timeout=5000),
        )
        page.screenshot(path=str(SHOTS / "1-landing.png"))

        # Offline mode is the path that must work with no backend.
        step(
            "offline mode enters the app",
            lambda: (
                page.click("text=Use offline"),
                page.wait_for_timeout(900),
                page.wait_for_selector("#s-home", state="visible", timeout=5000),
            ),
        )
        page.screenshot(path=str(SHOTS / "2-home.png"))

        # The bottom nav is how every screen is reached.
        step(
            "bottom navigation present",
            lambda: page.wait_for_selector(".nav-wrap", state="visible", timeout=4000),
        )

        # Each primary screen must exist in the DOM. Inactive screens are
        # display:none, so check for attachment rather than visibility.
        for screen in ("s-home", "s-contacts", "s-dashboard", "s-settings",
                       "s-newcontact", "s-contact-detail"):
            step(
                f"screen {screen} exists",
                lambda s=screen: page.wait_for_selector(
                    f"#{s}", state="attached", timeout=3000
                ),
            )

        # Every onclick target must resolve to a real function at runtime.
        missing = page.evaluate(
            """() => {
                const bad = [];
                document.querySelectorAll('[onclick]').forEach(el => {
                    const code = el.getAttribute('onclick') || '';
                    const m = code.match(/^\\s*([A-Za-z_$][\\w$]*)\\s*\\(/);
                    if (m && typeof window[m[1]] !== 'function') bad.push(m[1]);
                });
                return [...new Set(bad)];
            }"""
        )
        step(
            "all onclick targets resolve at runtime",
            lambda: (_ for _ in ()).throw(
                AssertionError("missing: " + ", ".join(missing))
            )
            if missing
            else None,
        )

        browser.close()

    passed = sum(1 for ok, _, _ in steps if ok)
    for ok, label, detail in steps:
        mark = f"{GREEN}ok{OFF}  " if ok else f"{RED}FAIL{OFF}"
        print(f"{mark}  {label}")
        if detail:
            print(f"      {DIM}{detail}{OFF}")

    failed = len(steps) - passed
    print()
    if errors:
        print(f"{RED}{len(errors)} uncaught error(s) in the browser:{OFF}")
        for err in errors[:10]:
            print(f"      {DIM}{err[:160]}{OFF}")
        print()
    if warnings:
        print(f"{YELLOW}{len(warnings)} console warning(s){OFF} {DIM}(not blocking){OFF}")
        for warn in warnings[:5]:
            print(f"      {DIM}{warn[:140]}{OFF}")
        print()
    if network:
        print(f"{DIM}{len(network)} blocked network request(s) — expected in "
              f"this sandbox, the CDN is not reachable here. Not a code "
              f"problem.{OFF}\n")

    print(f"{DIM}screenshots in {SHOTS}{OFF}")

    if failed or errors:
        print(f"\n{RED}{BOLD}{failed} step(s) failed, {len(errors)} error(s). "
              f"Do not push.{OFF}\n")
        return 1
    print(f"\n{GREEN}{BOLD}All {passed} steps passed, no uncaught errors.{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
