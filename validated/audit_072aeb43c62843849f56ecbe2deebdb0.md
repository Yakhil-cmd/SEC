Audit Report

## Title
`sender_info.sig` Included in `MessageId` Hash Enables Ingress Deduplication Bypass via Non-Deterministic Canister Signatures — (`rs/types/types/src/messages/http.rs`, `rs/messaging/src/scheduling/valid_set_rule.rs`)

## Summary

The `representation_independent_hash_call_or_query` function includes the `sender_info.sig` field verbatim in the hash that defines `MessageId`. Because canister signatures are non-deterministic (the embedded BLS certificate changes with every certified-state height), an attacker controlling any canister can produce two cryptographically valid but byte-distinct signatures over the same `info` blob, submit two otherwise-identical update calls, obtain two distinct `MessageId`s, and cause both to be inducted and executed — violating the at-most-once execution invariant.

## Finding Description

**Root cause — `sig` is hashed into `MessageId`**

`representation_independent_hash_call_or_query` in `rs/types/types/src/messages/http.rs` inserts all three `sender_info` sub-fields — including `sig` — into the hash map:

```rust
if let Some(RawSignedSenderInfoSlices { info, signer, sig }) = sender_info {
    map.insert("sender_info", Map(btreemap! {
        "info"   => Bytes(info),
        "signer" => Bytes(signer),
        "sig"    => Bytes(sig),   // non-deterministic canister signature bytes
    }));
}
``` [1](#0-0) 

`SignedIngressContent::id()` calls this function and passes `sender_info.sig` directly: [2](#0-1) 

**Root cause — canister signatures are non-deterministic**

A canister signature encodes a `certificate` (the subnet's BLS threshold signature over the state tree at a specific height) and a `tree` (a Merkle witness). The `certificate` changes with every new certified state. Signing the same `info` blob at two different block heights produces two byte-distinct but cryptographically valid signatures. `verify_sender_info_canister_sig` only checks cryptographic validity; it has no notion of "same logical message": [3](#0-2) 

**Deduplication is keyed solely on `MessageId`**

`ValidSetRuleImpl::is_duplicate` queries ingress history by `msg.content().id()`, which includes `sig`: [4](#0-3) 

`IngressHistorySet::contains` converts `IngressMessageId → MessageId` and checks status: [5](#0-4) 

`IngressMessageId` wraps `MessageId` directly, so the full `sig`-inclusive hash propagates through every deduplication layer: [6](#0-5) 

**Exploit flow**

1. Attacker deploys any canister (no special privilege required).
2. Attacker calls the signing canister at two different certified-state heights to obtain `sig1` and `sig2` — two valid canister signatures over the same `info` blob. `sig1 ≠ sig2` because the embedded BLS certificate differs.
3. Attacker submits two update calls with identical `sender`, `canister_id`, `method_name`, `arg`, `ingress_expiry`, and `nonce`, but `sender_info.sig = sig1` and `sender_info.sig = sig2`.
4. Both calls pass `validate_request` (each signature is individually valid).
5. `MessageId1 ≠ MessageId2` → both are `Unknown` in ingress history → both pass `is_duplicate` → both are inducted.
6. Both are executed in the same or successive rounds.

There is no feature flag gating `sender_info`; no registry setting disables it; the integration test `requests_with_valid_sender_info` confirms acceptance on production nodes: [7](#0-6) 

## Impact Explanation

Any canister reachable via ingress with side effects (ICP ledger `transfer`, cycles minting, governance `submit_proposal`, etc.) can be double-executed. For an ICP transfer, the ledger executes the transfer twice, crediting the recipient twice or debiting the sender twice. This constitutes **theft, permanent loss, or illegal minting of ICP/Cycles assets** and maps directly to the Critical impact class: *Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles or in-scope chain-key/ledger assets*.

## Likelihood Explanation

- No privileged access, governance majority, subnet-majority corruption, or key compromise is required.
- Any principal can deploy a canister and use it as a canister-signature signer.
- Obtaining two valid signatures for the same `info` blob requires only two calls to the signing canister at different block heights (certified state advances every ~2 seconds on mainnet).
- The `sender_info` feature is unconditionally enabled in production code with no feature flag.
- The attack is fully local-testable with a state-machine test using a mock canister signer that produces two distinct certificates for the same `info` blob.

## Recommendation

Exclude `sig` from the `MessageId` hash. The `sig` field is a proof-of-knowledge artifact whose byte representation is non-deterministic; it carries no semantic content beyond "this `info` was attested by `signer`". The `MessageId` should be computed from semantically meaningful fields only:

```rust
if let Some(RawSignedSenderInfoSlices { info, signer, .. }) = sender_info {
    map.insert("sender_info", Map(btreemap! {
        "info"   => Bytes(info),
        "signer" => Bytes(signer),
        // sig excluded from MessageId
    }));
}
```

The `sig` field is still validated cryptographically by `validate_sender_info` before induction; removing it from the hash does not weaken authentication. Two requests with the same `info`/`signer` but different `sig` bytes will then produce the same `MessageId`, restoring the at-most-once execution invariant. [1](#0-0) 

## Proof of Concept

```rust
// State-machine test sketch
let env = StateMachine::new();
let canister = deploy_signing_canister(&env);

let info_blob = b"user-attributes";
// Advance state between the two sign calls so certificates differ
let sig1 = canister.sign_at_height(info_blob, env.certified_height()); // cert S1
env.tick();
let sig2 = canister.sign_at_height(info_blob, env.certified_height()); // cert S2 ≠ S1
assert_ne!(sig1, sig2);

let base = UpdateCall { sender, canister_id: ledger, method: "transfer",
                        arg: transfer_args, ingress_expiry, nonce: None };
let r1 = base.with_sender_info(info_blob, signer, sig1);
let r2 = base.with_sender_info(info_blob, signer, sig2);

assert_ne!(r1.message_id(), r2.message_id()); // distinct MessageIds due to sig

env.submit_ingress(r1).unwrap(); // accepted
env.submit_ingress(r2).unwrap(); // accepted — id2 not in ingress history
env.execute_round();
env.execute_round();

// Transfer executed TWICE — at-most-once violated
assert_eq!(ledger_balance(recipient), initial + 2 * amount);
```

### Citations

**File:** rs/types/types/src/messages/http.rs (L68-78)
```rust
    if let Some(RawSignedSenderInfoSlices { info, signer, sig }) = sender_info {
        map.insert(
            "sender_info",
            Map(btreemap! {
                "info" => Bytes(info),
                "signer" => Bytes(signer),
                "sig" => Bytes(sig),
            }),
        );
    }
    hash_of_map(&map, |key, value| hash_key_val(key, value))
```

**File:** rs/types/types/src/messages/ingress_messages.rs (L113-130)
```rust
    fn id(&self) -> MessageId {
        MessageId::from(representation_independent_hash_call_or_query(
            CallOrQuery::Call,
            self.canister_id.as_ref(),
            &self.method_name,
            &self.arg,
            self.ingress_expiry,
            self.sender.get_ref().as_slice(),
            self.nonce.as_deref(),
            self.sender_info
                .as_ref()
                .map(|sender_info| RawSignedSenderInfoSlices {
                    info: &sender_info.info,
                    signer: sender_info.signer.as_ref(),
                    sig: &sender_info.sig,
                }),
        ))
    }
```

**File:** rs/validator/src/ingress_validation.rs (L529-544)
```rust
    // Construct the signable content (domain = "ic-sender-info")
    let sender_info_content = SenderInfoContent(&sender_info.info);
    let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));

    verify_canister_sig_with_fallback!(
        validator,
        &canister_sig,
        &sender_info_content,
        &public_key,
        root_of_trust_provider,
        |e| InvalidSenderInfo(format!("signature verification failed: {e}")),
        |e: <R as RootOfTrustProvider>::Error| InvalidSenderInfo(format!(
            "failed to get root of trust: {e}"
        ))
    );
    Ok(())
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L205-208)
```rust
    /// Checks whether the given message has already been inducted.
    fn is_duplicate(&self, state: &ReplicatedState, msg: &SignedIngress) -> bool {
        state.get_ingress_status(&msg.content().id()) != &IngressStatus::Unknown
    }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L697-700)
```rust
impl IngressSetQuery for IngressHistorySet {
    fn contains(&self, msg_id: &IngressMessageId) -> bool {
        (self.get_status)(&msg_id.into()) != IngressStatus::Unknown
    }
```

**File:** rs/types/types/src/artifact.rs (L133-136)
```rust
impl From<&SignedIngress> for IngressMessageId {
    fn from(signed_ingress: &SignedIngress) -> Self {
        IngressMessageId::new(signed_ingress.expiry_time(), signed_ingress.id())
    }
```

**File:** rs/tests/crypto/ingress_verification_test.rs (L1184-1217)
```rust
/// Tests that requests with valid canister-signed sender_info are accepted,
/// and that various forms of invalid sender_info are rejected.
pub fn requests_with_valid_sender_info(env: TestEnv) {
    let logger = env.logger();
    let node = env.get_first_healthy_node_snapshot();
    let agent = node.build_default_agent();
    block_on({
        async move {
            let node_url = node.get_public_url();
            debug!(logger, "Selected replica"; "url" => format!("{}", node_url));

            let canister =
                UniversalCanister::new_with_retries(&agent, node.effective_canister_id(), &logger)
                    .await;
            let test_info = TestInformation {
                url: node_url,
                canister_id: canister_id_from_principal(&canister.canister_id()),
            };

            let seed = b"sender_info_test_seed".to_vec();
            let signer = CanisterSigner::new(&canister, seed);
            let id = GenericIdentity::new_canister(signer.clone());

            // The info blob that the signing canister attests to.
            let info_bytes = b"some user attributes".to_vec();
            let sender_info_content = SenderInfoContent(&info_bytes);
            let sender_info_signed_bytes = sender_info_content.as_signed_bytes();
            let sender_info_sig = signer.sign(&sender_info_signed_bytes).await;

            let valid_sender_info = || RawSignedSenderInfo {
                info: Blob(info_bytes.clone()),
                signer: Blob(signer.canister_id().get().as_slice().to_vec()),
                sig: Blob(sender_info_sig.clone()),
            };
```
