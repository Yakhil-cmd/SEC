### Title
`SelfValidatingPayload` Bitcoin Adapter Response Content Not Validated Against Pending Requests — (`rs/bitcoin/consensus/src/payload_builder.rs`)

### Summary

The `validate_self_validating_payload_impl` function in the `BitcoinPayloadBuilder` accepts any non-empty `SelfValidatingPayload` as valid after checking only its byte size. It does not verify that each `BitcoinAdapterResponse`'s `callback_id` corresponds to an actual pending Bitcoin request in the certified state, nor that the response type matches the request type. A single malicious block-proposing node (below the consensus fault threshold) can craft a `SelfValidatingPayload` containing fabricated Bitcoin adapter responses with real `callback_id` values but fake content. All other nodes will accept the block because their validation also only checks size. The execution environment then processes the fabricated responses as legitimate Bitcoin network data.

### Finding Description

`BitcoinPayloadBuilder::validate_self_validating_payload_impl` is the function called by every replica to validate a proposed block's `SelfValidatingPayload` section:

```rust
fn validate_self_validating_payload_impl(
    &self,
    payload: &SelfValidatingPayload,
    validation_context: &ValidationContext,
) -> Result<NumBytes, SelfValidatingPayloadValidationError> {
    let since = Instant::now();

    // An empty block is always valid.
    if *payload == SelfValidatingPayload::default() {
        return Ok(0.into());
    }

    self.metrics
        .observe_validate_duration(VALIDATION_STATUS_VALID, since);
    let size = NumBytes::new(payload.count_bytes() as u64);

    Ok(size)
}
``` [1](#0-0) 

The only check performed is whether the payload is empty and whether its byte size is within limits. There is no check that:

1. Each `BitcoinAdapterResponse::callback_id` corresponds to a real pending request stored in `subnet_call_context_manager.bitcoin_get_successors_contexts` or `bitcoin_send_transaction_internal_contexts`.
2. The response variant (`GetSuccessorsResponse` vs. `SendTransactionResponse`) matches the request type for that `callback_id`.
3. The response content is consistent with what was actually requested.

By contrast, `get_self_validating_payload_impl` — the honest builder — iterates only over real pending requests from the certified state and assigns each response the correct `callback_id`:

```rust
for (callback_id, request) in bitcoin_requests_iter(&state) {
    ...
    let response = BitcoinAdapterResponse {
        response: ...,
        callback_id: callback_id.get(),
    };
``` [2](#0-1) 

The validation path through `BatchPayloadSectionBuilder::validate_payload` delegates entirely to `validate_self_validating_payload_impl` with no additional checks: [3](#0-2) 

### Impact Explanation

A single malicious node that wins a block-proposal slot can craft a `SelfValidatingPayload` containing `BitcoinAdapterResponse` entries that:

- Use a real `callback_id` from a currently pending `GetSuccessorsRequest` but supply fabricated Bitcoin blocks (e.g., blocks containing fake transactions crediting the attacker's ckBTC deposit address).
- Use a real `callback_id` from a pending `SendTransactionRequest` but supply a success response for a transaction that was never broadcast.

All other replicas call `validate_self_validating_payload_impl`, which accepts the payload because its byte size is within limits. The fabricated responses are then delivered to the execution environment and processed by the Bitcoin management canister as authentic Bitcoin network data. This can result in:

- Incorrect UTXO set state on the IC, causing the ckBTC minter to mint ckBTC for non-existent Bitcoin deposits.
- False confirmation of Bitcoin withdrawal transactions that were never submitted to the Bitcoin network.

### Likelihood Explanation

Every node participates in block proposal rotation. A single compromised or malicious node operator — well below the consensus fault threshold — will eventually be selected as block proposer. No threshold cryptography, admin key, or majority collusion is required. The attack requires only that the node craft a valid-sized `SelfValidatingPayload` with fabricated `BitcoinAdapterResponse` entries, which is trivially achievable by any node operator who controls the replica binary.

### Recommendation

`validate_self_validating_payload_impl` must load the certified state at `validation_context.certified_height` and, for each `BitcoinAdapterResponse` in the payload, verify:

1. `response.callback_id` exists in `subnet_call_context_manager.bitcoin_get_successors_contexts` or `bitcoin_send_transaction_internal_contexts` (analogous to checking `stack.lien.collateralId == collateralId` in the Seaport fix).
2. The response variant matches the request type stored for that `callback_id` (e.g., a `GetSuccessorsResponse` must correspond to a `GetSuccessorsRequest`).
3. No `callback_id` appears more than once in the payload.

This mirrors the pattern already correctly implemented in `validate_canister_http_payload_impl`, which looks up each `callback_id` in `canister_http_request_contexts` before accepting a response: [4](#0-3) 

### Proof of Concept

1. Attacker controls one replica node on the Bitcoin-enabled subnet.
2. Attacker reads the current certified state to obtain a live `callback_id` for a pending `GetSuccessorsRequest` belonging to a victim user's ckBTC deposit address.
3. When the attacker's node is selected as block proposer, it constructs a `SelfValidatingPayload` containing a `BitcoinAdapterResponse` with that `callback_id` and a fabricated `GetSuccessorsResponseComplete` that includes a fake Bitcoin block crediting the attacker's address with a large UTXO.
4. The block is proposed. Every other replica calls `validate_self_validating_payload_impl`; the payload passes because its byte size is within `MAX_BITCOIN_PAYLOAD_IN_BYTES`. [1](#0-0) 

5. The block is finalized. The execution environment delivers the fabricated `GetSuccessorsResponse` to the Bitcoin management canister, which updates its UTXO set with the fake transaction.
6. The attacker calls `update_balance` on the ckBTC minter; the minter observes the fake UTXO and mints ckBTC to the attacker's account with no corresponding real Bitcoin deposit. [5](#0-4)

### Citations

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L126-210)
```rust
        for (callback_id, request) in bitcoin_requests_iter(&state) {
            // We have already created a payload with the response for
            // this callback id, so skip it.
            if past_callback_ids.contains(&callback_id.get()) {
                continue;
            }

            let adapter_client = match request.network() {
                Network::BitcoinMainnet => &self.bitcoin_mainnet_adapter_client,
                Network::BitcoinTestnet | Network::BitcoinRegtest => {
                    &self.bitcoin_testnet_adapter_client
                }
                Network::DogecoinMainnet => &self.dogecoin_mainnet_adapter_client,
                Network::DogecoinTestnet | Network::DogecoinRegtest => {
                    &self.dogecoin_testnet_adapter_client
                }
            };

            // Send request to the adapter.
            let since = Instant::now();
            let result = adapter_client.send_blocking(
                request.clone(),
                Options {
                    timeout: self.config.adapter_timeout,
                },
            );

            // Update logs and metrics.
            match &result {
                Ok(wrapped_response) => {
                    self.metrics.observe_adapter_request_duration(
                        ADAPTER_REQUEST_STATUS_SUCCESS,
                        request.to_request_type_label(),
                        since,
                    );

                    if let BitcoinAdapterResponseWrapper::GetSuccessorsResponse(r) =
                        wrapped_response
                    {
                        self.metrics
                            .observe_blocks_per_get_successors_response(r.blocks.len());
                    }
                }
                Err(err) => {
                    self.metrics.observe_adapter_request_duration(
                        ADAPTER_REQUEST_STATUS_FAILURE,
                        request.to_request_type_label(),
                        since,
                    );

                    warn!(
                        self.log,
                        "Sending the request with callback id {} to the adapter failed with {:?}",
                        callback_id,
                        err
                    );
                }
            };

            // Build response.
            let response = BitcoinAdapterResponse {
                response: match result {
                    Ok(response_wrapper) => response_wrapper,
                    Err(err) => {
                        let error_message = err.to_string();
                        match request {
                            BitcoinAdapterRequestWrapper::SendTransactionRequest(context) => {
                                BitcoinAdapterResponseWrapper::SendTransactionReject(
                                    BitcoinReject {
                                        reject_code: RejectCode::SysTransient,
                                        message: error_message,
                                    },
                                )
                            }
                            BitcoinAdapterRequestWrapper::GetSuccessorsRequest(context) => {
                                BitcoinAdapterResponseWrapper::GetSuccessorsReject(BitcoinReject {
                                    reject_code: RejectCode::SysTransient,
                                    message: error_message,
                                })
                            }
                        }
                    }
                },
                callback_id: callback_id.get(),
            };
```

**File:** rs/bitcoin/consensus/src/payload_builder.rs (L234-251)
```rust
    fn validate_self_validating_payload_impl(
        &self,
        payload: &SelfValidatingPayload,
        validation_context: &ValidationContext,
    ) -> Result<NumBytes, SelfValidatingPayloadValidationError> {
        let since = Instant::now();

        // An empty block is always valid.
        if *payload == SelfValidatingPayload::default() {
            return Ok(0.into());
        }

        self.metrics
            .observe_validate_duration(VALIDATION_STATUS_VALID, since);
        let size = NumBytes::new(payload.count_bytes() as u64);

        Ok(size)
    }
```

**File:** rs/consensus/src/consensus/payload.rs (L408-416)
```rust
            Self::SelfValidating(builder) => {
                let past_payloads = builder.filter_past_payloads(past_payloads);
                builder
                    .validate_self_validating_payload(
                        &payload.self_validating,
                        proposal_context.validation_context,
                        &past_payloads,
                    )
                    .map_err(PayloadValidationError::from)
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L468-473)
```rust
            let callback_id = response.content.id;
            let request_context = http_contexts.get(&callback_id).ok_or(
                CanisterHttpPayloadValidationError::InvalidArtifact(
                    InvalidCanisterHttpPayloadReason::UnknownCallbackId(callback_id),
                ),
            )?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L143-193)
```rust
/// Notifies the ckBTC minter to update the balance of the user subaccount.
pub async fn update_balance<R: CanisterRuntime>(
    args: UpdateBalanceArgs,
    runtime: &R,
) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    let caller = runtime.caller();
    if args.owner.unwrap_or(caller) == runtime.id() {
        ic_cdk::trap("cannot update minter's balance");
    }

    // Record start time of method execution for metrics
    let start_time = runtime.time();

    // When the minter is in the mode using a whitelist we only want a certain
    // set of principal to be able to mint. But we also want those principals
    // to mint at any desired address. Therefore, the check below is on "caller".
    state::read_state(|s| s.mode.is_deposit_available_for(&caller))
        .map_err(UpdateBalanceError::TemporarilyUnavailable)?;

    init_ecdsa_public_key().await;

    let caller_account = Account {
        owner: args.owner.unwrap_or(caller),
        subaccount: args.subaccount,
    };
    let _guard = balance_update_guard(caller_account)?;

    let address = state::read_state(|s| runtime.derive_user_address(s, &caller_account));

    let (btc_network, min_confirmations) =
        state::read_state(|s| (s.btc_network, s.min_confirmations));

    let utxos = get_utxos(
        btc_network,
        &address,
        min_confirmations,
        CallSource::Client,
        runtime,
    )
    .await?
    .utxos;

    let now = Timestamp::from(runtime.time());
    let (processable_utxos, suspended_utxos) =
        state::read_state(|s| s.processable_utxos_for_account(utxos, &caller_account, &now));

    let (processable_utxos, duplicated_utxos): (BTreeSet<_>, BTreeSet<_>) = read_state(|s| {
        processable_utxos
            .into_iter()
            .partition(|utxo| !s.minted_outpoints.contains(&utxo.outpoint))
    });
```
