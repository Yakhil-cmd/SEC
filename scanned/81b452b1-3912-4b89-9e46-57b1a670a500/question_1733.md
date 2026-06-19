# Q1733: ledger: transfer from canonical encoding

## Question
Can an unprivileged attacker enter through public transfer or transfer_from flow and drive `packages/icrc-ledger-client/src/lib.rs`::transfer_from with attacker-controlled ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this undercharge fees or mis-handle refunds when transfer/approval state changes across retries, violating the invariant that duplicate/replay windows must reject repeated value movement under all encodings, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-client/src/lib.rs`::transfer_from
- Entrypoint: public transfer or transfer_from flow
- Attacker controls: ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields
- Exploit idea: undercharge fees or mis-handle refunds when transfer/approval state changes across retries
- Invariant to test: duplicate/replay windows must reject repeated value movement under all encodings
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
