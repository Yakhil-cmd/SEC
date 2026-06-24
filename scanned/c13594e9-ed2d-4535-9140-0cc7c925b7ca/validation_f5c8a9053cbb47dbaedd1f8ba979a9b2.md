### Title
`cycles_per_xdr` Cannot Be Updated via Governance — (File: `rs/nns/cmc/src/main.rs`)

### Summary
The `cycles_per_xdr` field in the Cycles Minting Canister (CMC) is initialized to a hardcoded constant at canister creation and is not exposed through any governance proposal type, canister update method, or upgrade argument. There is no on-chain mechanism to change this fundamental cycles-pricing parameter without modifying and redeploying the canister code itself.

### Finding Description
`cycles_per_xdr` is declared in `StateV2` as the number of cycles that 1 XDR is worth: [1](#0-0) 

It is initialized to `DEFAULT_CYCLES_PER_XDR = 1_000_000_000_000` (1 trillion cycles per XDR) inside `State::default()`: [2](#0-1) 

The constant is defined in `lib.rs`: [3](#0-2) 

The `init` function does not accept or set `cycles_per_xdr` — it falls through to the default: [4](#0-3) 

The `post_upgrade` function restores state from stable memory and only allows overriding `exchange_rate_canister_id` and `cycles_ledger_canister_id` via upgrade args. `cycles_per_xdr` is never touched: [5](#0-4) 

The `CyclesCanisterInitPayload` struct has no `cycles_per_xdr` field: [6](#0-5) 

The CMC's public Candid interface exposes no method to update `cycles_per_xdr`: [7](#0-6) 

There is no NNS governance proposal type (no `NNS_FUNCTION_SET_CYCLES_PER_XDR` equivalent) that targets this field. The only cycles-pricing governance hook is `NNS_FUNCTION_ICP_XDR_CONVERSION_RATE`, which updates the ICP/XDR market rate — a separate parameter: [8](#0-7) 

### Impact Explanation
`cycles_per_xdr` is a multiplier in every cycles-minting operation. The effective cycles minted per ICP is:

```
cycles = ICP_amount × xdr_permyriad_per_icp × cycles_per_xdr / 10_000
```

If the IC ever needs to adjust the absolute cycles-per-XDR ratio (e.g., to rebase cycles pricing, respond to compute-cost changes, or correct an economic miscalibration), there is no on-chain path to do so. The only recourse is a full code change, NNS upgrade proposal, and canister redeployment — a heavyweight process with no targeted governance action. Any period during which the ratio is economically wrong cannot be corrected in-band.

### Likelihood Explanation
The IC has operated with 1T cycles = 1 XDR since genesis, so the immediate risk is low. However, the absence of an update path is a structural gap: as compute costs evolve, the inability to adjust `cycles_per_xdr` without a code-level canister upgrade is a real operational constraint with no in-protocol remedy.

### Recommendation
Add a governance-gated update method (callable only by `GOVERNANCE_CANISTER_ID`, analogous to `set_icp_xdr_conversion_rate`) that allows updating `cycles_per_xdr` in CMC state. Alternatively, include `cycles_per_xdr` as an optional field in `CyclesCanisterInitPayload` so it can be adjusted at upgrade time without a code change. [9](#0-8) 

### Proof of Concept
1. Deploy the CMC. `cycles_per_xdr` is set to `1_000_000_000_000` and persisted in stable state.
2. Attempt to change it: there is no `set_cycles_per_xdr` canister method, no NNS proposal type targeting it, and no upgrade-arg field for it in `CyclesCanisterInitPayload`.
3. Confirm via `encode_metrics` that `cmc_cycles_per_xdr` is always `1_000_000_000_000` regardless of any governance action: [10](#0-9) 

The parameter is permanently frozen at its genesis value with no on-chain update path.

### Citations

**File:** rs/nns/cmc/src/main.rs (L229-230)
```rust
    /// How many cycles 1 XDR is worth.
    pub cycles_per_xdr: Cycles,
```

**File:** rs/nns/cmc/src/main.rs (L379-379)
```rust
            cycles_per_xdr: DEFAULT_CYCLES_PER_XDR.into(),
```

**File:** rs/nns/cmc/src/main.rs (L436-484)
```rust
#[init]
fn init(maybe_args: Option<CyclesCanisterInitPayload>) {
    let args =
        maybe_args.expect("Payload is expected to initialization the cycles minting canister.");
    print(format!(
        "[cycles] init() with ledger canister {}, governance canister {}, exchange rate canister {}, minting account {}, and cycles ledger canister {}",
        args.ledger_canister_id
            .as_ref()
            .map(|x| x.to_string())
            .unwrap_or_else(|| "<none>".to_string()),
        args.governance_canister_id
            .as_ref()
            .map(|x| x.to_string())
            .unwrap_or_else(|| "<none>".to_string()),
        args.exchange_rate_canister
            .as_ref()
            .map(|x| match x {
                ExchangeRateCanister::Set(id) => id.to_string(),
                ExchangeRateCanister::Unset => "<unset>".to_string(),
            })
            .unwrap_or_else(|| "<none>".to_string()),
        args.minting_account_id
            .map(|x| x.to_string())
            .unwrap_or_else(|| "<none>".to_string()),
        args.cycles_ledger_canister_id
            .as_ref()
            .map(|x| x.to_string())
            .unwrap_or_else(|| "<none>".to_string()),
    ));

    STATE.with(|state| state.replace(Some(State::default())));
    with_state_mut(|state| {
        state.ledger_canister_id = args
            .ledger_canister_id
            .expect("Ledger canister ID must be set!");
        state.governance_canister_id = args
            .governance_canister_id
            .expect("Governance canister ID must be set!");
        state.minting_account_id = args.minting_account_id;
        if let Some(last_purged_notification) = args.last_purged_notification {
            state.last_purged_notification = last_purged_notification;
        }
        if let Some(xrc_flag) = args.exchange_rate_canister {
            state.exchange_rate_canister_id = xrc_flag.extract_exchange_rate_canister_id();
        }
        if args.cycles_ledger_canister_id.is_some() {
            state.cycles_ledger_canister_id = args.cycles_ledger_canister_id;
        }
    });
```

**File:** rs/nns/cmc/src/main.rs (L978-1005)
```rust
#[update(hidden = true)]
fn set_icp_xdr_conversion_rate(
    proposed_conversion_rate: UpdateIcpXdrConversionRatePayload,
) -> Result<(), String> {
    let caller = caller();

    assert_eq!(
        caller,
        GOVERNANCE_CANISTER_ID.into(),
        "{} is not authorized to call this method: {}",
        caller,
        "set_icp_xdr_conversion_rate"
    );

    let env = CanisterEnvironment;
    let rate = IcpXdrConversionRate::from(&proposed_conversion_rate);
    let rate_timestamp_seconds = rate.timestamp_seconds;
    let result = do_set_icp_xdr_conversion_rate(&STATE, &env, rate);
    if result.is_ok() && with_state(|state| state.exchange_rate_canister_id.is_some()) {
        exchange_rate_canister::set_update_exchange_rate_state(
            &STATE,
            &proposed_conversion_rate.reason,
            rate_timestamp_seconds,
        );
    }

    result
}
```

**File:** rs/nns/cmc/src/main.rs (L2374-2395)
```rust
#[post_upgrade]
fn post_upgrade(maybe_args: Option<CyclesCanisterInitPayload>) {
    let bytes = stable_utils::stable_get().expect("Could not read data from stable memory");
    print(format!(
        "[cycles] deserializing state after upgrade ({} bytes)",
        bytes.len(),
    ));

    let mut new_state = State::decode(&bytes).unwrap();
    if new_state.subnet_types_to_subnets.is_none() {
        new_state.subnet_types_to_subnets = Some(BTreeMap::new());
    }

    if let Some(args) = maybe_args {
        if let Some(xrc_flag) = args.exchange_rate_canister {
            new_state.exchange_rate_canister_id = xrc_flag.extract_exchange_rate_canister_id();
        }
        new_state.cycles_ledger_canister_id = args.cycles_ledger_canister_id;
    }

    STATE.with(|state| state.replace(Some(new_state)));
}
```

**File:** rs/nns/cmc/src/main.rs (L2464-2468)
```rust
        w.encode_gauge(
            "cmc_cycles_per_xdr",
            state.cycles_per_xdr.get() as f64,
            "Number of cycles corresponding to 1 XDR.",
        )?;
```

**File:** rs/nns/cmc/src/lib.rs (L22-22)
```rust
pub const DEFAULT_CYCLES_PER_XDR: u128 = 1_000_000_000_000_u128; // 1T cycles = 1 XDR
```

**File:** rs/nns/cmc/src/lib.rs (L115-123)
```rust
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize, Serialize)]
pub struct CyclesCanisterInitPayload {
    pub ledger_canister_id: Option<CanisterId>,
    pub governance_canister_id: Option<CanisterId>,
    pub minting_account_id: Option<AccountIdentifier>,
    pub last_purged_notification: Option<BlockIndex>,
    pub exchange_rate_canister: Option<ExchangeRateCanister>,
    pub cycles_ledger_canister_id: Option<CanisterId>,
}
```

**File:** rs/nns/cmc/cmc.did (L240-272)
```text
service : (opt CyclesCanisterInitPayload) -> {
  // Prompts the cycles minting canister to process a payment by converting ICP
  // into cycles and sending the cycles the specified canister.
  notify_top_up : (NotifyTopUpArg) -> (NotifyTopUpResult);

  // Creates a canister using the cycles attached to the function call.
  create_canister : (CreateCanisterArg) -> (CreateCanisterResult);

  // Prompts the cycles minting canister to process a payment for canister creation.
  notify_create_canister : (NotifyCreateCanisterArg) -> (NotifyCreateCanisterResult);

  // Mints cycles and deposits them to the cycles ledger
  notify_mint_cycles : (NotifyMintCyclesArg) -> (NotifyMintCyclesResult);

  // Returns the ICP/XDR conversion rate.
  get_icp_xdr_conversion_rate : () -> (IcpXdrConversionRateResponse) query;

  // Returns the current mapping of subnet types to subnets.
  get_subnet_types_to_subnets : () -> (SubnetTypesToSubnetsResponse) query;

  // Returns the mapping from principals to subnets in which they are authorized
  // to create canisters.
  get_principals_authorized_to_create_canisters_to_subnets : () -> (PrincipalsAuthorizedToCreateCanistersToSubnetsResponse) query;

  get_default_subnets: () -> (vec principal) query;

  get_build_metadata : () -> (text) query;

  // Below are methods that can only be called by other NNS canisters.
  set_authorized_subnetwork_list : (SetAuthorizedSubnetworkListArgs) -> ();
  update_subnet_type : (UpdateSubnetTypeArgs) -> ();
  change_subnet_type_assignment : (ChangeSubnetTypeAssignmentArgs) -> ();
};
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L358-363)
```text
  // Update the ICP/XDR conversion rate.
  // Changes the ICP-to-XDR conversion rate in the governance canister. This
  // setting affects cycles pricing (as the value of cycles shall be constant
  // with respect to IMF SDRs) as well as the rewards paid for nodes, which
  // are expected to be specified in terms of IMF SDRs as well.
  NNS_FUNCTION_ICP_XDR_CONVERSION_RATE = 10;
```
