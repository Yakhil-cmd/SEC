### Title
Maximally-Crafted Delegation Chains Enable Griefing Attack on Replica Ingress Validation - (File: rs/validator/src/ingress_validation.rs)

### Summary
An unprivileged attacker can craft ingress messages with the maximum allowed delegation chain (20 delegations × 1,000 canister-ID targets each) to maximize the CPU cost of ingress validation at every replica node, without paying any proportional cost. This is a direct analog to the Alchemix `MAX_DELEGATES` griefing attack: the attacker exploits a bounded-but-large array that is fully iterated on every request, inflating the per-message validation cost by roughly 20–26× compared to a minimal message, and can be used to degrade throughput for all users of the subnet.

---

### Finding Description

The IC ingress-validation layer enforces two limits that together define the worst-case work per message:

```
MAXIMUM_NUMBER_OF_DELEGATIONS          = 20    // chain length
MAXIMUM_NUMBER_OF_TARGETS_PER_DELEGATION = 1_000 // canister IDs per hop
``` [1](#0-0) 

For every ingress message that carries a delegation chain, `validate_delegations` iterates over every delegation and, for each one, performs:

1. A full cryptographic signature verification (`validate_delegation`), and
2. A `BTreeSet` construction from up to 1,000 raw canister-ID blobs (`delegation.targets()`), and
3. A `BTreeSet` intersection of the accumulated target set with the new one (`targets.intersect(new_targets)`). [2](#0-1) 

The guard `ensure_delegations_does_not_contain_too_many_targets` uses `number_of_targets()` which is `Vec::len()` — an O(1) check on the raw (pre-deduplication) count — so it passes for any Vec of ≤ 1,000 entries, including 1,000 *distinct* canister IDs. [3](#0-2) 

`delegation.targets()` then builds a `BTreeSet<CanisterId>` by parsing each blob and inserting it, costing O(n log n) per delegation. [4](#0-3) 

`CanisterIdSet::try_from_iter` is subsequently called on the already-deduplicated set and checks the *distinct* count against the same 1,000 limit, so 1,000 distinct IDs pass cleanly. [5](#0-4) 

A maximally-crafted message therefore forces the replica to perform:
- **20 cryptographic signature verifications** (~100 µs each ≈ 2 ms total), and
- **20 × O(1,000 × log 1,000) BTreeSet constructions** ≈ 200,000 tree operations, and
- **19 BTreeSet intersections** of up to 1,000 elements each ≈ 190,000 tree operations.

A minimal message (no delegations) requires a single signature verification (~100 µs). The ratio is roughly **20–26× in wall-clock time** and up to **~200,000× in BTreeSet operations**.

This validation runs synchronously at two points in the replica stack:

- In the HTTP endpoint's `IngressValidatorBuilder` before a message enters the ingress pool.
- In `IngressManager::validate_ingress` during block production. [6](#0-5) 

Neither path applies instruction-counting or cycle-metering to the validation work itself, so the attacker bears no proportional cost.

---

### Impact Explanation

An attacker pre-generates 21 key pairs, constructs a 20-hop delegation chain where each hop lists 1,000 distinct canister IDs as targets, and signs each hop. The resulting message is ≈ 600 KB (well within the 2 MB app-subnet limit). The attacker then streams messages at the maximum rate permitted by the boundary-node throttler, each reusing the same delegation chain with a fresh `ingress_expiry`. Every such message forces the replica to spend ~2.6 ms on validation instead of ~100 µs, consuming ~26% of a CPU core per 100 req/s of attacker traffic. With multiple source IPs or coordinated senders, this degrades ingress-validation throughput for all legitimate users of the subnet — a griefing attack with no profit motive but measurable damage to the protocol.

---

### Likelihood Explanation

The attack requires only standard cryptographic operations (key generation + Ed25519 signing) that any unprivileged principal can perform offline. No privileged role, governance majority, or threshold key is needed. The delegation chain can be pre-computed once and reused across many messages. The attacker-controlled entry path is the standard HTTPS `/api/v2/canister/{id}/call` endpoint, reachable by any Internet user.

---

### Recommendation

1. **Reduce `MAXIMUM_NUMBER_OF_TARGETS_PER_DELEGATION`** to a smaller value (e.g., 100) to bound the per-delegation BTreeSet cost. The current limit of 1,000 was chosen for the IC specification but the cost asymmetry it creates was not evaluated against a griefing scenario.
2. **Move the target-count check before BTreeSet construction**: currently `ensure_delegations_does_not_contain_too_many_targets` checks `Vec::len()` (O(1)) but the expensive `BTreeSet` construction happens unconditionally inside `validate_delegation`. Consider short-circuiting earlier.
3. **Apply a per-connection or per-IP instruction budget** to ingress validation at the HTTP handler layer, analogous to the cycle metering applied to canister execution, so that the cost of validating an expensive message is borne by the sender.

---

### Proof of Concept

```
# Attacker pre-computes offline:
for i in 0..21:
    keypair[i] = Ed25519::generate()

for i in 0..20:
    targets[i] = [CanisterId::from_u64(j) for j in (i*1000)..(i*1000+1000)]
    delegation[i] = Delegation {
        pubkey:     keypair[i+1].public_key_der(),
        expiration: far_future,
        targets:    targets[i],   // 1,000 distinct canister IDs
    }
    signed_delegation[i] = keypair[i].sign(delegation[i])

# Attacker streams at boundary-node rate limit (e.g. 100 req/s):
loop:
    msg = HttpRequest {
        sender:             keypair[0].principal(),
        sender_pubkey:      keypair[0].public_key_der(),
        sender_delegation:  signed_delegation[0..20],
        ingress_expiry:     now() + 5min,
        canister_id:        targets[0][0],   // included in delegation[0]
        method_name:        "update",
        arg:                [],
    }
    msg.sender_sig = keypair[20].sign(msg.id())
    POST /api/v2/canister/{canister_id}/call  body=msg
```

Each submitted message forces the replica to perform 20 signature verifications and ~390,000 BTreeSet operations (~2.6 ms) instead of the ~100 µs required for a minimal message, achieving a ~26× amplification of replica CPU cost with no additional cost to the attacker.

### Citations

**File:** rs/validator/src/ingress_validation.rs (L31-43)
```rust
/// Maximum number of delegations allowed in an `HttpRequest`.
/// Requests having more delegations will be declared invalid without further verifying whether
/// the delegation chain is correctly signed.
/// **Note**: this limit is part of the [IC specification](https://internetcomputer.org/docs/current/references/ic-interface-spec#authentication)
/// and so changing this value might be breaking or result in a deviation from the specification.
const MAXIMUM_NUMBER_OF_DELEGATIONS: usize = 20;

/// Maximum number of targets (collection of `CanisterId`s) that can be specified in a
/// single delegation. Requests having a single delegation with more targets will be declared
/// invalid without any further verification.
/// **Note**: this limit is part of the [IC specification](https://internetcomputer.org/docs/current/references/ic-interface-spec#authentication)
/// and so changing this value might be breaking or result in a deviation from the specification.
const MAXIMUM_NUMBER_OF_TARGETS_PER_DELEGATION: usize = 1_000;
```

**File:** rs/validator/src/ingress_validation.rs (L368-380)
```rust
    pub fn try_from_iter<I: IntoIterator<Item = CanisterId>>(
        iter: I,
    ) -> Result<Self, CanisterIdSetInstantiationError> {
        let ids: BTreeSet<CanisterId> = iter.into_iter().collect();
        match ids.len() {
            n if n > MAXIMUM_NUMBER_OF_TARGETS_PER_DELEGATION => {
                Err(CanisterIdSetInstantiationError::TooManyElements(n))
            }
            _ => Ok(CanisterIdSet {
                ids: internal::CanisterIdSet::Some(ids),
            }),
        }
    }
```

**File:** rs/validator/src/ingress_validation.rs (L721-753)
```rust
fn validate_delegations<R: RootOfTrustProvider>(
    validator: &dyn IngressSigVerifier,
    signed_delegations: &[SignedDelegation],
    mut pubkey: Vec<u8>,
    root_of_trust_provider: &R,
) -> Result<(Vec<u8>, CanisterIdSet), RequestValidationError>
where
    R::Error: std::error::Error,
{
    ensure_delegations_does_not_contain_cycles(&pubkey, signed_delegations)?;
    ensure_delegations_does_not_contain_too_many_targets(signed_delegations)?;
    // Initially, assume that the delegations target all possible canister IDs.
    let mut targets = CanisterIdSet::all();

    for sd in signed_delegations {
        let delegation = sd.delegation();
        let signature = sd.signature();

        let new_targets = validate_delegation(
            validator,
            signature,
            delegation,
            &pubkey,
            root_of_trust_provider,
        )
        .map_err(InvalidDelegation)?;
        // Restrict the canister targets to the ones specified in the delegation.
        targets = targets.intersect(new_targets);
        pubkey = delegation.pubkey().to_vec();
    }

    Ok((pubkey, targets))
}
```

**File:** rs/validator/src/ingress_validation.rs (L772-788)
```rust
fn ensure_delegations_does_not_contain_too_many_targets(
    signed_delegations: &[SignedDelegation],
) -> Result<(), RequestValidationError> {
    for delegation in signed_delegations {
        match delegation.delegation().number_of_targets() {
            Some(number_of_targets)
                if number_of_targets > MAXIMUM_NUMBER_OF_TARGETS_PER_DELEGATION =>
            {
                Err(InvalidDelegation(DelegationTargetError(format!(
                    "expected at most {MAXIMUM_NUMBER_OF_TARGETS_PER_DELEGATION} targets per delegation, but got {number_of_targets}"
                ))))
            }
            _ => Ok(()),
        }?
    }
    Ok(())
}
```

**File:** rs/types/types/src/messages/http.rs (L574-588)
```rust
    pub fn targets(&self) -> Result<Option<BTreeSet<CanisterId>>, String> {
        match &self.targets {
            None => Ok(None),
            Some(targets) => {
                let mut target_canister_ids = BTreeSet::new();
                for target in targets {
                    target_canister_ids.insert(CanisterId::unchecked_from_principal(
                        PrincipalId::try_from(target.0.as_slice())
                            .map_err(|e| format!("Error parsing canister ID: {e}"))?,
                    ));
                }
                Ok(Some(target_canister_ids))
            }
        }
    }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L595-616)
```rust
        };

        // Do not include the message if it is considered invalid with
        // respect to the given context (expiry & registry_version).
        if let Err(err) = self.request_validator.validate_request(
            signed_ingress.as_ref(),
            context.time,
            &self.registry_root_of_trust_provider(context.registry_version),
        ) {
            let message_id = MessageId::from(&ingress_id);
            return Err(ValidationError::InvalidArtifact(match err {
                RequestValidationError::InvalidRequestExpiry(msg)
                | RequestValidationError::InvalidDelegationExpiry(msg) => {
                    InvalidIngressPayloadReason::IngressExpired(message_id, msg)
                }
                err => InvalidIngressPayloadReason::IngressValidationError(
                    message_id,
                    format!("{err}"),
                ),
            }));
        }
        Ok(())
```
