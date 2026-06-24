### Title
Anonymous Principal Bypass in `icrc152_burn` Allows Any User to Burn Tokens from Any Account — (File: `rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The `icrc152_burn` endpoint in the ICRC-1 ledger canister restricts access to canister controllers via `ic_cdk::api::is_controller`. However, the anonymous principal (`2vxsx-fae`) can legitimately be a canister controller on the Internet Computer, and any user can send ingress update calls as the anonymous principal without authentication. The code explicitly rejects the anonymous principal as the `from` account but never rejects it as the **caller**. If the ledger is deployed with the anonymous principal as a controller — which is the default when no explicit controller is specified at install time — any unauthenticated user can call `icrc152_burn` and destroy tokens from any non-anonymous, non-minting account.

---

### Finding Description

`icrc152_burn_not_async` in `rs/ledger_suite/icrc1/ledger/src/main.rs` performs two guards before executing the burn:

1. Feature-flag check (`icrc152` must be enabled).
2. Controller check: `if !ic_cdk::api::is_controller(&caller)`. [1](#0-0) 

The function then burns from the caller-supplied `args.from` account — any account in the ledger — without any ownership relationship between the caller and the target account. [2](#0-1) 

The code does guard against the anonymous principal appearing as the **burn target**: [3](#0-2) 

But there is **no symmetric guard** rejecting the anonymous principal as the **caller**. On the Internet Computer, the anonymous principal is a valid canister controller and any user can send an ingress update message as the anonymous principal without any key material. The integration test setup confirms this: `setup_icrc152` installs the canister with `None` as the sender, making the anonymous principal the controller, and all happy-path tests use `PrincipalId::new_anonymous()` as the authorized caller. [4](#0-3) [5](#0-4) 

The same gap exists in `icrc152_mint_not_async`. [6](#0-5) 

---

### Impact Explanation

**Vulnerability class**: Ledger conservation bug / governance authorization bypass.

When the anonymous principal is a controller of the ledger canister, any unauthenticated Internet Computer user can:

1. Call `icrc152_burn` as the anonymous principal.
2. Supply any non-anonymous, non-minting account in `args.from`.
3. Destroy an arbitrary amount of that account's tokens.

This is a direct analog to the external report: an unprivileged ingress sender can burn tokens from any address without the account owner's consent, violating ledger conservation and destroying user funds.

---

### Likelihood Explanation

**Medium.** The anonymous principal is the default controller when a canister is installed without specifying a controller list (`install_canister(..., None)`). Any ICRC-1 ledger with `icrc152: true` deployed this way — including developer-deployed or SNS-bootstrapped ledgers before controller handoff — is immediately exploitable by any user. The test harness itself demonstrates this exact configuration. Production deployments that correctly transfer control to a governance canister are not affected, but the code provides no defense-in-depth against the misconfiguration.

---

### Recommendation

Add an explicit anonymous-principal rejection for the **caller** in both `icrc152_burn_not_async` and `icrc152_mint_not_async`, immediately after the feature-flag check and before the `is_controller` check:

```rust
if caller == Principal::anonymous() {
    return Err(Icrc152BurnError::Unauthorized(
        "anonymous principal is not allowed as caller".to_string(),
    ));
}
```

This mirrors the existing guard on `args.from.owner` and closes the bypass regardless of controller configuration. [7](#0-6) 

---

### Proof of Concept

```
1. Deploy the ICRC-1 ledger with icrc152: true and no explicit controller
   (anonymous principal becomes the controller by default).

2. Fund victim account V with N tokens via icrc1_transfer from the minting account.

3. As the anonymous principal (no authentication required), call:

   icrc152_burn({
     from: V,          // victim's account
     amount: N,        // full balance
     created_at_time: <current_time>,
     reason: null
   })

4. is_controller(anonymous) == true  →  guard passes.
   args.from.owner != anonymous      →  InvalidAccount guard passes.
   Transaction applied: V's balance reduced to 0, total supply decremented.

5. Victim's tokens are permanently destroyed with no recourse.
``` [8](#0-7)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L916-920)
```rust
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152MintError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L998-1013)
```rust
fn icrc152_burn_not_async(
    caller: Principal,
    args: Icrc152BurnArgs,
) -> Result<u64, Icrc152BurnError> {
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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1025-1028)
```rust
        if args.from.owner == Principal::anonymous() {
            return Err(Icrc152BurnError::InvalidAccount(
                "anonymous principal is not allowed".to_string(),
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

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L6168-6178)
```rust
    let env = StateMachine::new();
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

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L6456-6458)
```rust
    let p1 = PrincipalId::new_user_test_id(1);
    let (env, canister_id) = setup_icrc152(ledger_wasm, encode_init_args, vec![]);
    let controller = PrincipalId::new_anonymous();
```
