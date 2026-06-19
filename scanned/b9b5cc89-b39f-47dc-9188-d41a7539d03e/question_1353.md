# Q1353: sns governance: set dapp controllers canonical encoding

## Question
Can an unprivileged attacker enter through an unprivileged SNS participant submits proposal, vote, neuron, swap, root, or treasury-manager calls and drive `rs/sns/swap/src/clients.rs`::set_dapp_controllers with attacker-controlled proposal payloads, upgrade targets, token ledger calls, archive/index references, and retry ordering to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this execute a root/governance proposal with payload different from the one that passed validation, violating the invariant that root upgrades and proposal execution must be exactly-once and payload-bound, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/swap/src/clients.rs`::set_dapp_controllers
- Entrypoint: an unprivileged SNS participant submits proposal, vote, neuron, swap, root, or treasury-manager calls
- Attacker controls: proposal payloads, upgrade targets, token ledger calls, archive/index references, and retry ordering
- Exploit idea: execute a root/governance proposal with payload different from the one that passed validation
- Invariant to test: root upgrades and proposal execution must be exactly-once and payload-bound
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
