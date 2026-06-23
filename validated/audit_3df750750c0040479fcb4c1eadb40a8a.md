### Title
Missing Anonymous Principal Validation for `minting_account` in ICRC1 Ledger Initialization - (File: `rs/ledger_suite/icrc1/ledger/src/lib.rs`)

---

### Summary

The `Ledger::from_init_args` function in the ICRC1 ledger canister accepts a `minting_account` parameter and stores it without checking whether its `owner` is the anonymous principal (`Principal::anonymous()`). The minting account is the sole privileged account that can create tokens from nothing (mint) and destroy tokens (burn). It is set only at initialization and is immutable thereafter. If mistakenly set to the anonymous principal, any unauthenticated ingress caller can mint unlimited tokens by calling `icrc1_transfer`, permanently destroying the token's economic model.

---

### Finding Description

In `rs/ledger_suite/icrc1/ledger/src/lib.rs`, `Ledger::from_init_args` destructures `InitArgs` and assigns `minting_account` directly to the ledger state with no validation:

```rust
let mut ledger = Self {
    ...
    minting_account,          // line 708 — stored unconditionally
    ...
};
```

The only guard present checks that `fee_collector_account` is not equal to `minting_account`:

```rust
if ledger.fee_collector.as_ref().map(|fc| fc.fee_collector) == Some(ledger.minting_account) {
    ic_cdk::trap("The fee collector account cannot be the same as the minting account");
}
```

There is no analogous check that `minting_account.owner != Principal::anonymous()`.

The `minting_account` is the IC equivalent of the Solidity `treasury` address: it is the single account whose `icrc1_transfer` calls are interpreted as mints (no debit from sender) rather than ordinary transfers. This is enforced in the transfer path: if `from_account == minting_account`, the operation is a `Mint`; if `to == minting_account`, it is a `Burn`. Setting this account to the anonymous principal means any unauthenticated caller becomes the minter.

The `minting_account` is set exclusively at initialization time. There is no upgrade path to change it. An incorrect setting is permanent and unrecoverable without redeploying the canister (losing all ledger state).

---

### Impact Explanation

If `minting_account` is initialized to `Account { owner: Principal::anonymous(), subaccount: None }`:

1. Any unauthenticated ingress message calling `icrc1_transfer` with `from_subaccount: None` is treated as a mint, creating tokens from nothing.
2. The total token supply becomes unbounded and attacker-controlled.
3. All existing token holders suffer complete dilution or value destruction.
4. The misconfiguration is permanent — `minting_account` is only set in `from_init_args` and there is no setter exposed post-initialization.

This matches the M-01 severity rationale exactly: the risk is loss of funds, and the inability to easily fix the misconfiguration after deployment.

---

### Likelihood Explanation

Medium. The minting account is typically set programmatically by the SNS deployment pipeline (to the governance canister ID) or by the ledger suite orchestrator (to the minter canister ID). However:

- Developers deploying custom ICRC1 tokens directly may use `Principal::anonymous()` as a placeholder during development and accidentally deploy it to production.
- The `SnsTestsInitPayloadBuilder` in `rs/sns/test_utils/src/itest_helpers.rs` explicitly sets `minting_account` to `Principal::anonymous()` as a placeholder (`// will be set when the Governance canister ID is allocated`), demonstrating this is a recognized pattern that could be accidentally left in place.
- No runtime guard prevents this misconfiguration from reaching production.

---

### Recommendation

Add an anonymous principal check in `Ledger::from_init_args` immediately after the `fee_collector` check:

```rust
if minting_account.owner == Principal::anonymous() {
    ic_cdk::trap("The minting account cannot be the anonymous principal");
}
```

This mirrors the existing guard pattern already used in the same function for the `fee_collector` invariant.

---

### Proof of Concept

1. Deploy an ICRC1 ledger canister with:
   ```
   minting_account = Account { owner = Principal::anonymous(), subaccount = None }
   ```
2. From any unauthenticated HTTP ingress call (anonymous caller), invoke `icrc1_transfer` with:
   ```
   TransferArg { from_subaccount: None, to: <attacker_account>, amount: 1_000_000_000, ... }
   ```
3. In `icrc1_transfer_not_async` (main.rs), `from_account = { owner: anonymous, subaccount: None }` equals `ledger.minting_account()`, so the operation is classified as `Operation::Mint { to: attacker_account, amount: 1_000_000_000 }`.
4. Tokens are minted to the attacker with no debit from any account, repeatable without limit.
5. The ledger cannot be reconfigured — `minting_account` has no post-init setter.

**Relevant code locations:** [1](#0-0) 

The `minting_account` is stored at line 708 with no anonymous-principal guard, while the only existing validation (lines 726–729) only checks the `fee_collector` relationship. [2](#0-1) 

The test builder explicitly uses `Principal::anonymous()` as a placeholder minting account, confirming this is a recognized pattern that could be accidentally deployed.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L676-729)
```rust
    pub fn from_init_args(
        sink: impl Sink + Clone,
        InitArgs {
            minting_account,
            initial_balances,
            transfer_fee,
            token_name,
            token_symbol,
            decimals,
            metadata,
            archive_options,
            fee_collector_account,
            max_memo_length,
            feature_flags,
            index_principal,
        }: InitArgs,
        now: TimeStamp,
    ) -> Self {
        if feature_flags.as_ref().map(|ff| ff.icrc2) == Some(false) {
            log!(
                sink,
                "[ledger] feature flag icrc2 is deprecated and won't disable ICRC-2 anymore"
            );
        }
        let mut ledger = Self {
            balances: LedgerBalances::default(),
            stable_balances: StableLedgerBalances::default(),
            approvals: Default::default(),
            stable_approvals: Default::default(),
            blockchain: Blockchain::new_with_archive(archive_options),
            transactions_by_hash: BTreeMap::new(),
            transactions_by_height: VecDeque::new(),
            minting_account,
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

**File:** rs/sns/test_utils/src/itest_helpers.rs (L132-135)
```rust
    pub fn new() -> SnsTestsInitPayloadBuilder {
        let ledger = LedgerInitArgsBuilder::for_tests()
            .with_minting_account(Principal::anonymous()) // will be set when the Governance canister ID is allocated
            .with_archive_options(ArchiveOptions {
```
