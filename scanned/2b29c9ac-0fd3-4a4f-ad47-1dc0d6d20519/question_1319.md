# Q1319: sns governance: remove neuron from follower index certification/witness

## Question
Can an unprivileged attacker enter through public neuron management flow and drive `rs/sns/governance/src/follower_index.rs`::remove_neuron_from_follower_index with attacker-controlled proposal payloads, upgrade targets, token ledger calls, archive/index references, and retry ordering to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state, violating the invariant that SNS governance actions must be authorized by neuron permissions and token-holder state, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/governance/src/follower_index.rs`::remove_neuron_from_follower_index
- Entrypoint: public neuron management flow
- Attacker controls: proposal payloads, upgrade targets, token ledger calls, archive/index references, and retry ordering
- Exploit idea: bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state
- Invariant to test: SNS governance actions must be authorized by neuron permissions and token-holder state
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization
