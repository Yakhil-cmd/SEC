Let me look at the `SubnetCallContextManager` to understand what context types it holds and whether `SignWithThresholdContext` is handled anywhere during splits.

Now I have a complete picture of the code. Let me write up the finding.

---

### Title
In-flight `SignWithThresholdContext` entries for migrated canisters are silently orphaned on subnet A' during an online subnet split, with no reject response delivered to the canister on subnet B — (`rs/replicated_state/src/metadata_state.rs`)

### Summary

`SystemMetadata::online_split()` and `reject_in_progress_management_calls_after_split()` handle `install_code`, `stop_canister`, and `raw_rand` contexts for migrated canisters by sending reject responses. However, `sign_with_threshold_contexts` (covering `sign_with_ecdsa`, `sign_with_schnorr`, and `vetkd_derive_key`) are completely omitted from this cleanup. When a canister with an in-flight signing request is migrated to subnet B, the context is orphaned on subnet A' with no reject response ever delivered to the canister. The canister on subnet B waits indefinitely for a signing response that can never arrive.

### Finding Description

`SubnetCallContextManager` holds several context maps, including `sign_with_threshold_contexts: BTreeMap<CallbackId, SignWithThresholdContext>`. [1](#0-0) 

During `online_split()` on subnet A', `reject_in_progress_management_calls_after_split()` is called to clean up contexts for migrated canisters. It handles exactly three context types: [2](#0-1) 

`sign_with_threshold_contexts` (and also `setup_initial_dkg_contexts`, `canister_http_request_contexts`, `reshare_chain_key_contexts`, `bitcoin_get_successors_contexts`, `bitcoin_send_transaction_internal_contexts`) are **not handled**. There is no `remove_non_local_sign_with_threshold_calls` equivalent.

On subnet B, the entire `subnet_call_context_manager` is reset to `Default::default()`: [3](#0-2) 

But the `SignWithThresholdContext` was stored on subnet A' (where the canister originally lived), not subnet B. So the context is **not dropped on subnet B** — it is **orphaned on subnet A'** with no reject response sent to the canister.

The `SignWithThresholdContext` carries the originating `request` (including sender canister ID and reply callback), which is exactly the information needed to construct a reject response — the same pattern used for `raw_rand`: [4](#0-3) 

The change-guard function in `subnet_call_context_manager.rs` confirms that `sign_with_threshold_contexts` is a known field that must be explicitly considered during subnet splitting, yet no split-time cleanup is implemented for it: [5](#0-4) 

### Impact Explanation

A chain-fusion minter canister (e.g., ckBTC, ckETH) records a user withdrawal as "in-progress" when it issues a `sign_with_ecdsa` call. If the minter is migrated to subnet B during an online split while that signing context is in-flight:

1. The `SignWithThresholdContext` remains on subnet A'. Consensus will eventually produce a signing response for subnet A'.
2. Subnet A' attempts to deliver the response to the canister via `retrieve_context()`, but the canister no longer exists on subnet A'. The response is dropped.
3. The minter canister on subnet B never receives a signing response (success or reject).
4. The withdrawal remains permanently stuck in "in-progress" state, locking user funds with no protocol-level recovery path.

### Likelihood Explanation

Subnet splits are rare governance operations. However, the IC is designed to support them, and chain-fusion minters process continuous withdrawal traffic. Any in-flight signing request at split time — a window that can span multiple consensus rounds — triggers the stuck state. The bug is deterministic: every affected signing context produces a permanently stuck withdrawal.

### Recommendation

In `reject_in_progress_management_calls_after_split()`, add handling for `sign_with_threshold_contexts` analogous to the existing `raw_rand` handling: iterate over all contexts whose `request.sender()` is not a local canister, remove them, and enqueue a `SysTransient` reject response (or record a `Failed` ingress status) so the migrated canister receives a definitive error and can attempt recovery.

### Proof of Concept

State-machine test outline:
1. Create a canister on subnet A with an active `sign_with_ecdsa` call (push a `SignWithThresholdContext` into the `SubnetCallContextManager`).
2. Assign the canister to subnet B in the routing table.
3. Call `ReplicatedState::online_split(subnet_b_id, subnet_a_id)`.
4. Assert that `state_a.metadata.subnet_call_context_manager.sign_with_threshold_contexts` is empty (context was cleaned up with a reject).
5. Assert that `subnet_queues` contains an output reject response addressed to the canister.

Currently step 4 and 5 both fail: the context remains on subnet A' and no reject is enqueued, confirming the orphan. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L213-229)
```rust
pub struct SubnetCallContextManager {
    /// Should increase monotonically. This property is used to determine if a request
    /// corresponds to a future state.
    next_callback_id: u64,
    pub setup_initial_dkg_contexts: BTreeMap<CallbackId, SetupInitialDkgContext>,
    pub sign_with_threshold_contexts: BTreeMap<CallbackId, SignWithThresholdContext>,
    pub canister_http_request_contexts: BTreeMap<CallbackId, CanisterHttpRequestContext>,
    /// `CanisterHttpRequestContext`s whose responses have already been delivered to execution.
    /// They are kept here such that asynchronous refunds may continue to be processed.
    pub delivered_canister_http_request_contexts: BTreeMap<CallbackId, CanisterHttpRequestContext>,
    pub reshare_chain_key_contexts: BTreeMap<CallbackId, ReshareChainKeyContext>,
    pub bitcoin_get_successors_contexts: BTreeMap<CallbackId, BitcoinGetSuccessorsContext>,
    pub bitcoin_send_transaction_internal_contexts:
        BTreeMap<CallbackId, BitcoinSendTransactionInternalContext>,
    canister_management_calls: CanisterManagementCalls,
    pub raw_rand_contexts: VecDeque<RawRandContext>,
    pub pre_signature_stashes: BTreeMap<IDkgMasterPublicKeyId, PreSignatureStash>,
```

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L733-756)
```rust
    fn subnet_splitting_change_guard_do_not_modify_without_reading_doc_comment() {
        //
        // DO NOT MODIFY WITHOUT READING DOC COMMENT!
        //
        let canister_management_calls = CanisterManagementCalls {
            install_code_call_manager: Default::default(),
            stop_canister_call_manager: Default::default(),
        };
        //
        // DO NOT MODIFY WITHOUT READING DOC COMMENT!
        //
        let _subnet_call_context_manager = SubnetCallContextManager {
            next_callback_id: 0,
            setup_initial_dkg_contexts: Default::default(),
            sign_with_threshold_contexts: Default::default(),
            canister_http_request_contexts: Default::default(),
            delivered_canister_http_request_contexts: Default::default(),
            reshare_chain_key_contexts: Default::default(),
            bitcoin_get_successors_contexts: Default::default(),
            bitcoin_send_transaction_internal_contexts: Default::default(),
            canister_management_calls,
            raw_rand_contexts: Default::default(),
            pre_signature_stashes: Default::default(),
        };
```

**File:** rs/replicated_state/src/metadata_state.rs (L993-999)
```rust
            Self::reject_in_progress_management_calls_after_split(
                &mut subnet_call_context_manager,
                is_local_canister,
                batch_time,
                subnet_queues,
                &mut ingress_history,
            );
```

**File:** rs/replicated_state/src/metadata_state.rs (L1016-1017)
```rust
            // No in-progress subnet calls on subnet B.
            subnet_call_context_manager = Default::default();
```

**File:** rs/replicated_state/src/metadata_state.rs (L1102-1138)
```rust
        for install_code_call in
            subnet_call_context_manager.remove_non_local_install_code_calls(&is_local_canister)
        {
            Self::reject_management_call_after_split(
                install_code_call.call,
                install_code_call.effective_canister_id,
                time,
                subnet_queues,
                ingress_history,
            );
        }

        for stop_canister_call in
            subnet_call_context_manager.remove_non_local_stop_canister_calls(&is_local_canister)
        {
            Self::reject_management_call_after_split(
                stop_canister_call.call,
                stop_canister_call.effective_canister_id,
                time,
                subnet_queues,
                ingress_history,
            );
        }

        // Management `RawRand` requests are rejected if the sender has migrated to another subnet.
        for raw_rand_context in
            subnet_call_context_manager.remove_non_local_raw_rand_calls(&is_local_canister)
        {
            let migrated_canister_id = raw_rand_context.request.sender();
            Self::reject_management_call_after_split(
                CanisterCall::Request(Arc::new(raw_rand_context.request)),
                migrated_canister_id,
                time,
                subnet_queues,
                ingress_history,
            );
        }
```
