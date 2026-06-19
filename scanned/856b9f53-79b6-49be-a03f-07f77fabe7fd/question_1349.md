# Q1349: sns governance: Try From certification/witness

## Question
Can an unprivileged attacker enter through an unprivileged SNS participant submits proposal, vote, neuron, swap, root, or treasury-manager calls and drive `rs/sns/root/src/pb/mod.rs`::TryFrom with attacker-controlled claim tickets, buyer state, finalization timing, ledger transfer metadata, and neuron basket parameters to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state, violating the invariant that root upgrades and proposal execution must be exactly-once and payload-bound, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/root/src/pb/mod.rs`::TryFrom
- Entrypoint: an unprivileged SNS participant submits proposal, vote, neuron, swap, root, or treasury-manager calls
- Attacker controls: claim tickets, buyer state, finalization timing, ledger transfer metadata, and neuron basket parameters
- Exploit idea: bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state
- Invariant to test: root upgrades and proposal execution must be exactly-once and payload-bound
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization
