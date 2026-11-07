"""Content-free capability contract for the public background-only build."""

def capabilities():
    return {
        "interactionMode": "background_only",
        "foregroundRequired": False,
        "pointerControl": False,
        "globalKeyboardInjection": False,
        "desktopFallback": False,
        "history": {"supported": True, "backend": "authenticated_local_database_snapshot"},
        "cachedImages": {"supported": True, "backend": "verified_local_cache"},
        "cachedAttachments": {"supported": True, "backend": "verified_local_copy"},
        "uncachedAttachmentDownload": {"supported": False, "reason": "background_backend_unavailable"},
        "sendText": {"supported": False, "previewSupported": True, "reason": "background_backend_unavailable"},
        "sendMedia": {"supported": False, "previewSupported": True, "reason": "background_backend_unavailable"},
        "clientLogin": {"supported": False, "reason": "reuse_existing_client"},
        "platforms": ["Windows"],
    }

def background_unavailable(operation):
    return {"ok": False, "attempted": False, "foregroundRequired": False,
            "interactionMode": "background_only", "backend": "unavailable",
            "operation": operation, "delivered": None,
            "error": {"code": "background_backend_unavailable", "stage": "backend_selection",
                      "message": "No verified background backend is available for this operation. No UI fallback was attempted."}}

def unconfigured_status():
    return {"ok": True, "configured": False, "capabilities": capabilities(),
            "relogin_recommended": False,
            "next": "Pass --config for an existing account-bound configuration, or initialize an explicitly selected account."}
