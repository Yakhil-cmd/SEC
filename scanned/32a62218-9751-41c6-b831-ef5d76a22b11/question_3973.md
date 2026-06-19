# Q3973: consensus: send signature shares canonical encoding

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/consensus/idkg/src/signer.rs`::send_signature_shares with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/signer.rs`::send_signature_shares
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
