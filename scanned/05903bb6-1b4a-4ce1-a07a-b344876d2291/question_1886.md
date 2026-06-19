# Q1886: ledger: set fee collector rollback edge case

## Question
Can an unprivileged attacker enter through an exchange/client sends Rosetta API requests with crafted operations, blocks, metadata, or signatures and drive `packages/icrc-ledger-types/src/icrc3/transactions.rs`::set_fee_collector with attacker-controlled accounts, subaccounts, spender, amount, fee, memo, created_at_time, block index, operation order, and signatures to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this make ledger, archive, index, or Rosetta derive different balances or transaction IDs for one operation, violating the invariant that archive and index views must not create spendable balance or hide finalized debits, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc3/transactions.rs`::set_fee_collector
- Entrypoint: an exchange/client sends Rosetta API requests with crafted operations, blocks, metadata, or signatures
- Attacker controls: accounts, subaccounts, spender, amount, fee, memo, created_at_time, block index, operation order, and signatures
- Exploit idea: make ledger, archive, index, or Rosetta derive different balances or transaction IDs for one operation
- Invariant to test: archive and index views must not create spendable balance or hide finalized debits
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs
