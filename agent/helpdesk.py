"""Client for the helpdesk API.

Built in small steps. So far: both headers on every request, a limiter that
keeps the request rate under the API's limit, and retries for 503 and 429.
"""

import random
import time

import httpx

GAP_SECONDS = 0.30  # one request start every 0.30 s. The API allows 5 a second.
MAX_TRIES = 5  # first try plus four retries, for any one request


class HelpdeskError(Exception):
    """An answer from the helpdesk that we cannot fix by trying again."""

    def __init__(self, status, kind):
        super().__init__(f"{status} {kind}")
        self.status = status
        self.kind = kind


class StopRun(HelpdeskError):
    """401 or missing email. Every other request would fail too, so stop."""


class AlreadyHandled(HelpdeskError):
    """409. The ticket already has an answer. Never send it again."""


class TicketError(HelpdeskError):
    """404, 422 or another 400. This one ticket is left open, the run goes on."""


class GaveUp(HelpdeskError):
    """Still failing after MAX_TRIES. The ticket is left open for a re-run."""


class Limiter:
    """Makes every request start at least `gap` seconds after the one before."""

    def __init__(self, gap=GAP_SECONDS, clock=time.monotonic, sleep=time.sleep):
        self.gap = gap
        self.clock = clock
        self.sleep = sleep
        self.next_start = 0.0  # earliest time the next request may start

    def wait(self):
        now = self.clock()
        if now < self.next_start:
            self.sleep(self.next_start - now)
            now = self.next_start
        self.next_start = now + self.gap


class Helpdesk:
    def __init__(self, url, key, email, dry_run, transport=None, limiter=None,
                 rng=random.uniform):
        self.email = email
        self.dry_run = dry_run
        self.limiter = limiter or Limiter()
        self.rng = rng  # random waits, so tests can make them fixed
        # These two headers go on every request. They are never logged.
        self.client = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}", "X-Candidate-Email": email},
            timeout=httpx.Timeout(30, connect=5),
            transport=transport,
        )

    def send(self, method, path, params=None, json=None, extra_headers=None):
        """One HTTP call. Every call, retries included, passes the limiter."""
        self.limiter.wait()
        return self.client.request(
            method, path, params=params, json=json, headers=extra_headers
        )

    def request(self, method, path, params=None, json=None, extra_headers=None):
        """send() plus retries. Returns the response, or raises a HelpdeskError."""
        status = 0
        for attempt in range(MAX_TRIES):
            try:
                response = self.send(method, path, params, json, extra_headers)
                status = response.status_code
            except httpx.TransportError:  # timeout or no connection
                response, status = None, 0

            if 200 <= status < 300:
                return response
            if status == 429:  # too fast: wait as long as the helpdesk says
                wait = self.retry_after(response)
            elif status == 0 or status >= 500:  # busy or unreachable: back off
                wait = self.rng(0, min(8, 0.5 * 2**attempt))
            else:  # our request is wrong. Trying again cannot help.
                raise self.error_for(response)

            if attempt < MAX_TRIES - 1:
                self.limiter.sleep(wait)
        raise GaveUp(status, "retries_exhausted")

    def retry_after(self, response):
        """Seconds to wait after a 429: the Retry-After header, or 1 if missing."""
        try:
            seconds = float(response.headers.get("Retry-After", ""))
        except ValueError:
            seconds = 1.0
        return min(30.0, max(0.1, seconds)) + self.rng(0, 0.25)

    def error_for(self, response):
        """Pick the error type from the status code."""
        status = response.status_code
        if status == 401:
            return StopRun(status, "unauthorized")
        if status == 400 and "missing_email" in response.text:
            return StopRun(status, "missing_email")
        if status == 409:
            return AlreadyHandled(status, "already_handled")
        return TicketError(status, "request_rejected")
