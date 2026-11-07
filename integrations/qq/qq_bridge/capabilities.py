"""Content-free public execution contract."""

def capabilities():
    return {"interactionMode": "background_only", "foregroundRequired": False,
            "pointerControl": False, "globalKeyboardInjection": False, "desktopFallback": False,
            "read_only": True, "account_locked": True,
            "history_cursor": "keyset_timestamp_and_stable_id",
            "cursor_only_pagination": True, "search_cursor": True, "single_message_context": True,
            "sendMessages": {"supported": False, "reason": "background_backend_unavailable"},
            "clientLogin": {"supported": False, "reason": "reuse_existing_client"},
            "history_complete": False, "platforms": ["Windows"]}

def unconfigured_status():
    return {"ok": True, "configured": False, "capabilities": capabilities(),
            "relogin_recommended": False, "next": "Pass --config for your explicitly selected QQ account."}
