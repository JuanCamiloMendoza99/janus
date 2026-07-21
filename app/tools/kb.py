"""The support knowledge base backing `search_kb`.

A tuple of articles in a Python module, deliberately, on two counts:

* **Not a vector index.** The sibling Veridex project demonstrates retrieval
  properly; a second, worse RAG system here would teach nobody anything. What
  this project has to demonstrate is the *tool loop*, and a tool loop is only as
  interesting as the fact that the tool returns something real.
* **Not a data file.** `pyproject.toml` packages `app*` only, so a `.json` next
  to this module would not ship in a wheel. Articles as code have no packaging
  question at all.

The corpus mirrors the categories in `app/domain/prompts/playbook.md`, so a
ticket that the playbook can classify is also a ticket the knowledge base can
answer — or visibly cannot, which is the more useful failure.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Article:
    """One knowledge base article."""

    id: str
    title: str
    tags: tuple[str, ...]
    body: str


ARTICLES: tuple[Article, ...] = (
    Article(
        id="kb-101",
        title="Duplicate charge on a single order",
        tags=("billing", "duplicate", "charge", "refund", "order"),
        body=(
            "A customer charged twice for one order is almost always seeing an "
            "authorization hold alongside the real capture. Confirm in the billing "
            "console whether the second entry is a hold: holds show status "
            "'pending' and clear on their own within 5 business days. If both "
            "entries are captured, refund the later one immediately — do not wait "
            "for the customer to ask twice. Refunds settle to the original card in "
            "5 to 10 business days and cannot be redirected to another card."
        ),
    ),
    Article(
        id="kb-102",
        title="Refund timelines and what cannot be refunded",
        tags=("billing", "refund", "timeline", "policy"),
        body=(
            "Refunds are issued to the original payment method and take 5 to 10 "
            "business days to appear, depending on the customer's bank. Usage-based "
            "charges from a closed billing period are refundable only within 30 days "
            "of the invoice date. Taxes are refunded proportionally. A refund never "
            "cancels a subscription: cancel the plan separately or the next cycle "
            "will bill as normal."
        ),
    ),
    Article(
        id="kb-103",
        title="Payment failed and the account was suspended",
        tags=("billing", "payment", "failed", "declined", "suspended", "card"),
        body=(
            "A failed payment is retried on days 1, 3 and 7. After the third failure "
            "the account moves to read-only: data is retained for 30 days and nothing "
            "is deleted. The customer restores access by updating the card in Billing "
            "and clicking 'Retry now' — waiting for the next automatic retry is not "
            "necessary. The most common decline reason is a card expiry the customer "
            "does not know about; ask them to check the expiry date first."
        ),
    ),
    Article(
        id="kb-104",
        title="Changing a plan mid-cycle",
        tags=("billing", "plan", "upgrade", "downgrade", "proration", "subscription"),
        body=(
            "Upgrades take effect immediately and are prorated for the remainder of "
            "the cycle. Downgrades take effect at the end of the current cycle, so "
            "the customer keeps the higher tier until then and is not refunded the "
            "difference. Seat count changes follow the same rule. A plan change never "
            "affects stored data or API keys."
        ),
    ),
    Article(
        id="kb-201",
        title="Password reset email never arrives",
        tags=("account_access", "password", "reset", "email", "login"),
        body=(
            "Reset links are valid for 60 minutes and only the most recent link works "
            "— a customer who clicks 'forgot password' three times invalidates the "
            "first two emails. If nothing arrives, confirm the address matches the "
            "one on the account exactly, then check whether the domain blocks mail "
            "from noreply@. Accounts using SSO cannot reset a password at all; they "
            "must sign in through their identity provider."
        ),
    ),
    Article(
        id="kb-202",
        title="Locked out by multi-factor authentication",
        tags=("account_access", "mfa", "2fa", "locked", "recovery", "login"),
        body=(
            "A customer without their MFA device signs in with a recovery code from "
            "the eight issued at enrollment. Each code works once. With no recovery "
            "codes left, MFA can only be reset by a workspace admin from Members; "
            "support cannot disable MFA on a customer's behalf, and this is not "
            "negotiable regardless of how the request is escalated. If the customer "
            "is the sole admin, the account requires identity verification through "
            "the account owner on file."
        ),
    ),
    Article(
        id="kb-203",
        title="SSO login loop after an identity provider change",
        tags=("account_access", "sso", "saml", "okta", "login", "loop"),
        body=(
            "A redirect loop after an IdP change means the SAML assertion no longer "
            "matches the configured entity ID or the certificate has rotated. Compare "
            "the entity ID and the signing certificate fingerprint in Settings > SSO "
            "against the IdP. Fixing the certificate resolves the loop without any "
            "change on the customer's side. Sessions established before the change "
            "stay valid until they expire, which is why only some users report it."
        ),
    ),
    Article(
        id="kb-301",
        title="Intermittent 500 responses from the API",
        tags=("technical", "api", "500", "error", "outage", "retry"),
        body=(
            "Sporadic 500s on write endpoints are usually a client retrying a request "
            "that already succeeded. Every write endpoint accepts an "
            "Idempotency-Key header; without one, a retried create can produce a "
            "duplicate record. Check status.example.com before treating an isolated "
            "report as an incident. If the error rate is above 1% for a single "
            "customer while the status page is green, gather the request ids and "
            "route it to engineering."
        ),
    ),
    Article(
        id="kb-302",
        title="Large exports time out before finishing",
        tags=("technical", "export", "timeout", "performance", "download"),
        body=(
            "Synchronous exports are capped at 100,000 rows and 60 seconds. Beyond "
            "that the export must be requested asynchronously with "
            "'Email me the file', which processes in the background and links to a "
            "download valid for 24 hours. A timeout is never data loss: nothing is "
            "modified by an export. Customers exporting on a schedule should use the "
            "API's cursor pagination instead."
        ),
    ),
    Article(
        id="kb-303",
        title="Data appears to be missing after a sync",
        tags=("technical", "data", "missing", "sync", "deleted"),
        body=(
            "Records that vanish after a sync are usually filtered, not deleted: a "
            "sync applies the source system's archive flags. Check the 'Include "
            "archived' filter first. Genuine deletion is recoverable from the 30-day "
            "trash by an admin. If records are missing from trash as well, treat it "
            "as potential data loss, escalate immediately, and do not run another "
            "sync — a second sync can overwrite the recovery window."
        ),
    ),
)
