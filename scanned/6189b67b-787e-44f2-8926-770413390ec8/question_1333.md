# Q1333: sns governance: set topics for custom proposals canonical encoding

## Question
Can an unprivileged attacker enter through public proposal submission/execution flow and drive `rs/sns/governance/src/proposal/set_topics_for_custom_proposals.rs`::set_topics_for_custom_proposals with attacker-controlled proposal payloads, upgrade targets, token ledger calls, archive/index references, and retry ordering to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state, violating the invariant that root upgrades and proposal execution must be exactly-once and payload-bound, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/governance/src/proposal/set_topics_for_custom_proposals.rs`::set_topics_for_custom_proposals
- Entrypoint: public proposal submission/execution flow
- Attacker controls: proposal payloads, upgrade targets, token ledger calls, archive/index references, and retry ordering
- Exploit idea: bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state
- Invariant to test: root upgrades and proposal execution must be exactly-once and payload-bound
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
