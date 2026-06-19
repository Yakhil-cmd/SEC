# Q46: ledger: Transfer From Args rollback edge case

## Question
Can an unprivileged attacker enter through public transfer or transfer_from flow and drive `packages/icrc-ledger-types/src/icrc2/transfer_from.rs`::TransferFromArgs with attacker-controlled Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this undercharge fees or mis-handle refunds when transfer/approval state changes across retries, violating the invariant that archive and index views must not create spendable balance or hide finalized debits, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc2/transfer_from.rs`::TransferFromArgs
- Entrypoint: public transfer or transfer_from flow
- Attacker controls: Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing
- Exploit idea: undercharge fees or mis-handle refunds when transfer/approval state changes across retries
- Invariant to test: archive and index views must not create spendable balance or hide finalized debits
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs
