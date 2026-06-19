# Q751: ledger: checked add authorization boundary

## Question
Can an unprivileged attacker enter through a spender races allowance, transfer_from, fee, memo, timestamp, and duplicate transaction windows and drive `rs/ledger_suite/common/ledger_core/src/tokens.rs`::checked_add with attacker-controlled ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this undercharge fees or mis-handle refunds when transfer/approval state changes across retries, violating the invariant that ledger balances plus fees must be conserved across transfer, approve, transfer_from, archive, and index paths, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `rs/ledger_suite/common/ledger_core/src/tokens.rs`::checked_add
- Entrypoint: a spender races allowance, transfer_from, fee, memo, timestamp, and duplicate transaction windows
- Attacker controls: ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields
- Exploit idea: undercharge fees or mis-handle refunds when transfer/approval state changes across retries
- Invariant to test: ledger balances plus fees must be conserved across transfer, approve, transfer_from, archive, and index paths
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs
