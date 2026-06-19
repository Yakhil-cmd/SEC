# Q3816: consensus: validate dealings payload when wrong dkg id fails test rollback edge case

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/consensus/dkg/src/payload_validator.rs`::validate_dealings_payload_when_wrong_dkg_id_fails_test with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/dkg/src/payload_validator.rs`::validate_dealings_payload_when_wrong_dkg_id_fails_test
- Entrypoint: publicly reachable validation path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
