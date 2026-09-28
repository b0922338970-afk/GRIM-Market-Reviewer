"""Explicit Telegram activation tools. Never manufacture an opportunity alert."""
import hashlib
import os
import re
import time


def telegram_config(env=None):
    env = os.environ if env is None else env
    prefix = "GRIM_NOTIFY_TELEGRAM_"
    token = env.get(prefix + "TOKEN", "")
    chat = env.get(prefix + "CHAT_ID", "")
    enabled = env.get(prefix + "ENABLED", "").lower() == "true"
    missing = [prefix + key for key in ("TOKEN", "CHAT_ID") if not env.get(prefix + key)]
    errors = []
    if token and not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        errors.append("TOKEN_FORMAT_INVALID")
    if chat and not re.fullmatch(r"-?[1-9][0-9]*|@[A-Za-z0-9_]+", chat):
        errors.append("CHAT_ID_FORMAT_INVALID")
    others = [c for c in ("DISCORD", "LINE", "WEBHOOK") if env.get("GRIM_NOTIFY_"+c+"_ENABLED", "").lower() == "true"]
    valid = not missing and not errors
    return {"enabled": enabled, "token_present": bool(token), "chat_id_present": bool(chat),
            "credentials_valid": valid, "missing_env": missing, "errors": errors,
            "other_enabled_channels": others, "telegram_only_ready": enabled and valid and not others,
            "validation_scope": "LOCAL_FORMAT_ONLY", "live_credentials_verified": False}


def telegram_status(path=None, env=None):
    from .notification_delivery import DEFAULT_JOURNAL, notification_status
    result = notification_status(DEFAULT_JOURNAL if path is None else path, env)
    return {"telegram": result["telegram_config"], "delivery": result}


def telegram_test_send(test_id, path=None, *, env=None, transport=None, clock=time.time):
    from .notification_delivery import DEFAULT_JOURNAL, _dispatch
    env = os.environ if env is None else env
    config = telegram_config(env)
    if not config["telegram_only_ready"]:
        return {"status": "BLOCKED_CONFIG", "config": config}
    if not isinstance(test_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", test_id):
        return {"status": "INVALID_TEST_ID"}
    # Test identity is independent of wall clock, destination secrets and market packets.
    identity = "TELEGRAM_TEST_" + hashlib.sha256(("telegram-synthetic-test.v1:"+test_id).encode()).hexdigest()
    message = ("[GRIM] SYNTHETIC TEST / NON-ACTIONABLE\n"
               "Telegram delivery connectivity test only.\n"
               "Not a live market review, opportunity or trade alert.\n"
               "No entry, direction or execution instruction. No order has been placed.\n"
               "Test ID: " + test_id)
    request = ("https://api.telegram.org/bot" + env["GRIM_NOTIFY_TELEGRAM_TOKEN"] + "/sendMessage",
               {"Content-Type": "application/json"},
               {"chat_id": env["GRIM_NOTIFY_TELEGRAM_CHAT_ID"], "text": message})
    return _dispatch(identity, lambda channel: (request, None), DEFAULT_JOURNAL if path is None else path,
                     channels=("TELEGRAM",), transport=transport, clock=clock)
