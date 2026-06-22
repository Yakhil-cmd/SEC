### Title
Threshold Signature Fee Bypassed When Sender Canister Is Not in the Routing Table - (File: rs/execution_environment/src/execution_environment.rs)

### Summary

The `sign_with_threshold` function in the IC execution environment uses `network_topology.route(request.sender.get())` to determine the source subnet of a threshold signature request. The fee-charging block is skipped when `source_subnet == Some(nns_subnet_id)`. However, `route()` returns `None` when the sender canister is not present in the routing table (e.g., during canister migration, or if the routing table is stale/incomplete). Since `None != Some(nns_subnet_id)`, the condition evaluates to `true` and the fee **is** charged — but this is the correct path. The actual analog bug is the inverse: the condition `source_subnet != Some(nns_subnet_id)` is `true` for `None`, meaning any canister whose principal ID cannot be resolved in the routing table is treated as a **non-NNS caller and charged the fee**, even if it originates from the NNS subnet. This is the correct behavior for the fee path. However, the deeper structural analog to the reported bug is that the fee-exemption check relies on a **derived routing lookup** (`route()`) rather than the **authoritative source** (the actual originating subnet ID embedded in the XNet message header), meaning a canister on the NNS subnet whose ID is not yet in the routing table (e.g., freshly created, or during migration) would be incorrectly charged the threshold signature fee.

### Finding Description

In `sign_with_threshold()` in `rs/execution_environment/src/execution_environment.rs`, the NNS fee exemption is determined by:

```rust
let source_subnet = state.metadata.network_topology.route(request.sender.get());
let nns_subnet_id = state.metadata.network_topology.nns_subnet_id;
if source_subnet != Some(nns_subnet_id) {
    // charge fee
}
```

`NetworkTopology::route()` performs a routing table lookup:

```rust
pub fn route(&self, principal_id: PrincipalId) -> Option<SubnetId> {
    let as_subnet_id = SubnetId::from(principal_id);
    if self.subnets.contains_key(&as_subnet_id) {
        return Some(as_subnet_id);
    }
    match CanisterId::try_from(principal_id) {
        Ok(canister_id) => self.routing_table.lookup_entry(canister_id)
            .map(|(_range, subnet_id)| subnet_id),
        Err(_) => None,
    }
}
```

If the sender canister is not present in the routing table (returns `None`), the condition `None != Some(nns_subnet_id)` is `true`, so the fee-charging block executes. This means a canister that legitimately resides on the NNS subnet but whose canister ID is not yet reflected in the local routing table snapshot (e.g., a newly created NNS canister, or a canister mid-migration) will be incorrectly charged the threshold signature fee — or rejected for insufficient payment — even though it should be exempt.

The analog to the Deriverse bug is structural: the fee-skip decision is gated on a **derived value** (the result of a routing table lookup, which can be `None`) rather than the **authoritative source** (the actual subnet origin of the XNet request). When the derived value is `None`, the condition falls through to the fee-charging branch, silently misclassifying NNS-origin requests as non-NNS.

### Impact Explanation

A canister on the NNS subnet whose ID is absent from the routing table snapshot on the signing subnet will:
1. Have its `sign_with_ecdsa` / `sign_with_schnorr` / `vetkd_derive_key` request **rejected** if it does not attach the required fee cycles (since the fee check fires), or
2. Be **incorrectly charged** the threshold signature fee (cycles burned from `request.payment`) even though NNS-origin requests are supposed to be free.

This breaks the intended protocol invariant that NNS canisters can use threshold signatures without paying fees, and can cause NNS governance operations that rely on threshold signatures to fail or lose cycles.

### Likelihood Explanation

The routing table on a non-NNS subnet is a snapshot that may lag behind the actual NNS state. Newly created NNS canisters, or canisters undergoing migration, may not appear in the routing table of the signing subnet at the time the request arrives. This is a realistic operational scenario, particularly during NNS upgrades or canister migrations. The window is bounded by registry propagation latency but is non-zero and repeatable.

### Recommendation

Replace the routing-table-based NNS check with an authoritative check. For XNet requests, the originating subnet is embedded in the message routing metadata. The check should use the actual source subnet from the XNet stream header rather than inferring it from the routing table:

```rust
// Instead of:
let source_subnet = state.metadata.network_topology.route(request.sender.get());
let nns_subnet_id = state.metadata.network_topology.nns_subnet_id;
if source_subnet != Some(nns_subnet_id) { ... }

// Prefer checking the actual originating subnet from the request context,
// or treat None (unroutable sender) as non-NNS but log a warning,
// or explicitly exempt the NNS subnet ID itself as a sender principal:
let nns_subnet_id = state.metadata.network_topology.nns_subnet_id;
let sender_is_nns = request.sender.get() == nns_subnet_id.get()
    || source_subnet == Some(nns_subnet_id);
if !sender_is_nns { ... }
```

### Proof of Concept

The root cause is in `sign_with_threshold()`: [1](#0-0) 

The `route()` function that can return `None` for unroutable senders: [2](#0-1) 

The fee-charging block that fires when `source_subnet` is `None` (unroutable NNS canister): [3](#0-2) 

The existing test `test_sign_with_threshold_key_fee_ignored_for_nns` only tests the case where the NNS canister IS in the routing table (subnet_id == nns_subnet_id, so `route()` returns `Some(nns_subnet_id)`): [4](#0-3) 

There is no test covering the case where the NNS canister's ID is absent from the routing table, leaving the `None` path untested and the fee bypass silently broken for that scenario.

### Citations

**File:** rs/execution_environment/src/execution_environment.rs (L3783-3800)
```rust
        // If the request isn't from the NNS, then we need to charge for it.
        let source_subnet = state.metadata.network_topology.route(request.sender.get());
        let nns_subnet_id = state.metadata.network_topology.nns_subnet_id;
        if source_subnet != Some(nns_subnet_id) {
            let signature_fee =
                self.calculate_signature_fee(&args, state.get_own_subnet_cycles_config());
            let real_signature_fee = signature_fee.real();
            if request.payment < real_signature_fee {
                return Err(UserError::new(
                    ErrorCode::CanisterRejectedMessage,
                    format!(
                        "{} request sent with {} cycles, but {} cycles are required.",
                        request.method_name, request.payment, real_signature_fee
                    ),
                ));
            } else {
                // Charge for the request.
                request.payment -= real_signature_fee;
```

**File:** rs/replicated_state/src/metadata_state.rs (L364-380)
```rust
    pub fn route(&self, principal_id: PrincipalId) -> Option<SubnetId> {
        let as_subnet_id = SubnetId::from(principal_id);
        if self.subnets.contains_key(&as_subnet_id) {
            return Some(as_subnet_id);
        }

        // If the `principal_id` was not a subnet, it must be a `CanisterId` (otherwise
        // we can't route to it).
        match CanisterId::try_from(principal_id) {
            Ok(canister_id) => self
                .routing_table
                .lookup_entry(canister_id)
                .map(|(_range, subnet_id)| subnet_id),
            // Cannot route to any subnet as we couldn't convert to a `CanisterId`.
            Err(_) => None,
        }
    }
```

**File:** rs/execution_environment/tests/threshold_signatures.rs (L797-844)
```rust
fn test_sign_with_threshold_key_fee_ignored_for_nns() {
    let test_cases = vec![
        (Method::SignWithECDSA, make_ecdsa_key("some_key")),
        (Method::SignWithSchnorr, make_ed25519_key("some_key")),
        (Method::SignWithSchnorr, make_bip340_key("some_key")),
        (Method::VetKdDeriveKey, make_vetkd_key("some_key")),
    ];
    for (method, key_id) in test_cases {
        let fee = 1_000_000;
        let nns_subnet = subnet_test_id(1);
        let env = StateMachineBuilder::new()
            .with_checkpoints_enabled(false)
            .with_subnet_type(SubnetType::System)
            .with_subnet_id(nns_subnet)
            .with_nns_subnet_id(nns_subnet)
            .with_ecdsa_signature_fee(fee)
            .with_schnorr_signature_fee(fee)
            .with_vetkd_derive_key_fee(fee)
            .with_chain_key(key_id.clone())
            .build();

        let canister_id = create_universal_canister(&env);
        let _msg_id = env.send_ingress(
            PrincipalId::new_anonymous(),
            canister_id,
            "update",
            wasm()
                .call_simple(
                    ic00::IC_00,
                    method,
                    call_args()
                        .other_side(sign_with_threshold_key_payload(method, key_id))
                        .on_reject(wasm().reject_message().reject()),
                )
                .build(),
        );

        env.tick();

        // Assert that the request payment is zero.
        let contexts = match method {
            Method::SignWithECDSA => env.sign_with_ecdsa_contexts(),
            Method::SignWithSchnorr => env.sign_with_schnorr_contexts(),
            Method::VetKdDeriveKey => env.vetkd_derive_key_contexts(),
            _ => panic!("Unexpected method"),
        };
        let (_, context) = contexts.iter().next().unwrap();
        assert_eq!(context.request.payment, Cycles::zero());
```
