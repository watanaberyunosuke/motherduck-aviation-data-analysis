"""Shared HTTP session with retries and a descriptive User-Agent."""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "aviation-data-analysis/0.1 (personal research project)"
DEFAULT_TIMEOUT = 30


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        # 429 is handled by the callers that know the provider's rate-limit headers.
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = USER_AGENT
    return s
