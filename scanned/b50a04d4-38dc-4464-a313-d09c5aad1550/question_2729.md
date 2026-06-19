# Q2729: chain fusion: Bitcoin Adapter Clients certification/witness

## Question
Can an unprivileged attacker enter through a ckBTC/ckETH/ckERC20/ckDOGE user submits deposit, withdrawal, update-balance, retrieve, or reimbursement calls and drive `rs/bitcoin/client/src/lib.rs`::BitcoinAdapterClients with attacker-controlled memos, reimbursement IDs, transaction status, fee estimates, finality depth, adapter responses, and ledger callbacks to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this replay or reorder external-chain observations to mint ck tokens twice or skip burn-on-withdrawal accounting, violating the invariant that ck-token supply must be conserved against finalized external-chain deposits and withdrawals, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/client/src/lib.rs`::BitcoinAdapterClients
- Entrypoint: a ckBTC/ckETH/ckERC20/ckDOGE user submits deposit, withdrawal, update-balance, retrieve, or reimbursement calls
- Attacker controls: memos, reimbursement IDs, transaction status, fee estimates, finality depth, adapter responses, and ledger callbacks
- Exploit idea: replay or reorder external-chain observations to mint ck tokens twice or skip burn-on-withdrawal accounting
- Invariant to test: ck-token supply must be conserved against finalized external-chain deposits and withdrawals
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency
