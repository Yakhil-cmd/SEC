# Q1805: ledger: Transfer Error cross module mismatch

## Question
Can an unprivileged attacker enter through public transfer or transfer_from flow and drive `packages/icrc-ledger-types/src/icrc1/transfer.rs`::TransferError with attacker-controlled ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests, violating the invariant that duplicate/replay windows must reject repeated value movement under all encodings, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc1/transfer.rs`::TransferError
- Entrypoint: public transfer or transfer_from flow
- Attacker controls: ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields
- Exploit idea: bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests
- Invariant to test: duplicate/replay windows must reject repeated value movement under all encodings
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs
