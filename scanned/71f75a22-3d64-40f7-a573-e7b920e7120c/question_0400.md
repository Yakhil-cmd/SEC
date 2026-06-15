# Q0400: get Grant feegrant invariant edge 4278

## Question
Can an unprivileged attacker reach `getGrant` in `sei-cosmos/x/feegrant/keeper/keeper.go` via public feegrant allowance creation, use, pruning, or revocation flow, controlling granter/grantee addresses, allowance types, spend limits, expiration, fee denom, and transaction fee selection, and make fee deduction use the wrong payer or wrong allowance after an execution error path so that the invariant `failed or reordered transactions must not consume or avoid consuming allowances inconsistently with fee charging` fails, causing `Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds`?

## Target
- File/function: `sei-cosmos/x/feegrant/keeper/keeper.go:99` `getGrant`
- Entrypoint: public feegrant allowance creation, use, pruning, or revocation flow
- Attacker controls: granter/grantee addresses, allowance types, spend limits, expiration, fee denom, and transaction fee selection
- Exploit idea: make fee deduction use the wrong payer or wrong allowance after an execution error path
- Invariant to test: failed or reordered transactions must not consume or avoid consuming allowances inconsistently with fee charging
- Expected Immunefi impact: Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds
- Fast validation: Create feegrant allowances, execute paid and failing transactions with edge fees, and assert allowance consumption equals actual protocol fee deduction.
