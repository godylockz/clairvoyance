# Troubleshooting Guide

At first, determine the case: whether the Clairvoyance doesn't work at all, with particular website or stops working during its executing. After this, refer to appropriate section for guidance.

## The Clairvoyance doesn't work at all
 - Installation: make sure that there were no errors during installation and that you are using correct version of dependencies.
 - Internet: make sure that you have a stable connection and that the Clairvoyance has rights to access your target.
 - Usage: refer to the [README](README.md) for usage instructions.

## The Clairvoyance doesn't work with particular website
 - Scope: make sure that there is no general issue: try `clairvoyance https://rickandmortyapi.com/graphql -o schema.json` or known working target.
 - URI: check endpoint URI validity (it might be a typo, an erroneous copy or other issue).
 - Authorization issue: ensure usage of correct headers, cookies and other authorization pieces.
 - WAF: ensure that the program imitates browser/web client at sufficient level, e.g. check that user agent matches modern browser/app-specific web client.
 - Error limit: some servers (graphql-js and Apollo by default) stop validating after 100 errors and reply `Too many validation errors`. Clairvoyance detects this and splits each bucket in half, which costs extra requests. Pass `--bucket-size` so one request stays under the limit: divide 100 by the number of errors a single wrong word produces (some servers repeat each error), e.g. `--bucket-size 32`.

## The Clairvoyance worked, but stopped working
 - Does the program work when you restart it? If the answer is yes, check "Rate Limits", if not, then check "Server-related issue"
 - Rate Limits: server might rate limit requests from the same IP/user. Lower amount of threads (`-c`), add a backoff (`-b`) or try to run the program in a slow mode. HTTP 429 responses are retried after the server's `Retry-After` delay, or after 5 seconds when the server sends none. A request that still fails after `-m` retries is logged as an error and its words are missing from the schema, so rerun if you see one.
 - Server-related issue: server might be temporary unavailable, check the endpoint manually using browser or other previously used tools. Alternatively, your IP/user might have been banned.
