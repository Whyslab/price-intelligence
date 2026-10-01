"""Who is asking, when the shelf is opened from inside Telegram.

The page writes now — a star against a product is a row in somebody's name —
and this is the whole of the authentication. Everything here is about the ways
it must say no, because the one way it says yes is easy and the failures are
what a hole would hide in.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, urlencode

from pi import webauth

TOKEN = "123456:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def signed(
    token: str = TOKEN,
    user_id: int = 42,
    auth_date: float | None = None,
    user: str | None = None,
) -> str:
    """initData as Telegram sends it, signed with `token`."""
    fields = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAHdqTcvAAAAAN2p1y7fpQ",
        "user": user if user is not None else json.dumps(
            {"id": user_id, "first_name": "Иван", "language_code": "ru"}
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class TestASignatureTelegramWrote:
    def test_it_names_the_reader(self):
        assert webauth.verify(signed(user_id=7), TOKEN) == 7

    def test_a_cyrillic_name_does_not_break_the_check(self):
        """The check string is bytes, and the user field is JSON with Russian in it."""
        assert webauth.verify(signed(user_id=99), TOKEN) == 99

    def test_yesterdays_signature_still_works(self):
        assert webauth.verify(signed(auth_date=time.time() - 3600), TOKEN) == 42


class TestEverythingItMustRefuse:
    def test_a_signature_dated_in_the_future_is_refused(self):
        assert webauth.verify(signed(auth_date=time.time() + 3600), TOKEN) is None

    def test_a_few_seconds_of_clock_skew_is_forgiven(self):
        assert webauth.verify(signed(auth_date=time.time() + 20), TOKEN) == 42

    def test_another_bots_token_cannot_sign_for_this_one(self):
        assert webauth.verify(signed(token="999:OTHER"), TOKEN) is None

    def test_swapping_the_user_and_keeping_the_hash_fools_nobody(self):
        """The point of signing: the fields are readable and not editable."""
        fields = dict(parse_qsl(signed(user_id=42)))
        fields["user"] = json.dumps({"id": 999, "first_name": "Кто-то"})
        assert webauth.verify(urlencode(fields), TOKEN) is None

    def test_a_signature_from_last_week_is_not_an_identity(self):
        """A URL pasted into a chat months later must not still be somebody."""
        assert webauth.verify(signed(auth_date=time.time() - 25 * 3600), TOKEN) is None

    def test_no_hash_is_no_identity(self):
        assert webauth.verify("auth_date=1&user=%7B%22id%22%3A1%7D", TOKEN) is None

    def test_an_empty_string_is_nobody(self):
        assert webauth.verify("", TOKEN) is None

    def test_without_a_bot_token_nobody_can_be_verified(self):
        """A shelf run with no token in .env cannot check anything, so it checks nobody."""
        assert webauth.verify(signed(), None) is None

    def test_a_signature_carrying_no_user_is_not_one(self):
        assert webauth.verify(signed(user="{}"), TOKEN) is None

    def test_an_unreadable_auth_date_is_refused(self):
        fields = {"auth_date": "soon", "user": json.dumps({"id": 5})}
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        assert webauth.verify(urlencode(fields), TOKEN) is None
