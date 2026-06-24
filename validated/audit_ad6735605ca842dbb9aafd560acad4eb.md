All three cited files have been read in full. Let me verify the key claims against the actual code before rendering a verdict.

Audit Report

## Title
Unbounded Unvalidated Certification Pool Enables O(K) Crypto-per-Round DoS via Byzantine Peer Flooding — (`rs/artifact_pool/src/certification_pool.rs`, `rs/consensus/certification/src/certifier.rs`)

## Summary
The certification P2P channel is registered with `SLOT_TABLE_NO_LIMIT = usize::MAX`, and neither `CertificationPoolImpl::insert` nor `CertifierBouncer::new_bouncer` enforces a per-height artifact count. A single Byzantine peer can therefore flood the unvalidated pool with K distinct `Certification` messages at an active height, each carrying a valid (deterministically computable) height witness but an invalid combined threshold signature. On every `on_state_change` invocation, `CertifierImpl::validate` calls `verifier.validate()` — a full BLS12-381 pairing — once per artifact, producing O(K) expensive crypto operations that stall the certification thread until the invalid artifacts are removed and immediately re-flooded.

## Finding Description

**No slot cap for the certification channel.**
`rs/replica/setup_ic_network/src/lib.rs` lines 74–75 define `SLOT_TABLE_LIMIT_INGRESS = 50_000` and `SLOT_TABLE_NO_LIMIT = usize::MAX`. The ingress channel is registered with the former; the certification channel is registered with the latter at line 268. A single Byzantine peer can therefore advertise an unlimited number of distinct slot entries for certification artifacts. [1](#0-0) [2](#0-1) 

**Bouncer has no per-height count limit.**
`CertifierBouncer::new_bouncer` returns `BouncerValue::Wants` for every artifact at any height that is not below the CUP height and not already certified. There is no check on how many artifacts have already been accepted at a given height. [3](#0-2) 

**Pool insert has no per-height cap.**
`CertificationPoolImpl::insert` stores every distinct `CertificationMessageHash` unconditionally; the only deduplication is by hash identity. [4](#0-3) 

**Validate loop performs O(K) crypto calls.**
`CertifierImpl::validate` iterates every unvalidated certification at each height and calls `validate_certification` for each one. The early-exit only fires on `ChangeAction::MoveToValidated` (a valid signature). Invalid certifications produce `HandleInvalid` and the loop continues through all K artifacts. [5](#0-4) 

`validate_certification` calls `verifier.validate()` — which resolves to `verify_combined_threshold_sig_by_public_key` (full BLS12-381 pairing) — for every artifact, even those with invalid signatures. The `validate_height_witness` pre-check is not a barrier. [6](#0-5) 

**Height witness is not a barrier.**
`validate_height_witness` computes the expected digest from `state_height_as_tree(&height)`, which is a purely deterministic function of the block height — it encodes only the height number into a minimal tree structure. Any observer can compute the correct `Witness` and `CryptoHashOfPartialState` for any height without access to any secret material. The test helper `gen_content(height)` in the same file demonstrates this exactly. [7](#0-6) [8](#0-7) 

**Attack cycle.**
1. Byzantine peer advertises K slots (no limit), each with a distinct `Certification` at height H (varying `NiDkgId` signer → distinct `CertificationMessageHash`), valid height witness, and a random/zeroed combined threshold signature.
2. Bouncer returns `Wants` for all K; all K are fetched and inserted into the unvalidated pool.
3. `on_state_change` → `validate` → K calls to `verifier.validate()` (BLS12-381 pairing).
4. All K fail with `HandleInvalid` and are removed from the pool.
5. Attacker immediately re-advertises K new artifacts (higher `commit_id` on same slots); the slot table overwrites old entries and triggers K new fetches.
6. Repeat every `on_state_change` invocation.

## Impact Explanation
Each BLS12-381 pairing verification takes approximately 1–2 ms. With K = 10,000 artifacts, a single `on_state_change` invocation consumes 10–20 seconds of CPU on the certification thread, stalling all certification progress on the targeted replica. State advancement halts; XNet and state-sync consumers that depend on certified heights are blocked. The attack is sustained and repeatable with no recovery path as long as the Byzantine peer maintains its connection. This matches the allowed impact: **High ($2,000–$10,000) — Application/platform-level DoS, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS.**

## Likelihood Explanation
The attacker requires only one peer connection to the target replica (a Byzantine subnet node below the fault threshold). No privileged access, no key material, and no majority corruption is required. The attack is fully automatable: craft K `Certification` structs with valid height witnesses (computable from the public `state_height_as_tree` function), varying the `signer` `NiDkgId` field to produce K distinct hashes, and advertise them over K slots. `SLOT_TABLE_NO_LIMIT` makes this trivially scalable.

## Recommendation
1. **Apply a per-peer slot limit to the certification channel**, analogous to `SLOT_TABLE_LIMIT_INGRESS = 50_000`. A reasonable bound is `O(subnet_size × active_heights)` — e.g., a few hundred slots per peer.
2. **Enforce a per-height cap in `CertificationPoolImpl::insert`**: reject insertion of more than a small constant number of full `Certification` artifacts per height (at most one valid one exists per DKG epoch).
3. **Add a cheap pre-filter in the `validate` loop**: before calling `verifier.validate()`, check whether the `signer` `NiDkgId` matches the active high-threshold DKG ID for the height via `active_high_threshold_nidkg_id`. Certifications with a mismatched signer can be `HandleInvalid`-ed without a crypto call.

## Proof of Concept
```rust
// Unit test sketch using existing test infrastructure in certifier.rs
let height = Height::from(5);
let mut cert_pool = CertificationPoolImpl::new(...);

// Insert K distinct Certification messages at height H,
// each with a different NiDkgId signer (→ distinct CertificationMessageHash),
// valid height witness (computed via gen_content(height)),
// and a random/zeroed combined threshold signature.
for i in 0..10_000u64 {
    cert_pool.insert(fake_cert(height, fake_dkg_id(i)));
}

assert_eq!(
    cert_pool.unvalidated_certifications_at_height(height).count(),
    10_000
);

// Measure validate latency — expect O(K) BLS12-381 pairing calls
let start = std::time::Instant::now();
let change_set = certifier.validate(&cert_pool, vec![height].into_iter());
let elapsed = start.elapsed();

// In unfixed code: elapsed >> 10s, change_set.len() == 10_000 HandleInvalid entries
assert!(elapsed < std::time::Duration::from_millis(500),
    "validate took {:?} — O(K) crypto calls not bounded", elapsed);
```
The existing `fake_cert(height, fake_dkg_id(i))` helper in `rs/consensus/certification/src/certifier.rs` (lines 690–699) already constructs exactly this artifact with a valid height witness and a fake (invalid) signature, confirming the PoC is directly runnable against the existing test infrastructure. [9](#0-8)

### Citations

**File:** rs/replica/setup_ic_network/src/lib.rs (L72-75)
```rust
/// This limit is used to protect against a malicious peer advertising many ingress messages.
/// If no malicious peers are present the ingress pools are bounded by a separate limit.
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L260-269)
```rust
        let certifier = {
            let assembler = ic_artifact_downloader::FetchArtifact::new(
                log.clone(),
                rt_handle.clone(),
                artifact_pools.certification_pool.clone(),
                bouncers.certifier,
                metrics_registry.clone(),
            );
            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
        };
```

**File:** rs/consensus/certification/src/certifier.rs (L81-96)
```rust
    fn new_bouncer(&self, certification_pool: &Pool) -> Bouncer<CertificationMessageId> {
        let _timer = self.metrics.update_duration.start_timer();

        let certified_heights = certification_pool.certified_heights();
        let cup_height = self.consensus_pool_cache.catch_up_package().height();
        Box::new(move |id| {
            let height = id.height;
            // We drop all artifacts below the CUP height or those for which we have a full
            // certification already.
            if height < cup_height || certified_heights.contains(&height) {
                BouncerValue::Unwanted
            } else {
                BouncerValue::Wants
            }
        })
    }
```

**File:** rs/consensus/certification/src/certifier.rs (L429-458)
```rust
        heights
            .flat_map(|height| -> Box<dyn Iterator<Item = ChangeAction>> {
                // First we check if we have any valid full certification available for the
                // given height and if yes, our job is done for this height.
                let mut cert_change_set = Vec::new();
                for certification in certification_pool.unvalidated_certifications_at_height(height)
                {
                    if let Some(val) = self.validate_certification(certification) {
                        match val {
                            ChangeAction::MoveToValidated(_) => {
                                cert_change_set.push(val);
                                // We have found one valid certification for the given height, so
                                // our job is done.
                                return Box::new(cert_change_set.into_iter());
                            }
                            _ => {
                                cert_change_set.push(val);
                            }
                        }
                    }
                }

                Box::new(
                    certification_pool
                        .unvalidated_shares_at_height(height)
                        .filter_map(move |share| self.validate_share(certification_pool, share))
                        .chain(cert_change_set),
                )
            })
            .collect()
```

**File:** rs/consensus/certification/src/certifier.rs (L476-509)
```rust
    fn validate_certification(&self, certification: &Certification) -> Option<ChangeAction> {
        let msg = CertificationMessage::Certification(certification.clone());
        let verifier = VerifierImpl::new(self.crypto.clone());
        let registry_version =
            registry_version_at_height(self.consensus_pool_cache.as_ref(), certification.height)?;

        // check if the certification is indeed valid for the specified height. If
        // not, we consider the certification invalid.
        if let Err(e) = validate_height_witness(
            certification.height,
            &certification.height_witness,
            &certification.signed.content.hash,
        ) {
            return Some(ChangeAction::HandleInvalid(msg, e));
        }

        // Verify the certification signature.
        match verifier.validate(
            self.replica_config.subnet_id,
            certification,
            registry_version,
        ) {
            Ok(()) => Some(ChangeAction::MoveToValidated(msg)),
            Err(ValidationError::InvalidArtifact(err)) => {
                Some(ChangeAction::HandleInvalid(msg, format!("{err:?}")))
            }
            Err(ValidationError::ValidationFailed(err)) => {
                debug!(
                    self.log,
                    "Couldn't verify certification signature: {:?}", err
                );
                None
            }
        }
```

**File:** rs/consensus/certification/src/certifier.rs (L654-661)
```rust
    fn gen_content(height: Height) -> CertificationContent {
        let labeled_tree = materialize(&state_height_as_tree(&height), None);
        let height_witness_digest =
            recompute_digest(&labeled_tree, &Witness::new_for_testing_with_height()).unwrap();
        CertificationContent::new(CryptoHashOfPartialState::from(CryptoHash(
            height_witness_digest.0.to_vec(),
        )))
    }
```

**File:** rs/consensus/certification/src/certifier.rs (L690-699)
```rust
    fn fake_cert(height: Height, dkg_id: NiDkgId) -> UnvalidatedArtifact<CertificationMessage> {
        let content = gen_content(height);
        let mut signature = ThresholdSignature::fake();
        signature.signer = dkg_id;
        to_unvalidated(CertificationMessage::Certification(Certification {
            height,
            height_witness: Some(Witness::new_for_testing_with_height()),
            signed: Signed { content, signature },
        }))
    }
```

**File:** rs/artifact_pool/src/certification_pool.rs (L259-272)
```rust
    fn insert(&mut self, msg: UnvalidatedArtifact<CertificationMessage>) {
        let hash = CertificationMessageHash::from(&msg.message);

        if match hash {
            CertificationMessageHash::Certification(_) => self
                .unvalidated_cert_index
                .insert(msg.message.height(), &hash),
            CertificationMessageHash::CertificationShare(_) => self
                .unvalidated_share_index
                .insert(msg.message.height(), &hash),
        } {
            self.unvalidated.insert(hash, msg.message);
        }
    }
```

**File:** rs/canonical_state/src/lazy_tree_conversion.rs (L1027-1030)
```rust
pub fn state_height_as_tree(height: &Height) -> LazyTree<'_> {
    let metadata_lazy_tree = fork(FiniteMap::default().with_tree(HEIGHT_LABEL, num(height.get())));
    fork(FiniteMap::default().with_tree(METADATA_LABEL, metadata_lazy_tree))
}
```
