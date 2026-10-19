"""OAuth helpers shared by the mail and cloud-storage backends.

* Microsoft (Outlook mail, OneDrive): MSAL device-code flow. You open a URL,
  type a short code and sign in (MFA works); the refresh token is cached in
  ``~/.litsuite/tokens/`` so later runs are silent.
* Google (Gmail, Google Drive): installed-app flow; a browser tab opens for
  consent, the token is cached the same way.
"""

from __future__ import annotations

import json
from pathlib import Path

from litsuite.secrets import secure_write, token_path

GRAPH = "https://graph.microsoft.com/v1.0"


class AuthError(Exception):
    pass


# ---------------------------------------------------------------------------
# Microsoft identity platform (MSAL)
# ---------------------------------------------------------------------------
def microsoft_token(client_id: str, scopes: list, cache_name: str, tenant: str = "common",
                    interactive: bool = True, printer=print) -> str:
    """Return an access token for Microsoft Graph, signing in if needed."""
    try:
        import msal
    except ImportError as e:
        raise AuthError("Microsoft sign-in needs the 'msal' package: pip install msal") from e
    if not client_id:
        raise AuthError("No Azure 'client_id' configured. See README > 'Outlook / Microsoft 365' for the "
                        "5-minute app registration.")
    path = token_path("msal", cache_name)
    cache = msal.SerializableTokenCache()
    if path.exists():
        cache.deserialize(path.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant}",
                                       token_cache=cache)
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(scopes, account=accounts[0])
    if not result:
        if not interactive:
            raise AuthError(f"Not signed in to Microsoft for '{cache_name}'. Run: litsuite login ...")
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise AuthError(f"Could not start Microsoft sign-in: {flow.get('error_description', flow)}")
        printer(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    if cache.has_state_changed:
        secure_write(path, cache.serialize())
    if "access_token" not in result:
        raise AuthError(f"Microsoft sign-in failed: {result.get('error_description', result.get('error'))}")
    return result["access_token"]


# ---------------------------------------------------------------------------
# Google (installed application flow)
# ---------------------------------------------------------------------------
def google_credentials(client_secret_file: str, scopes: list, cache_name: str, interactive: bool = True):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise AuthError("Google sign-in needs: pip install google-api-python-client google-auth-oauthlib") from e
    path = token_path("google", cache_name)
    creds = None
    if path.exists():
        creds = Credentials.from_authorized_user_info(json.loads(path.read_text(encoding="utf-8")), scopes)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            secure_write(path, creds.to_json())
            return creds
        except Exception:
            creds = None
    if not interactive:
        raise AuthError(f"Not signed in to Google for '{cache_name}'. Run: litsuite login ...")
    secret = Path(str(client_secret_file or "")).expanduser()
    if not secret.is_file():
        raise AuthError(f"Google OAuth client file not found: '{client_secret_file}'. See README > "
                        "'Gmail' for how to download it from Google Cloud Console.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes)
    creds = flow.run_local_server(port=0, open_browser=True)
    secure_write(path, creds.to_json())
    return creds
