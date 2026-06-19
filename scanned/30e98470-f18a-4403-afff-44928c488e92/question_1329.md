# Q1329: sns governance: topic id to api certification/witness

## Question
Can an unprivileged attacker enter through an unprivileged SNS participant submits proposal, vote, neuron, swap, root, or treasury-manager calls and drive `rs/sns/governance/src/pb/conversions.rs`::topic_id_to_api with attacker-controlled claim tickets, buyer state, finalization timing, ledger transfer metadata, and neuron basket parameters to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this trigger a failed ledger or root callback that leaves governance/swap state partially committed, violating the invariant that root upgrades and proposal execution must be exactly-once and payload-bound, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/governance/src/pb/conversions.rs`::topic_id_to_api
- Entrypoint: an unprivileged SNS participant submits proposal, vote, neuron, swap, root, or treasury-manager calls
- Attacker controls: claim tickets, buyer state, finalization timing, ledger transfer metadata, and neuron basket parameters
- Exploit idea: trigger a failed ledger or root callback that leaves governance/swap state partially committed
- Invariant to test: root upgrades and proposal execution must be exactly-once and payload-bound
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization
