### Title
Missing Same-Key Reuse Check in iDKG Dealing Encryption Key Rotation - (File: `rs/registry/canister/src/mutations/do_update_node_directly.rs`)

### Summary
`do_update_node` in the registry canister does not verify that the submitted new iDKG dealing encryption key differs from the node's currently registered key. A node (or compromised node operator) can call `update_node_directly` with the same key bytes already in the registry, resetting the rotation timestamp without actually rotating the key, defeating the security purpose of key rotation.

### Finding Description
In `do_update_node`, the existing key is fetched from the registry at step 3 solely to extract its timestamp for a freshness check. The key bytes (`pk.key_value`) are discarded after the timestamp is read — the `pk` variable is scoped inside the `match` arm and is never retained for comparison. [1](#0-0) 

After the timing checks pass, step 5 deserializes and validates the new key via `ValidIDkgDealingEncryptionPublicKey::try_from`, which only checks that the algorithm is `MegaSecp256k1` and that the key is a valid curve point. [2](#0-1) 

There is no comparison between the new key's `key_value` bytes and the existing key's `key_value` bytes. The mutation is then applied unconditionally (subject to timing constraints). [3](#0-2) 

The post-mutation invariant check `nodes_crypto_keys_and_certs_are_unique` only enforces uniqueness *across different nodes*, not within the same node's rotation history. It would not catch a node re-registering its own existing key. [4](#0-3) 

The `ValidIDkgDealingEncryptionPublicKey` type validation confirms algorithm and curve validity but does not enforce novelty relative to the existing registry entry. [5](#0-4) 

### Impact Explanation
The iDKG dealing encryption key (`KeyPurpose::IDkgMEGaEncryption`) is used for MEGa encryption of threshold signing shares. Key rotation is the mechanism by which a node limits the exposure window of a potentially compromised key. By submitting the same key bytes already in the registry, a malicious node operator can:

1. Reset the rotation timer (`timestamp`) to the current time without changing the key material.
2. Prevent the legitimate rotation period from expiring, keeping a compromised key active indefinitely.
3. Ensure that any attacker who has already obtained the private key continues to be able to decrypt all threshold signing shares sent to that node.

This directly undermines the security guarantee of the iDKG key rotation scheme described in the orchestrator documentation.



### Likelihood Explanation
**Medium-Low.** The call to `update_node_directly` is authenticated by the node's signing key — only the node itself can submit this update. This requires either a compromised node or a malicious node operator. However, the IC threat model explicitly considers Byzantine nodes below the fault threshold, and a node operator who has already obtained the iDKG private key has a direct incentive to suppress rotation. The entry path is a standard canister update call reachable from any node on a signing subnet. [6](#0-5) 

### Recommendation
In `do_update_node`, after fetching the existing key at step 3, retain the existing key's `key_value` bytes and compare them against the new key's `key_value` bytes before applying the mutation. Reject the update with an error if the bytes are identical:

```rust
// After decoding the existing pk, also capture key_value:
let existing_key_value = pk.key_value.clone();
// ... (existing timestamp check) ...

// After step 5, before step 6:
if valid_idkg_dealing_encryption_pk.get().key_value == existing_key_value {
    return Err("the new key must differ from the currently registered key".to_string());
}
``` [7](#0-6) 

### Proof of Concept

1. Node N is on a signing subnet with `idkg_key_rotation_period_ms` configured (e.g., 2 weeks).
2. Node N's current iDKG key in the registry has a timestamp old enough to pass the freshness check (step 3).
3. Node N calls `update_node_directly` with `idkg_dealing_encryption_pk` set to the *same* serialized key bytes already in the registry.
4. `do_update_node` passes all checks: node exists (step 1), signing subnet (step 2), timestamp expired (step 3), subnet rate limit (step 4), key structurally valid (step 5).
5. The mutation is applied: the registry entry for `(node_id, IDkgMEGaEncryption)` is overwritten with the same key bytes but a new current timestamp.
6. The rotation timer is reset. The node's iDKG private key is unchanged. Any attacker holding the old private key retains full decryption capability for all future threshold signing shares sent to this node, indefinitely. [8](#0-7)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_directly.rs (L44-154)
```rust
    fn do_update_node(
        &mut self,
        now: SystemTime,
        node_id: NodeId,
        payload: UpdateNodeDirectlyPayload,
    ) -> Result<(), String> {
        // 1. Check that caller is a node with a node_id that exists
        let node_key = make_node_record_key(node_id);
        self
            .get(node_key.as_bytes(), self.latest_version())
            .ok_or_else(|| format!(
            "{LOG_PREFIX}do_update_node_directly: Node Id {node_id:} not found in the registry, aborting node update."))?;

        // 2. Disallow updating if the node is not on an signing subnet or key rotation is disabled.
        let subnet_record = self.get_subnet_from_node_id_or_panic(node_id);
        let subnet_size = subnet_record.membership.len();
        if subnet_record
            .chain_key_config
            .as_ref()
            .map(|chain_key_config| chain_key_config.key_configs.is_empty())
            .unwrap_or(true)
        {
            return Err("the node is not on a signing subnet".to_string());
        }
        // Get key rotation period (delta) from config.
        let idkg_key_rotation_period_ms = subnet_record
            .chain_key_config
            .as_ref()
            .and_then(|chain_key_config| chain_key_config.idkg_key_rotation_period_ms)
            .ok_or_else(|| "the key rotation feature is disabled".to_string())?;

        // 3. Disallow updating if the existing key is sufficiently fresh.
        let duration_since_unix_epoch = now
            .duration_since(SystemTime::UNIX_EPOCH)
            .map_err(|err| format!("couldn't get time since unix epoch: {err}"))?;

        let idkg_pk_key = make_crypto_node_key(node_id, KeyPurpose::IDkgMEGaEncryption);
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

        // 5. Deserialize and validate the pk
        let valid_idkg_dealing_encryption_pk = {
            let mut pk = PublicKey::decode(
                &payload
                    .idkg_dealing_encryption_pk
                    .as_ref()
                    .map_or(&vec![], |v| v)[..],
            )
            .map_err(|e| {
                format!("idkg_dealing_encryption_pk is not in the expected format: {e:?}")
            })?;
            // Set the key timestamp to the current time.
            pk.timestamp = Some(duration_since_unix_epoch.as_millis() as u64);
            ValidIDkgDealingEncryptionPublicKey::try_from(pk)
                .map_err(|e| format!("key validation failed: {e}"))?
        };

        // 6. Create mutation for new record
        let insert_idkg_key = update(
            idkg_pk_key.as_bytes(),
            valid_idkg_dealing_encryption_pk.get().encode_to_vec(),
        );

        let mutations = vec![insert_idkg_key];

        // 7. Check invariants before applying mutations
        self.maybe_apply_mutation_internal(mutations);

        Ok(())
    }
```

**File:** rs/registry/canister/src/invariants/crypto.rs (L180-199)
```rust
fn nodes_crypto_keys_and_certs_are_unique(
    pks: AllPublicKeys,
    certs: AllTlsCertificates,
) -> Result<(), InvariantCheckError> {
    let mut unique_pks_and_certs: HashMap<Vec<u8>, NodeId> = HashMap::new();
    let mut maybe_error: Option<Result<(), InvariantCheckError>> = None;
    for ((node_id, _purpose), pk) in pks {
        match unique_pks_and_certs.get(&pk.key_value) {
            Some(prev) => {
                let msg = format!(
                    "nodes {} and {} use the same public key {:?}",
                    prev, node_id, pk.key_value
                );
                println!("{LOG_PREFIX} {msg}");
                maybe_error =
                    Some(maybe_error.unwrap_or(Err(InvariantCheckError { msg, source: None })));
            }
            None => {
                unique_pks_and_certs.insert(pk.key_value, node_id);
            }
```

**File:** rs/crypto/node_key_validation/src/lib.rs (L358-373)
```rust
impl TryFrom<PublicKey> for ValidIDkgDealingEncryptionPublicKey {
    type Error = KeyValidationError;

    fn try_from(public_key: PublicKey) -> Result<Self, Self::Error> {
        let curve_type = match AlgorithmIdProto::try_from(public_key.algorithm).ok() {
            Some(AlgorithmIdProto::MegaSecp256k1) => Ok(EccCurveType::K256),
            alg_id => Err(invalid_idkg_dealing_enc_pubkey_error(format!(
                "unsupported algorithm: {alg_id:?}"
            ))),
        }?;
        // `verify_mega_public_key` also ensures that the public key is a valid point on the curve.
        verify_mega_public_key(curve_type, &public_key.key_value).map_err(|e| {
            invalid_idkg_dealing_enc_pubkey_error(format!("verification failed: {e:?}"))
        })?;
        Ok(Self { public_key })
    }
```

**File:** rs/registry/canister/canister/canister.rs (L1227-1248)
```rust
#[unsafe(export_name = "canister_update update_node_directly")]
fn update_node_directly() {
    // This method can be called by anyone
    println!(
        "{}call: update_node_directly from: {}",
        LOG_PREFIX,
        dfn_core::api::caller()
    );
    over(candid_one, update_node_directly_);
}

#[candid_method(update, rename = "update_node_directly")]
fn update_node_directly_(payload: UpdateNodeDirectlyPayload) {
    registry_mut()
        .do_update_node_directly(payload)
        .unwrap_or_else(|error_message| {
            trap_with(&format!(
                "{LOG_PREFIX} Update node directly failed: {error_message}"
            ))
        });
    recertify_registry();
}
```
