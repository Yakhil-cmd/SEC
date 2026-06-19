# Q1004: consensus: strip resource accounting

## Question
Can an unprivileged attacker enter through a canister HTTP participant supplies divergent responses that enter consensus payload building and drive `rs/p2p/artifact_downloader/src/fetch_stripped_artifact/stripper.rs`::strip with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to make fees, cycles, memory, instructions, or refunds diverge across success, reject, trap, and retry paths; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/p2p/artifact_downloader/src/fetch_stripped_artifact/stripper.rs`::strip
- Entrypoint: a canister HTTP participant supplies divergent responses that enter consensus payload building
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
