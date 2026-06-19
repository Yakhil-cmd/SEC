# Q2217: chain fusion: pop front ordering/race

## Question
Can an unprivileged attacker enter through a ckBTC/ckETH/ckERC20/ckDOGE user submits deposit, withdrawal, update-balance, retrieve, or reimbursement calls and drive `rs/bitcoin/adapter/src/common.rs`::pop_front with attacker-controlled minter state transitions, retry ordering, chain-key signature requests, and external-chain transaction encodings to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this replay or reorder external-chain observations to mint ck tokens twice or skip burn-on-withdrawal accounting, violating the invariant that ck-token supply must be conserved against finalized external-chain deposits and withdrawals, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/adapter/src/common.rs`::pop_front
- Entrypoint: a ckBTC/ckETH/ckERC20/ckDOGE user submits deposit, withdrawal, update-balance, retrieve, or reimbursement calls
- Attacker controls: minter state transitions, retry ordering, chain-key signature requests, and external-chain transaction encodings
- Exploit idea: replay or reorder external-chain observations to mint ck tokens twice or skip burn-on-withdrawal accounting
- Invariant to test: ck-token supply must be conserved against finalized external-chain deposits and withdrawals
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency
