### Title
Lack of Slippage Protection in Cycles Minting Canister ICP-to-Cycles Conversion - (File: rs/nns/cmc/src/main.rs)

### Summary
The Cycles Minting Canister (CMC) converts ICP to cycles using a dynamically updated ICP/XDR exchange rate at the time the `notify_top_up`, `notify_mint_cycles`, or `notify_create_canister` call is executed. None of these methods accept a user-specified minimum cycles amount, leaving users with no protection against adverse rate movements between the time they send ICP and the time they trigger the conversion.

### Finding Description
The CMC implements a two-step ICP-to-cycles conversion flow:

1. The user transfers ICP to a CMC subaccount on the ICP ledger.
2. The user calls `notify_top_up`, `notify_mint_cycles`, or `notify_create_canister` to trigger the conversion.

The conversion is performed by `tokens_to_cycles` in `rs/nns/cmc/src/main.rs`:

```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
            ...
        }
    })
}
``` [1](#0-0) 

The `icp_xdr_conversion_rate` is updated periodically from the Exchange Rate Canister and can change between step 1 and step 2. None of the three public notify methods expose a `min_cycles_expected` or equivalent parameter:

- `notify_top_up` accepts only `block_index` and `canister_id`
- `notify_mint_cycles` accepts only `block_index`, `to_subaccount`, and `deposit_memo`
- `notify_create_canister` accepts only `block_index`, `controller`, `subnet_type`, `subnet_selection`, and `settings` [2](#0-1) [3](#0-2) 

The `process_top_up`, `process_mint_cycles`, and `process_create_canister` internal functions all call `tokens_to_cycles` with no minimum-cycles guard: [4](#0-3) [5](#0-4) [6](#0-5) 

This risk is explicitly acknowledged in the IC codebase itself for the Treasury Manager interface:

> "Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved." [7](#0-6) 

### Impact Explanation
A user who sends 10 ICP to the CMC when the rate is 100 XDR/ICP (expecting ~1,000T cycles) may receive only ~500T cycles if the rate drops to 50 XDR/ICP before they call `notify_top_up`. The ICP is irrevocably burned upon a successful notify call — there is no rollback. The user suffers a direct economic loss (fewer cycles than expected) with no recourse. This is a **cycles/resource accounting bug** affecting any unprivileged user of the CMC.

### Likelihood Explanation
The ICP/XDR rate is updated approximately every minute from the Exchange Rate Canister. ICP price volatility of 5–20% within minutes is realistic during active market conditions. The two-step flow (transfer then notify) introduces a mandatory window during which the rate can change. Any user who queries `get_icp_xdr_conversion_rate` before sending ICP and then calls a notify method seconds or minutes later is exposed. The flow is used by all developers and users who top up canisters or mint cycles via the CMC. [8](#0-7) 

### Recommendation
Add an optional `min_cycles_expected: opt nat` field to `NotifyTopUpArg`, `NotifyMintCyclesArg`, and `NotifyCreateCanisterArg` in `cmc.did`. In `tokens_to_cycles` (or its callers), after computing `cycles`, check:

```rust
if let Some(min_cycles) = min_cycles_expected {
    if cycles < min_cycles {
        return Err(NotifyError::Refunded {
            reason: format!("Slippage: got {} cycles, minimum was {}", cycles, min_cycles),
            block_index: refund_block,
        });
    }
}
```

This mirrors the fix described in the external report (`max_lamports_to_spend` field) and allows users to bound their exposure to rate volatility.

### Proof of Concept
1. Query `get_icp_xdr_conversion_rate` — observe rate R (e.g., 100 XDR/ICP → 100T cycles per ICP).
2. Transfer 1 ICP to the CMC top-up subaccount for canister C.
3. Wait for the ICP/XDR rate to drop (e.g., to 50 XDR/ICP due to market movement or a governance-approved rate update).
4. Call `notify_top_up { block_index: <block>, canister_id: C }`.
5. Observe that canister C receives only ~50T cycles instead of the ~100T cycles expected at step 1.
6. The ICP is burned; no refund is issued; the user has no recourse. [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** rs/nns/cmc/src/main.rs (L1140-1145)
```rust
async fn notify_top_up(
    NotifyTopUp {
        block_index,
        canister_id,
    }: NotifyTopUp,
) -> Result<Cycles, NotifyError> {
```

**File:** rs/nns/cmc/src/main.rs (L1239-1244)
```rust
async fn notify_mint_cycles(
    NotifyMintCyclesArg {
        block_index,
        to_subaccount,
        deposit_memo,
    }: NotifyMintCyclesArg,
```

**File:** rs/nns/cmc/src/main.rs (L1347-1355)
```rust
async fn notify_create_canister(
    NotifyCreateCanister {
        block_index,
        controller,
        subnet_type,
        subnet_selection,
        settings,
    }: NotifyCreateCanister,
) -> Result<CanisterId, NotifyError> {
```

**File:** rs/nns/cmc/src/main.rs (L1899-1923)
```rust
// If conversion fails, log and return an error
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
            None => {
                let error_message =
                    "No conversion rate found in CMC, notification aborted".to_string();
                print(&error_message);
                Err(NotifyError::Other {
                    error_code: NotifyErrorCode::Internal as u64,
                    error_message,
                })
            }
        }
    })
}
```

**File:** rs/nns/cmc/src/main.rs (L1925-1933)
```rust
async fn process_create_canister(
    controller: PrincipalId,
    from: AccountIdentifier,
    amount: Tokens,
    subnet_selection: Option<SubnetSelection>,
    settings: Option<CanisterSettings>,
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

```

**File:** rs/nns/cmc/src/main.rs (L1958-1966)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
```

**File:** rs/nns/cmc/src/main.rs (L1985-1992)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

```

**File:** rs/nns/cmc/cmc.did (L26-83)
```text
// The argument of the [notify_top_up] method.
type NotifyTopUpArg = record {
  // Index of the block on the ICP ledger that contains the payment.
  block_index : BlockIndex;

  // The canister to top up.
  canister_id : principal;
};

type SubnetSelection = variant {
  /// Choose a specific subnet
  Subnet : record {
    subnet : principal;
  };
  /// Choose a random subnet that fulfills the specified properties
  Filter : SubnetFilter;
};

type SubnetFilter = record {
  subnet_type : opt text;
};

// The argument of the [create_canister] method.
type CreateCanisterArg = record {
  // Optional canister settings that, if set, are applied to the newly created canister.
  // If not specified, the caller is the controller of the canister and the other settings are set to default values.
  settings : opt CanisterSettings;

  // An optional subnet type that, if set, determines what type of subnet
  // the new canister will be created on.
  // Deprecated. Use subnet_selection instead.
  subnet_type : opt text;

  // Optional instructions to select on which subnet the new canister will be created on.
  subnet_selection : opt SubnetSelection;
};

// The argument of the [notify_create_canister] method.
type NotifyCreateCanisterArg = record {
  // Index of the block on the ICP ledger that contains the payment.
  block_index : BlockIndex;

  // The controller of canister to create.
  controller : principal;

  // An optional subnet type that, if set, determines what type of subnet
  // the new canister will be created on.
  // Deprecated. Use subnet_selection instead.
  subnet_type : opt text;

  // Optional instructions to select on which subnet the new canister will be created on.
  // vec may contain no more than one element.
  subnet_selection : opt SubnetSelection;

  // Optional canister settings that, if set, are applied to the newly created canister.
  // If not specified, the caller is the controller of the canister and the other settings are set to default values.
  settings : opt CanisterSettings;
};
```

**File:** rs/nns/cmc/cmc.did (L200-218)
```text
type NotifyMintCyclesArg = record {
  block_index : BlockIndex;
  to_subaccount : Subaccount;
  deposit_memo : Memo;
};

type NotifyMintCyclesResult = variant {
  Ok : NotifyMintCyclesSuccess;
  Err : NotifyError;
};

type NotifyMintCyclesSuccess = record {
  // Cycles ledger block index of deposit
  block_index : nat;
  // Amount of cycles that were minted and deposited to the cycles ledger
  minted : nat;
  // New balance of the cycles ledger account
  balance : nat;
};
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L236-280)
```rust
pub async fn update_exchange_rate(
    safe_state: &'static LocalKey<RefCell<Option<State>>>,
    env: &impl Environment,
    xrc_client: &impl ExchangeRateCanisterClient,
) -> Result<(), UpdateExchangeRateError> {
    let now_timestamp_seconds = env.now_timestamp_seconds();
    let current_minute_seconds =
        round_down_to_multiple_of(now_timestamp_seconds, ONE_MINUTE_SECONDS);

    UpdateExchangeRateGuard::with_guard(safe_state, current_minute_seconds, async {
        let call_xrc_result = xrc_client.get_icp_to_xdr_exchange_rate(None).await;
        // Check if updating the rate via the exchange rate canister was disabled while retrieving the rate.
        // If it has, exit early.
        let is_updating_rate_disabled = read_state(safe_state, |state| {
            state
                .update_exchange_rate_canister_state
                .unwrap_or_default()
                == UpdateExchangeRateState::Disabled
        });
        if is_updating_rate_disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }

        match call_xrc_result {
            Ok(exchange_rate) => {
                validate_exchange_rate(&exchange_rate)
                    .map_err(|error| UpdateExchangeRateError::InvalidRate(error.to_string()))?;
                let icp_xdr_conversion_rate = IcpXdrConversionRate::from(exchange_rate);
                if let Err(error) =
                    do_set_icp_xdr_conversion_rate(safe_state, env, icp_xdr_conversion_rate)
                {
                    return Err(UpdateExchangeRateError::FailedToSetRate(error));
                }
            }
            Err(error) => {
                return Err(UpdateExchangeRateError::FailedToRetrieveRate(
                    error.to_string(),
                ));
            }
        };

        Ok(())
    })
    .await
}
```
