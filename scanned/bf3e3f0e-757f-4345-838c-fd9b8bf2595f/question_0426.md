# Q0426: first Address From Grant Store Key authz invariant edge 4d8c

## Question
Can an unprivileged attacker reach `firstAddressFromGrantStoreKey` in `sei-cosmos/x/authz/keeper/keys.go` via public authorization grant, revoke, or exec message flow, controlling grantee/granter addresses, authorization type URLs, spend limits, expiration times, nested exec messages, and revocation timing, and execute a nested message that exceeds or outlives the granted authorization due to type, expiration, or spend-limit mismatch so that the invariant `grantees must never execute messages outside the exact authorization, expiration, spend limit, and type constraints granted by users` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/authz/keeper/keys.go:54` `firstAddressFromGrantStoreKey`
- Entrypoint: public authorization grant, revoke, or exec message flow
- Attacker controls: grantee/granter addresses, authorization type URLs, spend limits, expiration times, nested exec messages, and revocation timing
- Exploit idea: execute a nested message that exceeds or outlives the granted authorization due to type, expiration, or spend-limit mismatch
- Invariant to test: grantees must never execute messages outside the exact authorization, expiration, spend limit, and type constraints granted by users
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Grant, exec, revoke, and replay nested messages in a msg-server test, then assert the executed bytes and spend are exactly covered by the active authorization.
