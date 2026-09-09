"""Shared fixtures.

S3 behaviour is exercised against a real ``moto`` server rather than mocked at
the client boundary, so the tests cover the parts that actually break in
practice: multipart assembly, Range requests, ETag shapes and error codes.
"""

from __future__ import annotations

import socket

import pytest

MASTER_KEY_FOR_TESTS = "8Z0aVQ4jJm1r-9xKpQ2sTuVwXyZaBcDeFgHiJkLmNoP="


@pytest.fixture(scope="session", autouse=True)
def _test_env() -> None:
    import os

    os.environ.setdefault("C2W_MASTER_KEY", MASTER_KEY_FOR_TESTS)
    os.environ.setdefault("C2W_ENVIRONMENT", "dev")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def moto_endpoint() -> str:
    """Run a moto S3 server for the session and yield its endpoint URL."""
    from moto.server import ThreadedMotoServer

    port = _free_port()
    server = ThreadedMotoServer(port=port, verbose=False)
    server.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


@pytest.fixture
def s3_creds(moto_endpoint: str):
    """Credentials factory pointing at the moto server."""
    from c2w.storage.base import S3Credentials

    def make(bucket: str) -> S3Credentials:
        return S3Credentials(
            endpoint_url=moto_endpoint,
            access_key="testing",
            secret_key="testing",
            region="us-east-1",
            bucket=bucket,
            path_style=True,
        )

    return make


@pytest.fixture
async def bucket(s3_creds):
    """Create a uniquely named bucket and return its credentials."""
    import uuid

    from c2w.storage.s3_adapter import S3Client

    name = f"test-{uuid.uuid4().hex[:12]}"
    creds = s3_creds(name)
    async with S3Client(creds) as client:
        await client.client.create_bucket(Bucket=name)
    return creds
