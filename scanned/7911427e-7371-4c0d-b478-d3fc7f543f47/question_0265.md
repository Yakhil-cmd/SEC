# Q0265: Unpack Interfaces feegrant invariant edge 97cc

## Question
Can an unprivileged attacker reach `UnpackInterfaces` in `sei-cosmos/x/feegrant/grant.go` via public feegrant allowance creation, use, pruning, or revocation flow, controlling granter/grantee addresses, allowance types, spend limits, expiration, fee denom, and transaction fee selection, and spend a grant beyond its denom, limit, period, or expiration by manipulating fee fields and transaction ordering so that the invariant `feegrant spend limits, periods, denoms, expiration, payer identity, and actual fee deduction must remain synchronized` fails, causing `Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds`?

## Target
- File/function: `sei-cosmos/x/feegrant/grant.go:66` `UnpackInterfaces`
- Entrypoint: public feegrant allowance creation, use, pruning, or revocation flow
- Attacker controls: granter/grantee addresses, allowance types, spend limits, expiration, fee denom, and transaction fee selection
- Exploit idea: spend a grant beyond its denom, limit, period, or expiration by manipulating fee fields and transaction ordering
- Invariant to test: feegrant spend limits, periods, denoms, expiration, payer identity, and actual fee deduction must remain synchronized
- Expected Immunefi impact: Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds
- Fast validation: Create feegrant allowances, execute paid and failing transactions with edge fees, and assert allowance consumption equals actual protocol fee deduction.
