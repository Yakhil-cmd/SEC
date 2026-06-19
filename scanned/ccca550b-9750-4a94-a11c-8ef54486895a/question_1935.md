# Q1935: consensus: insert validated certification cross module mismatch

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/artifact_pool/src/certification_pool.rs`::insert_validated_certification with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/certification_pool.rs`::insert_validated_certification
- Entrypoint: publicly reachable validation path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
