import logging
import re
import unittest
from typing import Any, Dict, List, Optional, Set

import aiounittest

from clairvoyance import oracle
from clairvoyance.config import Config
from clairvoyance.entities.context import client_ctx, logger_ctx
from clairvoyance.entities.interfaces import IClient
from clairvoyance.utils import parse_args

ERROR_LIMIT = 100
LIMIT_MESSAGE = "Too many validation errors, error limit reached. Validation aborted."


class CappedErrorsClient(IClient):
    """Mimics a graphql-js server that repeats every error twice and stops after 100 errors."""

    def __init__(self, valid: Set[str], give_up: bool = False) -> None:
        self.valid = valid
        self.give_up = give_up
        self.requests: List[str] = []
        client_ctx.set(self)

    async def post(self, document: Optional[str], retries: int = 0) -> Dict[str, Any]:
        assert document
        self.requests.append(document)
        if self.give_up:
            return {}

        args = re.search(r"\w+\((.*)\)", document)
        if args:
            words = [a.split(":")[0].strip() for a in args.group(1).split(",")]
            template = 'Unknown argument "{}" on field "Query.field".'
        else:
            words = document.split("{", 1)[1].rsplit("}", 1)[0].split()
            template = 'Cannot query field "{}" on type "Query".'

        errors: List[Dict[str, Any]] = []
        for word in words:
            if word not in self.valid:
                errors += [{"message": template.format(word)}] * 2
        if len(errors) > ERROR_LIMIT:
            errors = errors[:ERROR_LIMIT] + [{"message": LIMIT_MESSAGE}]

        return {"errors": errors} if errors else {"data": {}}

    async def close(self) -> None:
        pass


JUNK = [f"junk{i}" for i in range(200)]
REAL = ["products", "customer"]


class TestErrorLimit(aiounittest.AsyncTestCase):
    def setUp(self) -> None:
        logger_ctx.set(logging.getLogger("clairvoyance"))

    async def test_fields_split_when_errors_capped(self) -> None:
        Config(bucket_size=64)
        CappedErrorsClient(set(REAL))

        fields = await oracle.probe_valid_fields(JUNK + REAL, "query { FUZZ }")

        self.assertEqual(fields, set(REAL))

    async def test_args_split_when_errors_capped(self) -> None:
        Config(bucket_size=64)
        CappedErrorsClient({"sku", "id"})

        args = await oracle.probe_args("field", JUNK + ["sku", "id"], "query { FUZZ }")

        self.assertEqual(args, {"sku", "id"})

    async def test_small_bucket_does_not_split(self) -> None:
        Config(bucket_size=32)
        fake = CappedErrorsClient(set(REAL))

        fields = await oracle.probe_valid_fields(JUNK + REAL, "query { FUZZ }")

        self.assertEqual(fields, set(REAL))
        self.assertEqual(len(fake.requests), 7)

    async def test_failed_request_adds_nothing(self) -> None:
        Config(bucket_size=64)
        CappedErrorsClient(set(REAL), give_up=True)

        fields = await oracle.probe_valid_fields(JUNK + REAL, "query { FUZZ }")
        args = await oracle.probe_args("field", JUNK, "query { FUZZ }")

        self.assertEqual(fields, set())
        self.assertEqual(args, set())


class TestBucketSizeArg(unittest.TestCase):
    def test_bucket_size(self) -> None:
        self.assertEqual(
            parse_args(["--bucket-size", "32", "http://localhost"]).bucket_size, 32
        )
        self.assertIsNone(parse_args(["http://localhost"]).bucket_size)

    def test_bucket_size_must_be_positive(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--bucket-size", "0", "http://localhost"])


if __name__ == "__main__":
    unittest.main()
