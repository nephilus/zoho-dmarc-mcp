"""GET-only mailbox client with bounded token refresh and retry handling."""
import os
import time
import httpx
from .config import REGIONS


class APIError(RuntimeError):
    pass


class Zoho:
    def __init__(self, config, client=None):
        self.config = config
        self.mail_url, self.oauth_url = REGIONS[config.region]
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False)
        self.token = None
        self.expires = 0
        self.deadline = float("inf")

    def _check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise APIError("scan_time_limit")

    def refresh(self):
        self._check_deadline()
        try:
            response = self.client.post(self.oauth_url + "/oauth/v2/token", data={
                "grant_type": "refresh_token", "client_id": os.environ["ZOHO_CLIENT_ID"],
                "client_secret": os.environ["ZOHO_CLIENT_SECRET"], "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
            })
            payload = response.json()
            if response.status_code != 200 or not isinstance(payload.get("access_token"), str):
                raise APIError("oauth_refresh_failed")
            self.token = payload["access_token"]
            self.expires = time.monotonic() + min(3600, int(payload.get("expires_in", 3600))) - 60
        except (httpx.HTTPError, ValueError, KeyError):
            raise APIError("oauth_refresh_failed") from None

    def get(self, path, params=None, binary=False):
        for attempt in range(4):
            self._check_deadline()
            if not self.token or time.monotonic() >= self.expires:
                self.refresh()
            try:
                with self.client.stream("GET", self.mail_url + path, params=params, headers={"Authorization": "Zoho-oauthtoken " + self.token}) as response:
                    if response.status_code == 401 and attempt < 3:
                        self.token = None
                        continue
                    if response.status_code == 429 or response.status_code >= 500:
                        retry = response.headers.get("Retry-After", "")
                        delay = min(60, int(retry)) if retry.isdigit() else min(30, 2**attempt)
                        if attempt < 3 and time.monotonic()+delay < self.deadline:
                            time.sleep(delay)
                            continue
                        raise APIError("rate_limited" if response.status_code == 429 else "upstream_unavailable")
                    if response.status_code != 200:
                        raise APIError("mail_request_failed")
                    limit = 10 * 1024 * 1024 if binary else 2 * 1024 * 1024
                    content = bytearray()
                    for chunk in response.iter_bytes(65536):
                        self._check_deadline()
                        if len(content) + len(chunk) > limit:
                            raise APIError("compressed_limit" if binary else "metadata_size_limit")
                        content.extend(chunk)
                    if binary:
                        return bytes(content)
                    import json
                    payload = json.loads(content)
                    if payload.get("status", {}).get("code") != 200:
                        raise APIError("mail_api_error")
                    return payload["data"]
            except httpx.HTTPError:
                if attempt == 3:
                    raise APIError("network_failure") from None
            except (ValueError, KeyError, TypeError):
                raise APIError("invalid_api_response") from None
        raise APIError("retry_limit")

    def validate_owner(self):
        accounts = self.get("/api/accounts")
        for account in accounts:
            if str(account.get("accountId")) == self.config.account:
                primary = account.get("primaryEmailAddress")
                if primary is None:
                    primary = next((item.get("mailId") for item in account.get("emailAddress", []) if item.get("isPrimary") is True), None)
                if isinstance(primary, str) and primary.casefold() == self.config.owner.casefold():
                    return
        raise APIError("account_owner_mismatch")

    def messages(self, start):
        return self.get(f"/api/accounts/{self.config.account}/messages/view", {"folderId": self.config.folder, "start": start, "limit": 200, "status": "all", "sortBy": "date", "sortorder": "true"})

    def attachments(self, message):
        return self.get(f"/api/accounts/{self.config.account}/folders/{self.config.folder}/messages/{message}/attachmentinfo", {"includeInline": "false"}).get("attachments", [])

    def download(self, message, attachment):
        return self.get(f"/api/accounts/{self.config.account}/folders/{self.config.folder}/messages/{message}/attachments/{attachment}", binary=True)
