### Title
SNS Swap `Init.transaction_fee_e8s` and `Init.neuron_minimum_stake_e8s` Are Never Validated Against Actual Ledger/Governance Values, and the Pairing Is Immutable - (`rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto`)

---

### Summary

The SNS Swap canister's `Init` struct stores `transaction_fee_e8s` and `neuron_minimum_stake_e8s` as local copies of values that must match the SNS ledger and SNS governance canisters respectively. The `Init` is explicitly documented as immutable after creation. The codebase itself acknowledges — in source comments — that these values are never cross-checked against the actual on-chain canisters, and that a mismatch will cause breakage. This is a direct IC analog of M-12: two paired components share a critical parameter that is stored redundantly without a compatibility check, and the pairing cannot be corrected once set.

---

### Finding Description

In `rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto`, the `Init` message is declared immutable at creation:

> "The initialisation data of the canister. Always specified on canister creation, and cannot be modified afterwards." [1](#0-0) 

Within that same `Init` message, two fields carry explicit warnings that their values are never validated against the actual canisters they are supposed to mirror:

```proto
// Same as SNS ledger. Must hold the same value as SNS ledger. Whether the
// values match is not checked. If they don't match things will break.
optional uint64 transaction_fee_e8s = 13;

// Same as SNS governance. Must hold the same value as SNS governance. Whether
// the values match is not checked. If they don't match things will break.
optional uint64 neuron_minimum_stake_e8s = 14;
``` [2](#0-1) 

The same warnings appear verbatim in the generated Rust struct: [3](#0-2) 

The SNS initialization code in `rs/sns/init/src/lib.rs` populates `swap_init_args` by copying `self.transaction_fee_e8s` directly from the `SnsInitPayload` without querying the actual ledger canister at runtime: [4](#0-3) 

The ledger is initialized separately with `with_transfer_fee(self.transaction_fee_e8s.unwrap_or(DEFAULT_TRANSFER_FEE.get_e8s()))`: [5](#0-4) 

Because the `Init` is immutable, any divergence between the swap's cached `transaction_fee_e8s` and the actual SNS ledger fee — whether introduced at creation time or by a subsequent SNS governance proposal that upgrades the ledger with a different fee — cannot be corrected.

---

### Impact Explanation

The swap canister uses `transaction_fee_e8s` to compute neuron basket token amounts during finalization (the `sweep_sns` phase), accounting for per-transfer fees when distributing SNS tokens to participant neurons. If the cached value is lower than the actual ledger fee, the computed transfer amounts will be rejected by the ledger with `BadFee` errors. The `sweep_sns` phase will fail for affected participants, leaving their ICP contributions locked in the swap canister with no recovery path, since the `Init` cannot be updated and the swap lifecycle is a one-way state machine. If the cached value is higher than the actual fee, participants receive fewer SNS tokens than they are entitled to — a silent conservation violation. [6](#0-5) 

---

### Likelihood Explanation

In the normal SNS creation flow both values are sourced from the same `SnsInitPayload`, so they start equal. However, two realistic divergence paths exist:

1. **Post-initialization ledger fee change**: An SNS governance proposal (executable before the swap is finalized, via NNS governance) can upgrade the SNS ledger canister with a different `transfer_fee`. The swap's `transaction_fee_e8s` remains at the original value with no mechanism to detect or correct the mismatch.
2. **Malformed SNS proposal**: Anyone with sufficient ICP staked can submit an NNS proposal to create an SNS. A proposal that sets `transaction_fee_e8s` in the swap init to a value different from the ledger's `transfer_fee` (e.g., by exploiting the fact that they are set independently) will produce a permanently broken swap.

The codebase's own comment — "If they don't match things will break" — confirms the developers are aware of the gap and have not closed it.

---

### Recommendation

At swap `init` time (or at the latest when `open` is called), perform a cross-canister query to the `sns_ledger_canister_id` to fetch `icrc1_fee` and assert it equals `transaction_fee_e8s`. Similarly, query `sns_governance_canister_id` for `neuron_minimum_stake_e8s` and assert equality. If either check fails, trap immediately rather than allowing the swap to proceed with stale parameters. Since `Init` is immutable, the check must happen before any participant funds are accepted.

---

### Proof of Concept

1. Deploy an SNS with `transaction_fee_e8s = 1_000` in the swap init and `transfer_fee = 10_000` in the ledger init (or upgrade the ledger fee to `10_000` after SNS creation but before swap finalization).
2. Participants contribute ICP; the swap reaches the `Committed` lifecycle.
3. `finalize` → `sweep_sns` attempts to transfer SNS tokens to neuron accounts, computing amounts using the stale `transaction_fee_e8s = 1_000`.
4. The SNS ledger rejects each transfer with `BadFee { expected_fee: 10_000 }`.
5. `sweep_sns` records failures; finalization halts per the existing guard: [7](#0-6) 

6. Participant ICP is locked in the swap canister. The `Init` cannot be updated to correct `transaction_fee_e8s`. No recovery path exists.

### Citations

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L283-288)
```text
// The initialisation data of the canister. Always specified on
// canister creation, and cannot be modified afterwards.
//
// If the initialization parameters are incorrect, the swap will
// immediately be aborted.
message Init {
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L314-320)
```text
  // Same as SNS ledger. Must hold the same value as SNS ledger. Whether the
  // values match is not checked. If they don't match things will break.
  optional uint64 transaction_fee_e8s = 13;

  // Same as SNS governance. Must hold the same value as SNS governance. Whether
  // the values match is not checked. If they don't match things will break.
  optional uint64 neuron_minimum_stake_e8s = 14;
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L282-289)
```rust
    /// Same as SNS ledger. Must hold the same value as SNS ledger. Whether the
    /// values match is not checked. If they don't match things will break.
    #[prost(uint64, optional, tag = "13")]
    pub transaction_fee_e8s: ::core::option::Option<u64>,
    /// Same as SNS governance. Must hold the same value as SNS governance. Whether
    /// the values match is not checked. If they don't match things will break.
    #[prost(uint64, optional, tag = "14")]
    pub neuron_minimum_stake_e8s: ::core::option::Option<u64>,
```

**File:** rs/sns/init/src/lib.rs (L597-603)
```rust
        let mut payload_builder =
            LedgerInitArgsBuilder::with_symbol_and_name(token_symbol, token_name)
                .with_minting_account(sns_canister_ids.governance.0)
                .with_transfer_fee(
                    self.transaction_fee_e8s
                        .unwrap_or(DEFAULT_TRANSFER_FEE.get_e8s()),
                )
```

**File:** rs/sns/init/src/lib.rs (L679-719)
```rust
    fn swap_init_args(&self, sns_canister_ids: &SnsCanisterIds) -> Result<SwapInit, String> {
        // Safe to cast due to validation
        let min_participants = self
            .min_participants
            .map(|min_participants| min_participants as u32);

        let sns_tokens_e8s = Some(self.get_swap_distribution()?.initial_swap_amount_e8s);

        Ok(SwapInit {
            sns_root_canister_id: sns_canister_ids.root.to_string(),
            sns_governance_canister_id: sns_canister_ids.governance.to_string(),
            sns_ledger_canister_id: sns_canister_ids.ledger.to_string(),

            nns_governance_canister_id: NNS_GOVERNANCE_CANISTER_ID.to_string(),
            icp_ledger_canister_id: ICP_LEDGER_CANISTER_ID.to_string(),

            fallback_controller_principal_ids: self.fallback_controller_principal_ids.clone(),

            transaction_fee_e8s: self.transaction_fee_e8s,
            neuron_minimum_stake_e8s: self.neuron_minimum_stake_e8s,
            confirmation_text: self.confirmation_text.clone(),
            restricted_countries: self.restricted_countries.clone(),
            min_participants,
            min_icp_e8s: self.min_icp_e8s,
            max_icp_e8s: self.max_icp_e8s,
            min_direct_participation_icp_e8s: self.min_direct_participation_icp_e8s,
            max_direct_participation_icp_e8s: self.max_direct_participation_icp_e8s,
            min_participant_icp_e8s: self.min_participant_icp_e8s,
            max_participant_icp_e8s: self.max_participant_icp_e8s,
            swap_start_timestamp_seconds: self.swap_start_timestamp_seconds,
            swap_due_timestamp_seconds: self.swap_due_timestamp_seconds,
            sns_token_e8s: sns_tokens_e8s,
            neuron_basket_construction_parameters: self.neuron_basket_construction_parameters,
            nns_proposal_id: self.nns_proposal_id,
            should_auto_finalize: Some(true),
            neurons_fund_participation_constraints: self
                .neurons_fund_participation_constraints
                .clone(),
            neurons_fund_participation: self.neurons_fund_participation,
        })
    }
```

**File:** rs/sns/swap/src/swap.rs (L396-452)
```rust
// High level documentation in the corresponding Protobuf message.
impl Swap {
    /// Create state from an `Init` object.
    ///
    /// Requires that `init` is valid; otherwise it panics.
    pub fn new(init: Init) -> Self {
        if let Err(e) = init.validate() {
            panic!("Invalid init arg, reason: {e}\nArg: {init:#?}\n");
        }
        let mut res = Self {
            lifecycle: Lifecycle::Pending as i32,
            init: None, // Postpone setting this field to avoid cloning.
            params: None,
            cf_participants: vec![],
            buyers: Default::default(), // Btree map
            neuron_recipes: vec![],
            open_sns_token_swap_proposal_id: None,
            finalize_swap_in_progress: Some(false),
            decentralization_sale_open_timestamp_seconds: None,
            decentralization_swap_termination_timestamp_seconds: None,
            next_ticket_id: Some(0),
            purge_old_tickets_last_completion_timestamp_nanoseconds: Some(0),
            purge_old_tickets_next_principal: Some(FIRST_PRINCIPAL_BYTES.to_vec()),
            already_tried_to_auto_finalize: Some(false),
            auto_finalize_swap_response: None,
            direct_participation_icp_e8s: Some(0),
            neurons_fund_participation_icp_e8s: Some(0),
            timers: None,
        };
        if init.validate_swap_init_for_one_proposal_flow().is_ok() {
            // Automatically fill out the fields that the (legacy) open request
            // used to provide, supporting clients who read legacy Swap fields.
            {
                res.cf_participants = vec![];
                match Params::try_from(&init) {
                    Err(err) => {
                        log!(
                            ERROR,
                            "Failed filling out the legacy Param structure: {}. \
                            Falling back to None.",
                            err
                        );
                        res.params = None;
                    }
                    Ok(params) => {
                        res.params = Some(params);
                    }
                }
            }
            res.open_sns_token_swap_proposal_id = init.nns_proposal_id;
            res.decentralization_sale_open_timestamp_seconds = init.swap_start_timestamp_seconds;
            // Transit to the next SNS lifecycle state.
            res.lifecycle = Lifecycle::Adopted as i32;
        }
        res.init = Some(init);
        res
    }
```

**File:** rs/sns/swap/tests/swap.rs (L2960-2966)
```rust

    assert_eq!(
        result.error_message,
        Some(String::from(
            "Transferring SNS tokens did not complete fully, some transfers were invalid or failed. Halting swap finalization"
        ))
    );
```
