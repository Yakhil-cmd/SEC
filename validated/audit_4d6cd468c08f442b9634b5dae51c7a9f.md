### Title
Subnet Rental Request Price Locked at Proposal Creation Time Enables Front-Running ICP Price Drops - (File: `rs/nns/governance/src/proposals/execute_nns_function.rs`)

### Summary

The NNS governance canister passes `proposal_timestamp_seconds` (the proposal *creation* time) to the Subnet Rental Canister when executing a `SubnetRentalRequest` proposal. The Subnet Rental Canister uses this historical timestamp to look up the ICP/XDR exchange rate and lock `initial_cost_icp` at the proposal-creation-time price. Because NNS proposals can take days to be voted on and executed, a user can submit a rental request when ICP is expensive (rental price in ICP is low), then have the proposal execute after ICP has dropped significantly, obtaining a subnet rental at a fraction of the current market cost. The protocol bears the shortfall.

### Finding Description

When a `SubnetRentalRequest` NNS proposal is executed, the governance canister's `execute_nns_function` implementation reads `proposal_timestamp_seconds` from the stored `ProposalData` and forwards it to the Subnet Rental Canister as `proposal_creation_time_seconds` inside a `SubnetRentalProposalPayload`.

In `rs/nns/governance/src/canister_state.rs`, the execution path reads the proposal's creation timestamp and passes it directly to `re_encode_payload_to_target_canister`:

```rust
let proposal_timestamp_seconds = governance()
    .get_proposal_data(ProposalId(proposal_id))
    .map(|data| data.proposal_timestamp_seconds)
    ...;
let encoded_request = execute_nns_function
    .re_encode_payload_to_target_canister(proposal_id, proposal_timestamp_seconds)?;
``` [1](#0-0) 

Inside `re_encode_payload_to_target_canister`, the `SubnetRentalRequest` branch encodes this creation timestamp into the payload sent to the Subnet Rental Canister:

```rust
ValidNnsFunction::SubnetRentalRequest => {
    let decoded_payload = decode_subnet_rental_request(&self.payload)?;
    let encoded_payload = encode_subnet_rental_proposal_payload(
        decoded_payload,
        proposal_id,
        proposal_timestamp_seconds,   // ← proposal creation time, not execution time
    )?;
``` [2](#0-1) 

The helper `encode_subnet_rental_proposal_payload` embeds this as `proposal_creation_time_seconds` in the `SubnetRentalProposalPayload`: [3](#0-2) 

The `SubnetRentalProposalPayload` type explicitly carries this field: [4](#0-3) 

The Subnet Rental Canister's `execute_rental_request_proposal` method uses `proposal_creation_time_seconds` to look up the historical ICP/XDR rate and set `initial_cost_icp` — the amount of ICP the user must have pre-deposited. This is confirmed by the integration test, which explicitly asserts that `initial_cost_icp` equals `price3` (the price at proposal creation time), even after the ICP price has dropped significantly and the current rental price in ICP (`price5`) is much higher: [5](#0-4) 

The test comment at line 192 states this directly: *"The subnet rental NNS proposal is adopted and executed with the rental price in ICP fixed at step 3."*

### Impact Explanation

The Subnet Rental Canister collects ICP from the user and converts it to cycles to sustain the rented subnet. If the ICP/XDR rate at proposal creation time was higher than at execution time, the collected ICP is worth fewer XDR than the rental condition requires. The subnet rental canister will have insufficient cycles to sustain the subnet for the full rental period, and the protocol (NNS/ICP holders) absorbs the shortfall. Conversely, the user obtains a subnet rental at a below-market ICP cost. The magnitude of the loss scales with the ICP price drop and the rental cost (App13CH requires 820 TC/day for 180 days, a substantial sum).

### Likelihood Explanation

NNS proposals require a voting period that can last multiple days (the wait-for-quiet mechanism can extend it further). ICP price volatility over multi-day windows is well-documented. Any user who can submit an NNS proposal (any neuron holder) can observe ICP price trends and time their submission to coincide with a local price peak, then wait for the proposal to execute after a price decline. No privileged access, key compromise, or threshold attack is required — only the ability to submit an NNS proposal and pre-deposit ICP at the current price.

### Recommendation

Pass the **execution timestamp** (i.e., `now()` at the time the proposal is executed) to the Subnet Rental Canister instead of `proposal_timestamp_seconds`. In `execute_nns_function` within `rs/nns/governance/src/canister_state.rs`, replace the lookup of `proposal_timestamp_seconds` with the current time when constructing the `SubnetRentalProposalPayload`. This ensures the ICP/XDR rate used to validate the user's deposit reflects the market price at the moment the rental is actually created, eliminating the price-lock window.

Alternatively, the Subnet Rental Canister could re-validate the deposited ICP amount against the current exchange rate at execution time and reject the proposal if the deposit is insufficient.

### Proof of Concept

1. Observe that the current ICP/XDR rate is high (e.g., 25,000,000,000 units as in the test).
2. Call `get_todays_price` on the Subnet Rental Canister — the rental price in ICP is low (e.g., `price3`).
3. Transfer exactly `price3` ICP to the Subnet Rental Canister's subaccount.
4. Submit a `SubnetRentalRequest` NNS proposal via `manage_neuron` → `ExecuteNnsFunction`. The proposal records `proposal_timestamp_seconds = T_create`.
5. Wait for the ICP price to drop (e.g., to 5,000,000,000 units). The current rental price in ICP is now `price5 >> price3`.
6. Have the proposal voted in and executed. Governance calls `re_encode_payload_to_target_canister` with `proposal_timestamp_seconds = T_create`, encoding `proposal_creation_time_seconds = T_create` in the payload sent to the Subnet Rental Canister.
7. The Subnet Rental Canister looks up the ICP/XDR rate at `T_create` (the high rate), computes `initial_cost_icp = price3`, and accepts the user's pre-deposit as sufficient.
8. The rental request is created with `initial_cost_icp = price3`, even though the current market price would require `price5`. The attacker has obtained a subnet rental at a significant discount; the protocol is underpaid in XDR-equivalent value. [6](#0-5) [1](#0-0) [7](#0-6)

### Citations

**File:** rs/nns/governance/src/canister_state.rs (L223-228)
```rust
        let proposal_timestamp_seconds = governance()
            .get_proposal_data(ProposalId(proposal_id))
            .map(|data| data.proposal_timestamp_seconds)
            .ok_or(GovernanceError::new(ErrorType::PreconditionFailed))?;
        let encoded_request = execute_nns_function
            .re_encode_payload_to_target_canister(proposal_id, proposal_timestamp_seconds)?;
```

**File:** rs/nns/governance/src/proposals/execute_nns_function.rs (L154-187)
```rust
    pub fn re_encode_payload_to_target_canister(
        &self,
        proposal_id: u64,
        proposal_timestamp_seconds: u64,
    ) -> Result<Vec<u8>, GovernanceError> {
        match &self.nns_function {
            ValidNnsFunction::BitcoinSetConfig => {
                let request = get_request_for_bitcoin_set_config(&self.payload)
                    .map_err(|e| format!("Unable to decode BitcoinSetConfig proposal: {e}"))?;
                let encoded_request = Encode!(&request).unwrap();
                Ok(encoded_request)
            }
            ValidNnsFunction::SubnetRentalRequest => {
                let decoded_payload = decode_subnet_rental_request(&self.payload)?;
                let encoded_payload = encode_subnet_rental_proposal_payload(
                    decoded_payload,
                    proposal_id,
                    proposal_timestamp_seconds,
                )?;
                Ok(encoded_payload)
            }

            ValidNnsFunction::AddSnsWasm => {
                let decoded_payload = decode_add_wasm_request(&self.payload)?;
                let encoded_payload = encode_add_wasm_request(decoded_payload, proposal_id)?;
                Ok(encoded_payload)
            }

            // Most NNS functions don't require any transformation of the payload, and for new NNS
            // functions, consider use a proposal action instead, if the payload should be
            // understood by the NNS Governance canister.
            _ => Ok(self.payload.clone()),
        }
    }
```

**File:** rs/nns/governance/src/proposals/execute_nns_function.rs (L217-241)
```rust
fn encode_subnet_rental_proposal_payload(
    subnet_rental_request: SubnetRentalRequest,
    proposal_id: u64,
    proposal_creation_time_seconds: u64,
) -> Result<Vec<u8>, GovernanceError> {
    let SubnetRentalRequest {
        user,
        rental_condition_id,
    } = subnet_rental_request;

    let encoded_payload = Encode!(&SubnetRentalProposalPayload {
        user,
        rental_condition_id,
        proposal_id,
        proposal_creation_time_seconds,
    })
    .map_err(|_| {
        GovernanceError::new_with_message(
            ErrorType::InvalidProposal,
            "Unable to encode SubnetRentalProposalPayload proposal: {e}",
        )
    })?;

    Ok(encoded_payload)
}
```

**File:** rs/nns/governance/api/src/subnet_rental.rs (L34-40)
```rust
#[derive(candid::CandidType, candid::Deserialize)]
pub struct SubnetRentalProposalPayload {
    pub user: PrincipalId,
    pub rental_condition_id: RentalConditionId,
    pub proposal_id: u64,
    pub proposal_creation_time_seconds: u64,
}
```

**File:** rs/nns/integration_tests/src/subnet_rental_canister.rs (L182-335)
```rust
/// Test description:
///
///     1. ICP price goes up and thus the rental price in ICP decreases.
///
///     2. Renter principal sends ICP to Subnet Rental Canister in order to rent a subnet.
///
///     3. A subnet rental NNS proposal for renter is created.
///
///     4. ICP price goes down and thus the rental price in ICP increases.
///
///     5. The subnet rental NNS proposal is adopted and executed with the rental price in ICP fixed at step 3.
///
///     6. Renter requests a refund, thereby aborting the rental request.
///
/// In the end, this results in no rental requests, and renter gets their money back.
#[test]
fn subnet_rental_request_lifecycle() {
    let state_machine = setup_state_machine_with_nns_canisters();

    setup_mock_exchange_rate_canister(&state_machine, 5_000_000_000);
    let price1 = get_todays_price(&state_machine);

    // advance time by one day
    state_machine.advance_time(Duration::from_secs(86_400));

    setup_mock_exchange_rate_canister(&state_machine, 10_000_000_000);
    let price2 = get_todays_price(&state_machine);

    // advance time by one day
    state_machine.advance_time(Duration::from_secs(86_400));

    setup_mock_exchange_rate_canister(&state_machine, 25_000_000_000);
    let price3 = get_todays_price(&state_machine);

    // price should keep declining
    assert!(price1 > price2);
    assert!(price2 > price3);

    // advance time by half a day
    state_machine.advance_time(Duration::from_secs(12 * 60 * 60));

    setup_mock_exchange_rate_canister(&state_machine, 15_000_000_000);
    let price = get_todays_price(&state_machine);

    // the price should not change since the day is the same and thus the price
    // at last midnight is unchanged
    assert_eq!(price, price3);

    let renter = *TEST_USER1_PRINCIPAL;
    // check balance before user sends funds
    let balance_before = check_balance(&state_machine, renter, None);
    // user makes the initial transfer at price3
    send_icp_to_rent_subnet(&state_machine, renter);

    let large_neuron = get_neuron_1();
    let small_neuron = get_neuron_2();

    let subnet_rental_request = SubnetRentalRequest {
        user: renter,
        rental_condition_id: RentalConditionId::App13CH,
    };
    let proposal = MakeProposalRequest {
        title: Some("Create subnet rental request".to_string()),
        summary: "".to_string(),
        url: "".to_string(),
        action: Some(ProposalActionRequest::ExecuteNnsFunction(
            ExecuteNnsFunction {
                nns_function: NnsFunction::SubnetRentalRequest as i32,
                payload: Encode!(&subnet_rental_request).expect("Error encoding proposal payload"),
            },
        )),
    };
    // Make proposal with small neuron. It does not have enough voting power such that the proposal would be adopted immediately.
    let cmd = nns_governance_make_proposal(
        &state_machine,
        small_neuron.principal_id,
        small_neuron.neuron_id,
        &proposal,
    )
    .command
    .expect("Making NNS proposal failed");
    let proposal_id = match cmd {
        CommandResponse::MakeProposal(resp) => resp.proposal_id.unwrap(),
        other => panic!("Unexpected response: {other:?}"),
    };
    let proposal_time = state_machine.get_time();

    // advance time by one day
    state_machine.advance_time(Duration::from_secs(86_400));

    let price4 = get_todays_price(&state_machine);

    // advance time by one day
    state_machine.advance_time(Duration::from_secs(86_400));

    setup_mock_exchange_rate_canister(&state_machine, 5_000_000_000);
    let price5 = get_todays_price(&state_machine);

    // price should keep increasing
    assert!(price3 < price4);
    assert!(price4 < price5);

    // the proposal should not be decided yet as it was proposed by the small neuron
    let proposal_info =
        nns_governance_get_proposal_info_as_anonymous(&state_machine, proposal_id.id);
    assert_eq!(proposal_info.decided_timestamp_seconds, 0);

    // large neuron votes and thus the proposal should be executed now
    let response = nns_cast_vote_or_panic(
        &state_machine,
        large_neuron.principal_id,
        large_neuron.neuron_id,
        proposal_id.id,
        Vote::Yes,
    )
    .command
    .expect("Casting vote failed");
    assert_eq!(
        response,
        CommandResponse::RegisterVote(RegisterVoteResponse {})
    );
    nns_wait_for_proposal_execution(&state_machine, proposal_id.id);

    // check that the rental request has been created
    let raw_rental_requests = state_machine
        .query(
            SUBNET_RENTAL_CANISTER_ID,
            "list_rental_requests",
            Encode!(&()).unwrap(),
        )
        .unwrap();
    let rental_requests = match raw_rental_requests {
        WasmResult::Reply(bytes) => Decode!(&bytes, Vec<RentalRequest>).unwrap(),
        WasmResult::Reject(reason) => panic!("canister call rejected: {reason}"),
    };
    assert_eq!(rental_requests.len(), 1);
    let RentalRequest {
        user,
        initial_cost_icp,
        locked_amount_icp,
        initial_proposal_id,
        creation_time_nanos,
        rental_condition_id,
        last_locking_time_nanos,
        ..
    } = rental_requests[0];
    assert_eq!(user, renter);
    assert!(locked_amount_icp.get_e8s() <= initial_cost_icp.get_e8s() / 10);
    assert_eq!(initial_proposal_id, proposal_id.id);
    assert!(proposal_time.as_nanos_since_unix_epoch() <= creation_time_nanos);
    assert!(proposal_time.as_nanos_since_unix_epoch() <= last_locking_time_nanos);
    assert_eq!(creation_time_nanos, last_locking_time_nanos);
    assert_eq!(rental_condition_id, RentalConditionId::App13CH);
    assert_eq!(initial_cost_icp, price3);
```
