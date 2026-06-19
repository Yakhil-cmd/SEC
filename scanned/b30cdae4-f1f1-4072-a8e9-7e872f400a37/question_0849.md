# Q849: nns governance: new with message certification/witness

## Question
Can an unprivileged attacker enter through an unprivileged NNS user submits ManageNeuron, proposal, vote, claim, disburse, or stake commands and drive `rs/nns/governance/api/src/lib.rs`::new_with_message with attacker-controlled caller principal, neuron ID/subaccount, proposal action, memo, amount, dissolve delay, vote, and timestamps to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this bypass neuron permission checks by confusing neuron ID, subaccount, hotkey, or caller principal mapping, violating the invariant that proposal execution must be exactly-once and match the accepted proposal payload, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/api/src/lib.rs`::new_with_message
- Entrypoint: an unprivileged NNS user submits ManageNeuron, proposal, vote, claim, disburse, or stake commands
- Attacker controls: caller principal, neuron ID/subaccount, proposal action, memo, amount, dissolve delay, vote, and timestamps
- Exploit idea: bypass neuron permission checks by confusing neuron ID, subaccount, hotkey, or caller principal mapping
- Invariant to test: proposal execution must be exactly-once and match the accepted proposal payload
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution
