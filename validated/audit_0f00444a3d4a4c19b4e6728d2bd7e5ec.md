Audit Report

## Title
`SenderInfoContent` Omits Request-Context Binding, Enabling Cross-Canister Attestation Replay — (`rs/types/types/src/messages/http.rs`, `rs/validator/src/ingress_validation.rs`)

## Summary

`SenderInfoContent`, the signable type for `sender_info` canister-signature verification, signs only the raw `info` bytes with no binding to `canister_id`, `sender`, or `ingress_expiry`. Because `validate_sender_info` / `verify_sender_info_canister_sig` perform no request-context checks beyond matching the signer canister ID, a valid `sender_info.sig` obtained for one target canister is cryptographically valid for any other canister. Any canister that calls `msg_caller_info_data()` and makes access-control decisions on the returned blob is affected.

## Finding Description

`SenderInfoContent` is defined as a newtype over `&[u8]` and its `write_signed_bytes_without_domain_separator` writes only the raw info bytes:

```rust
// rs/types/types/src/messages/http.rs L344-347
impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);   // only info bytes
    }
}
```

The full domain-separated signed bytes are therefore `\x0Eic-sender-info` + `info_bytes` — with no `canister_id`, `sender`, or `ingress_expiry`.

`verify_sender_info_canister_sig` (L494–544) checks only that:
1. `sender_pubkey` is a valid canister-sig public key.
2. The canister ID encoded in `sender_pubkey` matches `sender_info.signer`.
3. The canister signature over `SenderInfoContent(&sender_info.info)` verifies.

No request-context field (`canister_id`, `sender`) is checked, and no certificate-timestamp check is performed. The `validate_sender_info` entry point (L459–488) passes no current time into `verify_sender_info_canister_sig` and performs no expiry check.

The envelope-level `sender_sig` is bound to the full request (including the `sender_info` blob) via the `MessageId` representation-independent hash (L112–129 of `ingress_messages.rs`). However, the inner `sender_info.sig` canister signature is not bound to any request context, so an attacker can embed the same `sender_info.sig` in a freshly signed request to a different canister.

The `msg_caller_info_data` system API is confirmed present in `rs/embedders/src/wasmtime_embedder/system_api.rs`, meaning canisters can read and act on the `info` blob at runtime.

## Impact Explanation

An attacker who has obtained a valid `sender_info.sig` for `info_bytes = b"role:admin"` targeting canister X can construct a new, validly signed ingress message targeting canister Y, embedding the same `sender_info.sig`. `validate_sender_info` passes because `SenderInfoContent(b"role:admin")` verifies against the reused certificate regardless of target canister. Canister Y's `msg_caller_info_data()` returns `b"role:admin"` and may grant elevated access. This constitutes unauthorized access to canister-controlled resources (governance, financial, identity) and matches the **High ($2,000–$10,000)** impact class: "Unauthorized access to neurons, governance assets, wallets, identities, ledgers, or canister-controlled funds where exploitation requires meaningful per-target work or other constraints."

## Likelihood Explanation

The attacker requires only a single prior valid `sender_info.sig` (obtainable from a prior legitimate request or by calling the signing canister directly). No special privileges, subnet-majority corruption, or social engineering are needed. The attack is repeatable and targets any canister that uses `msg_caller_info_data()` for access control. The `sender_info` feature is confirmed active in production code with integration tests.

## Recommendation

1. **Bind `SenderInfoContent` to the request context.** Extend the struct to carry `canister_id` and `sender`, and include them in `write_signed_bytes_without_domain_separator` before the `info` bytes, so the attestation is scoped to a specific target canister and principal.

2. **Enforce a certificate-age deadline.** In `validate_sender_info`, extract the `time` field from the canister-signature certificate and reject it if it is older than `MAX_INGRESS_TTL` relative to the current replica time.

## Proof of Concept

```
1. Legitimately obtain sender_info.sig for info_bytes = b"role:admin" from signing
   canister II, targeting canister X (e.g., from a prior on-chain request).

2. Construct a new HttpCanisterUpdate targeting canister Y:
   {
     canister_id: canister_Y,
     method_name: "privileged_action",
     sender: user_A,
     ingress_expiry: <fresh>,
     sender_info: Some({ info: b"role:admin", signer: II_id, sig: <reused> }),
   }

3. Sign the new MessageId with user_A's key (envelope sig is fresh and valid).

4. Submit to replica. validate_sender_info passes:
   - sender_pubkey encodes (II_id, seed_A) ✓
   - pubkey_canister_id == sender_info.signer ✓
   - SenderInfoContent(b"role:admin") verifies against reused certificate ✓
   - No canister_id or expiry check ✓

5. Canister Y's msg_caller_info_data() returns b"role:admin"; access granted.

Reproducible as a deterministic integration test using PocketIC:
- Deploy two canisters X and Y that gate a method on msg_caller_info_data().
- Obtain sender_info.sig for canister X via a legitimate call.
- Replay the same sig in a request to canister Y and assert the method succeeds.
```