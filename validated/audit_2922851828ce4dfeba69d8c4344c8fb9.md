### Title
Unbounded Unvalidated Certification Pool Enables O(K) Crypto-per-Round DoS via Byzantine Peer Flooding — (`rs/artifact_pool/src/certification_pool.rs`, `rs/consensus/certification/src/certifier.rs`)

---

### Summary

A Byzantine peer can flood the unvalidated section of `CertificationPoolImpl` with an unbounded number of distinct `Certification` messages at the same height. Because the certification P2P channel is registered with `SLOT_TABLE_NO_LIMIT` and neither the pool nor the bouncer enforces a per-height artifact cap, the certifier's `validate` loop calls `verify_combined_threshold_sig_by_public_key` (BLS12-381 pairing) once per artifact per `on_state_change` invocation. The attacker can sustain this by continuously re-advertising new artifacts after each round removes the invalid ones.

---

### Finding Description

**Entrypoint — no slot cap for certification:**

The certification P2P channel is created with `SLOT_TABLE_NO_LIMIT = usize::MAX`: [1](#0-0) [2](#0-1) 

Ingress uses `SLOT_TABLE_LIMIT_INGRESS = 50_000` as a per-peer cap, but certification has no equivalent guard. A single Byzantine peer can therefore advertise an unlimited number of distinct slot entries.

**Bouncer accepts all artifacts at uncertified heights:**

`CertifierBouncer::new_bouncer` returns `BouncerValue::Wants` for every artifact at any height that lacks a validated certification, with no per-height count limit: [3](#0-2) 

**Pool insert has no per-height cap:**

`CertificationPoolImpl::insert` stores every distinct `CertificationMessageHash` unconditionally: [4](#0-3) 

**Validate loop iterates all K artifacts with O(K) crypto calls:**

`CertifierImpl::validate` iterates every unvalidated certification at each height and calls `validate_certification` for each one. The early-exit only fires on `MoveToValidated` (a valid signature). Invalid certifications produce `HandleInvalid` and the loop continues: [5](#0-4) 

`validate_certification` calls `verifier.validate()` → `verify_combined_threshold_sig_by_public_key` (full BLS12-381 pairing) for every artifact, even those with invalid signatures: [6](#0-5) 

**Attack cycle:**

1. Attacker peer advertises K slots (no limit), each with a distinct `Certification` at height H (varying `NiDkgId` signer field → distinct `CertificationMessageHash`).
2. Bouncer returns `Wants` for all K; all K are fetched and inserted into the unvalidated pool.
3. `on_state_change` → `validate` → K calls to `verify_combined_threshold_sig_by_public_key`.
4. All K fail with `HandleInvalid` and are removed from the pool.
5. Attacker immediately re-advertises K new artifacts (higher `commit_id` on same slots); the slot table overwrites the old entries and triggers K new fetches.
6. Repeat every `on_state_change` invocation.

The `validate_height_witness` pre-check is not a barrier: the height witness is a deterministic Merkle proof that any observer can compute for any height. [7](#0-6) 

---

### Impact Explanation

Each BLS12-381 pairing verification takes ~1–2 ms. With K = 10,000 artifacts, a single `on_state_change` invocation consumes 10–20 seconds of CPU on the certification thread. This stalls certification progress on the targeted replica: state advancement halts, XNet and state-sync consumers that depend on certified heights are blocked. The impact is constrained to the targeted replica (not the whole subnet), but it is a sustained, repeatable denial of the certification component.

---

### Likelihood Explanation

The attacker needs only one peer connection below the fault threshold. No privileged access, no key material, and no majority corruption is required. The attack is fully automatable: craft K `Certification` structs with valid height witnesses but invalid (random) combined threshold signatures, varying the `signer` `NiDkgId` field to produce K distinct hashes, and advertise them over K slots. The `SLOT_TABLE_NO_LIMIT` constant makes this trivially scalable.

---

### Recommendation

1. **Apply a per-peer slot limit to the certification channel**, analogous to `SLOT_TABLE_LIMIT_INGRESS`. A reasonable bound is `O(subnet_size * active_heights)` — e.g., a few hundred slots per peer.

2. **Enforce a per-height cap in `CertificationPoolImpl::insert`**: reject insertion of more than one or a small constant number of full `Certification` artifacts per height (there is at most one valid one per DKG epoch).

3. **Add a cheap pre-filter in the `validate` loop**: before calling `verify_combined_threshold_sig_by_public_key`, check whether the `signer` NiDkgId matches the active high-threshold DKG ID for the height. Certifications with a mismatched signer can be `HandleInvalid`-ed without a crypto call.

---

### Proof of Concept

```rust
// State-machine test sketch
let mut cert_pool = CertificationPoolImpl::new(...);
let height = Height::from(5);

// Insert K distinct Certification messages at height H,
// each with a different NiDkgId signer (→ distinct CertificationMessageHash)
// and a random (invalid) combined threshold signature.
for i in 0..10_000u64 {
    let mut cert = fake_cert_default(height).message;
    if let CertificationMessage::Certification(ref mut c) = cert {
        c.signed.signature.signer = fake_dkg_id(i);
        // leave signature bytes random / zeroed → will fail crypto
    }
    cert_pool.insert(to_unvalidated(cert));
}

assert_eq!(
    cert_pool.unvalidated_certifications_at_height(height).count(),
    10_000
);

// Measure on_state_change latency — expect O(K) crypto calls
let start = std::time::Instant::now();
let change_set = certifier.validate(&cert_pool, vec![height].into_iter());
let elapsed = start.elapsed();

// Assert: either elapsed < bounded_threshold (cap enforced)
// or change_set contains exactly 1 entry (early exit after first valid cert)
// In the unfixed code, elapsed >> bounded_threshold and change_set.len() == 10_000
assert!(elapsed < std::time::Duration::from_millis(100),
    "validate took {:?} — O(K) crypto calls not bounded", elapsed);
```

### Citations

**File:** rs/replica/setup_ic_network/src/lib.rs (L72-75)
```rust
/// This limit is used to protect against a malicious peer advertising many ingress messages.
/// If no malicious peers are present the ingress pools are bounded by a separate limit.
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L260-268)
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
```

**File:** rs/consensus/certification/src/certifier.rs (L81-95)
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
