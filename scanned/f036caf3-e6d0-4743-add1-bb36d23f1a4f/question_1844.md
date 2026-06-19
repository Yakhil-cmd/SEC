# Q1844: ledger: Display Message Type resource accounting

## Question
Can an unprivileged attacker enter through a caller queries archive/index state while ledger blocks are being appended or synchronized and drive `packages/icrc-ledger-types/src/icrc21/requests.rs`::DisplayMessageType with attacker-controlled Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing to make fees, cycles, memory, instructions, or refunds diverge across success, reject, trap, and retry paths; specifically, can this bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests, violating the invariant that Rosetta construction/parse/submit must map one signed transaction to one ledger effect, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc21/requests.rs`::DisplayMessageType
- Entrypoint: a caller queries archive/index state while ledger blocks are being appended or synchronized
- Attacker controls: Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing
- Exploit idea: bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests
- Invariant to test: Rosetta construction/parse/submit must map one signed transaction to one ledger effect
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs
