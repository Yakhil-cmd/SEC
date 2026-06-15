# Q0475: Exec authz invariant edge 2485

## Question
Can an unprivileged attacker reach `Exec` in `sei-cosmos/x/authz/keeper/msg_server.go` via public authorization grant, revoke, or exec message flow, controlling grantee/granter addresses, authorization type URLs, spend limits, expiration times, nested exec messages, and revocation timing, and execute a nested message that exceeds or outlives the granted authorization due to type, expiration, or spend-limit mismatch so that the invariant `authorization validation must cover the same nested message bytes that are later executed` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/authz/keeper/msg_server.go:65` `Exec`
- Entrypoint: public authorization grant, revoke, or exec message flow
- Attacker controls: grantee/granter addresses, authorization type URLs, spend limits, expiration times, nested exec messages, and revocation timing
- Exploit idea: execute a nested message that exceeds or outlives the granted authorization due to type, expiration, or spend-limit mismatch
- Invariant to test: authorization validation must cover the same nested message bytes that are later executed
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Grant, exec, revoke, and replay nested messages in a msg-server test, then assert the executed bytes and spend are exactly covered by the active authorization.
