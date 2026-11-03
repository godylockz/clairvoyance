import logging
import re
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

import aiounittest

from clairvoyance import graphql, oracle
from clairvoyance.config import Config
from clairvoyance.entities.context import client_ctx, logger_ctx
from clairvoyance.entities.interfaces import IClient

# Arguments of the fake server's root fields: name -> type signature.
ARGS = {
    "addToCart": {"cartId": "String!", "items": "[ItemInput!]!"},
    "resetEmail": {"email": "String!"},
    "toggle": {"enabled": "Boolean"},
    "search": {"phrase": "String!", "exact": "Boolean"},
}


def required_error(field: str, arg: str) -> str:
    return f'Field "{field}" argument "{arg}" of type "{ARGS[field][arg]}" is required, but it was not provided.'


class ArgsClient(IClient):
    """Answers argument probes like graphql-js, repeating every error twice like the tested server."""

    def __init__(self) -> None:
        self.requests: List[str] = []
        client_ctx.set(self)

    async def post(self, document: Optional[str], retries: int = 0) -> Dict[str, Any]:
        assert document
        self.requests.append(document)
        match = re.search(r"(\w+)\(([^()]*)\)\s*\}?\s*$", document.split("{", 1)[1])
        assert match, document
        field, given = match.group(1), [a.split(":") for a in match.group(2).split(",")]
        known = ARGS[field]

        errors = []
        for name, value in ((n.strip(), v.strip()) for n, v in given):
            if name not in known:
                errors.append(f'Unknown argument "{name}" on field "Mutation.{field}".')
            elif value.startswith("$v"):
                if known[name] != "Boolean":
                    errors.append(
                        f'Variable "{value}" of type "Boolean" used in position expecting type "{known[name]}".'
                    )
            elif known[name] != "Boolean":
                errors.append(f"String cannot represent a non string value: {value}")
        provided = {n.strip() for n, _ in given}
        errors += [
            required_error(field, a)
            for a, t in known.items()
            if t.endswith("!") and a not in provided
        ]

        return (
            {"errors": [{"message": e} for e in errors for _ in range(2)]}
            if errors
            else {"data": {}}
        )

    async def close(self) -> None:
        pass


class TestArgumentTypes(aiounittest.AsyncTestCase):
    def setUp(self) -> None:
        logger_ctx.set(logging.getLogger("clairvoyance"))
        Config(bucket_size=32)
        self.fake = ArgsClient()

    async def test_batch_typing_is_exact(self) -> None:
        types = await oracle.probe_arg_types(
            "addToCart", ["cartId", "items"], "mutation { FUZZ }"
        )

        self.assertEqual(len(self.fake.requests), 1)
        self.assertTrue(
            self.fake.requests[0].startswith("mutation($v0: Boolean, $v1: Boolean)")
        )
        self.assertEqual(
            types["cartId"], graphql.TypeRef("String", "SCALAR", non_null=True)
        )
        self.assertEqual(
            types["items"],
            graphql.TypeRef(
                "ItemInput",
                "INPUT_OBJECT",
                is_list=True,
                non_null_item=True,
                non_null=True,
            ),
        )

    async def test_argument_without_error_accepts_boolean(self) -> None:
        types = await oracle.probe_arg_types(
            "search", ["exact", "phrase"], "mutation { FUZZ }"
        )

        self.assertEqual(types["exact"], graphql.TypeRef("Boolean", "SCALAR"))
        self.assertEqual(
            types["phrase"], graphql.TypeRef("String", "SCALAR", non_null=True)
        )

    async def test_no_variable_errors_leaves_typing_to_fallback(self) -> None:
        # Without a single variable error the server may not report them at all, so nothing is assumed.
        self.assertEqual(
            await oracle.probe_arg_types("toggle", ["enabled"], "mutation { FUZZ }"), {}
        )

    async def test_required_argument_outside_wordlist_is_found(self) -> None:
        args = await oracle.probe_args(
            "resetEmail", ["junk1", "junk2"], "mutation { FUZZ }"
        )

        self.assertEqual(args, {"email"})

    async def test_fallback_does_not_take_another_arguments_type(self) -> None:
        typeref = await oracle.probe_arg_typeref(
            "addToCart", "cartId", "mutation { FUZZ }"
        )

        self.assertFalse(typeref and typeref.name == "ItemInput", typeref)

    async def test_root_scalar_field_gets_arguments(self) -> None:
        boolean = graphql.TypeRef("Boolean", "SCALAR")
        with mock.patch.object(
            oracle, "probe_field_type", mock.AsyncMock(return_value=boolean)
        ):
            field, args = await oracle.explore_field(
                "resetEmail",
                "mutation { FUZZ }",
                ["junk"],
                "Mutation",
                probe_scalar_args=True,
            )
            self.assertEqual(
                [(a.name, a.type.name, a.type.non_null) for a in args],
                [("email", "String", True)],
            )
            self.assertEqual(field.args, args)

            requests = len(self.fake.requests)
            _, args = await oracle.explore_field(
                "resetEmail", "mutation { FUZZ }", ["junk"], "Mutation"
            )
            self.assertEqual(args, [])
            self.assertEqual(len(self.fake.requests), requests)


class TestRootDocument(unittest.TestCase):
    def test_root_documents(self) -> None:
        for document in ["query { FUZZ }", "mutation { FUZZ }", "{ FUZZ }"]:
            self.assertTrue(oracle.ROOT_DOCUMENT_REGEX.fullmatch(document), document)
        self.assertFalse(
            oracle.ROOT_DOCUMENT_REGEX.fullmatch("query { cart { FUZZ } }")
        )


class MetaFieldClient(IClient):
    def __init__(self) -> None:
        client_ctx.set(self)

    async def post(self, document: Optional[str], retries: int = 0) -> Dict[str, Any]:
        return {"errors": [{"message": 'Cannot query field "junk" on type "Query".'}]}

    async def close(self) -> None:
        pass


class TestMetaFields(aiounittest.AsyncTestCase):
    def setUp(self) -> None:
        logger_ctx.set(logging.getLogger("clairvoyance"))
        Config(bucket_size=32)

    async def test_typename_is_not_a_field(self) -> None:
        MetaFieldClient()

        fields = await oracle.probe_valid_fields(
            ["__typename", "junk"], "query { FUZZ }"
        )

        self.assertEqual(fields, set())

    def test_loaded_schema_drops_meta_fields(self) -> None:
        typ = graphql.Type.from_json(
            {
                "name": "Query",
                "kind": "OBJECT",
                "fields": [
                    graphql.Field(
                        "__typename", graphql.TypeRef("String", "SCALAR")
                    ).to_json(),
                    graphql.Field("cart", graphql.TypeRef("Cart", "OBJECT")).to_json(),
                ],
            }
        )
        self.assertEqual([f.name for f in typ.fields], ["cart"])


if __name__ == "__main__":
    unittest.main()
