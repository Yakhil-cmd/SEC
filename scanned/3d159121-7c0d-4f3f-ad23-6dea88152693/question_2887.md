# Q2887: Get Last Validator Power Key staking invariant edge cf6b

## Question
Can an unprivileged attacker reach `GetLastValidatorPowerKey` in `sei-cosmos/x/staking/types/keys.go` via public staking, delegation, undelegation, redelegation, evidence, or slashing message flow, controlling delegator/validator addresses, shares, denom amounts, redelegation targets, unbonding timing, evidence payloads, and message ordering, and make validator set updates diverge from staking keeper state during public end-block or message flows so that the invariant `delegator shares, validator tokens, power updates, unbonding queues, and slashing state must remain conserved and deterministic` fails, causing `High: Network not being able to confirm new transactions from total shutdown or consensus failure`?

## Target
- File/function: `sei-cosmos/x/staking/types/keys.go:118` `GetLastValidatorPowerKey`
- Entrypoint: public staking, delegation, undelegation, redelegation, evidence, or slashing message flow
- Attacker controls: delegator/validator addresses, shares, denom amounts, redelegation targets, unbonding timing, evidence payloads, and message ordering
- Exploit idea: make validator set updates diverge from staking keeper state during public end-block or message flows
- Invariant to test: delegator shares, validator tokens, power updates, unbonding queues, and slashing state must remain conserved and deterministic
- Expected Immunefi impact: High: Network not being able to confirm new transactions from total shutdown or consensus failure
- Fast validation: Create a state-machine test around delegation/redelegation/unbonding/evidence ordering and assert shares, tokens, power, slashing, and unbonding queues stay consistent.
