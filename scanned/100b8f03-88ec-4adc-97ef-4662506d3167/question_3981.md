# Q3981: consensus: signer has issued share authorization boundary

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/consensus/idkg/src/signer.rs`::signer_has_issued_share with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/signer.rs`::signer_has_issued_share
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
