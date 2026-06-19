# Q3945: consensus: validate transcript refs cross module mismatch

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/consensus/idkg/src/payload_verifier.rs`::validate_transcript_refs with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/payload_verifier.rs`::validate_transcript_refs
- Entrypoint: publicly reachable validation path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
