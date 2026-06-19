# Q896: nns governance: new for test rollback edge case

## Question
Can an unprivileged attacker enter through a user interacts with CMC/GTC/SNS-W/root/lifeline canister methods without privileged authority and drive `rs/nns/governance/src/neuron_store/voting_power.rs`::new_for_test with attacker-controlled proposal payloads, followee lists, hotkeys, permissions, ledger transfer metadata, and retry timing to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this double-apply a proposal/neuron state transition through retry, timer, or inter-canister callback ordering, violating the invariant that governance and ledger state must stay conserved across retries, callbacks, and failed transfers, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/src/neuron_store/voting_power.rs`::new_for_test
- Entrypoint: a user interacts with CMC/GTC/SNS-W/root/lifeline canister methods without privileged authority
- Attacker controls: proposal payloads, followee lists, hotkeys, permissions, ledger transfer metadata, and retry timing
- Exploit idea: double-apply a proposal/neuron state transition through retry, timer, or inter-canister callback ordering
- Invariant to test: governance and ledger state must stay conserved across retries, callbacks, and failed transfers
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution
