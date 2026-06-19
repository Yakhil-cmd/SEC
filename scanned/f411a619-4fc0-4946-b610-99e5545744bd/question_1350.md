# Q1350: sns governance: now signature/domain

## Question
Can an unprivileged attacker enter through a caller races SNS swap lifecycle, governance proposal execution, and root upgrade methods and drive `rs/sns/root/src/types.rs`::now with attacker-controlled claim tickets, buyer state, finalization timing, ledger transfer metadata, and neuron basket parameters to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state, violating the invariant that sale lifecycle transitions must not be skipped, replayed, or finalized from inconsistent state, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/root/src/types.rs`::now
- Entrypoint: a caller races SNS swap lifecycle, governance proposal execution, and root upgrade methods
- Attacker controls: claim tickets, buyer state, finalization timing, ledger transfer metadata, and neuron basket parameters
- Exploit idea: bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state
- Invariant to test: sale lifecycle transitions must not be skipped, replayed, or finalized from inconsistent state
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization; mutate domain separators, registry versions, signer IDs, and message bytes independently
