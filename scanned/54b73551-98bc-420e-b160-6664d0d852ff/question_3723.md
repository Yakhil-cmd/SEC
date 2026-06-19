# Q3723: consensus: fake cert canonical encoding

## Question
Can an unprivileged attacker enter through certified-state/read_state path and drive `rs/consensus/certification/src/certifier.rs`::fake_cert with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/certification/src/certifier.rs`::fake_cert
- Entrypoint: certified-state/read_state path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
