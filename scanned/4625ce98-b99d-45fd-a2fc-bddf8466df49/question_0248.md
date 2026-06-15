# Q0248: Get Grant feegrant invariant edge 8762

## Question
Can an unprivileged attacker reach `GetGrant` in `sei-cosmos/x/feegrant/grant.go` via public feegrant allowance creation, use, pruning, or revocation flow, controlling granter/grantee addresses, allowance types, spend limits, expiration, fee denom, and transaction fee selection, and make fee deduction use the wrong payer or wrong allowance after an execution error path so that the invariant `feegrant spend limits, periods, denoms, expiration, payer identity, and actual fee deduction must remain synchronized` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `sei-cosmos/x/feegrant/grant.go:56` `GetGrant`
- Entrypoint: public feegrant allowance creation, use, pruning, or revocation flow
- Attacker controls: granter/grantee addresses, allowance types, spend limits, expiration, fee denom, and transaction fee selection
- Exploit idea: make fee deduction use the wrong payer or wrong allowance after an execution error path
- Invariant to test: feegrant spend limits, periods, denoms, expiration, payer identity, and actual fee deduction must remain synchronized
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Create feegrant allowances, execute paid and failing transactions with edge fees, and assert allowance consumption equals actual protocol fee deduction.
