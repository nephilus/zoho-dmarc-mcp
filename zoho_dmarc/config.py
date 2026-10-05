from dataclasses import dataclass
import json
import os
from pathlib import Path
from .parser import domain

REGIONS = {"com": ("https://mail.zoho.com", "https://accounts.zoho.com"), "eu": ("https://mail.zoho.eu", "https://accounts.zoho.eu"), "in": ("https://mail.zoho.in", "https://accounts.zoho.in"), "com.au": ("https://mail.zoho.com.au", "https://accounts.zoho.com.au"), "jp": ("https://mail.zoho.jp", "https://accounts.zoho.jp"), "ca": ("https://mail.zohocloud.ca", "https://accounts.zohocloud.ca")}


@dataclass(frozen=True)
class Config:
    owner: str
    account: str
    folder: str
    domains: tuple[str, ...]
    region: str = "com"
    remediation: str | None = None
    scan_seconds: int = 1800

    @classmethod
    def load(cls, path=None):
        data = json.loads(Path(path or os.getenv("DMARC_CONFIG", "/config/config.json")).read_text())
        result = cls(**{**data, "domains": tuple(domain(item) for item in data["domains"])})
        if result.region not in REGIONS or not result.account.isdigit() or not result.folder.isdigit() or "@" not in result.owner or not result.domains or not 1 <= result.scan_seconds <= 7200:
            raise ValueError("invalid_runtime_configuration")
        return result


def database():
    return os.getenv("DMARC_DATABASE", "/data/dmarc.sqlite")
