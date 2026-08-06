"""Transactional email (verification + password reset) via Resend.

Provider choice (human, 2026-08-06): Resend, because it needs no new
dependency -- one authenticated POST through the `httpx` already required for
the rest of this project. Free tier is 3,000 emails/month capped at 100/day on
one custom domain, which is far above this product's signup volume. Its
DKIM/SPF/DMARC records live in **Railway's** DNS editor
(railway.com/workspace/domains/realm-arbitrage.com) -- the domain was purchased
through Railway, so Railway manages the zone. WHOIS reports Name.com because
that is Railway's backend registrar only; there is no separate registrar account
for this domain.

Two deliberate behaviors, both load-bearing:

  - **Unconfigured means log, not fail.** With no RESEND_API_KEY the message
    is written to the log and send() returns normally. That's what makes
    `python dashboard.py` usable on a fresh checkout (register an account,
    read the verification link straight out of the server log) and what keeps
    the test suite hermetic without mocking the network -- CI sets no
    RESEND_API_KEY, so this is the path it takes. Same posture as billing.py
    tolerating a missing Stripe key rather than refusing to import.

  - **send() never raises.** Every failure -- bad key, Resend outage, timeout,
    malformed response -- is caught and logged. auth.py calls this from
    UserManager.on_after_register, and an exception escaping there would turn
    a successfully-created account into a 500 with no way for the user to tell
    that the registration itself worked. A silently-undelivered email is
    recoverable (the "resend verification email" affordance on verify.html);
    a 500 on a created account is not.
"""
import logging
import os

import httpx

import blizz  # noqa: F401  -- triggers .env loading as a side effect, same as every other module here

log = logging.getLogger(__name__)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
# Must be an address on a Resend-verified domain. Until the domain's DNS
# records are in place, Resend only accepts onboarding@resend.dev and only
# delivers to the Resend account's own address -- fine for a first smoke test,
# useless for real users.
MAIL_FROM = os.environ.get("MAIL_FROM", "Realm Arbitrage <noreply@realm-arbitrage.com>")

RESEND_ENDPOINT = "https://api.resend.com/emails"
# Short on purpose: this runs inside the register/forgot-password request, so
# the user is waiting on it. Resend being slow shouldn't hold a request open --
# a timeout here is just another logged failure the resend affordance covers.
TIMEOUT_SECONDS = 10.0


def configured() -> bool:
    """Whether real delivery is possible. Read by dashboard.py's /api/status so
    the frontend can avoid promising a "check your email" state that will never
    arrive on an unconfigured local instance."""
    return bool(RESEND_API_KEY)


async def send(to: str, subject: str, html: str) -> bool:
    """Sends one transactional email. Returns True if Resend accepted it.

    Async (`httpx.AsyncClient`, not `requests`) because every caller is inside
    a FastAPI route's event loop -- the synchronous-call-in-an-async-route
    pitfall this project has already hit twice, see .claude/docs/matching.md.
    """
    if not RESEND_API_KEY:
        log.warning("RESEND_API_KEY unset -- email NOT sent. to=%s subject=%s\n%s",
                    to, subject, html)
        return False
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": MAIL_FROM, "to": [to], "subject": subject, "html": html},
            )
        if response.status_code >= 400:
            # Body, not just the status: Resend's errors are specific and
            # actionable ("domain not verified", "from address not allowed"),
            # and a bare 403 in the log would send us hunting.
            log.error("Resend rejected email to=%s subject=%s status=%s body=%s",
                      to, subject, response.status_code, response.text[:500])
            return False
        log.info("Sent email to=%s subject=%s", to, subject)
        return True
    except Exception:
        log.exception("Failed to send email to=%s subject=%s", to, subject)
        return False


# --- Message bodies -------------------------------------------------------
#
# Inline styles only, no <style> block or external CSS: every real mail client
# strips or ignores those, and Gmail in particular drops <head> entirely.
# Plain text would work too, but a bare URL in an unstyled mail reads as spam
# to both filters and people.

def _button_email(heading: str, body: str, link: str, cta: str, footer: str) -> str:
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
            max-width:480px;margin:0 auto;padding:24px;color:#1c1c1e;line-height:1.5">
  <h1 style="font-size:20px;margin:0 0 16px">{heading}</h1>
  <p style="margin:0 0 20px">{body}</p>
  <p style="margin:0 0 24px">
    <a href="{link}" style="display:inline-block;background:#2f6f4f;color:#fff;
       text-decoration:none;padding:11px 20px;border-radius:6px;font-weight:600">{cta}</a>
  </p>
  <p style="margin:0 0 8px;font-size:13px;color:#6b6b70">
    Or paste this into your browser:<br>
    <span style="word-break:break-all">{link}</span>
  </p>
  <p style="margin:16px 0 0;font-size:13px;color:#6b6b70">{footer}</p>
</div>"""


def verification_email(link: str) -> tuple[str, str]:
    """(subject, html) for the address-confirmation mail."""
    return ("Confirm your email — Realm Arbitrage", _button_email(
        heading="Confirm your email",
        body="You're one click from finishing your Realm Arbitrage account. "
             "Confirming your address unlocks subscribing, posting to the Snipe "
             "Board, and Discord alerts.",
        link=link,
        cta="Confirm email",
        footer="This link expires in one hour. If you didn't sign up, ignore "
               "this email — no account action is taken without the link.",
    ))


def password_reset_email(link: str) -> tuple[str, str]:
    """(subject, html) for the password-reset mail."""
    return ("Reset your password — Realm Arbitrage", _button_email(
        heading="Reset your password",
        body="Use the link below to choose a new password for your Realm "
             "Arbitrage account.",
        link=link,
        cta="Choose a new password",
        footer="This link expires in one hour. If you didn't ask for a reset, "
               "ignore this email — your current password still works.",
    ))
