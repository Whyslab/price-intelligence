"""Who is asking, when the shelf is opened from inside Telegram.

The shelf has never had to know. It reads, it binds to localhost, and the worst
a stranger could do with it is learn what is on sale. Favourites change that:
the page now writes, and a write needs a name to write under.

Telegram already answers this, and answers it without a password. A page opened
as a Web App is handed `initData` — the user, the chat, a timestamp — signed
with a key derived from the bot's own token. Anybody can read those fields;
nobody without the token can produce the signature. So the check here is the
whole of the authentication, and it is deliberately the only one:

* **No exemption for localhost.** It would be invisible from the outside and
  would turn `pi web --host 0.0.0.0`, a flag that exists, into an open door.
  A machine on the same café wifi is not the person who owns the database.
* **No cookie, no session.** `initData` is sent with every request the page
  makes. There is nothing to expire, steal from storage, or get wrong.
* **A stale signature is refused.** Telegram's own guidance, and the reason the
  timestamp is signed at all: a URL with valid initData in it, pasted into a
  chat months later, must not still be an identity.

The one way in without Telegram is `pi web --owner <id>`, which says out loud
whose shelf this is. Debugging needs it, and a flag somebody typed is a
different thing from a rule the code applies on its own.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

log = logging.getLogger(__name__)

# How long a signature stays an identity. Telegram suggests a day for anything
# that is not a payment, and a Web App left open on a phone overnight should
# still be able to star something in the morning.
MAX_AGE_SECONDS = 24 * 60 * 60
CLOCK_SKEW_SECONDS = 60


def _check_string(fields: list[tuple[str, str]]) -> str:
    """Telegram's data-check-string: every field but the hash, sorted, one per line."""
    return "\n".join(
        f"{key}={value}" for key, value in sorted(fields) if key != "hash"
    )


def verify(
    init_data: str,
    bot_token: str | None,
    max_age: int = MAX_AGE_SECONDS,
    now: float | None = None,
) -> int | None:
    """The Telegram user id this initData belongs to, or None.

    None for every kind of failure on purpose. The page has one thing to do
    about an unusable identity — stop offering to save anything — and telling a
    caller *why* the signature did not check out is telling whoever sent it.
    """
    if not init_data or not bot_token:
        return None
    fields = parse_qsl(init_data, keep_blank_values=True)
    signature = dict(fields).get("hash", "")
    if not signature:
        return None

    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(
        secret, _check_string(fields).encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None

    data = dict(fields)
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    age = (time.time() if now is None else now) - auth_date
    # A date in the future is as wrong as one too old: a minute of clock skew is
    # forgiven, a stolen or forged signature dated next year is not.
    if auth_date <= 0 or age > max_age or age < -CLOCK_SKEW_SECONDS:
        log.debug("initData signature is valid but %.0f hours old", age / 3600)
        return None

    try:
        user = json.loads(data.get("user", "{}"))
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError):
        return None
    return user_id or None
