# Q3961: consensus: crypto verify dealing authorization boundary

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `rs/consensus/idkg/src/pre_signer.rs`::crypto_verify_dealing with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/pre_signer.rs`::crypto_verify_dealing
- Entrypoint: publicly reachable verification path
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
