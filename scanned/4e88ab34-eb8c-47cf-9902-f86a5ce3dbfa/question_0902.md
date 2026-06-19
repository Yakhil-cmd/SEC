# Q902: nns governance: From replay/idempotency

## Question
Can an unprivileged attacker enter through a governance participant repeats or races neuron-management calls through Rosetta/NNS canister APIs and drive `rs/nns/governance/src/pb/convert_struct_to_enum/manage_neuron/configure.rs`::From with attacker-controlled claim/disburse parameters, governance state transitions, registry mutations, and upgrade payload references to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this submit an action whose validation differs from execution after registry/governance state changes, violating the invariant that voting power, maturity, rewards, and neuron ownership must not be forgeable or double-counted, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/governance/src/pb/convert_struct_to_enum/manage_neuron/configure.rs`::From
- Entrypoint: a governance participant repeats or races neuron-management calls through Rosetta/NNS canister APIs
- Attacker controls: claim/disburse parameters, governance state transitions, registry mutations, and upgrade payload references
- Exploit idea: submit an action whose validation differs from execution after registry/governance state changes
- Invariant to test: voting power, maturity, rewards, and neuron ownership must not be forgeable or double-counted
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
