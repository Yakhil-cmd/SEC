# Q820: nns governance: add neuron id principal id signature/domain

## Question
Can an unprivileged attacker enter through public neuron management flow and drive `rs/nervous_system/governance/src/index/neuron_principal.rs`::add_neuron_id_principal_id with attacker-controlled claim/disburse parameters, governance state transitions, registry mutations, and upgrade payload references to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this double-apply a proposal/neuron state transition through retry, timer, or inter-canister callback ordering, violating the invariant that governance and ledger state must stay conserved across retries, callbacks, and failed transfers, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nervous_system/governance/src/index/neuron_principal.rs`::add_neuron_id_principal_id
- Entrypoint: public neuron management flow
- Attacker controls: claim/disburse parameters, governance state transitions, registry mutations, and upgrade payload references
- Exploit idea: double-apply a proposal/neuron state transition through retry, timer, or inter-canister callback ordering
- Invariant to test: governance and ledger state must stay conserved across retries, callbacks, and failed transfers
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution; mutate domain separators, registry versions, signer IDs, and message bytes independently
