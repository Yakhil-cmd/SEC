### Title
ICRC-152 Controller-Gated Mint/Burn Grants Ledger Controllers Unrestricted Token Emission and Destruction — (`File: rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The ICRC-152 feature of the ICRC-1 ledger canister introduces `icrc152_mint` and `icrc152_burn` endpoints that allow **any controller** of the ledger canister to mint an arbitrary number of tokens to any account, or burn tokens from any user's account without their consent. This is a direct analog of the Crowdsale vulnerability: instead of restricting token emission to a well-defined, trustless mechanism (the `minting_account`), the design retains a privileged back-door for the deployer/team, reducing public trust in the token's supply integrity.

---

### Finding Description

In `rs/ledger_suite/icrc1/ledger/src/main.rs`, the `icrc152_mint_not_async` function (called by the public `#[update] icrc152_mint` endpoint) performs the following authorization check:

```rust
if !ic_cdk::api::is_controller(&caller) {
    return Err(Icrc152MintError::Unauthorized(
        "caller is not a controller".to_string(),
    ));
}
``` [1](#0-0) 

The only guard is `is_controller`. There is no cap on the amount minted, no governance approval, no time-lock, and no restriction to a specific minting account. A controller can mint up to `u64::MAX` tokens per call and repeat indefinitely. [2](#0-1) 

The `icrc152_burn_not_async` function (called by the public `#[update] icrc152_burn` endpoint) has the identical authorization pattern:

```rust
if !ic_cdk::api::is_controller(&caller) {
    return Err(Icrc152BurnError::Unauthorized(
        "caller is not a controller".to_string(),
    ));
}
``` [3](#0-2) 

This allows a controller to burn tokens from **any user's account** (`args.from`) without the account owner's consent — the only exclusion is the minting account itself. [4](#0-3) 

The feature is gated by the `icrc152` flag in `FeatureFlags`, which defaults to `false`:

```rust
pub struct FeatureFlags {
    pub icrc2: bool,
    #[serde(default)]
    pub icrc152: bool,
}
impl FeatureFlags {
    const fn const_default() -> Self {
        Self { icrc2: true, icrc152: false }
    }
}
``` [5](#0-4) 

However, the flag can be set to `true` at init time or via an upgrade argument (`UpgradeArgs.feature_flags`), and once enabled, the controller back-door is permanently open for the lifetime of the canister. [6](#0-5) 

---

### Impact Explanation

**Token inflation (mint):** Any controller of an ICRC-152-enabled ledger can call `icrc152_mint` to mint an unbounded number of tokens to any account at any time, with no on-chain governance approval. This directly dilutes all existing token holders and breaks the supply guarantees that users rely on.

**Token theft (burn):** Any controller can call `icrc152_burn` to destroy tokens from any user's account without the user's knowledge or consent. This is equivalent to a forced confiscation of user funds.

Both operations bypass the normal `minting_account` mechanism — the standard ICRC-1 path that users can verify and trust. The `icrc152_mint` path creates an `AuthorizedMint` block type (`122mint`) rather than a standard `Mint` block, which may not be monitored by all wallets and explorers. [7](#0-6) 

---

### Likelihood Explanation

- **Medium.** The feature is opt-in (`icrc152: false` by default), so only ledgers that explicitly enable it are affected. However, the `UpgradeArgs` struct allows the flag to be enabled post-deployment via a canister upgrade, meaning a deployer can enable it at any time after users have already deposited funds.
- The controller of an ICRC-1 ledger is typically the deploying team or a governance canister. If the controller is a team-controlled principal (not a DAO), the attack requires no additional compromise — the team can act unilaterally.
- The `icrc152_burn` path is particularly dangerous because it requires no user interaction and leaves no prior on-chain signal.

---

### Recommendation

1. **Restrict `icrc152_mint` to the designated `minting_account`**, not to arbitrary controllers. The minting account is the publicly declared, verifiable minting authority. Controllers should not have a separate, parallel minting path.
2. **Remove `icrc152_burn`'s ability to burn from arbitrary accounts.** If authorized burns are needed, require the account owner's signature (e.g., via an ICRC-2 approval) or restrict burns to the minting account's own balance.
3. **If the controller-gated path is intentional**, enforce a hard cap on the total supply and require the feature to be immutably set at init time (not upgradeable), so users can verify the constraint at deployment.
4. **Emit a distinct, prominently documented block type** and ensure all standard explorers and wallets surface `122mint`/`122burn` blocks with a clear warning that they originate from a controller action, not from the normal minting account.

---

### Proof of Concept

**Attacker-controlled entry path:**

1. Deploy an ICRC-1 ledger with `feature_flags: Some(FeatureFlags { icrc2: true, icrc152: true })`. [8](#0-7) 

2. The deployer retains controller status over the ledger canister (standard IC behavior).

3. At any point — including during an active token sale or after users have acquired tokens — the controller sends an ingress update call:
   ```
   icrc152_mint({ to: <attacker_account>, amount: 1_000_000_000_000, created_at_time: <now>, reason: null })
   ```
   The check `is_controller(&caller)` passes. No further authorization is required. [9](#0-8) 

4. Alternatively, the controller calls:
   ```
   icrc152_burn({ from: <victim_account>, amount: <victim_balance>, created_at_time: <now>, reason: null })
   ```
   The victim's entire balance is destroyed without their consent. [10](#0-9) 

5. Both operations succeed silently from the perspective of standard ICRC-1 monitoring tools that only watch `1mint`/`1burn` block types, not `122mint`/`122burn`.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L916-920)
```rust
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152MintError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L921-931)
```rust
        if args.amount == 0_u64 {
            return Err(Icrc152MintError::GenericError {
                error_code: Nat::from(0_u64),
                message: "amount must be greater than 0".to_string(),
            });
        }
        let amount =
            Tokens::try_from(args.amount.clone()).map_err(|_| Icrc152MintError::GenericError {
                error_code: Nat::from(0_u64),
                message: "amount is too large".to_string(),
            })?;
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L951-958)
```rust
        let tx = Transaction {
            operation: Operation::AuthorizedMint {
                to: args.to,
                amount,
                caller: Some(caller),
                mthd: Some(MTHD_152_MINT.to_string()),
                reason: args.reason,
            },
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L990-996)
```rust
#[update]
async fn icrc152_mint(args: Icrc152MintArgs) -> Result<Nat, Icrc152MintError> {
    let block_idx = icrc152_mint_not_async(ic_cdk::api::msg_caller(), args)?;
    ic_cdk::api::certified_data_set(Access::with_ledger(Ledger::root_hash));
    archive_blocks::<Access>(&LOG, MAX_MESSAGE_SIZE).await;
    Ok(Nat::from(block_idx))
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1009-1013)
```rust
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152BurnError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1030-1034)
```rust
        if &args.from == ledger.minting_account() {
            return Err(Icrc152BurnError::InvalidAccount(
                "cannot burn from the minting account".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1044-1051)
```rust
        let tx = Transaction {
            operation: Operation::AuthorizedBurn {
                from: args.from,
                amount,
                caller: Some(caller),
                mthd: Some(MTHD_152_BURN.to_string()),
                reason: args.reason,
            },
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L596-614)
```rust
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

impl Default for FeatureFlags {
    fn default() -> Self {
        Self::const_default()
    }
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L140-150)
```text
type UpgradeArgs = record {
  metadata : opt vec record { text; MetadataValue };
  token_symbol : opt text;
  token_name : opt text;
  transfer_fee : opt nat;
  change_fee_collector : opt ChangeFeeCollector;
  max_memo_length : opt nat16;
  feature_flags : opt FeatureFlags;
  change_archive_options : opt ChangeArchiveOptions;
  index_principal : opt principal
};
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L6169-6178)
```rust
    let args = encode_init_args(InitArgs {
        feature_flags: Some(FeatureFlags {
            icrc2: true,
            icrc152: true,
        }),
        ..init_args(initial_balances)
    });
    let args = Encode!(&args).unwrap();
    let canister_id = env.install_canister(ledger_wasm, args, None).unwrap();
    (env, canister_id)
```
