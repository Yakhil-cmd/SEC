# Q909: nns governance: build certification/witness

## Question
Can an unprivileged attacker enter through an unprivileged NNS user submits ManageNeuron, proposal, vote, claim, disburse, or stake commands and drive `rs/nns/governance/src/storage/neuron_indexes.rs`::build with attacker-controlled proposal payloads, followee lists, hotkeys, permissions, ledger transfer metadata, and retry timing to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this submit an action whose validation differs from execution after registry/governance state changes, violating the invariant that proposal execution must be exactly-once and match the accepted proposal payload, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/src/storage/neuron_indexes.rs`::build
- Entrypoint: an unprivileged NNS user submits ManageNeuron, proposal, vote, claim, disburse, or stake commands
- Attacker controls: proposal payloads, followee lists, hotkeys, permissions, ledger transfer metadata, and retry timing
- Exploit idea: submit an action whose validation differs from execution after registry/governance state changes
- Invariant to test: proposal execution must be exactly-once and match the accepted proposal payload
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution
