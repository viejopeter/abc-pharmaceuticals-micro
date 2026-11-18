# Prototype validation results

Run started 2026-11-18T23:21:26.848159+00:00.

System tested at http://127.0.0.1:8100. Overall result PASS.

| Test | Result | Observed evidence |
| --- | --- | --- |
| Stock update synchronises | PASS | HTTP 200; quantity 81; version 3 |
| Zero stock is a verified business result | PASS | HTTP 200; quantity 0; version 4 |
| Worker interruption withholds stale stock | PASS | Write committed while worker stopped; HTTP 503; stock null; feed age 30.358 s |
| Worker restart recovers the stock update | PASS | HTTP 200; quantity 82; version 5; recovered in 3.375 s |
| Duplicate event has no second stock effect | PASS | Duplicate counter 0 -> 1; quantity and version unchanged |
| Missing service key is denied | PASS | Direct backend enquiry without service key returned HTTP 401 |

## Session restoration

Restored AMX-500 quantity to 80; version is now 6.
All four backend processes are running.

## Scope

These checks exercise the running local prototype through its HTTP interfaces. The wait timeout is a test execution limit. Recovery time is measured from the start request until the first matching verified reply and includes polling overhead.

A group member still needs to validate the dashboard wording for zero and unavailable stock. Human usability, cloud capacity, production security and industry-standard conformance require separate evidence.

Full request and response evidence is saved in [evidence.json](evidence.json).
