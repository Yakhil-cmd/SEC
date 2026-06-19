# Q850: nns governance: create external update proposal candid signature/domain

## Question
Can an unprivileged attacker enter through public proposal submission/execution flow and drive `rs/nns/governance/api/src/proposal_submission_helpers.rs`::create_external_update_proposal_candid with attacker-controlled claim/disburse parameters, governance state transitions, registry mutations, and upgrade payload references to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this double-apply a proposal/neuron state transition through retry, timer, or inter-canister callback ordering, violating the invariant that voting power, maturity, rewards, and neuron ownership must not be forgeable or double-counted, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/api/src/proposal_submission_helpers.rs`::create_external_update_proposal_candid
- Entrypoint: public proposal submission/execution flow
- Attacker controls: claim/disburse parameters, governance state transitions, registry mutations, and upgrade payload references
- Exploit idea: double-apply a proposal/neuron state transition through retry, timer, or inter-canister callback ordering
- Invariant to test: voting power, maturity, rewards, and neuron ownership must not be forgeable or double-counted
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution; mutate domain separators, registry versions, signer IDs, and message bytes independently
