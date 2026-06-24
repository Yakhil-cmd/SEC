### Title
`icrc152_mint` Allows Any Canister Controller to Mint Arbitrary Tokens Without Supply Cap - (File: rs/ledger_suite/icrc1/ledger/src/main.rs)

### Summary
The ICRC-152 feature in the ICRC-1 ledger canister introduces an `icrc152_mint` endpoint that allows **any canister controller** to mint an arbitrary, unbounded amount of tokens to any account. There is no supply cap, no per-call limit, and no rate limiting. The authorization check is solely `ic_cdk::api::is_controller(&caller)` — the same principal set that can also upgrade the canister wasm. This is the direct IC analog of the `COLLATERAL_MINTER_ROLE` centralization risk: a privileged role (here: canister controller) can mint unlimited tokens, and the role itself can be reassigned (via `update_settings` on the management canister).

### Finding Description
In `rs/ledger_suite/icrc1/ledger/src/main.rs`, the `icrc152_mint_not_async` function (called by the public `#[update] icrc152_mint` endpoint) performs the following authorization:

```rust
if !ic_cdk::api::is_controller(&caller) {
    return Err(Icrc152MintError::Unauthorized(
        "caller is not a controller".to_string(),
    ));
}
```

After passing this single check, the function directly calls `apply_transaction` with an `Operation::AuthorizedMint` that credits the target account with the caller-supplied `amount`. There is:

- **No supply cap** — no maximum total supply is enforced.
- **No per-mint limit** — the only upper bound is the `Tokens::try_from` conversion (which rejects values too large to fit in the token type, but for `u256` tokens this is astronomically large).
- **No rate limiting** — a controller can call `icrc152_mint` in a tight loop.
- **No governance approval** — unlike `MintSnsTokens` proposals which require SNS governance voting, this is a direct canister call.

The `icrc152` feature flag (`FeatureFlags { icrc152: bool }`) can be enabled at init time or via an `UpgradeArgs` upgrade argument. Once enabled, **all current canister controllers** gain the ability to mint. Controllers can be added or removed via the IC management canister's `update_settings` endpoint — meaning the set of principals with minting power can change without any on-ledger record.

### Impact Explanation
If the ICRC-152 feature is enabled on a deployed ledger (e.g., via an upgrade proposal), any controller of that ledger canister can:
1. Mint an arbitrary number of tokens to any account, inflating total supply without limit.
2. Drain value from all existing token holders by diluting supply.
3. Mint tokens to their own account and sell them on secondary markets.

For chain-fusion tokens (ckBTC, ckETH, ckERC-20), the ledger controller is typically the NNS root or the ledger suite orchestrator. However, the orchestrator itself has multiple controllers and can be upgraded. Any future misconfiguration that adds an attacker-controlled principal as a ledger controller, combined with `icrc152: true` being set in an upgrade, would allow unlimited minting. The `icrc152_burn` endpoint has the same controller-only authorization and allows burning tokens from any account without the account holder's consent.

### Likelihood Explanation
The `icrc152` feature flag defaults to `false` in `FeatureFlags::const_default()`, so it is not active on existing production ledgers unless explicitly enabled. However:
- The flag can be enabled via a standard `UpgradeArgs` upgrade (no special governance action beyond the upgrade itself).
- The ledger suite orchestrator already holds controller rights over ckERC-20 ledgers and can upgrade them.
- Any NNS proposal that upgrades a ledger with `feature_flags: Some(FeatureFlags { icrc2: true, icrc152: true })` would activate this capability for all current controllers.
- The attack surface is therefore: (a) a malicious or compromised NNS/SNS governance proposal, or (b) any existing controller principal whose key is compromised.

### Recommendation
1. **Remove `icrc152_mint` as a direct controller-callable endpoint.** Instead, require that minting go through the designated `minting_account` (the existing ICRC-1 minting path), which is a fixed account set at init time and cannot be changed.
2. **If the ICRC-152 pattern is required**, restrict minting to a single, immutable, purpose-specific canister principal (analogous to the Rolla fix: "make the Controller contract the only minter"), not the entire set of canister controllers.
3. **Add a supply cap parameter** to `InitArgs`/`UpgradeArgs` that `icrc152_mint` must respect.
4. **Prevent `icrc152` from being enabled via upgrade** without an explicit governance-level approval step, or make the flag one-way (once disabled, cannot be re-enabled without reinstall).
5. **Emit an on-chain governance event** whenever `icrc152` is toggled, so token holders can observe the change.

### Proof of Concept
1. Deploy an ICRC-1 ledger with `feature_flags: Some(FeatureFlags { icrc2: true, icrc152: false })`.
2. Upgrade the ledger with `UpgradeArgs { feature_flags: Some(FeatureFlags { icrc2: true, icrc152: true }), .. }`.
3. As any canister controller, call `icrc152_mint` with `to = attacker_account`, `amount = u64::MAX` (or the token-type maximum).
4. Observe that `icrc1_total_supply` increases by `u64::MAX` and the attacker account holds the minted tokens.
5. Repeat indefinitely — there is no check preventing a second call with the same or different `created_at_time`.

The authorization gate is solely: [1](#0-0) 

The mint operation itself has no supply cap: [2](#0-1) 

The `AuthorizedMint` operation directly credits the balance with no upper bound: [3](#0-2) 

The feature flag that activates this path can be set at upgrade time: [4](#0-3) 

The public endpoint is exposed in the canister interface: [5](#0-4)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L916-920)
```rust
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152MintError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L951-963)
```rust
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
```

**File:** rs/ledger_suite/icrc1/src/lib.rs (L559-561)
```rust
            Operation::AuthorizedMint { to, amount, .. } => {
                context.balances_mut().mint(to, amount.clone())?;
            }
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L952-960)
```rust
        if let Some(feature_flags) = args.feature_flags {
            if !feature_flags.icrc2 {
                log!(
                    sink,
                    "[ledger] feature flag icrc2 is deprecated and won't disable ICRC-2 anymore"
                );
            }
            self.feature_flags = feature_flags;
        }
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L638-639)
```text
  icrc152_mint : (Icrc152MintArgs) -> (Icrc152MintResult);
  icrc152_burn : (Icrc152BurnArgs) -> (Icrc152BurnResult);
```
