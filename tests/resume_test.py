import importlib
import json
import logging
import os
import tempfile
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

import aiounittest

from clairvoyance import graphql, oracle
from clairvoyance.config import Config
from clairvoyance.entities.context import client_ctx, logger_ctx
from clairvoyance.entities.interfaces import IClient
from clairvoyance.utils import parse_args

# The package exports a cli() function that shadows the module of the same name.
cli = importlib.import_module("clairvoyance.cli")
LOGGER = logging.getLogger("clairvoyance")


class NoFieldsClient(IClient):
    """Rejects every field on the innermost type of the document."""

    def __init__(self, *_: Any, **__: Any) -> None:
        self.requests: List[str] = []
        client_ctx.set(self)

    async def post(self, document: Optional[str], retries: int = 0) -> Dict[str, Any]:
        assert document
        self.requests.append(document)
        inner = document.rsplit("{", 1)[1].split("}", 1)[0]
        typename = "Cart" if "cart {" in document else "Query"
        errors = [
            {"message": f'Cannot query field "{w}" on type "{typename}".'}
            for w in inner.split()
        ]
        return {"errors": errors}

    async def close(self) -> None:
        pass


def build_schema(query_fields: List[str]) -> graphql.Schema:
    """Query with the given String fields plus cart: Cart, where Cart has no fields yet."""

    schema = graphql.Schema(query_type="Query")
    schema.add_type("Cart", "OBJECT")
    query = schema.types["Query"]
    query.fields.append(graphql.Field("cart", graphql.TypeRef("Cart", "OBJECT")))
    for name in query_fields:
        query.fields.append(graphql.Field(name, graphql.TypeRef("String", "SCALAR")))
    return schema


class TestResume(aiounittest.AsyncTestCase):
    def setUp(self) -> None:
        logger_ctx.set(LOGGER)
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.path = os.path.join(self.tmp.name, "schema.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def run_resume(
        self, schema: graphql.Schema, wordlist: List[str]
    ) -> NoFieldsClient:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(repr(schema))

        fake = NoFieldsClient()
        with mock.patch.object(cli, "Client", lambda *a, **kw: fake):
            await cli.blind_introspection(
                "http://localhost",
                logger=LOGGER,
                wordlist=wordlist,
                input_schema_path=self.path,
                output_path=self.path,
            )
        return fake

    async def test_resume_skips_types_with_fields(self) -> None:
        fake = await self.run_resume(build_schema(["name"]), ["foo", "bar"])

        self.assertTrue(fake.requests)
        self.assertTrue(all("cart {" in r for r in fake.requests), fake.requests)

        with open(self.path, "r", encoding="utf-8") as f:
            query = graphql.Schema(schema=json.load(f)).types["Query"]
        self.assertEqual(sorted(f.name for f in query.fields), ["cart", "name"])

    async def test_resume_with_nothing_left(self) -> None:
        schema = build_schema(["name"])
        schema.types["Cart"].fields.append(
            graphql.Field("id", graphql.TypeRef("ID", "SCALAR"))
        )

        fake = await self.run_resume(schema, ["foo"])

        self.assertEqual(fake.requests, [])

    async def test_resume_probes_with_discovered_names(self) -> None:
        fake = await self.run_resume(build_schema(["shippingAddress"]), ["foo"])

        words = set(" ".join(fake.requests).replace("{", " ").replace("}", " ").split())
        self.assertTrue(
            {"shippingAddress", "shipping", "address", "foo"} <= words, words
        )


class TestDiscoveredWords(unittest.TestCase):
    def test_names_types_and_parts(self) -> None:
        schema = build_schema(["order_items"])
        schema.add_type("GiftCard", "OBJECT")
        schema.add_type("V2_Asset", "OBJECT")
        schema.types["Query"].fields[0].args.append(
            graphql.InputValue("pageSize", graphql.TypeRef("Int", "SCALAR"))
        )

        words = cli.discovered_words(schema)

        for want in [
            "cart",
            "pageSize",
            "page",
            "size",
            "order_items",
            "order",
            "items",
            "giftCard",
            "gift",
            "card",
        ]:
            self.assertIn(want, words)
        self.assertNotIn("v2_Asset", words)
        self.assertNotIn("string", words)


class TestNextDocument(unittest.TestCase):
    def setUp(self) -> None:
        logger_ctx.set(LOGGER)

    def test_unreachable_type_is_skipped(self) -> None:
        schema = build_schema([])
        schema.add_type("Orphan", "OBJECT")
        ignored = {"String", "ID", "Cart"}

        with self.assertLogs("clairvoyance", level="WARNING"):
            self.assertEqual(cli.next_document(schema, ignored), "")
        self.assertIn("Orphan", ignored)

    def test_returns_path_document(self) -> None:
        self.assertEqual(
            cli.next_document(build_schema([]), {"String", "ID"}),
            "query { cart { FUZZ } }",
        )


class TestReprobeDoesNotDuplicate(aiounittest.AsyncTestCase):
    def setUp(self) -> None:
        logger_ctx.set(LOGGER)
        Config()

    async def test_existing_field_is_replaced(self) -> None:
        cart = graphql.Field("cart", graphql.TypeRef("Cart", "OBJECT"))
        with mock.patch.object(
            oracle, "probe_typename", mock.AsyncMock(return_value="Query")
        ), mock.patch.object(
            oracle, "probe_valid_fields", mock.AsyncMock(return_value={"cart"})
        ), mock.patch.object(
            oracle, "explore_field", mock.AsyncMock(return_value=(cart, []))
        ):
            out = await oracle.clairvoyance(
                ["cart"], "query { FUZZ }", json.loads(repr(build_schema([])))
            )

        query = graphql.Schema(schema=json.loads(out)).types["Query"]
        self.assertEqual([f.name for f in query.fields], ["cart"])


class TestDocumentArg(unittest.TestCase):
    def test_document_defaults_to_none(self) -> None:
        self.assertIsNone(parse_args(["http://localhost"]).document)


if __name__ == "__main__":
    unittest.main()
