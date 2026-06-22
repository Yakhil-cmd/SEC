### Title
Controller Can Unilaterally Drain Any User's Token Balance via `icrc152_burn` Without Account Owner Consent - (`File: rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The ICRC-1 ledger exposes `icrc152_burn`, an authorized burn endpoint gated solely on the caller being a canister controller. When the `icrc152` feature flag is enabled, any controller can burn tokens from **any** user account at any time without the account owner's knowledge or approval. This is a direct analog to the H-03 `SafetyWithdraw` pattern: a privileged party (ledger controller instead of market owner) can drain user funds unilaterally.

---

### Finding Description

The `icrc152_burn` endpoint in `rs/ledger_suite/icrc1/ledger/src/main.rs` performs the following checks before executing a burn:

1. The `icrc152` feature flag is `true`.
2. The caller is a controller (`ic_cdk::api::is_controller(&caller)`).
3. Amount is non-zero.
4. The `from` account is not anonymous and not the minting account. [1](#0-0) 

There is **no check** that the account owner has consented to the burn, no allowance/approval mechanism, and no circuit-breaker or rate limit. The controller supplies an arbitrary `from: Account` field: [2](#0-1) 

The public endpoint is: [3](#0-2) 

Exposed in the Candid interface as a standard update call: [4](#0-3) 

The `icrc152` feature flag defaults to `false` but can be set to `true` at init or upgrade time: [5](#0-4) [6](#0-5) 

The same structural issue applies to `icrc152_mint`, which allows a controller to mint arbitrary tokens to any account, inflating supply without user consent: [7](#0-6) 

There is no ICRC-21 consent message defined for `icrc152_burn` or `icrc152_mint` — a search for any consent-message integration for these endpoints returns no results, meaning wallet UIs cannot even surface a human-readable warning to users before the burn executes.

---

### Impact Explanation

A malicious or compromised ledger controller with `icrc152: true` enabled can:

- Call `icrc152_burn` with `from = <victim_account>` and `amount = <victim_full_balance>` to drain any user's entire token balance in a single transaction.
- Repeat this for every account on the ledger, draining all user funds.
- Call `icrc152_mint` to inflate supply to any account (e.g., their own), devaluing all existing holders.

The victim receives no warning, no consent prompt, and has no on-chain mechanism to prevent or reverse the burn. The ledger's certified state will reflect the drained balance immediately. This constitutes **direct theft of user assets** — the exact impact class of H-03.

---

### Likelihood Explanation

- The `icrc152` feature flag is `false` by default, so only ledgers that explicitly opt in are affected. However, the feature is designed for production use (compliance/regulatory burn use cases), so real deployments will enable it.
- The controller of an ICRC-1 ledger is typically the deploying developer, a governance canister, or an orchestrator. Any of these being malicious or compromised exposes all token holders.
- The attack requires no special tooling: a single `icrc152_burn` ingress call from the controller principal suffices.
- The `icrc152_burn` endpoint is publicly documented in the Candid interface and requires no privileged network access — any controller principal can call it from a standard `dfx` CLI or agent. [8](#0-7) 

---

### Recommendation

1. **Require account-owner consent**: Before burning from an account, require either an ICRC-2 allowance granted by the account owner to the controller, or an explicit on-chain approval transaction from the account owner.
2. **Exclude the full balance**: At minimum, prevent burning 100% of a user's balance in a single call without a time-locked or governance-gated process.
3. **Add ICRC-21 consent messages**: Implement `icrc21_canister_call_consent_message` for `icrc152_burn` and `icrc152_mint` so wallet UIs can surface human-readable warnings.
4. **Rate-limit or time-lock**: Introduce a mandatory delay or per-account burn cap so users have time to react to a malicious controller.
5. **Emit observable events**: Ensure `icrc152_burn` events are prominently surfaced in monitoring so anomalous drains are detectable in real time.

---

### Proof of Concept

**Setup**: Deploy an ICRC-1 ledger with `icrc152: true` and fund a victim account.

```bash
# Deploy ledger with icrc152 enabled
dfx canister install ledger --argument '(variant { Init = record {
  minting_account = record { owner = principal "MINTER" };
  transfer_fee = 10000;
  token_symbol = "TKN";
  token_name = "Token";
  initial_balances = vec { record { record { owner = principal "VICTIM" }; 1_000_000_000 } };
  feature_flags = opt record { icrc2 = true; icrc152 = true };
  archive_options = record { ... };
}})'
```

**Attack**: As the controller, call `icrc152_burn` targeting the victim:

```bash
dfx canister call ledger icrc152_burn '(record {
  from = record { owner = principal "VICTIM"; subaccount = null };
  amount = 1_000_000_000;
  created_at_time = <current_ns>;
  reason = opt "compliance"
})'
```

**Result**: The victim's balance drops from `1_000_000_000` to `0`. The controller has drained all user funds in a single call with no user consent required, directly mirroring the `SafetyWithdraw` drain pattern from H-03. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L905-920)
```rust
fn icrc152_mint_not_async(
    caller: Principal,
    args: Icrc152MintArgs,
) -> Result<u64, Icrc152MintError> {
    let block_idx = Access::with_ledger_mut(|ledger| {
        if !ledger.feature_flags().icrc152 {
            return Err(Icrc152MintError::GenericError {
                error_code: Nat::from(0_u64),
                message: "ICRC-152 is not enabled".to_string(),
            });
        }
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152MintError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1002-1013)
```rust
    let block_idx = Access::with_ledger_mut(|ledger| {
        if !ledger.feature_flags().icrc152 {
            return Err(Icrc152BurnError::GenericError {
                error_code: Nat::from(0_u64),
                message: "ICRC-152 is not enabled".to_string(),
            });
        }
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152BurnError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1044-1055)
```rust
        let tx = Transaction {
            operation: Operation::AuthorizedBurn {
                from: args.from,
                amount,
                caller: Some(caller),
                mthd: Some(MTHD_152_BURN.to_string()),
                reason: args.reason,
            },
            created_at_time: Some(args.created_at_time),
            memo: None,
        };
        let (block_idx, _) =
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1088-1094)
```rust
#[update]
async fn icrc152_burn(args: Icrc152BurnArgs) -> Result<Nat, Icrc152BurnError> {
    let block_idx = icrc152_burn_not_async(ic_cdk::api::msg_caller(), args)?;
    ic_cdk::api::certified_data_set(Access::with_ledger(Ledger::root_hash));
    archive_blocks::<Access>(&LOG, MAX_MESSAGE_SIZE).await;
    Ok(Nat::from(block_idx))
}
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L94-97)
```text
type FeatureFlags = record {
  icrc2 : bool;
  icrc152 : bool
};
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L605-642)
```text
service : (ledger_arg : LedgerArg) -> {
  archives : () -> (vec ArchiveInfo) query;
  get_transactions : (GetTransactionsRequest) -> (GetTransactionsResponse) query;
  get_blocks : (GetBlocksArgs) -> (GetBlocksResponse) query;
  get_data_certificate : () -> (DataCertificate) query;

  icrc1_name : () -> (text) query;
  icrc1_symbol : () -> (text) query;
  icrc1_decimals : () -> (nat8) query;
  icrc1_metadata : () -> (vec record { text; MetadataValue }) query;
  icrc1_total_supply : () -> (Tokens) query;
  icrc1_fee : () -> (Tokens) query;
  icrc1_minting_account : () -> (opt Account) query;
  icrc1_balance_of : (Account) -> (Tokens) query;
  icrc1_transfer : (TransferArg) -> (TransferResult);
  icrc1_supported_standards : () -> (vec StandardRecord) query;

  icrc2_approve : (ApproveArgs) -> (ApproveResult);
  icrc2_allowance : (AllowanceArgs) -> (Allowance) query;
  icrc2_transfer_from : (TransferFromArgs) -> (TransferFromResult);

  icrc3_get_archives : (GetArchivesArgs) -> (GetArchivesResult) query;
  icrc3_get_tip_certificate : () -> (opt ICRC3DataCertificate) query;
  icrc3_get_blocks : (vec GetBlocksArgs) -> (GetBlocksResult) query;
  icrc3_supported_block_types : () -> (vec record { block_type : text; url : text }) query;

  icrc21_canister_call_consent_message : (icrc21_consent_message_request) -> (icrc21_consent_message_response);
  icrc10_supported_standards : () -> (vec record { name : text; url : text }) query;

  icrc103_get_allowances : (GetAllowancesArgs) -> (icrc103_get_allowances_response) query;

  icrc106_get_index_principal : () -> (GetIndexPrincipalResult) query;

  icrc152_mint : (Icrc152MintArgs) -> (Icrc152MintResult);
  icrc152_burn : (Icrc152BurnArgs) -> (Icrc152BurnResult);

  is_ledger_ready : () -> (bool) query
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L595-609)
```rust
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize, Serialize)]
pub struct FeatureFlags {
    pub icrc2: bool,
    #[serde(default)]
    pub icrc152: bool,
}

impl FeatureFlags {
    const fn const_default() -> Self {
        Self {
            icrc2: true,
            icrc152: false,
        }
    }
}
```
