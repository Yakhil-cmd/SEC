I need to trace the full attack path carefully before rendering a verdict.

### Title
Permanent Stuck Entry in `ongoing_xnet_reshares` via Empty-Receiver `reshare_chain_key` Call — (`rs/consensus/idkg/src/payload_builder/resharing.rs`)

### Summary

Any canister can call `reshare_chain_key` on the management canister with an empty `nodes` list. The execution environment accepts it without validating that the receiver set is non-empty. This causes `initiate_reshare_requests` to insert a `ReshareOfUnmaskedParams` with an empty receiver set into `ongoing_xnet_reshares`. Every subsequent call to `update_completed_reshare_requests` fails to translate the params (`IDkgReceivers::new` returns `ReceiversEmpty`), logs a warning, and `continue`s — leaving the entry permanently stuck with no removal path.

### Finding Description

**Step 1 — Entry point (no authorization check).**
`reshare_chain_key` is only blocked for ingress messages; any inter-canister call is accepted. There is no caller-identity check inside the handler. [1](#0-0) 

**Step 2 — `get_set_of_nodes()` does not reject an empty list.**
The only validation is a duplicate-node check. An empty `nodes` field returns `Ok(BTreeSet::new())`. [2](#0-1) 

**Step 3 — Empty-node context stored in replicated state.**
`reshare_chain_key` pushes a `ReshareChainKeyContext` with `nodes = BTreeSet::new()` into `subnet_call_context_manager` with no further validation. [3](#0-2) 

**Step 4 — `initiate_reshare_requests` inserts a stuck entry.**
`receiving_node_ids` is empty, so `receivers` is an empty `BTreeSet`. `ReshareOfUnmaskedParams::new` delegates to `IDkgTranscriptParamsRef::new`, which stores the empty set without validation. The entry is unconditionally inserted into `ongoing_xnet_reshares`. [4](#0-3) 

`ReshareOfUnmaskedParams::new` / `IDkgTranscriptParamsRef::new` perform no receiver-count check: [5](#0-4) [6](#0-5) 

**Step 5 — `translate()` always fails for the stuck entry.**
`IDkgTranscriptParamsRef::translate` calls `IDkgTranscriptParams::new`, which calls `IDkgReceivers::new(empty_set)` → `Err(ReceiversEmpty)`. The error is mapped to `TranscriptParamsError::ParamsValidation`. [7](#0-6) [8](#0-7) [9](#0-8) 

**Step 6 — No removal path exists.**
`update_completed_reshare_requests` logs the error and `continue`s. The only call to `ongoing_xnet_reshares.remove` is inside the `completed_reshares` loop, which is never reached for this entry. A `grep` across the entire codebase confirms there is no other removal site. [10](#0-9) [11](#0-10) 

### Impact Explanation

- **Permanent stuck entries**: Each call with an empty `nodes` list inserts one entry into `ongoing_xnet_reshares` that is iterated every block but never removed.
- **Unbounded map growth**: Repeated calls accumulate entries, increasing per-block processing overhead linearly.
- **Consumed transcript IDs**: One `uid_generator` transcript ID is consumed per call (minor but non-recoverable).
- **Stuck caller callback**: The calling canister's inter-canister callback is permanently unresolved; the canister never receives a response.
- **Correctness note**: Other reshares for the same key are not blocked (the loop `continue`s past the stuck entry), so the claim of "blocking reshare completion indefinitely" is overstated. The real impact is unbounded state growth and per-block overhead.

### Likelihood Explanation

Any canister deployed on any subnet can trigger this. The attacker only needs to know a valid key ID (public information) and pay cycles for the call. There is no rate limit or authorization gate on `reshare_chain_key` beyond key existence. The attack is cheap to repeat.

### Recommendation

1. **Validate non-empty receivers in `get_set_of_nodes()`** in `ReshareChainKeyArgs`:
   ```rust
   if set.is_empty() {
       return Err(UserError::new(
           ErrorCode::InvalidManagementPayload,
           "nodes list must not be empty",
       ));
   }
   ```
2. **Add a guard in `initiate_reshare_requests`** before inserting into `ongoing_xnet_reshares`:
   ```rust
   if receivers.is_empty() {
       warn!(...); continue;
   }
   ```
3. **Add a removal path** in `update_completed_reshare_requests` for entries whose `translate()` fails with `ReceiversEmpty` (or any non-transient error), so they do not accumulate indefinitely.

### Proof of Concept

```rust
// State-machine test sketch
let mut payload = /* payload with valid current key transcript */;
let empty_request = IDkgReshareRequest {
    master_key_id: valid_key_id,
    receiving_node_ids: vec![],   // empty
    registry_version: RegistryVersion::from(1),
};

initiate_reshare_requests(
    &mut payload,
    BTreeSet::from([empty_request.clone()]),
    None,
    &no_op_logger(),
);
assert_eq!(payload.ongoing_xnet_reshares.len(), 1); // entry inserted

let transcript_builder = TestIDkgTranscriptBuilder::new();
for _ in 0..100 {
    update_completed_reshare_requests(
        &mut payload,
        &BTreeMap::new(),
        &block_reader,
        &transcript_builder,
        None,
        &no_op_logger(),
    );
    // Entry is never removed; translate() always returns ReceiversEmpty
    assert_eq!(payload.ongoing_xnet_reshares.len(), 1);
    assert!(payload.xnet_reshare_agreements.is_empty());
}
```

### Citations

**File:** rs/execution_environment/src/execution_environment.rs (L1547-1562)
```rust
            Ok(Ic00Method::ReshareChainKey) => {
                let cycles = msg.take_cycles();
                match msg {
                    CanisterCall::Request(ref request) => self
                        .reshare_chain_key(&mut state, rng, chain_key_data, request)
                        .map_or_else(
                            |err| ExecuteSubnetMessageResult::Finished {
                                response: Err(err),
                                refund: cycles,
                            },
                            |()| ExecuteSubnetMessageResult::Processing,
                        ),
                    CanisterCall::Ingress(_) => {
                        self.reject_unexpected_ingress(Ic00Method::ReshareChainKey)
                    }
                }
```

**File:** rs/execution_environment/src/execution_environment.rs (L3872-3903)
```rust
    fn reshare_chain_key(
        &self,
        state: &mut ReplicatedState,
        rng: &mut dyn RngCore,
        chain_key_data: &ChainKeyData,
        request: &Request,
    ) -> Result<(), UserError> {
        let args = ReshareChainKeyArgs::decode(request.method_payload())?;
        let _key = get_master_public_key(
            &chain_key_data.master_public_keys,
            self.own_subnet_id,
            &args.key_id,
        )?;

        let mut target_id = [0_u8; 32];
        rng.fill_bytes(&mut target_id);

        let nodes = args.get_set_of_nodes()?;
        let registry_version = args.get_registry_version();

        state.metadata.subnet_call_context_manager.push_context(
            SubnetCallContext::ReshareChainKey(ReshareChainKeyContext {
                request: request.clone(),
                key_id: args.key_id,
                nodes,
                registry_version,
                time: state.time(),
                target_id: NiDkgTargetId::new(target_id),
            }),
        );
        Ok(())
    }
```

**File:** rs/types/management_canister_types/src/lib.rs (L3352-3363)
```rust
    pub fn get_set_of_nodes(&self) -> Result<BTreeSet<NodeId>, UserError> {
        let mut set = BTreeSet::<NodeId>::new();
        for node_id in self.nodes.get().iter() {
            if !set.insert(NodeId::new(*node_id)) {
                return Err(UserError::new(
                    ErrorCode::InvalidManagementPayload,
                    format!("Expected a set of NodeIds. The NodeId {node_id} is repeated"),
                ));
            }
        }
        Ok(set)
    }
```

**File:** rs/consensus/idkg/src/payload_builder/resharing.rs (L50-66)
```rust
        // Set up the transcript params for the new request
        let transcript_id = payload.uid_generator.next_transcript_id();
        let receivers = request
            .receiving_node_ids
            .iter()
            .copied()
            .collect::<BTreeSet<_>>();
        let transcript_params = idkg::ReshareOfUnmaskedParams::new(
            transcript_id,
            receivers,
            request.registry_version,
            key_transcript,
            key_transcript.unmasked_transcript(),
        );
        payload
            .ongoing_xnet_reshares
            .insert(request, transcript_params);
```

**File:** rs/consensus/idkg/src/payload_builder/resharing.rs (L119-130)
```rust
        let transcript_params = match reshare_param.as_ref().translate(resolver) {
            Ok(params) => params,
            Err(err) => {
                warn!(
                    every_n_seconds => 10,
                    log,
                    "Failed to resolve reshare transcript params: {:?}", err
                );
                idkg_payload_metrics.payload_errors_inc("reshare_transcript_params_resolution");
                continue;
            }
        };
```

**File:** rs/consensus/idkg/src/payload_builder/resharing.rs (L165-182)
```rust
    // Insert any newly completed reshares
    for (request, initial_dealings) in completed_reshares {
        if let Some(response) =
            make_reshare_dealings_response(&request, &initial_dealings, idkg_dealings_contexts)
        {
            payload.ongoing_xnet_reshares.remove(&request);
            payload
                .xnet_reshare_agreements
                .insert(request, idkg::CompletedReshareRequest::Unreported(response));
        } else {
            warn!(
                every_n_seconds => 10,
                log,
                "Cannot find the request for the initial dealings created: {:?}", request
            );
            idkg_payload_metrics.payload_errors_inc("reshare_request_context_missing");
        }
    }
```

**File:** rs/types/types/src/consensus/idkg/common.rs (L505-521)
```rust
impl ReshareOfUnmaskedParams {
    pub fn new(
        transcript_id: IDkgTranscriptId,
        receivers: BTreeSet<NodeId>,
        registry_version: RegistryVersion,
        unmasked_attrs: &dyn TranscriptAttributes,
        transcript: UnmaskedTranscript,
    ) -> Self {
        Self(IDkgTranscriptParamsRef::new(
            transcript_id,
            unmasked_attrs.receivers().clone(),
            receivers,
            registry_version,
            unmasked_attrs.algorithm_id(),
            IDkgTranscriptOperationRef::ReshareOfUnmasked(transcript),
        ))
    }
```

**File:** rs/types/types/src/consensus/idkg/common.rs (L917-934)
```rust
impl IDkgTranscriptParamsRef {
    pub fn new(
        transcript_id: IDkgTranscriptId,
        dealers: BTreeSet<NodeId>,
        receivers: BTreeSet<NodeId>,
        registry_version: RegistryVersion,
        algorithm_id: AlgorithmId,
        operation_type_ref: IDkgTranscriptOperationRef,
    ) -> Self {
        Self {
            transcript_id,
            dealers,
            receivers,
            registry_version,
            algorithm_id,
            operation_type_ref,
        }
    }
```

**File:** rs/types/types/src/consensus/idkg/common.rs (L936-954)
```rust
    /// Resolves the refs to get the IDkgTranscriptParams.
    pub fn translate(
        &self,
        resolver: &dyn IDkgBlockReader,
    ) -> Result<IDkgTranscriptParams, TranscriptParamsError> {
        let operation_type = self
            .operation_type_ref
            .translate(resolver)
            .map_err(TranscriptParamsError::OperationRef)?;
        IDkgTranscriptParams::new(
            self.transcript_id,
            self.dealers.clone(),
            self.receivers.clone(),
            self.registry_version,
            self.algorithm_id,
            operation_type,
        )
        .map_err(TranscriptParamsError::ParamsValidation)
    }
```

**File:** rs/types/types/src/crypto/canister_threshold_sig/idkg.rs (L133-142)
```rust
    pub fn new(receivers: BTreeSet<NodeId>) -> Result<Self, IDkgParamsValidationError> {
        Self::ensure_receivers_not_empty(&receivers)?;
        let count = Self::number_of_receivers(receivers.len())?;

        let ret = IDkgReceivers { receivers, count };

        ret.ensure_verification_threshold_satisfied()?;

        Ok(ret)
    }
```

**File:** rs/types/types/src/crypto/canister_threshold_sig/idkg.rs (L151-158)
```rust
    fn ensure_receivers_not_empty(
        receivers: &BTreeSet<NodeId>,
    ) -> Result<(), IDkgParamsValidationError> {
        if receivers.is_empty() {
            return Err(IDkgParamsValidationError::ReceiversEmpty);
        }
        Ok(())
    }
```
