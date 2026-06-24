### Title
Anonymous Principal Accepted as `fee_collector_account` in ICRC-1 Ledger, Causing Fees to Accumulate in Publicly Drainable Account - (File: rs/ledger_suite/icrc1/ledger/src/lib.rs)

---

### Summary

The ICRC-1 ledger's `from_init_args` and `upgrade` functions accept `fee_collector_account = Account { owner: Principal::anonymous(), subaccount: None }` without rejection. All transaction fees are then credited to the anonymous principal's account — a publicly accessible account that any caller can drain — rather than being burned or going to a controlled address. This is the direct IC analog of the Solidity `address(0)` fee-recipient bug.

---

### Finding Description

In `Ledger::from_init_args`, the `fee_collector_account` field is accepted and stored with only one validation: that it is not equal to the minting account. [1](#0-0) 

No check is performed to reject `Principal::anonymous()` as the owner of the fee collector account. The same gap exists in `Ledger::upgrade` when processing `ChangeFeeCollector::SetTo(account)`: [2](#0-1) 

This is inconsistent with the rest of the codebase. For example, `icrc152_mint_not_async` in the same ledger canister explicitly rejects the anonymous principal as a mint target: [3](#0-2) 

Similarly, the ckETH minter's `parse_principal_from_slice` explicitly rejects the anonymous principal as a deposit recipient: [4](#0-3) 

And SNS governance's `locally_validate_and_render_transfer_sns_treasury_funds` explicitly rejects it as a transfer target: [5](#0-4) 

The `InitArgs` struct exposes `fee_collector_account` as a plain `Option<Account>` with no type-level restriction: [6](#0-5) 

The `ChangeFeeCollector` enum similarly places no restriction on the inner `Account`: [7](#0-6) 

---

### Impact Explanation

When `fee_collector_account` is set to the anonymous principal's account, every `icrc1_transfer`, `icrc2_approve`, and `icrc2_transfer_from` call credits the transaction fee to `Account { owner: Principal::anonymous(), subaccount: None }` instead of burning it or routing it to a controlled account.

On the Internet Computer, the anonymous principal is not a burn address — it is a valid, publicly accessible identity. Any ingress caller can submit an `icrc1_transfer` call authenticated as the anonymous principal (since anonymous calls are permitted by the IC protocol) and drain the accumulated fees. The ledger deployer loses all intended fee revenue to any opportunistic caller.

Additionally, unlike the Solidity `address(0)` case where tokens are permanently destroyed, here the total supply is not reduced — the fees remain in circulation but are freely claimable by anyone. This breaks the ledger's economic model and the deployer's fee-collection intent.

**Impact: Medium** — Fee revenue is permanently redirected to a publicly drainable account; the ledger deployer loses all fees.

---

### Likelihood Explanation

Any developer deploying an ICRC-1 ledger canister (including via the ledger-suite-orchestrator, which accepts user-supplied `LedgerInitArg`) can trigger this by passing `fee_collector_account = Some(Account { owner: Principal::anonymous(), subaccount: None })`. The ledger-suite-orchestrator's `icrc1_ledger_init_arg` hardcodes the fee collector to the minter's subaccount, but direct ledger deployments and SNS-launched ledgers accept arbitrary init args. [8](#0-7) 

**Likelihood: Medium** — Requires a developer to make this configuration mistake, either accidentally or as a self-inflicted misconfiguration. The absence of a guard makes it easy to overlook.

---

### Recommendation

1. In `Ledger::from_init_args`, add a trap if the `fee_collector_account` owner is `Principal::anonymous()`:

```rust
if let Some(ref fc) = ledger.fee_collector {
    if fc.fee_collector.owner == Principal::anonymous() {
        ic_cdk::trap("The fee collector account cannot be the anonymous principal");
    }
    if fc.fee_collector == ledger.minting_account {
        ic_cdk::trap("The fee collector account cannot be the same as the minting account");
    }
}
```

2. Apply the same check in `Ledger::upgrade` when processing `ChangeFeeCollector::SetTo`.

3. Optionally, also reject `Principal::management_canister()` as the fee collector owner, consistent with the pattern in `parse_principal_from_slice`. [9](#0-8) [10](#0-9) 

---

### Proof of Concept

A canister developer deploys an ICRC-1 ledger with:

```rust
InitArgs {
    minting_account: Account { owner: minter_principal, subaccount: None },
    fee_collector_account: Some(Account {
        owner: Principal::anonymous(),  // <-- no validation rejects this
        subaccount: None,
    }),
    transfer_fee: Nat::from(10_000_u64),
    // ...
}
```

After deployment, any `icrc1_transfer` call deducts the fee from the sender and credits it to `Account { owner: Principal::anonymous(), subaccount: None }`. An attacker then calls:

```rust
icrc1_transfer(
    // called as Principal::anonymous() (permitted by IC ingress)
    TransferArg {
        from_subaccount: None,
        to: attacker_account,
        amount: Nat::from(accumulated_fees),
        fee: None,
        memo: None,
        created_at_time: None,
    }
)
```

This drains all accumulated fees to the attacker's account. The ledger deployer receives nothing. The existing test `test_anonymous_transfers` confirms that transfers from the anonymous principal's account succeed without restriction: [11](#0-10)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L265-278)
```rust
pub struct InitArgs {
    pub minting_account: Account,
    pub fee_collector_account: Option<Account>,
    pub initial_balances: Vec<(Account, Nat)>,
    pub transfer_fee: Nat,
    pub decimals: Option<u8>,
    pub token_name: String,
    pub token_symbol: String,
    pub metadata: Vec<(String, Value)>,
    pub archive_options: ArchiveOptions,
    pub max_memo_length: Option<u16>,
    pub feature_flags: Option<FeatureFlags>,
    pub index_principal: Option<Principal>,
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L281-293)
```rust
pub enum ChangeFeeCollector {
    Unset,
    SetTo(Account),
}

impl From<ChangeFeeCollector> for Option<FeeCollector<Account>> {
    fn from(value: ChangeFeeCollector) -> Self {
        match value {
            ChangeFeeCollector::Unset => None,
            ChangeFeeCollector::SetTo(account) => Some(FeeCollector::from(account)),
        }
    }
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L709-729)
```rust
            fee_collector: fee_collector_account.map(FeeCollector::from),
            transfer_fee: Tokens::try_from(transfer_fee.clone()).unwrap_or_else(|e| {
                panic!("failed to convert transfer fee {transfer_fee} to tokens: {e}")
            }),
            token_symbol,
            token_name,
            decimals: decimals.unwrap_or_else(default_decimals),
            metadata: map_metadata_or_trap(metadata, true, sink), // require_valid=true for init
            max_memo_length: max_memo_length.unwrap_or(DEFAULT_MAX_MEMO_LENGTH),
            feature_flags: feature_flags.unwrap_or_default(),
            maximum_number_of_accounts: 0,
            accounts_overflow_trim_quantity: 0,
            ledger_version: LEDGER_VERSION,
            index_principal,
            token_type: wasm_token_type(),
        };

        if ledger.fee_collector.as_ref().map(|fc| fc.fee_collector) == Some(ledger.minting_account)
        {
            ic_cdk::trap("The fee collector account cannot be the same as the minting account");
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L943-951)
```rust
        if let Some(change_fee_collector) = args.change_fee_collector {
            self.fee_collector = change_fee_collector.into();
            if self.fee_collector.as_ref().map(|fc| fc.fee_collector) == Some(self.minting_account)
            {
                ic_cdk::trap(
                    "The fee collector account cannot be the same account as the minting account",
                );
            }
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L932-936)
```rust
        if args.to.owner == Principal::anonymous() {
            return Err(Icrc152MintError::InvalidAccount(
                "anonymous principal is not allowed".to_string(),
            ));
        }
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L288-291)
```rust
    if principal_bytes == ANONYMOUS_PRINCIPAL_BYTES {
        return Err("anonymous principal is not allowed".to_string());
    }
    Principal::try_from_slice(principal_bytes).map_err(|err| err.to_string())
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L926-931)
```rust
    LedgerInitArgs {
        minting_account: LedgerAccount::from(minter_id),
        fee_collector_account: Some(LedgerAccount {
            owner: minter_id,
            subaccount: Some(LEDGER_FEE_SUBACCOUNT),
        }),
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L673-686)
```rust
    // Transfer to the account of the anonymous principal
    println!("transferring to the account of the anonymous principal");
    transfer(&env, canister_id, p1.0, anon.0, TRANSFER_AMOUNT).expect("transfer failed");

    // Transfer from the account of the anonymous principal
    println!("transferring from the account of the anonymous principal");
    transfer(&env, canister_id, anon.0, p1.0, TRANSFER_AMOUNT).expect("transfer failed");

    assert_eq!(
        INITIAL_BALANCE * 2 - FEE * 2,
        total_supply(&env, canister_id)
    );
    assert_eq!(INITIAL_BALANCE - FEE, balance_of(&env, canister_id, p1.0));
    assert_eq!(INITIAL_BALANCE - FEE, balance_of(&env, canister_id, anon.0));
```
