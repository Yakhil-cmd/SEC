# Q0590: Get Tx Cmd authz invariant edge d086

## Question
Can an unprivileged attacker reach `GetTxCmd` in `sei-cosmos/x/authz/module/module.go` via public authorization grant, revoke, or exec message flow, controlling grantee/granter addresses, authorization type URLs, spend limits, expiration times, nested exec messages, and revocation timing, and make authorization validation inspect different message bytes than the message eventually executed so that the invariant `grantees must never execute messages outside the exact authorization, expiration, spend limit, and type constraints granted by users` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/authz/module/module.go:98` `GetTxCmd`
- Entrypoint: public authorization grant, revoke, or exec message flow
- Attacker controls: grantee/granter addresses, authorization type URLs, spend limits, expiration times, nested exec messages, and revocation timing
- Exploit idea: make authorization validation inspect different message bytes than the message eventually executed
- Invariant to test: grantees must never execute messages outside the exact authorization, expiration, spend limit, and type constraints granted by users
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Grant, exec, revoke, and replay nested messages in a msg-server test, then assert the executed bytes and spend are exactly covered by the active authorization.
