# Q922: nns governance: temporarily set over soft message limit replay/idempotency

## Question
Can an unprivileged attacker enter through a governance participant repeats or races neuron-management calls through Rosetta/NNS canister APIs and drive `rs/nns/governance/src/voting.rs`::temporarily_set_over_soft_message_limit with attacker-controlled caller principal, neuron ID/subaccount, proposal action, memo, amount, dissolve delay, vote, and timestamps to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this bypass neuron permission checks by confusing neuron ID, subaccount, hotkey, or caller principal mapping, violating the invariant that voting power, maturity, rewards, and neuron ownership must not be forgeable or double-counted, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/src/voting.rs`::temporarily_set_over_soft_message_limit
- Entrypoint: a governance participant repeats or races neuron-management calls through Rosetta/NNS canister APIs
- Attacker controls: caller principal, neuron ID/subaccount, proposal action, memo, amount, dissolve delay, vote, and timestamps
- Exploit idea: bypass neuron permission checks by confusing neuron ID, subaccount, hotkey, or caller principal mapping
- Invariant to test: voting power, maturity, rewards, and neuron ownership must not be forgeable or double-counted
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
