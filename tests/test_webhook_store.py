"""Where the Discord webhook lives, and why it lives outside the repo.

The URL is a bearer token in disguise: anyone holding it can post to the
channel. This repo is about to be public, and the scheduled workflow runs
`git add data/...`, so `data/` is the one directory it must never be in.
"""
from __future__ import annotations

import json

import pytest

from src.webhook_store import (
    is_discord_webhook,
    load_webhook,
    mask,
    save_webhook,
)

REAL = "https://discord.com/api/webhooks/1409876543210987654/xY2b-kQ7fLpZ9wA3tR6vN1sD4gH8jM0c"


# ------------------------------------------------------------------- reading


def test_reads_the_saved_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    save_webhook(REAL, tmp_path)
    assert load_webhook(tmp_path) == (REAL, "file")


def test_the_environment_wins_over_the_file(tmp_path, monkeypatch):
    """GitHub Actions sets the env var and has no file. Preferring the env
    means adding local storage cannot change what a cloud run sends to."""
    save_webhook(REAL, tmp_path)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/from-env")
    assert load_webhook(tmp_path) == ("https://discord.com/api/webhooks/1/from-env", "environment")


def test_nothing_configured_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert load_webhook(tmp_path) == (None, "none")


def test_a_corrupt_file_degrades_instead_of_ending_the_sweep(tmp_path, monkeypatch):
    """Same forgiveness as `load_best` and `load_overrides`: a bad state file
    may cost you the notification, never the sweep that earned it."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    (tmp_path / "discord.json").write_text("{ half written", encoding="utf-8")
    assert load_webhook(tmp_path) == (None, "none")


def test_an_empty_env_var_is_the_same_as_unset(tmp_path, monkeypatch):
    # Actions sets the variable to "" when the repo secret does not exist, and
    # "" is not a webhook.
    save_webhook(REAL, tmp_path)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    assert load_webhook(tmp_path) == (REAL, "file")


# ------------------------------------------------------------------- writing


def test_saving_rejects_something_that_is_not_a_discord_webhook(tmp_path):
    with pytest.raises(ValueError, match="discord.com"):
        save_webhook("https://example.com/hook", tmp_path)


def test_saving_rejects_the_channel_url_people_paste_by_mistake(tmp_path):
    with pytest.raises(ValueError, match="discord.com"):
        save_webhook("https://discord.com/channels/123/456", tmp_path)


def test_the_file_is_written_outside_data(tmp_path):
    """`data/` is committed by the workflow, so the secret cannot go there."""
    path = save_webhook(REAL, tmp_path / ".secrets")
    assert "data" not in path.parts
    assert json.loads(path.read_text(encoding="utf-8"))["discord_webhook_url"] == REAL


# ------------------------------------------------------------------- masking


def test_mask_hides_the_token_completely():
    """Not even a tail. The id identifies the channel; the token authorises."""
    masked = mask(REAL)
    token = "xY2b-kQ7fLpZ9wA3tR6vN1sD4gH8jM0c"
    assert token not in masked
    for length in range(3, 9):
        assert token[-length:] not in masked
    assert masked.startswith("https://discord.com/api/webhooks/")


def test_mask_keeps_enough_to_recognise_which_webhook_it_is():
    # The point of showing it at all is telling one channel from another.
    assert "1409876543210987654" in mask(REAL)


def test_mask_of_nothing_is_nothing():
    assert mask(None) == ""


def test_is_discord_webhook_accepts_the_discordapp_host():
    # Discord still hands out discordapp.com URLs, and they work.
    assert is_discord_webhook("https://discordapp.com/api/webhooks/1/abc")
    assert is_discord_webhook("https://discord.com/api/webhooks/1/abc")
    assert not is_discord_webhook("http://discord.com/api/webhooks/1/abc")
