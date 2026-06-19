# Q2161: chain fusion: remove from active authorization boundary

## Question
Can an unprivileged attacker enter through a ckBTC/ckETH/ckERC20/ckDOGE user submits deposit, withdrawal, update-balance, retrieve, or reimbursement calls and drive `rs/bitcoin/adapter/src/addressbook.rs`::remove_from_active with attacker-controlled deposit addresses, UTXO sets, withdrawal amounts, destination addresses, ERC20 logs, block heights, and RPC payloads to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this cause minter and ledger state to diverge after a failed withdrawal, reimbursement, or callback retry, violating the invariant that ck-token supply must be conserved against finalized external-chain deposits and withdrawals, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/adapter/src/addressbook.rs`::remove_from_active
- Entrypoint: a ckBTC/ckETH/ckERC20/ckDOGE user submits deposit, withdrawal, update-balance, retrieve, or reimbursement calls
- Attacker controls: deposit addresses, UTXO sets, withdrawal amounts, destination addresses, ERC20 logs, block heights, and RPC payloads
- Exploit idea: cause minter and ledger state to diverge after a failed withdrawal, reimbursement, or callback retry
- Invariant to test: ck-token supply must be conserved against finalized external-chain deposits and withdrawals
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency
