### Title
Incorrect `decimals` Supplied to ICRC-1 Ledger Initialization Cannot Be Corrected via Upgrade — (`rs/ledger_suite/icrc1/ledger/ledger.did`)

---

### Summary

The ICRC-1 ledger's `decimals` field is set once at initialization time and is permanently excluded from `UpgradeArgs`. If an incorrect decimal value is supplied — either directly or via the ckERC20 Ledger Suite Orchestrator's `AddErc20Arg` — there is no on-chain mechanism to correct it without a full canister reinstall, which destroys all existing balances and transaction history. This is a direct structural analog to the reported Connext `SwapAdminFacet` bug.

---

### Finding Description

**Root cause — `decimals` absent from `UpgradeArgs`:**

`InitArgs` in the ICRC-1 ledger Candid interface includes `decimals : opt nat8`: [1](#0-0) 

`UpgradeArgs` does **not** include `decimals`: [2](#0-1) 

The Rust `upgrade()` method on `Ledger` handles `token_name`, `token_symbol`, `transfer_fee`, `max_memo_length`, `change_fee_collector`, `feature_flags`, `change_archive_options`, and `index_principal` — but never `decimals`: [3](#0-2) 

The `decimals` value is frozen at construction time: [4](#0-3) 

**Amplified impact via the ckERC20 Ledger Suite Orchestrator:**

When a new ckERC20 token is added via `AddErc20Arg`, the orchestrator's `LedgerInitArg` carries a `decimals: u8` field: [5](#0-4) 

This value is forwarded verbatim into the ICRC-1 ledger's `InitArgs`: [6](#0-5) 

Once `add_erc20` schedules the `InstallLedgerSuite` task, the ledger is deployed and the decimals are locked: [7](#0-6) 

There is no `RemoveErc20Arg` or `UpdateErc20DecimalsArg` variant in the orchestrator's argument type, and no `UpgradeArgs.decimals` field in the ICRC-1 ledger, so neither the orchestrator nor the ledger itself provides a correction path.

---

### Impact Explanation

`decimals` is the authoritative precision field returned by `icrc1_decimals()` and `icrc1_metadata()`. Every wallet, DEX, bridge, and indexer uses it to convert raw token amounts to human-readable values. A wrong value causes:

1. **Display corruption** — all balances shown to users are off by a factor of `10^|correct − wrong|`.
2. **ckERC20 mint/burn accounting errors** — the ckETH minter mints ICRC-1 units equal to the Ethereum token units received. If the ledger's `decimals` does not match the ERC-20 contract's `decimals()`, every downstream system that reads `icrc1_decimals` to interpret amounts will compute wrong real-world values. For example, ckUSDC with 18 decimals instead of 6 would make 1 USDC appear as 0.000000000001 ckUSDC in every conforming client.
3. **Permanent state** — because `UpgradeArgs` has no `decimals` field, the only remediation is a full canister reinstall, which wipes all balances and the entire transaction log, destroying user funds and audit history.

---

### Likelihood Explanation

Adding a new ckERC20 token requires an NNS upgrade proposal. Proposals are human-authored and the `decimals` value must be looked up manually from the ERC-20 contract. Historical proposals show values of `6` (USDC, USDT), `8` (WBTC), and `18` (LINK, PEPE, UNI) — a one-digit transcription error (e.g., `16` instead of `18`, or `8` instead of `6`) would be syntactically valid, pass all current validation, and be silently accepted. The orchestrator's `validate_add_erc20` does not cross-check `decimals` against any on-chain ERC-20 source. Once the proposal executes and the ledger is installed, the mistake is irreversible without destroying all user balances.

---

### Recommendation

1. **Add `decimals` to `UpgradeArgs`** in `rs/ledger_suite/icrc1/ledger/ledger.did` and implement the corresponding branch in `Ledger::upgrade()` in `rs/ledger_suite/icrc1/ledger/src/lib.rs`. Guard the update so it is only permitted when `total_supply == 0` (no balances exist yet), mirroring the Connext fix of removing the swap before any funds are deposited.
2. **Add a `RemoveErc20Arg` variant** to the Ledger Suite Orchestrator's `OrchestratorArg` so that a newly deployed ledger with wrong decimals can be torn down before any deposits occur, analogous to Connext's "remove the swap if we made a mistake."
3. **Validate `decimals` in `validate_add_erc20`** against a known-good range or require an explicit acknowledgement field to reduce transcription errors.

---

### Proof of Concept

```
# 1. Deploy an ICRC-1 ledger with wrong decimals (e.g., 18 instead of 6 for a USDC-like token)
dfx deploy ledger --argument '(variant { Init = record {
    decimals = opt (18 : nat8);   # <-- wrong: should be 6
    token_symbol = "ckUSDC";
    transfer_fee = 10_000;
    ...
}})'

# 2. Attempt to correct via upgrade — UpgradeArgs has no decimals field,
#    so the only available fields are token_name, token_symbol, transfer_fee, etc.
dfx deploy ledger --argument '(variant { Upgrade = opt record {
    # decimals field does not exist here — compile/candid error or silently ignored
}})'

# 3. Query confirms decimals are still 18
dfx canister call ledger icrc1_decimals
# => (18 : nat8)   -- permanently wrong, no correction path

# 4. For the ckERC20 orchestrator path:
#    Submit NNS proposal with AddErc20Arg { decimals = 18 } for a 6-decimal ERC-20.
#    After execution, the spawned ICRC-1 ledger has decimals=18 forever.
#    A user depositing 1 USDC (1_000_000 Ethereum units) receives 1_000_000 ICRC-1 units,
#    but every wallet reads icrc1_decimals()=18 and displays 0.000000000001 ckUSDC.
```

### Citations

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L100-122)
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
  archive_options : record {
    num_blocks_to_archive : nat64;
    max_transactions_per_response : opt nat64;
    trigger_threshold : nat64;
    max_message_size_bytes : opt nat64;
    cycles_for_archive_creation : opt nat64;
    node_max_memory_size_bytes : opt nat64;
    controller_id : principal;
    more_controller_ids : opt vec principal
  };
  index_principal : opt principal
};
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

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L715-715)
```rust
            decimals: decimals.unwrap_or_else(default_decimals),
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L913-977)
```rust
    pub fn upgrade(&mut self, sink: impl Sink + Clone, args: UpgradeArgs) {
        if let Some(upgrade_metadata_args) = args.metadata {
            // Only enforce strict validation if existing metadata has no invalid keys.
            // This allows ledgers with legacy invalid keys to still be upgraded.
            let existing_all_valid = self.metadata.iter().all(|(k, _)| k.is_valid());
            self.metadata =
                map_metadata_or_trap(upgrade_metadata_args, existing_all_valid, sink.clone());
        }
        if let Some(token_name) = args.token_name {
            self.token_name = token_name;
        }
        if let Some(token_symbol) = args.token_symbol {
            self.token_symbol = token_symbol;
        }
        if let Some(transfer_fee) = args.transfer_fee {
            self.transfer_fee = Tokens::try_from(transfer_fee.clone()).unwrap_or_else(|e| {
                ic_cdk::trap(format!(
                    "failed to convert transfer fee {transfer_fee} to tokens: {e}"
                ))
            });
        }
        if let Some(max_memo_length) = args.max_memo_length {
            if self.max_memo_length > max_memo_length {
                ic_cdk::trap(format!(
                    "The max len of the memo can be changed only to be bigger or equal than the current size. Current size: {}",
                    self.max_memo_length
                ));
            }
            self.max_memo_length = max_memo_length;
        }
        if let Some(change_fee_collector) = args.change_fee_collector {
            self.fee_collector = change_fee_collector.into();
            if self.fee_collector.as_ref().map(|fc| fc.fee_collector) == Some(self.minting_account)
            {
                ic_cdk::trap(
                    "The fee collector account cannot be the same account as the minting account",
                );
            }
        }
        if let Some(feature_flags) = args.feature_flags {
            if !feature_flags.icrc2 {
                log!(
                    sink,
                    "[ledger] feature flag icrc2 is deprecated and won't disable ICRC-2 anymore"
                );
            }
            self.feature_flags = feature_flags;
        }
        if let Some(change_archive_options) = args.change_archive_options {
            let mut maybe_archive = self.blockchain.archive.write().expect(
                "BUG: should be unreachable since upgrade has exclusive write access to the ledger",
            );
            if maybe_archive.is_none() {
                ic_cdk::trap(
                    "[ERROR]: Archive options cannot be changed, since there is no archive!",
                );
            }
            if let Some(archive) = maybe_archive.deref_mut() {
                change_archive_options.apply(archive);
            }
        }
        if let Some(index_principal) = args.index_principal {
            self.index_principal = Some(index_principal);
        }
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/candid/mod.rs (L57-64)
```rust
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize, serde::Serialize)]
pub struct LedgerInitArg {
    pub transfer_fee: Nat,
    pub decimals: u8,
    pub token_name: String,
    pub token_symbol: String,
    pub token_logo: String,
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L926-949)
```rust
    LedgerInitArgs {
        minting_account: LedgerAccount::from(minter_id),
        fee_collector_account: Some(LedgerAccount {
            owner: minter_id,
            subaccount: Some(LEDGER_FEE_SUBACCOUNT),
        }),
        initial_balances: vec![],
        transfer_fee: ledger_init_arg.transfer_fee,
        decimals: Some(ledger_init_arg.decimals),
        token_name: ledger_init_arg.token_name,
        token_symbol: ledger_init_arg.token_symbol,
        metadata: vec![(
            MetadataKey::ICRC1_LOGO.to_string(),
            LedgerMetadataValue::from(ledger_init_arg.token_logo),
        )],
        archive_options: icrc1_archive_options(
            archive_controller_id,
            archive_more_controller_ids,
            cycles_for_archive_creation,
        ),
        max_memo_length: Some(MAX_MEMO_LENGTH),
        feature_flags: Some(ICRC2_FEATURE),
        index_principal: Some(index_principal),
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/lifecycle/mod.rs (L88-103)
```rust
pub fn add_erc20(token: AddErc20Arg) {
    match read_state(|s| {
        read_wasm_store(|w| InstallLedgerSuiteArgs::validate_add_erc20(s, w, token.clone()))
    }) {
        Ok(args) => {
            schedule_now(Task::InstallLedgerSuite(args), &IC_CANISTER_RUNTIME);
        }
        Err(e) => {
            ic_cdk::trap(format!(
                "[add_erc20]: ERROR: invalid arguments to add erc20 token {token:?}: {e:?}"
            ));
        }
    }
    read_state(|s| s.validate_config().expect("ERROR: invalid state"));
    setup_tasks_and_timers()
}
```
