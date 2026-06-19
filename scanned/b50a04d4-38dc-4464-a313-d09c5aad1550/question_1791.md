# Q1791: ledger: principal to subaccount authorization boundary

## Question
Can an unprivileged attacker enter through a spender races allowance, transfer_from, fee, memo, timestamp, and duplicate transaction windows and drive `packages/icrc-ledger-types/src/icrc1/account.rs`::principal_to_subaccount with attacker-controlled Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this make ledger, archive, index, or Rosetta derive different balances or transaction IDs for one operation, violating the invariant that ledger balances plus fees must be conserved across transfer, approve, transfer_from, archive, and index paths, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc1/account.rs`::principal_to_subaccount
- Entrypoint: a spender races allowance, transfer_from, fee, memo, timestamp, and duplicate transaction windows
- Attacker controls: Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing
- Exploit idea: make ledger, archive, index, or Rosetta derive different balances or transaction IDs for one operation
- Invariant to test: ledger balances plus fees must be conserved across transfer, approve, transfer_from, archive, and index paths
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs
