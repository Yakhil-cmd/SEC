### Title
Unauthenticated Canister Impersonation in `vetkd_public_key` Allows Any Canister to Derive Another Canister's VetKD Public Key - (File: rs/execution_environment/src/execution_environment.rs)

### Summary
The `vetkd_public_key` management canister API accepts an optional `canister_id` field. When a caller supplies an arbitrary `canister_id`, the execution environment uses that supplied value — without any authorization check — as the identity for key derivation. Any canister can therefore obtain the VetKD public key derived for any other canister's identity, breaking the identity-binding guarantee that the VetKD scheme depends on.

### Finding Description
The `VetKdPublicKeyArgs` struct exposes an optional `canister_id` field:

```rust
pub struct VetKdPublicKeyArgs {
    pub canister_id: Option<CanisterId>,  // caller-controlled
    pub context: Vec<u8>,
    pub key_id: VetKdKeyId,
}
``` [1](#0-0) 

In the execution environment, when this field is `Some(id)`, the supplied `id` is used directly as the derivation identity with no check that the caller owns or is authorized to act as that identity:

```rust
let canister_id = match args.canister_id {
    Some(id) => id.into(),   // ← no authorization check
    None => *msg.sender(),
};
self.get_vetkd_public_key(pubkey, canister_id, args.context)
``` [2](#0-1) 

The `get_vetkd_public_key` function then derives the public key using the caller-supplied `canister_id` as the identity anchor:

```rust
Ok(dpk
    .derive_canister_key(caller.as_slice())   // caller = attacker-supplied id
    .derive_sub_key(&context)
    .serialize())
``` [3](#0-2) 

The VetKD derivation context used during actual key share creation binds the `caller` field to `context.request.sender` (the true message sender), not to the `canister_id` argument:

```rust
let inputs = ThresholdSigInputs::VetKd(VetKdArgs {
    context: VetKdDerivationContextRef {
        caller: context.request.sender.get_ref(),   // ← always the real sender
        context: context.derivation_path.first()...
    },
    ...
});
``` [4](#0-3) 

This creates an asymmetry: `vetkd_public_key` uses the attacker-supplied `canister_id` as the derivation identity, while `vetkd_derive_key` always uses the actual message sender. The public key returned for `canister_id = victim` will therefore never match any encrypted key that the victim canister can actually decrypt — but the attacker gains the victim's public key, which is the verification key for the victim's derived secret. In VetKD, the derived public key is the commitment to the victim's secret key material. Possessing it enables an attacker to:

1. Verify whether a given plaintext corresponds to data encrypted under the victim's VetKD key.
2. Perform offline brute-force or dictionary attacks against data encrypted to the victim's key.
3. Correlate encrypted payloads across different callers by comparing public keys, breaking pseudonymity.

The `vetkd_derive_key` path does not have this flaw — the caller identity is always taken from `request.sender` and cannot be spoofed. [5](#0-4) 

### Impact Explanation
Any unprivileged canister can call `vetkd_public_key` with `canister_id = Some(<victim_canister_id>)` and receive the exact BLS12-381 G2 public key that corresponds to the victim canister's VetKD-derived secret. This public key is the verification key for all data the victim encrypts or authenticates using VetKD. An attacker who knows the victim's public key can:

- Confirm whether a ciphertext was encrypted to the victim's key (offline pairing check).
- Mount dictionary/brute-force attacks against short or low-entropy plaintexts encrypted to the victim.
- Break the anonymity/pseudonymity of the victim's VetKD usage by linking public keys to canister identities across contexts.

This is the IC analog of the NuCypher finding: re-encryption nodes (here, subnet nodes) and a colluding party learn the target's public key material without authorization, enabling attacks on the confidentiality guarantees the scheme is supposed to provide.

### Likelihood Explanation
The attack requires only a single canister call to `vetkd_public_key` with an arbitrary `canister_id`. No special privileges, governance access, or threshold corruption is needed. Any deployed canister on a subnet that holds a VetKD key can execute this immediately. The `canister_id` field is explicitly documented as optional and caller-supplied, so the attack surface is intentionally exposed with no access control.

### Recommendation
Add an authorization check in the `VetKdPublicKey` handler: if `args.canister_id` is `Some(id)` and `id != msg.sender()`, reject the request with an authorization error, or restrict the `canister_id` override to callers that are controllers of the target canister (mirroring the pattern used for `ecdsa_public_key` and `schnorr_public_key`). Alternatively, remove the `canister_id` override entirely from `vetkd_public_key` and always derive using `msg.sender()`, since `vetkd_derive_key` already enforces this.

### Proof of Concept
1. Attacker deploys canister `A` on a subnet holding VetKD key `key_1`.
2. Victim canister `V` exists on the same subnet and uses VetKD for data encryption.
3. Canister `A` calls the management canister:
   ```
   vetkd_public_key({
     canister_id: Some(V),
     context: b"",
     key_id: { curve: bls12_381_g2, name: "key_1" }
   })
   ```
4. The execution environment executes:
   ```rust
   let canister_id = match args.canister_id {
       Some(id) => id.into(),  // = V, no check
       None => *msg.sender(),
   };
   self.get_vetkd_public_key(pubkey, canister_id, args.context)
   // derives key for V's identity, returns V's public key to A
   ``` [2](#0-1) 
5. Canister `A` receives `V`'s VetKD public key. `A` can now verify pairings against any ciphertext purportedly encrypted to `V`, enabling offline attacks against `V`'s encrypted data.

### Citations

**File:** rs/types/management_canister_types/src/lib.rs (L3531-3537)
```rust
#[derive(Eq, PartialEq, Debug, CandidType, Deserialize)]
pub struct VetKdPublicKeyArgs {
    pub canister_id: Option<CanisterId>,
    #[serde(with = "serde_bytes")]
    pub context: Vec<u8>,
    pub key_id: VetKdKeyId,
}
```

**File:** rs/execution_environment/src/execution_environment.rs (L1526-1530)
```rust
                                    let canister_id = match args.canister_id {
                                        Some(id) => id.into(),
                                        None => *msg.sender(),
                                    };
                                    self.get_vetkd_public_key(pubkey, canister_id, args.context)
```

**File:** rs/execution_environment/src/execution_environment.rs (L3675-3678)
```rust
        Ok(dpk
            .derive_canister_key(caller.as_slice())
            .derive_sub_key(&context)
            .serialize())
```

**File:** rs/execution_environment/src/execution_environment.rs (L3716-3725)
```rust
        self.sign_with_threshold(
            (*request).clone(),
            ThresholdArguments::VetKd(VetKdArguments {
                key_id: args.key_id,
                input: Arc::new(args.input),
                transport_public_key: args.transport_public_key.to_vec(),
                ni_dkg_id: ni_dkg_id.clone(),
                height: Height::new(current_round.get()),
            }),
            vec![args.context],
```

**File:** rs/consensus/utils/src/chain_key.rs (L82-90)
```rust
            let inputs = ThresholdSigInputs::VetKd(VetKdArgs {
                context: VetKdDerivationContextRef {
                    caller: context.request.sender.get_ref(),
                    context: context.derivation_path.first().unwrap_or(EMPTY_VEC_REF),
                },
                ni_dkg_id: &args.ni_dkg_id,
                input: &args.input,
                transport_public_key: &args.transport_public_key,
            });
```
