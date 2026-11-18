# Check questions

One question per piece of real logic, with my answer. If I did not know, it says so,
and the explanation I got is written below it.

## 1. Limiter and retries (helpdesk.py)

Question: a request fails with a 503 and we retry it. Should the retry also go through
the limiter, or can it skip the wait?

My answer: I did not know.

What I learned: the retry must go through the limiter too. The helpdesk counts every
request it receives. It does not know or care that one of them is a retry. If retries
skipped the limiter, a few 503s in a row could push us over 5 requests a second, and then
we would get a 429 on top of the 503. So in the code there is one door, `send()`, and
every call passes the limiter there, first tries and retries alike.

## 2. Why a 409 is never retried (helpdesk.py)

Question: the helpdesk answers 409, "this ticket already has an answer". Why must the
program not try again, when for a 503 it does?

My answer: I did not know.

What I learned: a 503 means nothing was saved, so sending again is safe. A 409 means an
answer is already saved for this ticket. Sending again can never succeed, and if it did,
the customer would get two answers. So on a 409 the program stops for that ticket and
counts it in the summary. It never sends a second time.
