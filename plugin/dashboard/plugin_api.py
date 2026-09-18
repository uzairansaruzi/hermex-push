"""Pairing route, mounted by the dashboard at ``/api/plugins/hermex-push/`` behind dashboard
auth. Hermex calls ``GET /pairing`` once with the user's dashboard login and stores the result
in the Keychain; no key is ever typed.

Response: ``{"relay_url": str, "install_key": <64 hex>, "preview_key": <base64 of 32 bytes>,
"platform": "hermex", "payload_version": 1}``. 409 when ``HERMEX_PUSH_RELAY_URL`` is unset,
because a pairing without a relay could never deliver.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

from hermex_push import PLATFORM_NAME, RELAY_URL_ENV  # noqa: E402
from hermex_push.hooks import relay_url_from_env  # noqa: E402
from hermex_push.keys import load_or_create_keys  # noqa: E402
from hermex_push.payload import PAYLOAD_VERSION  # noqa: E402

router = APIRouter()


@router.get("/pairing")
def get_pairing() -> JSONResponse:
    relay_url = relay_url_from_env()
    if not relay_url:
        raise HTTPException(status_code=409, detail=f"{RELAY_URL_ENV} is not set on this host")
    keys = load_or_create_keys()
    return JSONResponse(
        {
            "relay_url": relay_url,
            "install_key": keys.install_key,
            "preview_key": keys.preview_key_b64,
            "platform": PLATFORM_NAME,
            "payload_version": PAYLOAD_VERSION,
        },
        headers={"Cache-Control": "no-store"},
    )
