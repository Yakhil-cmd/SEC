# Q0466: Grant Allowance feegrant invariant edge d1a7

## Question
Can an unprivileged attacker reach `GrantAllowance` in `sei-cosmos/x/feegrant/keeper/msg_server.go` via public feegrant allowance creation, use, pruning, or revocation flow, controlling granter/grantee addresses, allowance types, spend limits, expiration, fee denom, and transaction fee selection, and spend a grant beyond its denom, limit, period, or expiration by manipulating fee fields and transaction ordering so that the invariant `feegrant spend limits, periods, denoms, expiration, payer identity, and actual fee deduction must remain synchronized` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/feegrant/keeper/msg_server.go:27` `GrantAllowance`
- Entrypoint: public feegrant allowance creation, use, pruning, or revocation flow
- Attacker controls: granter/grantee addresses, allowance types, spend limits, expiration, fee denom, and transaction fee selection
- Exploit idea: spend a grant beyond its denom, limit, period, or expiration by manipulating fee fields and transaction ordering
- Invariant to test: feegrant spend limits, periods, denoms, expiration, payer identity, and actual fee deduction must remain synchronized
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Create feegrant allowances, execute paid and failing transactions with edge fees, and assert allowance consumption equals actual protocol fee deduction.
