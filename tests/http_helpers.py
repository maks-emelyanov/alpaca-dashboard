"""In-memory broker responses; tests never contact a real account."""

import io
import json
from urllib.error import HTTPError

from alpaca_dashboard.client import ALPACA_PAPER_BASE_URL, AlpacaReadOnlyClient


class Response:
    def __init__(self, value=None, *, status=200, body=None):
        self.status = status
        self.body = json.dumps(value).encode() if body is None else body
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def read(self, limit):
        return self.body[:limit]


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


def client(*responses):
    opener = Opener(*responses)
    return AlpacaReadOnlyClient("test-key", "test-secret", opener=opener), opener


def http_error(status):
    return HTTPError(
        ALPACA_PAPER_BASE_URL + "/v2/orders",
        status,
        "private: test-secret",
        {"Location": "https://example.test"},
        io.BytesIO(b'{"message":"test-key test-secret"}'),
    )
