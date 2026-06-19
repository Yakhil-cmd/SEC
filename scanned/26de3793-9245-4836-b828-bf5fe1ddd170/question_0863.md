# Q863: nns governance: try new canonical encoding

## Question
Can an unprivileged attacker enter through a caller crafts proposal payloads, neuron IDs, subaccounts, followees, or voting inputs and drive `rs/nns/governance/src/governance/disburse_maturity.rs`::try_new with attacker-controlled claim/disburse parameters, governance state transitions, registry mutations, and upgrade payload references to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this submit an action whose validation differs from execution after registry/governance state changes, violating the invariant that only authorized principals may mutate neurons, proposals, governance config, or treasury-affecting state, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/src/governance/disburse_maturity.rs`::try_new
- Entrypoint: a caller crafts proposal payloads, neuron IDs, subaccounts, followees, or voting inputs
- Attacker controls: claim/disburse parameters, governance state transitions, registry mutations, and upgrade payload references
- Exploit idea: submit an action whose validation differs from execution after registry/governance state changes
- Invariant to test: only authorized principals may mutate neurons, proposals, governance config, or treasury-affecting state
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
