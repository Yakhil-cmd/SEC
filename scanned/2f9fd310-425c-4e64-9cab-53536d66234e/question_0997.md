# Q997: consensus: uri prefix ordering/race

## Question
Can an unprivileged attacker enter through a Byzantine-but-below-threshold peer gossips malformed consensus artifacts and drive `rs/p2p/artifact_downloader/src/fetch_artifact/download.rs`::uri_prefix with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/p2p/artifact_downloader/src/fetch_artifact/download.rs`::uri_prefix
- Entrypoint: a Byzantine-but-below-threshold peer gossips malformed consensus artifacts
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
