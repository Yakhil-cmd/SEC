Audit Report

## Title
Permanent Stuck Entry in `ongoing_xnet_reshares` via Empty-Receiver `reshare_chain_key` Call — (`rs/consensus/idkg/src/payload_builder/resharing.rs`)

## Summary
Any canister can call `reshare_chain_key` with an empty `nodes` list. `get_set_of_nodes()` accepts it, the empty-receiver context is stored in replicated state, and `initiate_reshare_requests` inserts a `ReshareOfUnmaskedParams` with an empty receiver set into `ongoing_xnet_reshares`. Every subsequent call to `update_completed_reshare_requests` fails at `translate()` with `ReceiversEmpty` and `continue`s, leaving the entry permanently stuck with no removal path. Repeated calls with distinct registry versions accumulate multiple stuck entries, each iterated every block.

## Finding Description

**Step 1 — No authorization check.** `reshare_chain_key` at L1547–1562 only rejects `CanisterCall::Ingress`; any inter-canister `Request` is accepted without caller-identity validation. [1](#0-0) 

**Step 2 — `get_set_of_nodes()` accepts empty input.** The function only checks for duplicate node IDs. An empty `nodes` field iterates zero times and returns `Ok(BTreeSet::new())`. [2](#0-1) 

**Step 3 — Empty-node context stored in replicated state.** `reshare_chain_key` calls `args.get_set_of_nodes()?`, receives `Ok(BTreeSet::new())`, and pushes a `ReshareChainKeyContext` with `nodes = BTreeSet::new()` into `subnet_call_context_manager` with no further validation. [3](#0-2) 

**Step 4 — `initiate_reshare_requests` inserts the stuck entry.** `receivers` is built directly from `request.receiving_node_ids` (empty). `ReshareOfUnmaskedParams::new` delegates to `IDkgTranscriptParamsRef::new`, which stores the empty set without any receiver-count check. The entry is unconditionally inserted into `ongoing_xnet_reshares`. [4](#0-3) [5](#0-4) [6](#0-5) 

**Step 5 — `translate()` always fails for the stuck entry.** `IDkgTranscriptParamsRef::translate` calls `IDkgTranscriptParams::new` → `IDkgReceivers::new(empty_set)` → `Err(IDkgParamsValidationError::ReceiversEmpty)`, mapped to `TranscriptParamsError::ParamsValidation`. [7](#0-6) [8](#0-7) 

**Step 6 — No removal path.** `update_completed_reshare_requests` logs the error and `continue`s. The only call to `ongoing_xnet_reshares.remove` inside this function is within the `completed_reshares` loop, which is never reached for this entry. A codebase-wide grep confirms removal only occurs in `resharing.rs` (inside the completed loop) and `payload_verifier.rs` (which mirrors builder logic, not an independent cleanup path). [9](#0-8) [10](#0-9) 

**Deduplication constraint.** `initiate_reshare_requests` deduplicates by `IDkgReshareRequest` key (which includes `master_key_id`, `receiving_node_ids`, and `registry_version`). An attacker can bypass deduplication by varying the `registry_version` field across calls, creating one stuck entry per unique (key_id, registry_version) pair. [11](#0-10) 

## Impact Explanation

Each stuck entry is iterated every block in `update_completed_reshare_requests`, calling `translate()` (which fails fast) and incrementing a metrics counter. The `ongoing_xnet_reshares` map is part of the consensus payload and is serialized/deserialized each round. Accumulation of stuck entries causes unbounded state growth and linear per-block processing overhead. The calling canister's inter-canister callback is permanently unresolved. Critically, other reshares for the same key are not blocked — the loop `continue`s past stuck entries — so the impact is subnet performance degradation and state bloat rather than consensus blocking. This maps to **High ($2,000–$10,000): Application/platform-level DoS or subnet availability impact not based on raw volumetric DDoS**, as the attack degrades per-block processing overhead unboundedly and permanently pollutes replicated state.

## Likelihood Explanation

Any canister on any subnet can trigger this. The attacker only needs a valid key ID (public information) and cycles to pay for the inter-canister call. There is no rate limit, authorization gate, or receiver-count validation beyond key existence. The attack is cheap and repeatable; varying `registry_version` across calls multiplies the number of stuck entries.

## Recommendation

1. **Validate non-empty receivers in `get_set_of_nodes()`** (`rs/types/management_canister_types/src/lib.rs`):
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
3. **Add a removal path** in `update_completed_reshare_requests` for entries whose `translate()` fails with a non-transient error (e.g., `ReceiversEmpty`), so they do not accumulate indefinitely.

## Proof of Concept

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

// Vary registry_version to bypass deduplication and accumulate more entries
for v in 2..=50u64 {
    let req = IDkgReshareRequest {
        master_key_id: valid_key_id,
        receiving_node_ids: vec![],
        registry_version: RegistryVersion::from(v),
    };
    initiate_reshare_requests(&mut payload, BTreeSet::from([req]), None, &no_op_logger());
}
assert_eq!(payload.ongoing_xnet_reshares.len(), 50); // 50 permanently stuck entries
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

**File:** rs/execution_environment/src/execution_environment.rs (L3889-3901)
```rust
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

**File:** rs/consensus/idkg/src/payload_builder/resharing.rs (L43-48)
```rust
        // Ignore requests we already know about
        if payload.ongoing_xnet_reshares.contains_key(&request)
            || payload.xnet_reshare_agreements.contains_key(&request)
        {
            continue;
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

**File:** rs/types/types/src/crypto/canister_threshold_sig/idkg.rs (L133-158)
```rust
    pub fn new(receivers: BTreeSet<NodeId>) -> Result<Self, IDkgParamsValidationError> {
        Self::ensure_receivers_not_empty(&receivers)?;
        let count = Self::number_of_receivers(receivers.len())?;

        let ret = IDkgReceivers { receivers, count };

        ret.ensure_verification_threshold_satisfied()?;

        Ok(ret)
    }

    fn number_of_receivers(
        receivers_count: usize,
    ) -> Result<NumberOfNodes, IDkgParamsValidationError> {
        number_of_nodes_from_usize(receivers_count)
            .map_err(|_| IDkgParamsValidationError::TooManyReceivers { receivers_count })
    }

    fn ensure_receivers_not_empty(
        receivers: &BTreeSet<NodeId>,
    ) -> Result<(), IDkgParamsValidationError> {
        if receivers.is_empty() {
            return Err(IDkgParamsValidationError::ReceiversEmpty);
        }
        Ok(())
    }
```
