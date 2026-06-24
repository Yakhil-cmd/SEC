### Title
Hardcoded `E8` Divisor in SNS Token Treasury Valuation Ignores Actual Token Decimal Precision - (`rs/sns/governance/token_valuation/src/lib.rs`)

---

### Summary

`try_get_balance_valuation_factors` in the SNS governance token valuation library unconditionally divides the raw ICRC-1 balance by the hardcoded constant `E8` (`100_000_000` = 10^8) to convert from smallest-unit to whole-token representation. This constant is correct only for tokens with exactly 8 decimal places (e.g., ICP). SNS tokens are ICRC-1 tokens whose `decimals` field is freely configurable at ledger initialization. When an SNS token is deployed with a decimal count other than 8, the treasury valuation used by SNS governance proposals is systematically wrong by a factor of `10^(8 - actual_decimals)`.

---

### Finding Description

In `rs/sns/governance/token_valuation/src/lib.rs`, the function `try_get_balance_valuation_factors` fetches the raw ICRC-1 balance and converts it to a human-readable `Decimal` token count:

```rust
let tokens = Decimal::from(u128::try_from(balance_of_response.0)...) / Decimal::from(E8);
``` [1](#0-0) 

`E8` is defined as the constant `100_000_000`: [2](#0-1) 

This same `try_get_balance_valuation_factors` function is called by both `try_get_icp_balance_valuation` (ICP always has 8 decimals — correct) and `try_get_sns_token_balance_valuation` (SNS token decimals are configurable — potentially wrong): [3](#0-2) 

The ICRC-1 ledger's `InitArgs` accepts an optional `decimals: opt nat8` field, meaning any SNS token can be initialized with a decimal count other than 8: [4](#0-3) 

The `Ledger` struct stores this as a runtime field: [5](#0-4) 

The code never queries `icrc1_decimals` on the SNS token ledger before performing the division. There is no normalization step. The `Icrc1Client` trait only exposes `icrc1_balance_of`: [6](#0-5) 

---

### Impact Explanation

The `Valuation` result feeds directly into SNS governance treasury-related proposal checks in `rs/sns/governance/src/treasury.rs` and `rs/sns/governance/src/proposal.rs`: [7](#0-6) 

- **SNS token with 6 decimals** (e.g., a USDC-like SNS token): balance is divided by 10^8 instead of 10^6 → treasury appears **100× smaller** than reality → treasury spending proposals that should be blocked (exceeding the allowed fraction of treasury) are incorrectly permitted.
- **SNS token with 18 decimals**: balance is divided by 10^8 instead of 10^18 → treasury appears **10^10× larger** than reality → all treasury spending proposals are incorrectly blocked, freezing the SNS treasury.

Both outcomes corrupt SNS governance decisions about treasury transfers, directly affecting token conservation and governance authorization.

---

### Likelihood Explanation

The ICRC-1 standard and the SNS ledger initialization interface explicitly support configurable decimals. The ckERC20 system already deploys tokens with 6 decimals (ckUSDC): [8](#0-7) 

Any SNS that initializes its governance token with a non-8 decimal count — a valid and supported configuration — will silently produce wrong treasury valuations. No privileged access is required; the miscalculation is triggered automatically whenever SNS governance evaluates a treasury proposal for such an SNS.

---

### Recommendation

`try_get_balance_valuation_factors` (or its callers) should query `icrc1_decimals` on the target ledger and use `10^decimals` as the divisor instead of the hardcoded `E8`. The `Icrc1Client` trait should be extended with a `icrc1_decimals` method, or the divisor should be passed as a parameter derived from the ledger's actual decimal metadata.

---

### Proof of Concept

1. Deploy an SNS whose governance token ledger is initialized with `decimals = 6`.
2. Fund the SNS treasury with `1_000_000` raw units (= 1.0 whole token at 6 decimals).
3. Call `try_get_sns_token_balance_valuation` on the treasury account.
4. Observe: `tokens = 1_000_000 / 100_000_000 = 0.01` instead of the correct `1_000_000 / 1_000_000 = 1.0`.
5. The treasury is valued at 1/100th of its true worth, causing governance to permit treasury transfers that exceed the intended spending limit. [9](#0-8)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L37-57)
```rust
pub async fn try_get_sns_token_balance_valuation(
    account: Account,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
) -> Result<Valuation, ValuationError> {
    let timestamp = now();

    try_get_balance_valuation_factors(
        account,
        &mut LedgerCanister::<CdkRuntime>::new(sns_ledger_canister_id),
        &mut IcpsPerSnsTokenClient::<CdkRuntime>::new(swap_canister_id, sns_ledger_canister_id),
        &mut new_standard_xdrs_per_icp_client::<CdkRuntime>(),
    )
    .await
    .map(|valuation_factors| Valuation {
        token: Token::SnsToken,
        account,
        timestamp,
        valuation_factors,
    })
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L176-191)
```rust
    // Extract and interpret the data we actually care about from the (Ok) responses.
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
    let icps_per_token = icps_per_token_response;
    let xdrs_per_icp = xdrs_per_icp_response;

    // Compose the fetched/interpretted data (i.e. multiply them) to construct the final result.
    Ok(ValuationFactors {
        tokens,
        icps_per_token,
        xdrs_per_icp,
    })
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L242-246)
```rust
#[automock]
#[async_trait]
trait Icrc1Client: Send {
    async fn icrc1_balance_of(&mut self, account: Account) -> Result<Nat, (i32, String)>;
}
```

**File:** rs/nervous_system/common/src/lib.rs (L1-1)
```rust
use by_address::ByAddress;
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L100-110)
```text
type InitArgs = record {
  minting_account : Account;
  fee_collector_account : opt Account;
  transfer_fee : nat;
  decimals : opt nat8;
  max_memo_length : opt nat16;
  token_symbol : text;
  token_name : text;
  metadata : vec record { text; MetadataValue };
  initial_balances : vec record { Account; nat };
  feature_flags : opt FeatureFlags;
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L572-573)
```rust
    #[serde(default = "default_decimals")]
    decimals: u8,
```

**File:** rs/sns/governance/src/treasury.rs (L54-66)
```rust
pub(crate) fn tokens_to_e8s(tokens: Decimal) -> Result<u64, String> {
    let e8s = tokens.checked_mul(Decimal::from(E8)).ok_or_else(|| {
        format!(
            "Unable to convert {tokens} tokens (Decimal) to e8s (u64) due to multiplication overflow.",
        )
    })?;

    let e8s = u64::try_from(e8s).map_err(|err| {
        format!("Unable to convert {tokens} tokens (Decimal) to e8s (u64): {err:?}",)
    })?;

    Ok(e8s)
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L933-934)
```rust
        transfer_fee: ledger_init_arg.transfer_fee,
        decimals: Some(ledger_init_arg.decimals),
```
