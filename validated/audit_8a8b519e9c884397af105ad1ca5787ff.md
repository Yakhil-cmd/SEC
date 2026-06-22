### Title
ICRC-152 Controller Can Drain Any User's Token Balance via `icrc152_burn` Without Consent or Timelock — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The ICRC-1 ledger exposes `icrc152_burn` and `icrc152_mint` endpoints gated solely by `is_controller(&caller)`. Unlike normal ICRC-1 burns (which require the token holder to initiate), `icrc152_burn` allows any canister controller to burn tokens **from any user's account** without that user's consent. The Ledger Suite Orchestrator (LSO) is a controller of every ckERC20 ledger it manages, making it a single privileged aggregator analogous to `SocketGateway`. A compromised or maliciously upgraded LSO could call `icrc152_burn` across all managed ledgers to drain all user balances, with no timelock, no per-account limit, and no additional safeguard in the production code.

---

### Finding Description

`icrc152_burn` in `rs/ledger_suite/icrc1/ledger/src/main.rs` performs a single access check:

```rust
if !ic_cdk::api::is_controller(&caller) {
    return Err(Icrc152BurnError::Unauthorized(
        "caller is not a controller".to_string(),
    ));
}
``` [1](#0-0) 

After passing that check, it constructs an `AuthorizedBurn` operation targeting the **caller-supplied** `args.from` account — any account on the ledger:

```rust
operation: Operation::AuthorizedBurn {
    from: args.from,
    amount,
    ...
}
``` [2](#0-1) 

The only exclusion is the minting account itself:

```rust
if &args.from == ledger.minting_account() {
    return Err(Icrc152BurnError::InvalidAccount(...));
}
``` [3](#0-2) 

Symmetrically, `icrc152_mint` allows a controller to mint to any account:

```rust
if !ic_cdk::api::is_controller(&caller) {
    return Err(Icrc152MintError::Unauthorized(...));
}
``` [4](#0-3) 

The public endpoints simply forward `msg_caller()` into these helpers with no additional guard:

```rust
async fn icrc152_burn(args: Icrc152BurnArgs) -> Result<Nat, Icrc152BurnError> {
    let block_idx = icrc152_burn_not_async(ic_cdk::api::msg_caller(), args)?;
``` [5](#0-4) 

**The Ledger Suite Orchestrator (LSO) is the privileged aggregator.** It is the controller of every ckERC20 ledger canister it spawns:

> All canisters spawned off by the orchestrator will be controlled by the orchestrator itself `vxkom-oyaaa-aaaar-qafda-cai` and by the NNS root `r7inp-6aaaa-aaaaa-aaabq-cai`. [6](#0-5) 

The LSO's upgrade path accepts arbitrary Wasm via an NNS proposal:

```
propose-to-change-nns-canister
  --canister-id vxkom-oyaaa-aaaar-qafda-cai
  --mode upgrade
  --wasm-module-path ./ic-ledger-suite-orchestrator-canister.wasm.gz
``` [7](#0-6) 

The LSO's `upgrade_canister` helper installs whatever Wasm bytes are provided, with no hash-pinning of the installed code beyond what the NNS proposal specifies:

```rust
runtime.upgrade_canister(canister_id, wasm.to_bytes()).await
``` [8](#0-7) 

**Analog mapping to the SocketGateway report:**

| SocketGateway | IC / ICRC-1 |
|---|---|
| `Owner` adds a new route (arbitrary code) | NNS upgrades LSO with arbitrary Wasm |
| Route executed via `delegatecall` from SocketGateway | Malicious LSO calls `icrc152_burn` as a controller |
| `CelerStorageWrapper.deleteTransferId` / `setAddressForTransferId` | `icrc152_burn` targeting any user account |
| Drain user refund mappings | Drain all ckERC20 token balances |

The `icrc152_burn` function is the IC equivalent of `CelerStorageWrapper`'s privileged mutation functions — access-protected to the controller (SocketGateway / LSO), callable with arbitrary parameters, and with no timelock or secondary approval.

---

### Impact Explanation

A malicious or compromised LSO Wasm can iterate over all known user accounts and call `icrc152_burn` on each managed ckERC20 ledger (ckUSDC, ckUSDT, and any future ckERC20 tokens), burning every user's balance to zero. Because `icrc152_burn` bypasses the normal ICRC-1 transfer flow (no `from_subaccount` approval, no fee deduction from the caller), the drain is silent from the user's perspective until balances are already zeroed. The `icrc152_mint` path can additionally redirect value to an attacker-controlled account. The impact is a **complete, irreversible loss of all ckERC20 user funds** across every ledger managed by the LSO. [9](#0-8) 

---

### Likelihood Explanation

Exploiting the LSO path requires passing an NNS governance proposal — a malicious voting majority. This is a high bar. However, the same `icrc152_burn` surface is present on **any** ICRC-1 ledger with `feature_flags.icrc152 = true` deployed by any developer on the IC. For such ledgers, a single compromised controller key (not a governance majority) is sufficient. The NNS access-control module itself demonstrates the pattern of single-canister-ID checks that are the norm across the IC:

```rust
pub fn check_caller_is_root() {
    if caller() != PrincipalId::from(ic_nns_constants::ROOT_CANISTER_ID) {
        panic!("Only the root canister is allowed to call this method.");
    }
}
``` [10](#0-9) 

As the Socket report notes, the risk scales with the number of dependent contracts/canisters that grant special privileges to the aggregator. The LSO already manages multiple ckERC20 ledgers and is designed to grow.

---

### Recommendation

1. **Timelock `icrc152_burn` and `icrc152_mint`**: Introduce a mandatory delay between a controller submitting a burn/mint request and its execution, giving users and monitors time to detect and respond.
2. **Scope-limit the burn target**: Restrict `icrc152_burn` to accounts that have explicitly opted in, or require a per-account allowance granted by the account holder.
3. **Separate the controller role from the burn-authority role**: Rather than reusing `is_controller`, introduce a dedicated `burn_authority` principal stored in ledger state, so that the LSO's upgrade controller and its burn authority are distinct keys.
4. **Minimize LSO-initiated privileged transactions**: Architect future ckERC20 integrations so that the LSO requires as few direct ledger mutations as possible, reducing the blast radius of a compromised LSO Wasm.

---

### Proof of Concept

1. Deploy an ICRC-1 ledger with `feature_flags.icrc152 = true` and set controller to canister `C`.
2. Upgrade canister `C` with Wasm containing:
   ```rust
   // In C's update method:
   ic_cdk::call(ledger_id, "icrc152_burn", (Icrc152BurnArgs {
       from: victim_account,
       amount: victim_balance,
       created_at_time: ic_cdk::api::time(),
       reason: Some("compliance".to_string()),
   },)).await.unwrap();
   ```
3. Call `C`'s update method. Because `C` is a controller of the ledger, `is_controller(&caller)` returns `true` at line 1009, and `victim_account`'s entire balance is burned with no consent from the account holder. [1](#0-0) [2](#0-1)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L905-988)
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
        if args.to.owner == Principal::anonymous() {
            return Err(Icrc152MintError::InvalidAccount(
                "anonymous principal is not allowed".to_string(),
            ));
        }
        if &args.to == ledger.minting_account() {
            return Err(Icrc152MintError::InvalidAccount(
                "cannot mint to the minting account".to_string(),
            ));
        }
        if let Some(ref reason) = args.reason
            && reason.len() > MAX_REASON_LENGTH
        {
            return Err(Icrc152MintError::GenericError {
                error_code: Nat::from(0_u64),
                message: format!("reason must be at most {} bytes", MAX_REASON_LENGTH),
            });
        }
        let now = TimeStamp::from_nanos_since_unix_epoch(ic_cdk::api::time());
        let tx = Transaction {
            operation: Operation::AuthorizedMint {
                to: args.to,
                amount,
                caller: Some(caller),
                mthd: Some(MTHD_152_MINT.to_string()),
                reason: args.reason,
            },
            created_at_time: Some(args.created_at_time),
            memo: None,
        };
        let (block_idx, _) =
            apply_transaction(ledger, tx, now, Tokens::zero()).map_err(|err| match err {
                CoreTransferError::TxDuplicate { duplicate_of } => Icrc152MintError::Duplicate {
                    duplicate_of: Nat::from(duplicate_of),
                },
                CoreTransferError::TxTooOld { .. } => Icrc152MintError::GenericError {
                    error_code: Nat::from(0_u64),
                    message: "transaction too old".to_string(),
                },
                CoreTransferError::TxCreatedInFuture { .. } => Icrc152MintError::GenericError {
                    error_code: Nat::from(0_u64),
                    message: "transaction created in the future".to_string(),
                },
                CoreTransferError::TxThrottled => Icrc152MintError::GenericError {
                    error_code: Nat::from(0_u64),
                    message: "temporarily unavailable".to_string(),
                },
                other => Icrc152MintError::GenericError {
                    error_code: Nat::from(0_u64),
                    message: format!("unexpected error: {:?}", other),
                },
            })?;
        update_total_volume(amount, false);
        Ok(block_idx)
    })?;
    Ok(block_idx)
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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1030-1033)
```rust
        if &args.from == ledger.minting_account() {
            return Err(Icrc152BurnError::InvalidAccount(
                "cannot burn from the minting account".to_string(),
            ));
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

**File:** rs/ethereum/cketh/mainnet/orchestrator_install_2024_05_10.md (L22-22)
```markdown
* All canisters spawned off by the orchestrator will be controlled by the orchestrator itself `vxkom-oyaaa-aaaar-qafda-cai` and by the NNS root `r7inp-6aaaa-aaaaa-aaabq-cai`.
```

**File:** rs/ethereum/ledger-suite-orchestrator/README.adoc (L136-150)
```text
ic-admin \
    --use-hsm \
    --key-id 01 \
    --slot 0 \
    --pin ${HSM_PIN} \
    --nns-url "https://ic0.app" \
    propose-to-change-nns-canister \
    --proposer ${NEURON_ID} \
    --canister-id vxkom-oyaaa-aaaar-qafda-cai \
    --mode upgrade \
    --wasm-module-path ./ic-ledger-suite-orchestrator-canister.wasm.gz \
    --wasm-module-sha256 ${LEDGER_SUITE_ORCHESTRATOR_WASM_HASH} \
    --arg args.bin \
    --summary-file ./orchestrator_add_new_ckerc20.md
----
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1294-1297)
```rust
    runtime
        .upgrade_canister(canister_id, wasm.to_bytes())
        .await
        .map_err(UpgradeLedgerSuiteError::UpgradeCanisterError)?;
```

**File:** rs/nns/common/src/access_control.rs (L7-11)
```rust
pub fn check_caller_is_root() {
    if caller() != PrincipalId::from(ic_nns_constants::ROOT_CANISTER_ID) {
        panic!("Only the root canister is allowed to call this method.");
    }
}
```
