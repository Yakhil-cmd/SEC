### Title
Subnet-wide iDKG Key Rotation Serialization Allows Nodes Without Timestamps to Repeatedly Reset the Blocking Timer, Delaying Other Nodes' Key Rotations - (File: `rs/registry/canister/src/mutations/do_update_node_directly.rs`)

---

### Summary

The `do_update_node_directly` endpoint in the registry canister enforces a subnet-wide serialization of iDKG dealing encryption key rotations. A node with no existing timestamp (e.g., a node that joined a signing subnet before key rotation was enabled, or any new node added to the subnet) can **bypass the subnet-wide cooldown check** and immediately register its key. This resets the `last_key_update_on_subnet` timer for the entire subnet, blocking all other nodes from rotating their keys for a full gamma = `delta / subnet_size * 0.85` window. A node operator controlling multiple such nodes can chain these bypasses to extend the blocking period beyond what the protocol intends.

---

### Finding Description

`do_update_node_directly` (callable directly by any registered node) delegates to `do_update_node`, which enforces two guards:

**Guard 1 – Per-node freshness check (step 3):** The calling node's own key must be older than `idkg_key_rotation_period_ms` (delta). If the key has no timestamp at all, `previous_timestamp_set` is set to `false` and this guard is skipped entirely.

**Guard 2 – Subnet-wide serialization check (step 4):** The most recent key update across all nodes in the subnet (`last_key_update_on_subnet`, which returns the **maximum** timestamp) must be older than `gamma = delta / subnet_size * DELAY_COMPENSATION (0.85)`. This guard is **only applied when `previous_timestamp_set == true`**. [1](#0-0) 

The critical bypass:

```rust
let previous_timestamp_set = match self.get(idkg_pk_key.as_bytes(), self.latest_version()) {
    Some(record) => {
        match pk.timestamp {
            Some(...) => { /* freshness check */ true }
            None => false,   // ← no timestamp → previous_timestamp_set = false
        }
    }
    None => false,           // ← no key record → previous_timestamp_set = false
};

// 4. Disallow updating if the most recent key update on the subnet is not old enough.
//    If the node has no timestamp, skip all checks.
if previous_timestamp_set                          // ← guard skipped entirely
    && let Some(last_key_update_timestamp) = self.last_key_update_on_subnet(subnet_record)
{
    ...
    return Err("the signing subnet had a key update recently".to_string());
}
``` [2](#0-1) 

After the bypass succeeds, the key is written with `pk.timestamp = Some(duration_since_unix_epoch.as_millis() as u64)`, which immediately becomes the new `last_key_update_on_subnet` maximum, imposing a fresh gamma-length cooldown on every other node in the subnet. [3](#0-2) 

The orchestrator side (`is_time_to_rotate`) contains the same bypass: if `own_key_timestamp.is_none()`, it returns `true` unconditionally, bypassing the `is_time_to_rotate_in_subnet` gamma check entirely. [4](#0-3) 

The subnet-wide gamma is computed as:

```rust
let gamma = delta.div_f64(subnet_size as f64).mul_f64(DELAY_COMPENSATION);
``` [5](#0-4) 

And the registry canister mirrors this:

```rust
let key_rotation_period_on_subnet = (idkg_key_rotation_period_ms as f64
    / subnet_size as f64
    * DELAY_COMPENSATION) as u64;
``` [6](#0-5) 

---

### Impact Explanation

Every time a node with no timestamp calls `update_node_directly`, it:

1. Bypasses the subnet-wide cooldown check.
2. Writes a fresh timestamp as the new `last_key_update_on_subnet` maximum.
3. Imposes a gamma-length cooldown on **all other nodes** in the subnet.

For a signing subnet of size N=13 with the typical `delta = 2 weeks`, gamma ≈ **1.1 days** per bypass. A node operator who controls K nodes that each lack a timestamp (e.g., when chain-key signing is first enabled on a subnet that already has nodes, or when multiple new nodes are added in a batch) can chain K consecutive bypasses, blocking all other nodes from rotating their iDKG dealing encryption keys for up to **K × gamma** time.

Delayed key rotation degrades the forward-secrecy guarantee of the iDKG dealing encryption scheme: nodes whose keys cannot be rotated continue using older keys for longer than the configured security window, increasing the window of exposure if a key is ever compromised. This is a **liveness/security-degradation** impact on the chain-key (tECDSA/tSchnorr) signing infrastructure.

---

### Likelihood Explanation

The precondition — a node in a signing subnet with no iDKG key timestamp — arises naturally in two common operational scenarios:

1. **Chain-key signing is enabled on an existing subnet.** All existing nodes have keys without timestamps. Every node can bypass the check on its first rotation call.
2. **New nodes are added to a signing subnet.** Each newly added node has no timestamp and can bypass the check immediately upon joining.

Both scenarios occur during normal NNS-governed operations. A node operator who controls even a single node in a signing subnet can exploit this at the moment their node's timestamp is absent. The call to `update_node_directly` is a direct ingress update to the registry canister, authenticated only by the node's own signing key — no additional privilege is required beyond being a registered node in the subnet.

---

### Recommendation

Decouple "first-time key registration" from "key rotation" in the subnet-wide serialization logic. Specifically:

- When a node with no prior timestamp registers its key for the first time, **do not update `last_key_update_on_subnet`** (i.e., do not let first-time registrations reset the subnet-wide cooldown timer).
- Alternatively, track first-time registrations separately and exclude them from the `last_key_update_on_subnet` maximum computation in `last_key_update_on_subnet`.

This preserves the ability of new nodes to register immediately while preventing them from imposing a gamma-length cooldown on all other nodes. [7](#0-6) 

---

### Proof of Concept

**Setup:** Signing subnet of size N=13, `delta = 2 weeks`, `gamma ≈ 1.1 days`. Chain-key signing is just enabled; all 13 nodes have keys with no timestamp.

**Attack steps:**

1. Malicious node operator controls node A (no timestamp). Node A calls `update_node_directly` immediately.
2. Registry canister: `previous_timestamp_set = false` → subnet-wide check skipped → key registered with `timestamp = T_now`.
3. `last_key_update_on_subnet` = `T_now`. All other 12 nodes are blocked until `T_now + gamma` (~1.1 days).
4. Node operator also controls node B (no timestamp). Node B calls `update_node_directly` at `T_now + ε`.
5. Registry canister: `previous_timestamp_set = false` → subnet-wide check skipped again → key registered with `timestamp = T_now + ε`.
6. `last_key_update_on_subnet` = `T_now + ε`. All other 11 nodes are blocked until `T_now + ε + gamma`.
7. Repeat for each controlled node with no timestamp.

**Result:** With K controlled nodes, the operator blocks all remaining nodes from rotating their keys for approximately `K × gamma` time, extending the key exposure window proportionally. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_directly.rs (L19-24)
```rust
// Since nodes update their keys in turn, every potential update delay will carry over to all
// subsequent slots. At some point we might end up in a situation where many nodes race for an update,
// which is not harmful, but unnecessary. So we use a 15% time buffer compensating for a
// potential delay of the previous node. But since the key update of every node is still delayed by
// it's own expiration timestamp, they won't update too early.
const DELAY_COMPENSATION: f64 = 0.85;
```

**File:** rs/registry/canister/src/mutations/do_update_node_directly.rs (L81-123)
```rust
        let previous_timestamp_set = match self.get(idkg_pk_key.as_bytes(), self.latest_version()) {
            Some(record) => {
                let pk = PublicKey::decode(record.value.as_slice()).map_err(|e| {
                    format!("idkg_dealing_encryption_pk is not in the expected format: {e:?}")
                })?;
                // If the timestamp exists, we reject if it's recent enough, otherwise we accept the
                // update as this is a new node joining the signing subnet.
                match pk.timestamp {
                    Some(last_update_timestamp) => {
                        let sum = last_update_timestamp
                            .checked_add(idkg_key_rotation_period_ms)
                            .ok_or_else(|| {
                                "Integer overflow when adding key rotation period.".to_string()
                            })?;
                        if Duration::from_millis(sum) > duration_since_unix_epoch {
                            return Err("the key of this node is sufficiently fresh".to_string());
                        }
                        true
                    }
                    None => false,
                }
            }
            None => false,
        };

        // 4. Disallow updating if the most recent key update on the subnet is not old enough.
        //    If the node has no timestamp, skip all checks.
        if previous_timestamp_set
            && let Some(last_key_update_timestamp) = self.last_key_update_on_subnet(subnet_record)
        {
            // The node is on a signing subnet, and has a timestamp
            let key_rotation_period_on_subnet = (idkg_key_rotation_period_ms as f64
                / subnet_size as f64
                * DELAY_COMPENSATION) as u64;
            let sum = last_key_update_timestamp
                .checked_add(key_rotation_period_on_subnet)
                .ok_or_else(|| {
                    "Integer overflow when adding key rotation period on subnet.".to_string()
                })?;
            if Duration::from_millis(sum) > duration_since_unix_epoch {
                return Err("the signing subnet had a key update recently".to_string());
            }
        }
```

**File:** rs/registry/canister/src/mutations/do_update_node_directly.rs (L136-138)
```rust
            // Set the key timestamp to the current time.
            pk.timestamp = Some(duration_since_unix_epoch.as_millis() as u64);
            ValidIDkgDealingEncryptionPublicKey::try_from(pk)
```

**File:** rs/registry/canister/src/mutations/do_update_node_directly.rs (L166-184)
```rust
    // Get the latest idkg encryption key timestamp of all nodes in the given subnet record
    fn last_key_update_on_subnet(&self, subnet_record: SubnetRecord) -> Option<u64> {
        subnet_record
            .membership
            .into_iter()
            .filter_map(|node_id| {
                let idkg_pk_key = make_crypto_node_key(
                    NodeId::from(PrincipalId::try_from(node_id.as_slice()).unwrap_or_default()),
                    KeyPurpose::IDkgMEGaEncryption,
                );
                self.get(idkg_pk_key.as_bytes(), self.latest_version())
            })
            .filter_map(|value| {
                PublicKey::decode(value.value.as_slice())
                    .ok()
                    .and_then(|key| key.timestamp)
            })
            .max()
    }
```

**File:** rs/orchestrator/src/registration.rs (L485-488)
```rust
        // A node can register its key if there is no previous timestamp set, regardless of Gamma
        if own_key_timestamp.is_none() {
            return true;
        }
```

**File:** rs/orchestrator/src/registration.rs (L769-786)
```rust
/// Given Δ (= key rotation period of a single node), calculates Ɣ = Δ/subnet_size * delay_compensation
/// (= key rotation period of the subnet as a whole). Then determines if at least Ɣ time has passed
/// since all of the given timestamps. Iff so, return true to indicate that the subnet is ready to accept
/// a new key rotation.
pub(crate) fn is_time_to_rotate_in_subnet(
    delta: Duration,
    subnet_size: usize,
    timestamps: Vec<SystemTime>,
) -> bool {
    // gamma determines the frequency at which the registry accepts key updates from the subnet as a whole
    let gamma = delta
        .div_f64(subnet_size as f64)
        .mul_f64(DELAY_COMPENSATION);
    let now = SystemTime::now();
    timestamps
        .iter()
        .all(|ts| now.duration_since(*ts).is_ok_and(|d| d >= gamma))
}
```
