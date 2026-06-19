# Q1336: sns governance: get upgrade params rollback edge case

## Question
Can an unprivileged attacker enter through a canister/user invokes SNS governance/root/swap endpoints with malformed candid payloads and drive `rs/sns/governance/src/sns_upgrade.rs`::get_upgrade_params with attacker-controlled SNS neuron IDs, principals, permissions, ballots, sale amounts, swap lifecycle, treasury requests, and timestamps to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this trigger a failed ledger or root callback that leaves governance/swap state partially committed, violating the invariant that swap/treasury/ledger flows must conserve tokens and be idempotent under retries, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/governance/src/sns_upgrade.rs`::get_upgrade_params
- Entrypoint: a canister/user invokes SNS governance/root/swap endpoints with malformed candid payloads
- Attacker controls: SNS neuron IDs, principals, permissions, ballots, sale amounts, swap lifecycle, treasury requests, and timestamps
- Exploit idea: trigger a failed ledger or root callback that leaves governance/swap state partially committed
- Invariant to test: swap/treasury/ledger flows must conserve tokens and be idempotent under retries
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization
