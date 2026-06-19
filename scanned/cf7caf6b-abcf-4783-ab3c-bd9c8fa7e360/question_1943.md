# Q1943: consensus: certification shares canonical encoding

## Question
Can an unprivileged attacker enter through certified-state/read_state path and drive `rs/artifact_pool/src/certification_pool.rs`::certification_shares with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/certification_pool.rs`::certification_shares
- Entrypoint: certified-state/read_state path
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
