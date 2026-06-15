# Q0737: Set Validator Signing Info staking invariant edge 2953

## Question
Can an unprivileged attacker reach `SetValidatorSigningInfo` in `sei-cosmos/x/slashing/keeper/signing_info.go` via public staking, delegation, undelegation, redelegation, evidence, or slashing message flow, controlling delegator/validator addresses, shares, denom amounts, redelegation targets, unbonding timing, evidence payloads, and message ordering, and break delegation, share, unbonding, redelegation, or slashing accounting through ordering and rounding edge cases so that the invariant `delegator shares, validator tokens, power updates, unbonding queues, and slashing state must remain conserved and deterministic` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `sei-cosmos/x/slashing/keeper/signing_info.go:34` `SetValidatorSigningInfo`
- Entrypoint: public staking, delegation, undelegation, redelegation, evidence, or slashing message flow
- Attacker controls: delegator/validator addresses, shares, denom amounts, redelegation targets, unbonding timing, evidence payloads, and message ordering
- Exploit idea: break delegation, share, unbonding, redelegation, or slashing accounting through ordering and rounding edge cases
- Invariant to test: delegator shares, validator tokens, power updates, unbonding queues, and slashing state must remain conserved and deterministic
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Create a state-machine test around delegation/redelegation/unbonding/evidence ordering and assert shares, tokens, power, slashing, and unbonding queues stay consistent.
