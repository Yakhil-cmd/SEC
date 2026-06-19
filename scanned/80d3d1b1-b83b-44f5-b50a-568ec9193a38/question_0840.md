# Q840: nns governance: stable set signature/domain

## Question
Can an unprivileged attacker enter through a user interacts with CMC/GTC/SNS-W/root/lifeline canister methods without privileged authority and drive `rs/nns/cmc/src/stable_utils.rs`::stable_set with attacker-controlled caller principal, neuron ID/subaccount, proposal action, memo, amount, dissolve delay, vote, and timestamps to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this double-apply a proposal/neuron state transition through retry, timer, or inter-canister callback ordering, violating the invariant that governance and ledger state must stay conserved across retries, callbacks, and failed transfers, and produce HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets?

## Target
- File/function: `rs/nns/cmc/src/stable_utils.rs`::stable_set
- Entrypoint: a user interacts with CMC/GTC/SNS-W/root/lifeline canister methods without privileged authority
- Attacker controls: caller principal, neuron ID/subaccount, proposal action, memo, amount, dissolve delay, vote, and timestamps
- Exploit idea: double-apply a proposal/neuron state transition through retry, timer, or inter-canister callback ordering
- Invariant to test: governance and ledger state must stay conserved across retries, callbacks, and failed transfers
- Expected HackenProof impact: HackenProof Critical/High: unauthorized NNS governance/neuron access or theft/illegal minting of ICP/Cycles/assets
- Fast validation: write a PocketIC/state-machine test that races the public governance call and asserts permissions/accounting/exactly-once execution; mutate domain separators, registry versions, signer IDs, and message bytes independently
