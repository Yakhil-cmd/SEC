# Q893: nns governance: retain canonical encoding

## Question
Can an unprivileged attacker enter through an unprivileged NNS user submits ManageNeuron, proposal, vote, claim, disburse, or stake commands and drive `rs/nns/governance/src/neuron_lock.rs`::retain with attacker-controlled proposal payloads, followee lists, hotkeys, permissions, ledger transfer metadata, and retry timing to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this bypass neuron permission checks by confusing neuron ID, subaccount, hotkey, or caller principal mapping, violating the invariant that proposal execution must be exactly-once and match the accepted proposal payload, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/src/neuron_lock.rs`::retain
- Entrypoint: an unprivileged NNS user submits ManageNeuron, proposal, vote, claim, disburse, or stake commands
- Attacker controls: proposal payloads, followee lists, hotkeys, permissions, ledger transfer metadata, and retry timing
- Exploit idea: bypass neuron permission checks by confusing neuron ID, subaccount, hotkey, or caller principal mapping
- Invariant to test: proposal execution must be exactly-once and match the accepted proposal payload
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
