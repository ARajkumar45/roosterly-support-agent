"""Client for the helpdesk API.

What it does: sends both headers on every request, keeps the request rate
under the API's limit, retries on 503 and 429, reads all tickets page by page,
and posts one decision per ticket without ever answering a ticket twice.
"""

import random
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

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


class Body(BaseModel):
    """Fields every decision has. Unknown fields or values are refused."""

    model_config = ConfigDict(extra="forbid")
    language: Literal["nl", "en", "de"]
    category: Literal["bug", "billing", "feature_request", "question", "other"]
    priority: Literal["P1", "P2", "P3", "P4"]
    reason: str = Field(min_length=1, max_length=300)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ReplyBody(Body):
    draft_reply: str = Field(min_length=1)


class HandoffBody(Body):
    handoff_note: str = Field(min_length=1)


class CloseBody(Body):
    pass


BODIES = {"reply": ReplyBody, "handoff": HandoffBody, "close": CloseBody}


def build_body(action, fields):
    """Check a decision and return exactly the fields the API wants for it.

    The API only knows nl, en and de. A handoff in another language is sent
    as en. A close with no language is sent as nl. A reply in another
    language is refused here, because we could not have written it safely.
    """
    fields = dict(fields)
    if fields.get("language") not in ("nl", "en", "de"):
        if action == "handoff":
            fields["language"] = "en"
        elif action == "close":
            fields["language"] = "nl"
    return BODIES[action](**fields).model_dump(exclude_none=True)


def is_handled(ticket):
    """True if the helpdesk already holds a decision for this ticket."""
    return ticket.get("handled") is not None


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
        self.known_ids = set()  # ids the helpdesk gave us in this run
        self.warning = ""  # set when the ticket list may be incomplete
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

    def request(self, method, path, params=None, json=None, extra_headers=None,
                saved_already=None):
        """send() plus retries. Returns the response, or raises a HelpdeskError.

        saved_already is a check that runs before every retry. If it says yes,
        the earlier try did get through, so we stop and return None.
        """
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
                if saved_already and saved_already():
                    return None
        raise GaveUp(status, "retries_exhausted")

    def retry_after(self, response):
        """Seconds to wait after a 429: the Retry-After header, or 1 if missing."""
        try:
            seconds = float(response.headers.get("Retry-After", ""))
        except ValueError:
            seconds = 1.0
        # Waiting less than the helpdesk asks only earns another 429. 300 s caps a broken header.
        return min(300.0, max(0.1, seconds)) + self.rng(0, 0.25)

    def error_for(self, response):
        """Pick the error type from the status code."""
        status = response.status_code
        if status == 401:
            return StopRun(status, "unauthorized")
        if status == 400 and "missing_email" in response.text:
            return StopRun(status, "missing_email")
        if status == 409:
            return AlreadyHandled(status, "already_handled")
        try:
            problems = response.json().get("problems") or response.json().get("error")
        except ValueError:
            problems = None
        return TicketError(status, f"request_rejected: {problems}" if problems else "request_rejected")

    def status(self, name=""):
        """Mode and ticket counts. The optional name header is sent only here."""
        extra = {"X-Candidate-Name": name} if name else None
        return self.request("GET", "/status", extra_headers=extra).json()

    def examples(self):
        return self.request("GET", "/examples").json()["examples"]

    def get_ticket(self, ticket_id):
        return self.request("GET", f"/tickets/{ticket_id}").json()

    def list_tickets(self, status="open"):
        """All tickets, page by page, oldest first. Each ticket appears once."""
        tickets, cursors, cursor = {}, set(), None
        for _ in range(50):  # hard stop, in case the paging never ends
            params = {"limit": 10, "status": status}
            if cursor:
                params["cursor"] = cursor
            page = self.request("GET", "/tickets", params=params).json()
            for ticket in page["tickets"]:
                if isinstance(ticket, dict) and ticket.get("id"):
                    tickets.setdefault(ticket["id"], ticket)
                else:
                    self.warning = "the ticket list held an entry without an id; it was skipped"
            cursor = page.get("next_cursor")
            if not cursor:  # last page
                break
            if cursor in cursors:
                self.warning = "paging stopped: the helpdesk repeated a cursor"
                break
            cursors.add(cursor)
        else:
            self.warning = "paging stopped after 50 pages"
        self.known_ids.update(tickets)
        return sorted(tickets.values(), key=lambda t: t.get("received_at") or "")

    def post_action(self, ticket_id, action, fields):
        """Post one decision. Returns how it ended. Never answers twice."""
        if self.dry_run:
            raise RuntimeError("dry-run never posts")
        if ticket_id not in self.known_ids:
            raise ValueError("this id did not come from the ticket list in this run")
        body = build_body(action, fields)

        def saved_already():
            # If this lookup itself fails, GaveUp is raised and we do not post again.
            return is_handled(self.get_ticket(ticket_id))

        try:
            response = self.request(
                "POST", f"/tickets/{ticket_id}/{action}", json=body,
                saved_already=saved_already,
            )
        except AlreadyHandled:
            return "already_handled_409"
        return "posted" if response is not None else "posted_confirmed_by_get"
