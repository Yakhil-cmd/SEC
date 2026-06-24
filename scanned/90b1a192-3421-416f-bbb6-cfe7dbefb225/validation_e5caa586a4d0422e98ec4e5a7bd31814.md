### Title
Unbounded ICRC-2 Allowance Entries Enable Heap Memory Bloat in ICP/ICRC-1 Ledger Canisters — (`File: rs/ledger_suite/common/ledger_core/src/approvals.rs`)

---

### Summary

The `icrc2_approve` endpoint on both the ICP ledger and ICRC-1 ledger canisters imposes no per-account limit on the number of distinct allowance entries that can be created. An unprivileged caller with sufficient tokens can create an unbounded number of `(account, spender)` entries in the ledger's heap-resident `HeapAllowancesData::allowances` `BTreeMap`, directly analogous to the Acala M-04 storage bloat pattern. Approvals created without an `expires_at` field are never automatically pruned.

---

### Finding Description

The `AllowanceTable::approve` function in `rs/ledger_suite/common/ledger_core/src/approvals.rs` inserts a new entry into `HeapAllowancesData::allowances` for every unique `(account, spender)` pair where `amount > 0`: [1](#0-0) 

The backing store is an unbounded heap `BTreeMap`: [2](#0-1) 

There is no check anywhere in `AllowanceTable::approve` for a maximum number of allowances per account, nor a global cap on total allowances: [3](#0-2) 

The `icrc2_approve` endpoint in the ICRC-1 ledger calls directly into this path: [4](#0-3) 

And the ICP ledger exposes the same endpoint: [5](#0-4) 

Neither ledger's init/upgrade args define a `max_approvals_per_account` field: [6](#0-5) 

The existing test `test_allowance_listing_take` confirms that 501 approvals from a single account succeed without error, demonstrating no enforced cap: [7](#0-6) 

The only pruning mechanism removes entries whose `expires_at` timestamp has passed. Approvals created without `expires_at` are **never** automatically removed, meaning the heap grows monotonically with each new `(account, spender)` pair approved.

---

### Impact Explanation

The `HeapAllowancesData::allowances` `BTreeMap` lives in the ledger canister's heap memory. Each entry consumes roughly 200–300 bytes (key: two `AccountIdentifier`/`Account` values; value: `Allowance` struct; BTreeMap node overhead). An attacker creating 1 million non-expiring allowance entries would consume ~200–300 MB of heap. At 10 million entries the heap approaches the canister's wasm memory limit.

Concrete effects:
- **Performance degradation**: `BTreeMap` lookup/insert is O(log n); at millions of entries, every `icrc2_approve`, `icrc2_transfer_from`, and `icrc2_allowance` call slows measurably.
- **Memory exhaustion**: The ICP ledger canister's heap is bounded. Exhausting it causes the canister to trap on any state-mutating call, effectively freezing the ICP ledger — a critical system canister.
- **Freezing threshold trigger**: Increased memory raises the canister's idle cycles burn rate, potentially triggering the freezing threshold and halting the canister.

The ICP ledger is the canonical token ledger for the Internet Computer; its unavailability would halt NNS governance staking, neuron operations, and all ICP transfers.

---

### Likelihood Explanation

The attack entry point is the publicly callable `icrc2_approve` update method, reachable by any unprivileged ingress sender. The only cost barrier is the transfer fee per approval (10,000 e8s = 0.0001 ICP ≈ $0.001 at current prices). An attacker with 100 ICP (~$1,000) can create ~1,000,000 allowance entries. Principals on the IC are free to generate, so the attacker can distribute the cost across many source accounts, each funding a single approval. No privileged role, governance majority, or threshold key is required.

---

### Recommendation

1. **Introduce a `max_approvals_per_account` parameter** in both the ICP ledger and ICRC-1 ledger init/upgrade args, enforced inside `AllowanceTable::approve` before inserting a new entry.
2. **Enforce a global cap** on total allowances in the `AllowanceTable` to bound heap growth regardless of the number of accounts.
3. **Require a minimum non-trivial allowance amount** (e.g., at least the transfer fee) to raise the cost per entry.
4. **Mandate `expires_at`** for new allowances, or implement a background pruning task for stale non-expiring entries.

---

### Proof of Concept

The following demonstrates unbounded allowance creation on any ICRC-2-enabled ledger. An attacker generates N distinct spender principals and calls `icrc2_approve` once per spender:

```rust
// Attacker controls `approver_account` with sufficient balance.
// Each call creates a new entry in HeapAllowancesData::allowances.
for i in 0..1_000_000_u64 {
    let spender = Account {
        owner: Principal::from_slice(&i.to_be_bytes()),
        subaccount: None,
    };
    ledger.icrc2_approve(ApproveArgs {
        from_subaccount: None,
        spender,
        amount: Nat::from(1_u64),   // minimum non-zero amount
        expected_allowance: None,
        expires_at: None,           // no expiry → never pruned
        fee: Some(Nat::from(FEE)),
        memo: None,
        created_at_time: None,
    }).await.unwrap();
}
// AllowanceTable now holds 1,000,000 entries in heap BTreeMap.
// Each subsequent icrc2_allowance / icrc2_transfer_from call
// traverses a BTreeMap of depth ~20, and heap usage is ~200–300 MB.
```

This is directly analogous to the Acala PoC: repeated calls with a minimal amount, no minimum enforced, each creating a new persistent storage entry. [8](#0-7) [2](#0-1)

### Citations

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L67-75)
```rust
pub struct HeapAllowancesData<AccountId, Tokens>
where
    AccountId: Ord,
{
    allowances: BTreeMap<(AccountId, AccountId), Allowance<Tokens>>,
    expiration_queue: BTreeSet<(TimeStamp, (AccountId, AccountId))>,
    #[serde(default = "Default::default")]
    arrival_queue: BTreeSet<(TimeStamp, (AccountId, AccountId))>,
}
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L232-277)
```rust
    /// Changes the spender's allowance for the account to the specified amount and expiration.
    pub fn approve(
        &mut self,
        account: &AD::AccountId,
        spender: &AD::AccountId,
        amount: AD::Tokens,
        expires_at: Option<TimeStamp>,
        now: TimeStamp,
        expected_allowance: Option<AD::Tokens>,
    ) -> Result<AD::Tokens, ApproveError<AD::Tokens>> {
        self.with_postconditions_check(|table| {
            if account == spender {
                return Err(ApproveError::SelfApproval);
            }

            if expires_at.unwrap_or_else(remote_future) <= now {
                return Err(ApproveError::ExpiredApproval { now });
            }

            let key = (account.clone(), spender.clone());

            match table.allowances_data.get_allowance(&key) {
                None => {
                    if let Some(expected_allowance) = expected_allowance
                        && !expected_allowance.is_zero()
                    {
                        return Err(ApproveError::AllowanceChanged {
                            current_allowance: AD::Tokens::zero(),
                        });
                    }
                    if amount == AD::Tokens::zero() {
                        return Ok(amount);
                    }
                    if let Some(expires_at) = expires_at {
                        table.allowances_data.insert_expiry(expires_at, key.clone());
                    }
                    table.allowances_data.set_allowance(
                        key,
                        Allowance {
                            amount: amount.clone(),
                            expires_at,
                            arrived_at: now,
                        },
                    );
                    Ok(amount)
                }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L820-891)
```rust
fn icrc2_approve_not_async(caller: Principal, arg: ApproveArgs) -> Result<u64, ApproveError> {
    let block_idx = Access::with_ledger_mut(|ledger| {
        let now = TimeStamp::from_nanos_since_unix_epoch(ic_cdk::api::time());

        let from_account = Account {
            owner: caller,
            subaccount: arg.from_subaccount,
        };
        if from_account.owner == arg.spender.owner {
            ic_cdk::trap("self approval is not allowed")
        }
        if &from_account == ledger.minting_account() {
            ic_cdk::trap("the minting account cannot delegate mints")
        }
        match arg.memo.as_ref() {
            Some(memo) if memo.0.len() > ledger.max_memo_length() as usize => {
                ic_cdk::trap("the memo field is too large")
            }
            _ => {}
        };
        let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
        let expected_allowance = match arg.expected_allowance {
            Some(n) => match Tokens::try_from(n) {
                Ok(n) => Some(n),
                Err(_) => {
                    let current_allowance = ledger
                        .approvals()
                        .allowance(&from_account, &arg.spender, now)
                        .amount;
                    return Err(ApproveError::AllowanceChanged {
                        current_allowance: current_allowance.into(),
                    });
                }
            },
            None => None,
        };

        let expected_fee_tokens = ledger.transfer_fee();
        let expected_fee: Nat = expected_fee_tokens.into();
        if arg.fee.is_some() && arg.fee.as_ref() != Some(&expected_fee) {
            return Err(ApproveError::BadFee { expected_fee });
        }

        let tx = Transaction {
            operation: Operation::Approve {
                from: from_account,
                spender: arg.spender,
                amount,
                expected_allowance,
                expires_at: arg.expires_at,
                fee: arg.fee.map(|_| expected_fee_tokens),
            },
            created_at_time: arg.created_at_time,
            memo: arg.memo,
        };

        let (block_idx, _) = apply_transaction(ledger, tx, now, expected_fee_tokens)
            .map_err(convert_transfer_error)
            .map_err(|err| {
                let err: ApproveError = match err.try_into() {
                    Ok(err) => err,
                    Err(err) => ic_cdk::trap(&err),
                };
                err
            })?;
        Ok(block_idx)
    })?;

    update_total_volume(Tokens::zero(), true);

    Ok(block_idx)
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1418-1425)
```rust
#[update]
async fn icrc2_approve(arg: ApproveArgs) -> Result<Nat, ApproveError> {
    let block_index = icrc2_approve_not_async(caller(), arg, None)?;

    let max_msg_size = *MAX_MESSAGE_SIZE_BYTES.read().unwrap();
    archive_blocks::<Access>(DebugOutSink, max_msg_size as u64).await;
    Ok(block_index)
}
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

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L3082-3133)
```rust
// The test focuses on testing various values for the `take` parameter.
pub fn test_allowance_listing_take<T>(ledger_wasm: Vec<u8>, encode_init_args: fn(InitArgs) -> T)
where
    T: CandidType,
{
    const MAX_RESULTS: usize = 500;
    const NUM_SPENDERS: usize = MAX_RESULTS + 1;

    let approver = Account {
        owner: PrincipalId::new_user_test_id(1).0,
        subaccount: None,
    };

    let mut spenders = vec![];
    for i in 2..NUM_SPENDERS + 2 {
        spenders.push(Account {
            owner: PrincipalId::new_user_test_id(i as u64).0,
            subaccount: None,
        });
    }
    assert_eq!(spenders.len(), NUM_SPENDERS);

    let (env, canister_id) = setup(
        ledger_wasm,
        encode_init_args,
        vec![(approver, 1_000_000_000)],
    );

    for spender in &spenders {
        let approve_args = ApproveArgs {
            from_subaccount: None,
            spender: *spender,
            amount: Nat::from(10_u64),
            expected_allowance: None,
            expires_at: None,
            fee: Some(Nat::from(FEE)),
            memo: None,
            created_at_time: None,
        };
        let _ = send_approval(&env, canister_id, approver.owner, &approve_args)
            .expect("approval failed");
    }

    let mut args = GetAllowancesArgs {
        from_account: Some(approver),
        prev_spender: None,
        take: None,
    };

    let allowances = list_allowances(&env, canister_id, approver.owner, args.clone())
        .expect("failed to list allowances");
    assert_eq!(allowances.len(), MAX_RESULTS);
```
