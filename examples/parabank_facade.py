"""Record a scripted loan-denial test through the synchronous Playwright facade."""

import argparse
from pathlib import Path

from testar_evidence_playwright.sync_api import sync_playwright, expect


def run(page, url: str) -> None:
    page.goto(url)
    page.locator("input[name='username']").fill("john")
    page.locator("input[name='password']").fill("demo")
    page.get_by_role("button", name="Log In", exact=True).click()
    expect(page.get_by_text("Account Services", exact=True)).to_have_text("Account Services")

    page.get_by_role("link", name="Request Loan", exact=True).click()
    expect(page.get_by_text("Apply for a Loan", exact=True)).to_have_text("Apply for a Loan")
    page.locator("#amount").fill("999999")
    page.locator("#downPayment").fill("1")
    page.get_by_role("button", name="Apply Now", exact=True).click()

    # Denial is the expected business result for this test objective.
    expect(page.locator("#loanStatus")).to_have_text("Denied")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://para.testar.org/")
    parser.add_argument("--output", type=Path, default=Path("target", "traces"),
        help="Parent directory for timestamped evidence traces")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--channel", default="chrome", help="Playwright browser channel")
    arguments = parser.parse_args()

    expect.set_options(timeout=15_000)
    print(f"Evidence parent: {arguments.output.resolve()}")
    with sync_playwright(
        evidence_path=arguments.output,
        sut="parabank",
        test_objective="Verify that an oversized loan request is denied",
        origin="Loan-denial example script",
    ) as playwright:
        browser = playwright.chromium.launch(
            headless=not arguments.headed,
            channel=arguments.channel or None,
        )
        try:
            page = browser.new_page()
            page.set_default_timeout(15_000)
            run(page, arguments.url)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
