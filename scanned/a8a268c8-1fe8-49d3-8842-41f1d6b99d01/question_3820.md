# Q3820: Get End Time cosmos tx invariant edge 32a7

## Question
Can an unprivileged attacker reach `GetEndTime` in `sei-cosmos/x/auth/vesting/types/vesting_account.go` via public Cosmos SDK transaction or query handler, controlling public message fields, signatures, fees, gas, pagination, addresses, denoms, and repeated transaction ordering, and trigger deterministic excessive work, panic, or fee undercharging in a default transaction or query path so that the invariant `module keepers must preserve balances, authorization, sequence, gas, and state invariants for every public transaction path` fails, causing `Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages`?

## Target
- File/function: `sei-cosmos/x/auth/vesting/types/vesting_account.go:590` `GetEndTime`
- Entrypoint: public Cosmos SDK transaction or query handler
- Attacker controls: public message fields, signatures, fees, gas, pagination, addresses, denoms, and repeated transaction ordering
- Exploit idea: trigger deterministic excessive work, panic, or fee undercharging in a default transaction or query path
- Invariant to test: module keepers must preserve balances, authorization, sequence, gas, and state invariants for every public transaction path
- Expected Immunefi impact: Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages
- Fast validation: Create a module keeper/msg-server regression test using only public messages, then assert the target invariant before and after success, failure, replay, and batching.
