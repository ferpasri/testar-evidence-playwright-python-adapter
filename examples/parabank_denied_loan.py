"""Record a scripted login and denied loan request as portable evidence."""

import argparse
from datetime import datetime
from pathlib import Path
import re

from playwright.sync_api import sync_playwright
from testar_evidence_playwright import PlaywrightEvidence


def safe_label(value: str, fallback: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_")
    return label[:32].rstrip("-_") or fallback


def create_trace_directory(
    parent: Path,
    sut: str,
    test_objective: str,
    now: datetime | None = None,
) -> Path:
    instant = now or datetime.now()
    timestamp = instant.strftime("%Y-%m-%d_%H-%M-%S-") + f"{instant.microsecond // 1000:03d}"
    name = f"{safe_label(sut, 'sut')}_{safe_label(test_objective, 'objective')}_trace-{timestamp}"
    parent.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        candidate = parent / (name if suffix == 1 else f"{name}-{suffix}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            suffix += 1


def output_directory(
    explicit: Path | None,
    parent: Path = Path("target", "traces"),
    now: datetime | None = None,
) -> Path:
    if explicit is not None:
        return explicit
    return create_trace_directory(parent, "parabank", "denied-loan", now)


def run(evidence: PlaywrightEvidence, page, url: str) -> None:
    evidence.navigate(url)
    evidence.fill(page.locator("input[name='username']"), "john")
    evidence.fill_secret(page.locator("input[name='password']"), "demo")
    evidence.click(page.get_by_role("button", name="Log In", exact=True))
    evidence.checks.has_text(
        "parabank:account-services",
        "Account Services is displayed",
        page.get_by_text("Account Services", exact=True),
        "Account Services",
    )

    evidence.click(page.get_by_role("link", name="Request Loan", exact=True))
    evidence.checks.has_text(
        "parabank:loan-form",
        "Apply for a Loan is displayed",
        page.get_by_text("Apply for a Loan", exact=True),
        "Apply for a Loan",
    )
    evidence.fill(page.locator("#amount"), "999999")
    evidence.fill(page.locator("#downPayment"), "1")
    evidence.click(page.get_by_role("button", name="Apply Now", exact=True))

    # Denial is the expected result, so this test oracle passes when Denied appears.
    evidence.checks.has_text(
        "parabank:loan-denied",
        "The loan request is denied",
        page.locator("#loanStatus"),
        "Denied",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://para.testar.org/")
    parser.add_argument("--output", type=Path, help="Exact evidence directory to create")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--channel", default="chrome", help="Playwright browser channel")
    arguments = parser.parse_args()
    output = output_directory(arguments.output)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not arguments.headed,
            channel=arguments.channel or None,
        )
        try:
            page = browser.new_page()
            page.set_default_timeout(15_000)
            with PlaywrightEvidence.start(
                output,
                page,
                properties={"example.sut": "parabank", "example.test_objective": "denied-loan"},
            ) as evidence:
                print(f"Evidence: {output.resolve()}")
                evidence.checks.timeout_millis = 15_000
                run(evidence, page, arguments.url)
                evidence.finish("completed")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
