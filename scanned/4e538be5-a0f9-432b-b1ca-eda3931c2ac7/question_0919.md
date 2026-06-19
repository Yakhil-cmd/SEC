# Q919: nns governance: execute certification/witness

## Question
Can an unprivileged attacker enter through a caller crafts proposal payloads, neuron IDs, subaccounts, followees, or voting inputs and drive `rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs`::execute with attacker-controlled proposal payloads, followee lists, hotkeys, permissions, ledger transfer metadata, and retry timing to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this bypass neuron permission checks by confusing neuron ID, subaccount, hotkey, or caller principal mapping, violating the invariant that only authorized principals may mutate neurons, proposals, governance config, or treasury-affecting state, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs`::execute
- Entrypoint: a caller crafts proposal payloads, neuron IDs, subaccounts, followees, or voting inputs
- Attacker controls: proposal payloads, followee lists, hotkeys, permissions, ledger transfer metadata, and retry timing
- Exploit idea: bypass neuron permission checks by confusing neuron ID, subaccount, hotkey, or caller principal mapping
- Invariant to test: only authorized principals may mutate neurons, proposals, governance config, or treasury-affecting state
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution
