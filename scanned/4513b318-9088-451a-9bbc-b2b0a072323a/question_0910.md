# Q910: nns governance: build signature/domain

## Question
Can an unprivileged attacker enter through a governance participant repeats or races neuron-management calls through Rosetta/NNS canister APIs and drive `rs/nns/governance/src/storage/neurons.rs`::build with attacker-controlled claim/disburse parameters, governance state transitions, registry mutations, and upgrade payload references to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this double-apply a proposal/neuron state transition through retry, timer, or inter-canister callback ordering, violating the invariant that voting power, maturity, rewards, and neuron ownership must not be forgeable or double-counted, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/src/storage/neurons.rs`::build
- Entrypoint: a governance participant repeats or races neuron-management calls through Rosetta/NNS canister APIs
- Attacker controls: claim/disburse parameters, governance state transitions, registry mutations, and upgrade payload references
- Exploit idea: double-apply a proposal/neuron state transition through retry, timer, or inter-canister callback ordering
- Invariant to test: voting power, maturity, rewards, and neuron ownership must not be forgeable or double-counted
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution; mutate domain separators, registry versions, signer IDs, and message bytes independently
