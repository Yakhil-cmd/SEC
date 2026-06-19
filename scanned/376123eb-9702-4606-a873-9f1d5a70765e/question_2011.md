# Q2011: consensus: clear authorization boundary

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer replays notarization/finalization/CUP artifacts and drive `rs/artifact_pool/src/height_index.rs`::clear with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/height_index.rs`::clear
- Entrypoint: a below-threshold protocol peer replays notarization/finalization/CUP artifacts
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
