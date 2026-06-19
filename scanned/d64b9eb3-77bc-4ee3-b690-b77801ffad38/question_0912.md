# Q912: nns governance: next reward task from now replay/idempotency

## Question
Can an unprivileged attacker enter through a user interacts with CMC/GTC/SNS-W/root/lifeline canister methods without privileged authority and drive `rs/nns/governance/src/timer_tasks/calculate_distributable_rewards.rs`::next_reward_task_from_now with attacker-controlled caller principal, neuron ID/subaccount, proposal action, memo, amount, dissolve delay, vote, and timestamps to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this submit an action whose validation differs from execution after registry/governance state changes, violating the invariant that governance and ledger state must stay conserved across retries, callbacks, and failed transfers, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/src/timer_tasks/calculate_distributable_rewards.rs`::next_reward_task_from_now
- Entrypoint: a user interacts with CMC/GTC/SNS-W/root/lifeline canister methods without privileged authority
- Attacker controls: caller principal, neuron ID/subaccount, proposal action, memo, amount, dissolve delay, vote, and timestamps
- Exploit idea: submit an action whose validation differs from execution after registry/governance state changes
- Invariant to test: governance and ledger state must stay conserved across retries, callbacks, and failed transfers
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
