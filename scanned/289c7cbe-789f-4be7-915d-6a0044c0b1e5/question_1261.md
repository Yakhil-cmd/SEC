# Q1261: ledger: tokens to amount authorization boundary

## Question
Can an unprivileged attacker enter through a ledger user submits ICRC/ICP transfer, approve, transfer_from, archive, index, or Rosetta construction calls and drive `rs/rosetta-api/icp/src/models/amount.rs`::tokens_to_amount with attacker-controlled Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this confuse account/subaccount encoding so authorization or accounting applies to the wrong account, violating the invariant that duplicate/replay windows must reject repeated value movement under all encodings, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `rs/rosetta-api/icp/src/models/amount.rs`::tokens_to_amount
- Entrypoint: a ledger user submits ICRC/ICP transfer, approve, transfer_from, archive, index, or Rosetta construction calls
- Attacker controls: Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing
- Exploit idea: confuse account/subaccount encoding so authorization or accounting applies to the wrong account
- Invariant to test: duplicate/replay windows must reject repeated value movement under all encodings
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs
