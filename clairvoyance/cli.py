import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

from clairvoyance import graphql, oracle
from clairvoyance.client import Client
from clairvoyance.config import Config
from clairvoyance.entities import GraphQLPrimitive
from clairvoyance.entities.context import client, log, logger_ctx
from clairvoyance.utils import parse_args, setup_logger


def setup_context(
    url: str,
    logger: logging.Logger,
    headers: Optional[Dict[str, str]] = None,
    concurrent_requests: Optional[int] = None,
    proxy: Optional[str] = None,
    max_retries: Optional[int] = None,
    backoff: Optional[int] = None,
    disable_ssl_verify: Optional[bool] = None,
    bucket_size: Optional[int] = None,
) -> None:
    """Initialize objects and freeze them into the context."""

    Config(bucket_size=bucket_size)
    Client(
        url,
        headers=headers,
        concurrent_requests=concurrent_requests,
        proxy=proxy,
        max_retries=max_retries,
        backoff=backoff,
        disable_ssl_verify=disable_ssl_verify,
    )
    logger_ctx.set(logger)


def load_default_wordlist() -> List[str]:
    wl = Path(__file__).parent / "wordlist.txt"
    with open(wl, "r", encoding="utf-8") as f:
        return [w.strip() for w in f.readlines() if w.strip()]


# Words inside camelCase, PascalCase and snake_case names.
NAME_PART_REGEX = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
TYPE_NAME_REGEX = re.compile(r"[A-Z][A-Za-z0-9]*")


def discovered_words(schema: graphql.Schema) -> List[str]:
    """Names found so far, to probe other types with.

    Includes field and argument names, type names in field form (OrderItems -> orderItems), and the lowercased parts of all of these.
    """

    names: List[str] = []
    for t in schema.types.values():
        if TYPE_NAME_REGEX.fullmatch(t.name) and t.name not in GraphQLPrimitive:
            names.append(t.name[0].lower() + t.name[1:])
        for f in t.fields:
            names.append(f.name)
            names.extend(a.name for a in f.args)

    words: List[str] = []
    for name in names:
        words.append(name)
        words.extend(p.lower() for p in NAME_PART_REGEX.findall(name) if len(p) >= 3)
    return words


def extend_wordlist(wordlist: List[str], schema: graphql.Schema) -> None:
    """Append discovered names that are not in the wordlist yet."""

    known = set(wordlist)
    new = [w for w in dict.fromkeys(discovered_words(schema)) if w not in known]
    if new:
        wordlist.extend(new)
        log().info(f"Added {len(new)} names found in the schema to the wordlist")


def next_document(schema: graphql.Schema, ignored: Set[str]) -> str:
    """Return the document that probes the next type without fields, or "" when none is left.

    Every returned type is added to ignored so one run probes it once.
    """

    while True:
        _next = schema.get_type_without_fields(ignored)
        if not _next:
            return ""
        ignored.add(_next)
        try:
            return schema.convert_path_to_document(schema.get_path_from_root(_next))
        except ValueError as e:
            log().warning(f"Skipping type {_next}: {e}")


async def blind_introspection(  # pylint: disable=too-many-arguments
    url: str,
    logger: logging.Logger,
    wordlist: List[str],
    concurrent_requests: Optional[int] = None,
    headers: Optional[Dict[str, str]] = None,
    input_document: Optional[str] = None,
    input_schema_path: Optional[str] = None,
    output_path: Optional[str] = None,
    proxy: Optional[str] = None,
    max_retries: Optional[int] = None,
    backoff: Optional[int] = None,
    disable_ssl_verify: Optional[bool] = None,
    bucket_size: Optional[int] = None,
) -> str:
    wordlist = wordlist or load_default_wordlist()
    assert wordlist, "No wordlist provided"

    setup_context(
        url,
        logger=logger,
        headers=headers,
        concurrent_requests=concurrent_requests,
        proxy=proxy,
        max_retries=max_retries,
        backoff=backoff,
        disable_ssl_verify=disable_ssl_verify,
        bucket_size=bucket_size,
    )

    logger.info(f"Starting blind introspection on {url}...")

    input_schema = None
    schema = ""
    if input_schema_path:
        with open(input_schema_path, "r", encoding="utf-8") as f:
            input_schema = json.load(f)
        schema = json.dumps(input_schema, indent=4, sort_keys=True)

    ignored = set(e.value for e in GraphQLPrimitive)
    if input_schema:
        extend_wordlist(wordlist, graphql.Schema(schema=input_schema))
    if not input_document:
        if input_schema:
            # Resume: types that already have fields are done.
            input_document = next_document(graphql.Schema(schema=input_schema), ignored)
            logger.info(f"Resuming with {input_document or 'nothing left to probe'}")
        else:
            input_document = "query { FUZZ }"

    iterations = 1
    while input_document:
        logger.info(f"Iteration {iterations}")
        iterations += 1
        schema = await oracle.clairvoyance(
            wordlist,
            input_document=input_document,
            input_schema=input_schema,
        )

        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(schema)

        input_schema = json.loads(schema)
        s = graphql.Schema(schema=input_schema)
        extend_wordlist(wordlist, s)
        input_document = next_document(s, ignored)

    logger.info("Blind introspection complete.")
    await client().close()
    return schema


def cli(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    args = parse_args(argv)
    setup_logger(args.verbose)

    headers = {}
    for h in args.headers:
        key, value = h.split(": ", 1)
        headers[key] = value

    wordlist = []
    if args.wordlist:
        wordlist = [w.strip() for w in args.wordlist.readlines() if w.strip()]
        # de-dupe the wordlist.
        wordlist = list(set(wordlist))

    # remove wordlist items that don't conform to graphQL regex github-issue #11
    if args.validate:
        wordlist_parsed = [
            w for w in wordlist if re.match(r"[_A-Za-z][_0-9A-Za-z]*", w)
        ]
        logging.info(
            f"Removed {len(wordlist) - len(wordlist_parsed)} items from wordlist, to conform to name regex. "
            f"https://spec.graphql.org/June2018/#sec-Names"
        )
        wordlist = wordlist_parsed

    asyncio.run(
        blind_introspection(
            args.url,
            logger=logging.getLogger("clairvoyance"),
            concurrent_requests=args.concurrent_requests,
            headers=headers,
            input_document=args.document,
            input_schema_path=args.input_schema,
            output_path=args.output,
            wordlist=wordlist,
            proxy=args.proxy,
            max_retries=args.max_retries,
            backoff=args.backoff,
            disable_ssl_verify=args.no_ssl,
            bucket_size=args.bucket_size,
        )
    )
