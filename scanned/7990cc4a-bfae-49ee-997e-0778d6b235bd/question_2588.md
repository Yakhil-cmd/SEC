# Q2588: chain fusion: received at bounds/overflow

## Question
Can an unprivileged attacker enter through a protocol peer or adapter client supplies chain-fusion payloads that enter consensus or minter state and drive `rs/bitcoin/ckbtc/minter/src/state.rs`::received_at with attacker-controlled deposit addresses, UTXO sets, withdrawal amounts, destination addresses, ERC20 logs, block heights, and RPC payloads to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this obtain a chain-key signature for a transaction not authorized by the finalized minter state, violating the invariant that threshold signing must only authorize transactions derived from valid minter state, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/ckbtc/minter/src/state.rs`::received_at
- Entrypoint: a protocol peer or adapter client supplies chain-fusion payloads that enter consensus or minter state
- Attacker controls: deposit addresses, UTXO sets, withdrawal amounts, destination addresses, ERC20 logs, block heights, and RPC payloads
- Exploit idea: obtain a chain-key signature for a transaction not authorized by the finalized minter state
- Invariant to test: threshold signing must only authorize transactions derived from valid minter state
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
