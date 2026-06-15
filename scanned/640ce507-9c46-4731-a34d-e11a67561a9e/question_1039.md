# Q1039: Route authz invariant edge 0f28

## Question
Can an unprivileged attacker reach `Route` in `sei-cosmos/x/authz/msgs.go` via public authorization grant, revoke, or exec message flow, controlling grantee/granter addresses, authorization type URLs, spend limits, expiration times, nested exec messages, and revocation timing, and make authorization validation inspect different message bytes than the message eventually executed so that the invariant `authorization validation must cover the same nested message bytes that are later executed` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/authz/msgs.go:167` `Route`
- Entrypoint: public authorization grant, revoke, or exec message flow
- Attacker controls: grantee/granter addresses, authorization type URLs, spend limits, expiration times, nested exec messages, and revocation timing
- Exploit idea: make authorization validation inspect different message bytes than the message eventually executed
- Invariant to test: authorization validation must cover the same nested message bytes that are later executed
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Grant, exec, revoke, and replay nested messages in a msg-server test, then assert the executed bytes and spend are exactly covered by the active authorization.
