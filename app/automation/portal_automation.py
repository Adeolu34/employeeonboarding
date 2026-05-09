"""Playwright-based HR portal automation for employee record management."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from app.config import Settings
from app.utils.logger import get_logger
from app.utils.retry import retry_async

log = get_logger(__name__)


class HRPortalAutomation:
    """Drives a headless browser to create and manage employee records in an HR portal.

    Usage::

        automation = HRPortalAutomation(settings)
        result = await automation.run(employee)
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.screenshots_path: Path = settings.screenshots_path

    async def run(self, employee: Any) -> dict[str, Any]:
        """Full portal workflow: login → create record → upload docs → verify.

        Args:
            employee: Employee ORM model instance.

        Returns:
            Dict with keys: ``employee_record_id``, ``verified`` (bool).
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1920,1080",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            try:
                await self.login(page)
                record_id = await self.create_employee_record(page, employee)

                # Upload documents if present
                doc_dir = self.settings.docs_output_path / str(employee.id)
                if doc_dir.exists():
                    doc_paths = [str(p) for p in doc_dir.glob("*.docx")]
                    if doc_paths:
                        await self.upload_documents(page, employee.id, doc_paths)

                verified = await self.verify_record_created(page, employee.id)

                return {"employee_record_id": record_id, "verified": verified}
            except Exception as exc:
                log.error(
                    "HR portal automation failed for employee {id}: {exc}",
                    id=employee.id,
                    exc=exc,
                )
                await self.take_failure_screenshot(page, employee.id)
                raise
            finally:
                await context.close()
                await browser.close()

    @retry_async(max_attempts=3, delay=2.0, backoff=2.0, exceptions=(Exception,))
    async def login(self, page: Any) -> None:
        """Log into the HR portal.

        Args:
            page: Playwright Page object.
        """
        login_url = self.settings.hr_portal_url.rstrip("/") + "/login"
        log.debug("Navigating to HR portal login: {url}", url=login_url)

        await page.goto(login_url, wait_until="networkidle", timeout=30_000)

        await page.wait_for_selector(
            "input[name='username'], input[type='email'], input[name='email']",
            timeout=10_000,
        )
        await page.fill(
            "input[name='username'], input[type='email'], input[name='email']",
            self.settings.hr_portal_user,
        )
        await page.fill(
            "input[name='password'], input[type='password']",
            self.settings.hr_portal_pass,
        )
        await page.click("button[type='submit'], input[type='submit']")
        await page.wait_for_url(
            lambda url: "/login" not in url and "/auth" not in url,
            timeout=20_000,
        )
        log.info("Logged in to HR portal as {user}", user=self.settings.hr_portal_user)

    @retry_async(max_attempts=3, delay=1.5, backoff=2.0, exceptions=(Exception,))
    async def create_employee_record(self, page: Any, employee: Any) -> str:
        """Navigate to the new employee form and fill all fields.

        Args:
            page: Playwright Page object (authenticated).
            employee: Employee ORM model instance.

        Returns:
            The employee record ID assigned by the portal.
        """
        log.debug("Creating HR portal record for {name}", name=employee.full_name)

        # Navigate to new employee form
        new_emp_url = self.settings.hr_portal_url.rstrip("/") + "/employees/new"
        await page.goto(new_emp_url, wait_until="networkidle", timeout=20_000)

        # If we got redirected, try menu navigation
        if "/employees/new" not in page.url and "/employee" not in page.url.lower():
            for selector in [
                "a:has-text('New Employee')",
                "button:has-text('Add Employee')",
                "a[href*='employee']",
            ]:
                try:
                    await page.click(selector, timeout=4_000)
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                    break
                except Exception:
                    continue

        # Fill form fields
        async def safe_fill(selector: str, value: str | None, label: str) -> None:
            if not value:
                return
            try:
                await page.wait_for_selector(selector, timeout=5_000)
                await page.fill(selector, value)
            except Exception as exc:
                log.warning("Could not fill HR field {label}: {exc}", label=label, exc=exc)

        await safe_fill("input[name='first_name'], input[name='firstName']", employee.first_name, "first_name")
        await safe_fill("input[name='last_name'], input[name='lastName']", employee.last_name, "last_name")
        await safe_fill("input[name='email'], input[type='email']", employee.email, "email")
        await safe_fill("input[name='department']", employee.department or "", "department")
        await safe_fill("input[name='job_title'], input[name='jobTitle'], input[name='position']", employee.job_title or "", "job_title")
        await safe_fill(
            "input[name='start_date'], input[name='startDate'], input[type='date'][name*='start']",
            str(employee.start_date) if employee.start_date else "",
            "start_date",
        )
        await safe_fill(
            "input[name='manager_email'], input[name='managerEmail']",
            employee.manager_email or "",
            "manager_email",
        )

        await asyncio.sleep(0.5)

        # Submit
        submitted = False
        for selector in ["button[type='submit']", "button:has-text('Save')", "button:has-text('Create')"]:
            try:
                await page.click(selector, timeout=5_000)
                submitted = True
                break
            except Exception:
                continue

        if not submitted:
            raise RuntimeError("Could not find submit button on HR employee form")

        await page.wait_for_load_state("networkidle", timeout=20_000)

        # Extract record ID from URL or confirmation element
        import re

        url_match = re.search(r"/employees?/(\d+|[A-Z0-9\-]+)", page.url)
        if url_match:
            record_id = url_match.group(1)
            log.info("Created HR record {id} for {name}", id=record_id, name=employee.full_name)
            return record_id

        # Try confirmation element
        for selector in ["[data-testid='employee-id']", ".employee-id", "#employee-id"]:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue

        fallback_id = f"HR-{employee.id}-{int(time.time())}"
        log.warning("No record ID found in portal, using fallback: {id}", id=fallback_id)
        return fallback_id

    @retry_async(max_attempts=2, delay=1.0, backoff=2.0, exceptions=(Exception,))
    async def upload_documents(self, page: Any, employee_id: int, doc_paths: list[str]) -> bool:
        """Upload generated documents to the employee's HR record.

        Args:
            page: Playwright Page object (on the employee record page).
            employee_id: Internal employee ID.
            doc_paths: List of absolute file paths to upload.

        Returns:
            True if at least one document was uploaded successfully.
        """
        log.debug(
            "Uploading {n} documents for employee {id}",
            n=len(doc_paths),
            id=employee_id,
        )
        any_uploaded = False

        # Navigate to document upload section
        for selector in [
            "a:has-text('Documents')",
            "button:has-text('Upload')",
            "[data-tab='documents']",
            "a[href*='documents']",
        ]:
            try:
                await page.click(selector, timeout=4_000)
                await page.wait_for_load_state("networkidle", timeout=8_000)
                break
            except Exception:
                continue

        for doc_path in doc_paths:
            path = Path(doc_path)
            if not path.exists():
                log.warning("Document not found, skipping: {path}", path=doc_path)
                continue

            try:
                # Look for file input element
                file_input = await page.query_selector("input[type='file']")
                if file_input:
                    await file_input.set_input_files(str(path))
                    await asyncio.sleep(1)

                    # Click upload/submit button if present
                    for btn in ["button:has-text('Upload')", "button[type='submit']"]:
                        try:
                            await page.click(btn, timeout=3_000)
                            break
                        except Exception:
                            continue

                    any_uploaded = True
                    log.info("Uploaded document: {name}", name=path.name)
                else:
                    log.warning("No file input found on document page for {name}", name=path.name)
            except Exception as exc:
                log.warning("Failed to upload {name}: {exc}", name=path.name, exc=exc)

        return any_uploaded

    @retry_async(max_attempts=2, delay=1.0, backoff=2.0, exceptions=(Exception,))
    async def verify_record_created(self, page: Any, employee_id: int) -> bool:
        """Confirm the employee record exists in the portal.

        Args:
            page: Playwright Page object.
            employee_id: Internal employee ID (used in URL or search).

        Returns:
            True if record is confirmed, False otherwise.
        """
        # Simply check that the current page does not show an error
        for selector in [
            ".alert-error",
            ".error-message",
            "[class*='error']",
            "h1:has-text('Error')",
            "h1:has-text('Not Found')",
        ]:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = await el.inner_text()
                    log.warning(
                        "Error element found on HR portal during verification: {text}", text=text
                    )
                    return False
            except Exception:
                continue

        # Check for success indicators
        for selector in [
            ".alert-success",
            "[class*='success']",
            "[data-testid='employee-created']",
        ]:
            try:
                el = await page.query_selector(selector)
                if el:
                    log.debug("Verification success element found")
                    return True
            except Exception:
                continue

        # Fallback: if URL looks like a record page, assume success
        import re

        if re.search(r"/employees?/\d+", page.url):
            log.debug("URL suggests record exists: {url}", url=page.url)
            return True

        log.warning("Could not definitively verify HR record creation")
        return False

    async def take_failure_screenshot(self, page: Any, employee_id: int) -> str | None:
        """Capture a full-page screenshot on automation failure.

        Args:
            page: Playwright Page object.
            employee_id: Used in the screenshot filename.

        Returns:
            Absolute path to the screenshot file, or None on failure.
        """
        try:
            self.screenshots_path.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            path = self.screenshots_path / f"employee_{employee_id}_failure_{ts}.png"
            await page.screenshot(path=str(path), full_page=True)
            log.info("Failure screenshot saved: {path}", path=path)
            return str(path)
        except Exception as exc:
            log.warning("Could not save failure screenshot: {exc}", exc=exc)
            return None
