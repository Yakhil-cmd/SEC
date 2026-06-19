# Q745: ledger: install code cross module mismatch

## Question
Can an unprivileged attacker enter through a ledger user submits ICRC/ICP transfer, approve, transfer_from, archive, index, or Rosetta construction calls and drive `rs/ledger_suite/common/ledger_canister_core/src/spawn.rs`::install_code with attacker-controlled accounts, subaccounts, spender, amount, fee, memo, created_at_time, block index, operation order, and signatures to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests, violating the invariant that duplicate/replay windows must reject repeated value movement under all encodings, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `rs/ledger_suite/common/ledger_canister_core/src/spawn.rs`::install_code
- Entrypoint: a ledger user submits ICRC/ICP transfer, approve, transfer_from, archive, index, or Rosetta construction calls
- Attacker controls: accounts, subaccounts, spender, amount, fee, memo, created_at_time, block index, operation order, and signatures
- Exploit idea: bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests
- Invariant to test: duplicate/replay windows must reject repeated value movement under all encodings
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs
