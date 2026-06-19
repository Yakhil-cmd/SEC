# Q1326: sns governance: is sns extensions enabled rollback edge case

## Question
Can an unprivileged attacker enter through a caller races SNS swap lifecycle, governance proposal execution, and root upgrade methods and drive `rs/sns/governance/src/lib.rs`::is_sns_extensions_enabled with attacker-controlled SNS neuron IDs, principals, permissions, ballots, sale amounts, swap lifecycle, treasury requests, and timestamps to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state, violating the invariant that sale lifecycle transitions must not be skipped, replayed, or finalized from inconsistent state, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/governance/src/lib.rs`::is_sns_extensions_enabled
- Entrypoint: a caller races SNS swap lifecycle, governance proposal execution, and root upgrade methods
- Attacker controls: SNS neuron IDs, principals, permissions, ballots, sale amounts, swap lifecycle, treasury requests, and timestamps
- Exploit idea: bypass SNS neuron permissions by confusing caller, principal alias, subaccount, or cached permission state
- Invariant to test: sale lifecycle transitions must not be skipped, replayed, or finalized from inconsistent state
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization
