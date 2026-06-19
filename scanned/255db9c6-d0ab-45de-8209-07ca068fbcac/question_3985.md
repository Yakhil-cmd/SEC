# Q3985: consensus: create sig share and assert that verification fails cross module mismatch

## Question
Can an unprivileged attacker enter through a Byzantine-but-below-threshold peer gossips malformed consensus artifacts and drive `rs/consensus/idkg/src/signer.rs`::create_sig_share_and_assert_that_verification_fails with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/signer.rs`::create_sig_share_and_assert_that_verification_fails
- Entrypoint: a Byzantine-but-below-threshold peer gossips malformed consensus artifacts
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
