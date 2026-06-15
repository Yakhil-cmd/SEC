# Q2100: Get All Historical Info staking invariant edge e31e

## Question
Can an unprivileged attacker reach `GetAllHistoricalInfo` in `sei-cosmos/x/staking/keeper/historical_info.go` via public staking, delegation, undelegation, redelegation, evidence, or slashing message flow, controlling delegator/validator addresses, shares, denom amounts, redelegation targets, unbonding timing, evidence payloads, and message ordering, and make validator set updates diverge from staking keeper state during public end-block or message flows so that the invariant `public staking/evidence flows must not slash, freeze, or move funds for the wrong account or validator` fails, causing `High: Network not being able to confirm new transactions from total shutdown or consensus failure`?

## Target
- File/function: `sei-cosmos/x/staking/keeper/historical_info.go:57` `GetAllHistoricalInfo`
- Entrypoint: public staking, delegation, undelegation, redelegation, evidence, or slashing message flow
- Attacker controls: delegator/validator addresses, shares, denom amounts, redelegation targets, unbonding timing, evidence payloads, and message ordering
- Exploit idea: make validator set updates diverge from staking keeper state during public end-block or message flows
- Invariant to test: public staking/evidence flows must not slash, freeze, or move funds for the wrong account or validator
- Expected Immunefi impact: High: Network not being able to confirm new transactions from total shutdown or consensus failure
- Fast validation: Create a state-machine test around delegation/redelegation/unbonding/evidence ordering and assert shares, tokens, power, slashing, and unbonding queues stay consistent.
