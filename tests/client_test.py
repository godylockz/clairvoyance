import asyncio
import logging
import time
import unittest
from typing import List

import aiounittest
from aiohttp import web

from clairvoyance.client import Client, parse_retry_after
from clairvoyance.entities.context import logger_ctx


class ScriptedServer:
    """Answers each POST with the next status in the script, then 200 with a JSON body."""

    def __init__(self, statuses: List[int], retry_after: str = "") -> None:
        self.statuses = statuses
        self.retry_after = retry_after
        self.hits = 0
        self.runner: web.AppRunner
        self.url = ""

    async def handle(self, _: web.Request) -> web.Response:
        self.hits += 1
        if self.statuses:
            status = self.statuses.pop(0)
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return web.Response(status=status, text="busy", headers=headers)
        return web.json_response({"data": {"__typename": "Query"}})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/graphql", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.url = f"http://127.0.0.1:{port}/graphql"

    async def stop(self) -> None:
        await self.runner.cleanup()


class TestClientRetries(aiounittest.AsyncTestCase):
    def setUp(self) -> None:
        logger_ctx.set(logging.getLogger("clairvoyance"))

    async def test_rate_limit_honors_retry_after(self) -> None:
        server = ScriptedServer([429], retry_after="1")
        await server.start()
        client = Client(server.url, max_retries=3)
        start = time.monotonic()
        try:
            response = await asyncio.wait_for(client.post("query { __typename }"), 5)
        finally:
            await client.close()
            await server.stop()

        self.assertEqual(response, {"data": {"__typename": "Query"}})
        self.assertEqual(server.hits, 2)
        self.assertGreaterEqual(time.monotonic() - start, 1)

    async def test_retries_do_not_deadlock_the_semaphore(self) -> None:
        server = ScriptedServer([503, 503, 503])
        await server.start()
        client = Client(server.url, concurrent_requests=1, max_retries=5)
        try:
            responses = await asyncio.wait_for(
                asyncio.gather(
                    *(client.post("query { __typename }") for _ in range(3))
                ),
                5,
            )
        finally:
            await client.close()
            await server.stop()

        self.assertTrue(all(r == {"data": {"__typename": "Query"}} for r in responses))

    async def test_gives_up_loudly(self) -> None:
        server = ScriptedServer([503, 503])
        await server.start()
        client = Client(server.url, max_retries=2)
        try:
            with self.assertLogs("clairvoyance", level="ERROR"):
                response = await asyncio.wait_for(
                    client.post("query { __typename }"), 5
                )
        finally:
            await client.close()
            await server.stop()

        self.assertEqual(response, {})


class TestParseRetryAfter(unittest.TestCase):
    def test_values(self) -> None:
        self.assertEqual(parse_retry_after("7"), 7.0)
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("-1"))
        self.assertIsNone(parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT"))


if __name__ == "__main__":
    unittest.main()
