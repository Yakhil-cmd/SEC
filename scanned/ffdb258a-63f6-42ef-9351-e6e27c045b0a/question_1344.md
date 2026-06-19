# Q1344: sns governance: get account ids and tokens resource accounting

## Question
Can an unprivileged attacker enter through a canister/user invokes SNS governance/root/swap endpoints with malformed candid payloads and drive `rs/sns/init/src/distributions.rs`::get_account_ids_and_tokens with attacker-controlled proposal payloads, upgrade targets, token ledger calls, archive/index references, and retry ordering to make fees, cycles, memory, instructions, or refunds diverge across success, reject, trap, and retry paths; specifically, can this trigger a failed ledger or root callback that leaves governance/swap state partially committed, violating the invariant that swap/treasury/ledger flows must conserve tokens and be idempotent under retries, and produce HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss?

## Target
- File/function: `rs/sns/init/src/distributions.rs`::get_account_ids_and_tokens
- Entrypoint: a canister/user invokes SNS governance/root/swap endpoints with malformed candid payloads
- Attacker controls: proposal payloads, upgrade targets, token ledger calls, archive/index references, and retry ordering
- Exploit idea: trigger a failed ledger or root callback that leaves governance/swap state partially committed
- Invariant to test: swap/treasury/ledger flows must conserve tokens and be idempotent under retries
- Expected HackenProof impact: HackenProof High: SNS governance compromise, token loss/minting, or canister integrity loss
- Fast validation: run a PocketIC SNS lifecycle test with reordered callbacks/retries and assert token conservation and authorization
