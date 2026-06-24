### Title
Missing Slippage Protection in SNS TreasuryManager DEX Deposit/Withdraw API Enables Front-Running of Governance-Approved Operations - (File: rs/sns/treasury_manager/treasury_manager.did)

---

### Summary

The `TreasuryManager` extension API, used by SNS DAOs to deposit and withdraw treasury assets into/from DEX liquidity pools, provides no mechanism for callers to specify slippage bounds. Because SNS governance proposals are public and their execution is deterministic and delayed (by the voting period), an unprivileged attacker can observe a pending deposit/withdraw proposal and sandwich the execution transaction against the DEX, extracting value from the SNS treasury at no risk.

---

### Finding Description

The `TreasuryManager` Candid interface defines two state-changing operations:

```candid
type DepositRequest = record {
  allowances : vec Allowance;
};

type WithdrawRequest = record {
  withdraw_accounts : opt vec record { principal; Account };
};
```

Neither `DepositRequest` nor `WithdrawRequest` carries any slippage-protection field (e.g., `min_output_amount`, `max_price_impact`, or `deadline`). The interface itself acknowledges this in its "Known Security Risks" block:

> *Some liquidity pools do not implement slippage protection for deposits. In other words, the price ratio at the time of execution may differ from the ratio at the time the proposal was approved.* [1](#0-0) 

The Rust trait mirrors this omission exactly: [2](#0-1) 

When SNS governance executes an approved `TreasuryManagerDeposit` proposal, `execute_treasury_manager_deposit` in `extensions.rs` constructs a `DepositRequest` containing only the approved allowance amounts and calls `deposit` on the treasury manager canister with no minimum-output or price-bound argument: [3](#0-2) 

The payload construction function `construct_treasury_manager_deposit_payload` likewise encodes only `DepositRequest { allowances }` — no slippage field exists to encode: [4](#0-3) 

The same structural gap exists for withdrawals. `construct_treasury_manager_withdraw_payload` produces `WithdrawRequest { withdraw_accounts: None }` with no price protection: [5](#0-4) 

The `external_custodian` role in the asset flow diagram is explicitly a DEX canister: [6](#0-5) 

---

### Impact Explanation

An attacker who can interact with the same DEX canister that the TreasuryManager uses can execute a classic sandwich:

1. **Front-run**: Before the governance-triggered `deposit` call reaches the DEX, the attacker submits a large swap that skews the pool price in the direction that disadvantages the incoming deposit (e.g., buying up one side of the pool).
2. **Victim execution**: The TreasuryManager deposits at the now-unfavorable price. Because no `min_lp_tokens_out` or equivalent bound is enforced anywhere in the call chain, the deposit succeeds regardless of how bad the price is.
3. **Back-run**: The attacker immediately reverses their position, profiting from the price impact they imposed on the SNS treasury.

The result is a direct, quantifiable loss of SNS treasury value. The magnitude scales with the deposit size and the liquidity depth of the pool. For large SNS treasuries, this can be significant.

---

### Likelihood Explanation

The conditions for exploitation are favorable:

- **Proposal visibility**: All SNS governance proposals, including `TreasuryManagerDeposit` proposals with their exact allowance amounts, are publicly readable via query calls to the SNS governance canister before they are executed. The voting period (typically days) gives the attacker ample preparation time.
- **Deterministic execution trigger**: Proposal execution happens automatically once the voting deadline passes and the proposal is adopted. The attacker can predict the execution window precisely.
- **No privileged access required**: The attacker only needs to be able to call the DEX canister — an action available to any principal on the IC.
- **Capital requirement**: The attacker needs enough capital to move the DEX pool price meaningfully, but this is a standard DeFi constraint, not an IC-specific barrier.

The only mitigating factor is that the TreasuryManager extension must first be blessed by the NNS community and registered with an SNS, which limits the attack surface to SNS DAOs that have adopted such an extension. [7](#0-6) 

---

### Recommendation

1. **Add slippage parameters to the API types.** `DepositRequest` should include a `min_lp_tokens_out : opt nat` (or equivalent per-asset minimum output) field. `WithdrawRequest` should include `min_amounts_out : opt vec record { principal; nat }`. These fields should be validated by the TreasuryManager implementation before submitting to the DEX.

2. **Enforce slippage at the governance layer.** `execute_treasury_manager_deposit` and `execute_treasury_manager_withdraw` in `extensions.rs` should pass the caller-specified slippage bounds through to the treasury manager canister, and the governance proposal validation step should require that these bounds are present and reasonable.

3. **Add a deadline parameter.** Include a `deadline_ns : opt nat64` in both request types so that stale executions (e.g., due to IC message scheduling delays) are rejected by the DEX rather than executed at an arbitrarily bad price.

4. **Document the risk prominently in the governance proposal rendering.** The `render_for_proposal` path for `TreasuryManagerDeposit` should warn voters that execution price is not guaranteed.

---

### Proof of Concept

**Setup**: An SNS DAO has registered a TreasuryManager extension backed by an on-chain DEX. The DAO submits a `TreasuryManagerDeposit` proposal to deposit 100,000 SNS tokens and 10,000 ICP into a liquidity pool.

**Attack sequence**:

1. Attacker queries SNS governance canister (public query) and reads the pending `TreasuryManagerDeposit` proposal, learning the exact deposit amounts and the execution deadline.

2. Attacker monitors the IC for the proposal to reach adopted status.

3. Immediately before the governance canister calls `deposit` on the TreasuryManager, the attacker submits a large swap on the DEX (e.g., sells a large amount of ICP for SNS tokens), moving the pool price significantly against the incoming deposit.

4. The governance canister executes `execute_treasury_manager_deposit`:
   - `approve_treasury_manager` transfers the approved amounts to the TreasuryManager.
   - `call_canister(..., "deposit", arg_blob)` is called with `DepositRequest { allowances: [...] }` — no minimum LP token output is specified anywhere in the call chain.
   - The TreasuryManager forwards the deposit to the DEX at the manipulated price.
   - The DEX accepts the deposit and issues fewer LP tokens than the fair-price equivalent.

5. Attacker immediately reverses their swap position on the DEX, profiting from the price impact they imposed.

The SNS treasury has now received fewer LP tokens than it should have, with the difference extracted by the attacker. No privileged access was required at any step. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/treasury_manager/treasury_manager.did (L9-19)
```text
// 1. An implementation of this API can be integrated into the SNS framework only if it is blessed
//    by the NNS community.
// 2. Before blessing a particular implementation, the NNS community will review the implementation.
//    The following requirements will be taken into account:
//    - The implementation must be open source, version controlled, and publically available
//      at a known location.
//    - The purpose of the implementation must be clearly stated, and the implementation must
//      be designed to achieve exactly that purpose.
//    - Implementations that rely on external trusted components (e.g., DEXs) must attest to those
//      components being reputable and trustworthy. At the very least, the external components
//      should be controlled by a DAO.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-93)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};

type WithdrawRequest = record {
  // Maps Ledger canister IDs of assets to be withdrawn to the respective withdraw accounts.
  //
  // If not set, accounts specified at the time of deposit will be used for the withdrawal.
  withdraw_accounts : opt vec record { principal; Account };
};
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L271-295)
```text
// Parties involved in the treasury asset management process:
// 1. treasury_owner     - e.g., the SNS Governance canister.
// 2. treasury_manager   - this canister.
// 3. external_custodian - e.g., the DEX in which assets are held temporarily.
// 4. fee_collector      - takes into account all the fees incurred due to treasury_manager's work.
// 5. payees             - e.g., developer salary payments.
// 6. payers             - e.g., liquidity provider rewards.
//
// Expects flow of assets:
//
// (A) Initialization / Deposit
// ============================
//                                      ,--------------> payees
//                                     /
// treasury_owner ---> treasury_manager ---> external_custodian
//              \                      \                       \
//               `----------------------`-----------------------`--------> fee_collector
//
// (B) Withdrawal
// ==============
//             payers --->.
//                         \
//  external_custodian ---> treasury_manager ---> treasury_owner
//                    \                     \
//                     `---------------------`---------------------------> fee_collector
```

**File:** rs/sns/treasury_manager/src/lib.rs (L250-261)
```rust
pub trait TreasuryManager {
    /// Implements the `deposit` API function.
    fn deposit(
        &mut self,
        request: DepositRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

    /// Implements the `withdraw` API function.
    fn withdraw(
        &mut self,
        request: WithdrawRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;
```

**File:** rs/sns/governance/src/extensions.rs (L1088-1099)
```rust
fn construct_treasury_manager_deposit_payload(
    context: TreasuryManagerDepositContext,
    value: Precise,
) -> Result<Vec<u8>, String> {
    let allowances = construct_treasury_manager_deposit_allowances(context, value)?;

    let arg = DepositRequest { allowances };
    let arg =
        candid::encode_one(&arg).map_err(|err| format!("Error encoding DepositRequest: {err}"))?;

    Ok(arg)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1102-1110)
```rust
fn construct_treasury_manager_withdraw_payload(_value: Precise) -> Result<Vec<u8>, String> {
    let arg = WithdrawRequest {
        withdraw_accounts: None,
    };
    let arg =
        candid::encode_one(&arg).map_err(|err| format!("Error encoding WithdrawRequest: {err}"))?;

    Ok(arg)
}
```

**File:** rs/sns/governance/src/extensions.rs (L1566-1601)
```rust
    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;

    // 2. Call deposit on treasury manager
    let balances = governance
        .env
        .call_canister(extension_canister_id, "deposit", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.deposit failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error decoding TreasuryManager.deposit response: {err:?}"),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.deposit failed: {err:?}"),
            )
        })?;
```
