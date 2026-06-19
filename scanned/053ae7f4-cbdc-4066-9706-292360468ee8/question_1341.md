# Q1341: sns governance: from proposal authorization boundary

## Question
Can an unprivileged attacker enter through public proposal submission/execution flow and drive `rs/sns/governance/src/upgrade_journal.rs`::from_proposal with attacker-controlled SNS neuron IDs, principals, permissions, ballots, sale amounts, swap lifecycle, treasury requests, and timestamps to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this make swap finalization or claim logic mint, burn, or assign SNS tokens inconsistently across retries, violating the invariant that root upgrades and proposal execution must be exactly-once and payload-bound, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/governance/src/upgrade_journal.rs`::from_proposal
- Entrypoint: public proposal submission/execution flow
- Attacker controls: SNS neuron IDs, principals, permissions, ballots, sale amounts, swap lifecycle, treasury requests, and timestamps
- Exploit idea: make swap finalization or claim logic mint, burn, or assign SNS tokens inconsistently across retries
- Invariant to test: root upgrades and proposal execution must be exactly-once and payload-bound
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization
