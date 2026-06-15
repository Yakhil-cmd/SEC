# Q1111: try Reset Period feegrant invariant edge 7e00

## Question
Can an unprivileged attacker reach `tryResetPeriod` in `sei-cosmos/x/feegrant/periodic_fee.go` via public feegrant allowance creation, use, pruning, or revocation flow, controlling granter/grantee addresses, allowance types, spend limits, expiration, fee denom, and transaction fee selection, and spend a grant beyond its denom, limit, period, or expiration by manipulating fee fields and transaction ordering so that the invariant `failed or reordered transactions must not consume or avoid consuming allowances inconsistently with fee charging` fails, causing `Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds`?

## Target
- File/function: `sei-cosmos/x/feegrant/periodic_fee.go:56` `tryResetPeriod`
- Entrypoint: public feegrant allowance creation, use, pruning, or revocation flow
- Attacker controls: granter/grantee addresses, allowance types, spend limits, expiration, fee denom, and transaction fee selection
- Exploit idea: spend a grant beyond its denom, limit, period, or expiration by manipulating fee fields and transaction ordering
- Invariant to test: failed or reordered transactions must not consume or avoid consuming allowances inconsistently with fee charging
- Expected Immunefi impact: Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds
- Fast validation: Create feegrant allowances, execute paid and failing transactions with edge fees, and assert allowance consumption equals actual protocol fee deduction.
