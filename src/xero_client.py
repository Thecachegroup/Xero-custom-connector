"""
Xero Accounting + Payroll AU API client.

Auth model: Xero **Custom Connection** (OAuth2 client_credentials).

Machine-to-machine, bound to a single Xero org. No user login, no consent screen,
no refresh token to rotate or go stale. The app swaps client id + secret for a
30-minute access token and re-requests as needed. This is the paid Xero option
(per-connection monthly subscription) and is the more robust of the two routes
precisely because there is no refresh token to lose.

Note: the authorising Xero user must be a Payroll Admin for the payroll scopes
to return data.

Rate limits enforced by Xero:
  - 60 calls / minute
  - 5,000 calls / day
  - 5 concurrent
This client self-throttles and honours the Retry-After header on 429.
"""

from __future__ import annotations

import os
import re
import time
import threading
import logging
from collections import deque
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://identity.xero.com/connect/token"
API_BASE = "https://api.xero.com/api.xro/2.0"
PAYROLL_BASE = "https://api.xero.com/payroll.xro/1.0"

# Granular read scopes. accounting.transactions.read does NOT exist - Xero split
# it into accounting.invoices.read + accounting.payments.read etc. New custom
# connections use the granular set.
# These are only sent as a fallback: normally Xero issues a token carrying
# whatever scopes the Custom Connection was configured with.
SCOPES = [
    "accounting.invoices.read",
    "accounting.contacts.read",
    "accounting.settings.read",
    "accounting.payments.read",
    "accounting.attachments.read",
    "accounting.reports.read",
    "payroll.payruns.read",
    "payroll.employees.read",
    "payroll.timesheets.read",
    "payroll.settings.read",
]


class RateLimiter:
    """Token-bucket-ish limiter: max `per_minute` calls in any rolling 60s window."""

    def __init__(self, per_minute: int = 55, per_day: int = 4800):
        self.per_minute = per_minute
        self.per_day = per_day
        self._minute: deque[float] = deque()
        self._day: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                while self._minute and now - self._minute[0] > 60:
                    self._minute.popleft()
                while self._day and now - self._day[0] > 86400:
                    self._day.popleft()

                if len(self._day) >= self.per_day:
                    raise RuntimeError(
                        "Xero daily API limit reached (5,000/day). "
                        "Use the cached snapshot or resume tomorrow."
                    )
                if len(self._minute) < self.per_minute:
                    self._minute.append(now)
                    self._day.append(now)
                    return

                sleep_for = 60 - (now - self._minute[0]) + 0.05
                log.info("Rate limit: sleeping %.1fs", sleep_for)
                time.sleep(sleep_for)


class XeroClient:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        tenant_id: str | None = None,
    ):
        try:
            self.client_id = client_id or os.environ["XERO_CLIENT_ID"]
            self.client_secret = client_secret or os.environ["XERO_CLIENT_SECRET"]
        except KeyError as e:
            raise RuntimeError(
                f"Missing environment variable {e.args[0]}. Set XERO_CLIENT_ID and "
                "XERO_CLIENT_SECRET from your Xero Custom Connection."
            ) from None
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._limiter = RateLimiter()
        self._session = requests.Session()
        self._auth_lock = threading.Lock()
        self._tenant_id = tenant_id or os.environ.get("XERO_TENANT_ID")

    # ---------- auth ----------

    def _request_token(self, with_scopes: bool) -> requests.Response:
        data: dict[str, str] = {"grant_type": "client_credentials"}
        if with_scopes:
            data["scope"] = " ".join(SCOPES)
        return requests.post(
            TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data=data,
            timeout=30,
        )

    def _access_token(self) -> str:
        with self._auth_lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token

            # Preferred: no explicit scope, so Xero issues exactly what the
            # Custom Connection was configured with. Fall back to the explicit
            # list only if Xero insists on one.
            resp = self._request_token(with_scopes=False)
            if resp.status_code == 400:
                resp = self._request_token(with_scopes=True)

            if resp.status_code in (400, 401):
                raise RuntimeError(
                    "Xero rejected the client credentials. Check XERO_CLIENT_ID and "
                    "XERO_CLIENT_SECRET, and that the Custom Connection is authorised "
                    "and its subscription is active. Xero said: "
                    f"{resp.status_code} {resp.text[:300]}"
                )
            resp.raise_for_status()
            payload = resp.json()

            self._token = payload["access_token"]
            self._token_expiry = time.time() + int(payload.get("expires_in", 1800))
            return self._token

    @property
    def tenant_id(self) -> str:
        if self._tenant_id:
            return self._tenant_id
        resp = requests.get(
            "https://api.xero.com/connections",
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=30,
        )
        resp.raise_for_status()
        conns = resp.json()
        if not conns:
            raise RuntimeError("No Xero tenants on this connection.")
        self._tenant_id = conns[0]["tenantId"]
        return self._tenant_id

    # ---------- transport ----------

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict:
        for attempt in range(6):
            self._limiter.acquire()
            resp = self._session.get(
                url,
                headers={
                    "Authorization": f"Bearer {self._access_token()}",
                    "Xero-tenant-id": self.tenant_id,
                    "Accept": "application/json",
                },
                params=params or {},
                timeout=60,
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "60")) + 1
                log.warning("429 from Xero; backing off %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Xero request failed after retries: {url}")

    # ---------- accounting ----------

    def iter_invoices(
        self,
        invoice_type: str,          # "ACCREC" (sales) | "ACCPAY" (bills)
        date_from: str,             # YYYY-MM-DD
        date_to: str,               # YYYY-MM-DD
        statuses: list[str] | None = None,
    ) -> Iterator[dict]:
        """
        Yields full invoice objects INCLUDING LineItems.

        Critical: Xero omits LineItems from any response containing >100 invoices
        (the 'high volume threshold'). Paging at 100/page keeps line items in the
        payload. We still defensively re-fetch by ID if LineItems is missing.
        """
        statuses = statuses or ["DRAFT", "SUBMITTED", "AUTHORISED", "PAID"]
        where = (
            f'Type=="{invoice_type}"'
            f' AND Date>=DateTime({date_from.replace("-", ",")})'
            f' AND Date<=DateTime({date_to.replace("-", ",")})'
        )
        page = 1
        while True:
            data = self.get(
                f"{API_BASE}/Invoices",
                params={
                    "where": where,
                    "Statuses": ",".join(statuses),
                    "page": page,
                    "order": "Date ASC",
                    "unitdp": 4,
                },
            )
            invoices = data.get("Invoices", [])
            if not invoices:
                return
            for inv in invoices:
                if "LineItems" not in inv or not inv["LineItems"]:
                    inv = self.get(f"{API_BASE}/Invoices/{inv['InvoiceID']}")["Invoices"][0]
                yield inv
            if len(invoices) < 100:
                return
            page += 1

    def items(self) -> list[dict]:
        return self.get(f"{API_BASE}/Items").get("Items", [])

    def contacts(self) -> list[dict]:
        out, page = [], 1
        while True:
            batch = self.get(f"{API_BASE}/Contacts", params={"page": page}).get("Contacts", [])
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    # ---------- payroll (AU) ----------

    @staticmethod
    def _xero_date(val) -> str | None:
        """Xero payroll returns /Date(1585699200000+0000)/. Convert to YYYY-MM-DD."""
        if not val:
            return None
        m = re.search(r"/Date\((-?\d+)", str(val))
        if not m:
            return str(val)[:10]
        from datetime import datetime, timezone
        return datetime.fromtimestamp(
            int(m.group(1)) / 1000, tz=timezone.utc
        ).date().isoformat()

    def pay_runs(self, date_from: str, date_to: str) -> list[dict]:
        """All pay runs whose period ends inside the window.

        The Payroll AU API does not honour the Accounting API's
        DateTime(y,m,d) where-syntax - passing it returns an empty set rather
        than an error, which silently loses the entire payroll. So page the
        endpoint and filter here instead.
        """
        out, page = [], 1
        while True:
            batch = self.get(
                f"{PAYROLL_BASE}/PayRuns", params={"page": page}
            ).get("PayRuns", [])
            if not batch:
                break
            for run in batch:
                end = self._xero_date(run.get("PayRunPeriodEndDate"))
                if end and date_from <= end <= date_to:
                    out.append(run)
            if len(batch) < 100:
                break
            page += 1
        log.info("pay_runs: %d in %s..%s", len(out), date_from, date_to)
        return out

    def pay_run(self, pay_run_id: str) -> dict:
        """A single pay run, which carries its payslip list."""
        return self.get(f"{PAYROLL_BASE}/PayRuns/{pay_run_id}")["PayRuns"][0]

    def pay_items(self) -> dict:
        """Earnings rates, deduction types etc. Payroll AU is on 1.0."""
        return self.get(f"{PAYROLL_BASE}/PayItems").get("PayItems", {})

    def payroll_calendars(self) -> list[dict]:
        return self.get(f"{PAYROLL_BASE}/PayrollCalendars").get("PayrollCalendars", [])

    def payslip(self, payslip_id: str) -> dict:
        """Full payslip detail: earnings, super and tax as separate typed
        collections. The pay run list only carries payslip IDs."""
        return self.get(f"{PAYROLL_BASE}/Payslips/{payslip_id}")["Payslips"][0]

    def employees(self) -> list[dict]:
        return self.get(f"{PAYROLL_BASE}/Employees").get("Employees", [])
