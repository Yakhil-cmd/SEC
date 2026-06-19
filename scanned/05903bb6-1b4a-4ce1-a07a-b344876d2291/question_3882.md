# Q3882: consensus: payload metrics set without key id label replay/idempotency

## Question
Can an unprivileged attacker enter through an unprivileged ingress sender fills payload candidates that reach consensus validation and drive `rs/consensus/idkg/src/metrics.rs`::payload_metrics_set_without_key_id_label with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/metrics.rs`::payload_metrics_set_without_key_id_label
- Entrypoint: an unprivileged ingress sender fills payload candidates that reach consensus validation
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
