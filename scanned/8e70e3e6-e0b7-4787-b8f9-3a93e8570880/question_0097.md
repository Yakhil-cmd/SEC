# Q97: chain fusion: from transaction ordering/race

## Question
Can an unprivileged attacker enter through a ckBTC/ckETH/ckERC20/ckDOGE user submits deposit, withdrawal, update-balance, retrieve, or reimbursement calls and drive `rs/bitcoin/checker/src/state.rs`::from_transaction with attacker-controlled memos, reimbursement IDs, transaction status, fee estimates, finality depth, adapter responses, and ledger callbacks to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this obtain a chain-key signature for a transaction not authorized by the finalized minter state, violating the invariant that ck-token supply must be conserved against finalized external-chain deposits and withdrawals, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/checker/src/state.rs`::from_transaction
- Entrypoint: a ckBTC/ckETH/ckERC20/ckDOGE user submits deposit, withdrawal, update-balance, retrieve, or reimbursement calls
- Attacker controls: memos, reimbursement IDs, transaction status, fee estimates, finality depth, adapter responses, and ledger callbacks
- Exploit idea: obtain a chain-key signature for a transaction not authorized by the finalized minter state
- Invariant to test: ck-token supply must be conserved against finalized external-chain deposits and withdrawals
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency
